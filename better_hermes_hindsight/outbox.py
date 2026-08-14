"""Private schema-v1 SQLite outbox with atomic bounded whole-turn admission."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import math
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from .config import (
    OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES,
    PAYLOAD_SCHEMA_VERSION,
    BetterHindsightConfig,
)
from .retention import (
    DOCUMENT_ID_PREFIX,
    RetainedSegment,
    derive_segment_payload_hash,
)

OUTBOX_SCHEMA_VERSION = 1
OUTBOX_OPEN_FAILED_MESSAGE = "Better Hindsight outbox could not be opened."
OUTBOX_SCHEMA_UNSUPPORTED_MESSAGE = "Better Hindsight outbox schema is unsupported."
OUTBOX_READ_FAILED_MESSAGE = "Better Hindsight outbox read failed."

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outbox (
    document_id TEXT PRIMARY KEY NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_schema TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    segment_index INTEGER NOT NULL CHECK (segment_index >= 0),
    segment_count INTEGER NOT NULL CHECK (segment_count > 0),
    content TEXT NOT NULL,
    destination_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'sending')),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    last_error_category TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
) WITHOUT ROWID
"""
_SCHEMA_SQL_SIGNATURE = " ".join(_SCHEMA_SQL.replace("IF NOT EXISTS ", "").split())
_SCHEMA_COLUMN_INFO = (
    ("document_id", "TEXT", 1, None, 1),
    ("payload_hash", "TEXT", 1, None, 0),
    ("payload_schema", "TEXT", 1, None, 0),
    ("source_sha256", "TEXT", 1, None, 0),
    ("segment_index", "INTEGER", 1, None, 0),
    ("segment_count", "INTEGER", 1, None, 0),
    ("content", "TEXT", 1, None, 0),
    ("destination_fingerprint", "TEXT", 1, None, 0),
    ("state", "TEXT", 1, None, 0),
    ("attempt_count", "INTEGER", 1, None, 0),
    ("next_attempt_at", "REAL", 1, None, 0),
    ("last_error_category", "TEXT", 0, None, 0),
    ("created_at", "REAL", 1, None, 0),
    ("updated_at", "REAL", 1, None, 0),
)
# SQLite's built-in unix VFS maps WAL-index shared memory in fixed 32 KiB regions.
_STATUS_SHM_REGION_BYTES = 32_768
_STATUS_ERROR_CATEGORIES = frozenset({"retain_timeout", "retain_failed", "retain_unconfirmed"})


class OutboxOpenError(RuntimeError):
    """A fixed, sanitized outbox-open failure."""


class OutboxReadError(RuntimeError):
    """A fixed, sanitized outbox-read failure."""


class _UnsupportedSchemaError(RuntimeError):
    pass


class _InvalidSchemaError(RuntimeError):
    pass


class _GuardedTransitionError(RuntimeError):
    pass


class AdmissionStatus(StrEnum):
    """Fixed local admission outcomes safe for diagnostics."""

    ADMITTED = "admitted"
    DUPLICATE = "duplicate"
    CAPACITY_EXCEEDED = "capacity_exceeded"
    CONFLICT = "conflict"
    CONTENDED = "contended"
    LOCAL_FAILURE = "local_failure"
    INVALID = "invalid"


class ProfileLockStatus(StrEnum):
    """Fixed, sanitized outcomes for nonblocking profile-lock acquisition."""

    ACQUIRED = "acquired"
    CONTENDED = "contended"
    LOCAL_FAILURE = "local_failure"


class OutboxClaimStatus(StrEnum):
    """Fixed, sanitized outcomes for one deterministic due-row claim."""

    CLAIMED = "claimed"
    EMPTY = "empty"
    LOCAL_FAILURE = "local_failure"


class OutboxTransitionStatus(StrEnum):
    """Fixed, sanitized outcomes for a short persisted-state transition."""

    APPLIED = "applied"
    LOCAL_FAILURE = "local_failure"


class OutboxFailureCategory(StrEnum):
    """The complete code-owned set of categories that may be persisted after retain failure."""

    RETAIN_TIMEOUT = "retain_timeout"
    RETAIN_FAILED = "retain_failed"
    RETAIN_UNCONFIRMED = "retain_unconfirmed"


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """A sanitized aggregate result for one whole-turn admission."""

    status: AdmissionStatus
    inserted_count: int = 0
    duplicate_count: int = 0

    @property
    def accepted(self) -> bool:
        """Whether every requested row is durably present with exact immutable values."""

        return self.status in {AdmissionStatus.ADMITTED, AdmissionStatus.DUPLICATE}


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One unconfirmed row exposed through the minimal read seam."""

    document_id: str
    payload_hash: str
    payload_schema: str
    source_sha256: str
    segment_index: int
    segment_count: int
    content: str = field(repr=False)
    destination_fingerprint: str = field(repr=False)
    state: Literal["pending", "sending"] = "pending"
    attempt_count: int = 0
    next_attempt_at: float = 0.0
    last_error_category: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class ProfileLockIdentity:
    """The reserved private lock pathname and captured regular-file identity."""

    path: Path = field(repr=False)
    device: int
    inode: int


class ProfileLockOwner:
    """A live exclusive profile-lock descriptor passed to owner-only data transitions."""

    __slots__ = ("_descriptor", "_identity", "_token")

    def __init__(
        self,
        *,
        descriptor: int,
        identity: ProfileLockIdentity,
        token: object,
    ) -> None:
        self._descriptor = descriptor
        self._identity = identity
        self._token = token

    def __repr__(self) -> str:
        return "ProfileLockOwner()"

    @property
    def identity(self) -> ProfileLockIdentity:
        """Return the identity whose descriptor remains locked by this owner."""

        return self._identity

    def release(self) -> None:
        """Release and close this owner idempotently."""

        descriptor = self._descriptor
        if descriptor < 0:
            return
        self._descriptor = -1
        _release_profile_lock_descriptor(descriptor)

    def __enter__(self) -> ProfileLockOwner:
        return self

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        self.release()

    def _descriptor_for(self, token: object) -> int | None:
        if self._token is not token or self._descriptor < 0:
            return None
        return self._descriptor


@dataclass(frozen=True, slots=True)
class ProfileLockAcquisitionResult:
    """One nonblocking ownership attempt without exposing local exception text."""

    status: ProfileLockStatus
    owner: ProfileLockOwner | None = field(default=None, repr=False)

    @property
    def acquired(self) -> bool:
        return self.status is ProfileLockStatus.ACQUIRED and self.owner is not None


@dataclass(frozen=True, slots=True)
class OutboxClaimResult:
    """One exact due-row claim or a fixed empty/local-failure outcome."""

    status: OutboxClaimStatus
    row: OutboxRow | None = field(default=None, repr=False)

    @property
    def claimed(self) -> bool:
        return self.status is OutboxClaimStatus.CLAIMED and self.row is not None


@dataclass(frozen=True, slots=True)
class OutboxTransitionResult:
    """One short recovery/completion/retry transition result."""

    status: OutboxTransitionStatus
    affected_count: int = 0

    @property
    def applied(self) -> bool:
        return self.status is OutboxTransitionStatus.APPLIED


@dataclass(frozen=True, slots=True)
class OutboxInspection:
    """One aggregate-only, point-in-time view of an existing profile outbox."""

    outbox: Literal["ready", "uninitialized"]
    mismatch_count: int = 0
    pending_count: int = 0
    retry_count: int = 0
    sending_count: int = 0
    logical_queued_bytes: int = 0
    oldest_created_at: float | None = None
    last_error_category: OutboxFailureCategory | None = None
    max_attempt_count: int = 0
    next_retry_at: float | None = None
    error_category_counts: Mapping[str, int] = field(default_factory=dict)
    sender_ownership: Literal["held", "free", "unavailable"] = "free"


class SQLiteOutbox:
    """One connection owner for a profile-local private outbox."""

    __slots__ = (
        "_closed",
        "_connection",
        "_destination_fingerprint",
        "_hermes_home",
        "_lock_token",
        "_max_pending_bytes",
        "_max_pending_rows",
        "_mutex",
        "_payload_schema",
        "_profile_lock_identity",
        "_retry_initial_seconds",
        "_retry_max_seconds",
        "_segment_max_bytes",
    )

    def __init__(
        self,
        *,
        config: BetterHindsightConfig,
        connection: sqlite3.Connection,
        hermes_home: Path,
        profile_lock_identity: ProfileLockIdentity,
    ) -> None:
        self._connection = connection
        self._hermes_home = hermes_home
        self._profile_lock_identity = profile_lock_identity
        self._lock_token = object()
        self._destination_fingerprint = config.destination_fingerprint
        self._payload_schema = config.outbox.payload_schema
        self._max_pending_rows = config.outbox.max_pending_rows
        self._max_pending_bytes = config.outbox.max_pending_bytes
        self._retry_initial_seconds = config.outbox.retry_initial_seconds
        self._retry_max_seconds = config.outbox.retry_max_seconds
        self._segment_max_bytes = config.retain.segment_max_bytes
        self._mutex = threading.Lock()
        self._closed = False

    @classmethod
    def open(cls, config: BetterHindsightConfig) -> SQLiteOutbox:
        """Open schema v1 after revalidating the configured path inside ``hermes_home``."""

        connection: sqlite3.Connection | None = None
        try:
            (
                hermes_home,
                database_path,
                profile_lock_path,
                database_identity,
                profile_lock_identity_values,
            ) = _prepare_private_paths(config)
            connection = _connect_existing_database(database_path, config)
            _revalidate_database_identity(
                config.hermes_home,
                database_path,
                database_identity,
            )
            connection.execute(f"PRAGMA busy_timeout = {config.outbox.busy_timeout_ms}")
            _initialize_schema(connection)
            _revalidate_open_paths(
                config.hermes_home,
                database_path,
                profile_lock_path,
                database_identity,
                profile_lock_identity_values,
            )
            _set_private_file_mode(database_path)
            _set_private_file_mode(profile_lock_path)
            _revalidate_profile_lock_identity(
                hermes_home,
                ProfileLockIdentity(
                    path=profile_lock_path,
                    device=profile_lock_identity_values[0],
                    inode=profile_lock_identity_values[1],
                ),
                descriptor=None,
                require_private_mode=True,
            )
        except _UnsupportedSchemaError:
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()
            raise OutboxOpenError(OUTBOX_SCHEMA_UNSUPPORTED_MESSAGE) from None
        except Exception:
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()
            raise OutboxOpenError(OUTBOX_OPEN_FAILED_MESSAGE) from None
        return cls(
            config=config,
            connection=connection,
            hermes_home=hermes_home,
            profile_lock_identity=ProfileLockIdentity(
                path=profile_lock_path,
                device=profile_lock_identity_values[0],
                inode=profile_lock_identity_values[1],
            ),
        )

    def __repr__(self) -> str:
        return "SQLiteOutbox()"

    @property
    def profile_lock_path(self) -> Path:
        """Return the private lock file used for sender ownership."""

        return self._profile_lock_identity.path

    @property
    def profile_lock_identity(self) -> ProfileLockIdentity:
        """Return the captured reserved lock identity for sender ownership."""

        return self._profile_lock_identity

    def try_acquire_profile_lock(self) -> ProfileLockAcquisitionResult:
        """Open the reserved file existing-only and try one nonblocking exclusive POSIX lock."""

        if os.name != "posix":
            return ProfileLockAcquisitionResult(ProfileLockStatus.LOCAL_FAILURE)

        descriptor: int | None = None
        acquired = False
        with self._mutex:
            if self._closed:
                return ProfileLockAcquisitionResult(ProfileLockStatus.LOCAL_FAILURE)
            try:
                flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self._profile_lock_identity.path, flags)
                _revalidate_profile_lock_identity(
                    self._hermes_home,
                    self._profile_lock_identity,
                    descriptor=descriptor,
                    require_private_mode=True,
                )
                acquired = _try_flock_exclusive(descriptor)
                if not acquired:
                    return ProfileLockAcquisitionResult(ProfileLockStatus.CONTENDED)
                _revalidate_profile_lock_identity(
                    self._hermes_home,
                    self._profile_lock_identity,
                    descriptor=descriptor,
                    require_private_mode=True,
                )
                owner = ProfileLockOwner(
                    descriptor=descriptor,
                    identity=self._profile_lock_identity,
                    token=self._lock_token,
                )
                descriptor = None
                return ProfileLockAcquisitionResult(ProfileLockStatus.ACQUIRED, owner)
            except Exception:
                return ProfileLockAcquisitionResult(ProfileLockStatus.LOCAL_FAILURE)
            finally:
                if descriptor is not None:
                    if acquired:
                        _release_profile_lock_descriptor(descriptor)
                    else:
                        with contextlib.suppress(OSError):
                            os.close(descriptor)

    def recover_sending(
        self,
        owner: ProfileLockOwner,
        *,
        now: float,
    ) -> OutboxTransitionResult:
        """Reset every stale sending row after exclusive ownership has been acquired."""

        normalized_now = _valid_wall_time(now)
        if normalized_now is None:
            return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)
        with self._mutex:
            if self._closed or self._owner_descriptor(owner) is None:
                return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """
                    UPDATE outbox
                    SET state='pending', next_attempt_at=?, updated_at=?
                    WHERE state='sending'
                    """,
                    (normalized_now, normalized_now),
                )
                affected_count = cursor.rowcount
                if affected_count < 0 or self._owner_descriptor(owner) is None:
                    raise _GuardedTransitionError
                self._connection.commit()
                return OutboxTransitionResult(
                    OutboxTransitionStatus.APPLIED,
                    affected_count=affected_count,
                )
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.rollback()
                return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)

    def claim_due(self, owner: ProfileLockOwner, *, now: float) -> OutboxClaimResult:
        """Atomically claim exactly one due matching row and increment its attempt before I/O."""

        normalized_now = _valid_wall_time(now)
        if normalized_now is None:
            return OutboxClaimResult(OutboxClaimStatus.LOCAL_FAILURE)
        with self._mutex:
            if self._closed or self._owner_descriptor(owner) is None:
                return OutboxClaimResult(OutboxClaimStatus.LOCAL_FAILURE)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                record = self._connection.execute(
                    """
                    SELECT
                        document_id,
                        payload_hash,
                        payload_schema,
                        source_sha256,
                        segment_index,
                        segment_count,
                        content,
                        destination_fingerprint,
                        state,
                        attempt_count,
                        next_attempt_at,
                        last_error_category,
                        created_at,
                        updated_at
                    FROM outbox
                    WHERE state='pending'
                      AND next_attempt_at <= ?
                      AND destination_fingerprint = ?
                      AND payload_schema = ?
                    ORDER BY
                        next_attempt_at,
                        created_at,
                        source_sha256,
                        segment_index,
                        document_id
                    LIMIT 1
                    """,
                    (normalized_now, self._destination_fingerprint, self._payload_schema),
                ).fetchone()
                if record is None:
                    self._connection.rollback()
                    return OutboxClaimResult(OutboxClaimStatus.EMPTY)

                prior_attempt_count = int(cast(int, record[9]))
                cursor = self._connection.execute(
                    """
                    UPDATE outbox
                    SET state='sending', attempt_count=attempt_count + 1, updated_at=?
                    WHERE document_id=?
                      AND state='pending'
                      AND attempt_count=?
                      AND next_attempt_at <= ?
                      AND destination_fingerprint=?
                      AND payload_schema=?
                    """,
                    (
                        normalized_now,
                        str(record[0]),
                        prior_attempt_count,
                        normalized_now,
                        self._destination_fingerprint,
                        self._payload_schema,
                    ),
                )
                if cursor.rowcount != 1 or self._owner_descriptor(owner) is None:
                    raise _GuardedTransitionError
                self._connection.commit()

                claimed_record = list(record)
                claimed_record[8] = "sending"
                claimed_record[9] = prior_attempt_count + 1
                claimed_record[13] = normalized_now
                return OutboxClaimResult(
                    OutboxClaimStatus.CLAIMED,
                    _row_from_record(tuple(claimed_record)),
                )
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.rollback()
                return OutboxClaimResult(OutboxClaimStatus.LOCAL_FAILURE)

    def complete_claim(
        self,
        owner: ProfileLockOwner,
        *,
        document_id: str,
        attempt_count: int,
    ) -> OutboxTransitionResult:
        """Delete only the exact currently sending attempt after confirmed remote completion."""

        if not _valid_claim_guard(document_id, attempt_count):
            return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)
        with self._mutex:
            if self._closed or self._owner_descriptor(owner) is None:
                return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """
                    DELETE FROM outbox
                    WHERE document_id=? AND state='sending' AND attempt_count=?
                    """,
                    (document_id, attempt_count),
                )
                if cursor.rowcount != 1 or self._owner_descriptor(owner) is None:
                    raise _GuardedTransitionError
                self._connection.commit()
                return OutboxTransitionResult(
                    OutboxTransitionStatus.APPLIED,
                    affected_count=1,
                )
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.rollback()
                return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)

    def reschedule_claim(
        self,
        owner: ProfileLockOwner,
        *,
        document_id: str,
        attempt_count: int,
        category: OutboxFailureCategory,
        completed_at: float,
    ) -> OutboxTransitionResult:
        """Guardedly return one failed attempt to pending with capped deterministic delay."""

        normalized_completed_at = _valid_wall_time(completed_at)
        if (
            not _valid_claim_guard(document_id, attempt_count)
            or type(category) is not OutboxFailureCategory
            or normalized_completed_at is None
        ):
            return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)
        delay = _retry_delay_seconds(
            attempt_count=attempt_count,
            initial=self._retry_initial_seconds,
            maximum=self._retry_max_seconds,
        )
        next_attempt_at = normalized_completed_at + delay
        if not math.isfinite(next_attempt_at):
            return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)

        with self._mutex:
            if self._closed or self._owner_descriptor(owner) is None:
                return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """
                    UPDATE outbox
                    SET state='pending', next_attempt_at=?, last_error_category=?, updated_at=?
                    WHERE document_id=? AND state='sending' AND attempt_count=?
                    """,
                    (
                        next_attempt_at,
                        category.value,
                        normalized_completed_at,
                        document_id,
                        attempt_count,
                    ),
                )
                if cursor.rowcount != 1 or self._owner_descriptor(owner) is None:
                    raise _GuardedTransitionError
                self._connection.commit()
                return OutboxTransitionResult(
                    OutboxTransitionStatus.APPLIED,
                    affected_count=1,
                )
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.rollback()
                return OutboxTransitionResult(OutboxTransitionStatus.LOCAL_FAILURE)

    def next_matching_retry_deadline(self) -> float | None:
        """Inspect the earliest pending retry deadline for this destination and payload schema."""

        with self._mutex:
            if self._closed:
                raise OutboxReadError(OUTBOX_READ_FAILED_MESSAGE) from None
            try:
                record = self._connection.execute(
                    """
                    SELECT MIN(next_attempt_at)
                    FROM outbox
                    WHERE state='pending'
                      AND destination_fingerprint=?
                      AND payload_schema=?
                    """,
                    (self._destination_fingerprint, self._payload_schema),
                ).fetchone()
                if record is None or record[0] is None:
                    return None
                deadline = float(cast(float, record[0]))
                if not math.isfinite(deadline):
                    raise sqlite3.DatabaseError
                return deadline
            except Exception:
                raise OutboxReadError(OUTBOX_READ_FAILED_MESSAGE) from None

    def _owner_descriptor(self, owner: ProfileLockOwner) -> int | None:
        if not isinstance(owner, ProfileLockOwner):
            return None
        descriptor = owner._descriptor_for(self._lock_token)
        if descriptor is None:
            return None
        try:
            _revalidate_profile_lock_identity(
                self._hermes_home,
                self._profile_lock_identity,
                descriptor=descriptor,
                require_private_mode=True,
            )
        except Exception:
            return None
        return descriptor

    def admit(self, segments: Sequence[RetainedSegment]) -> AdmissionResult:
        """Atomically admit all new nonduplicate segments or none.

        ``pending`` and ``sending`` rows both remain unconfirmed and consume the configured logical
        row/byte capacity. No network or sender operation occurs here.
        """

        try:
            candidates, repeated_count = self._validate_candidates(segments)
        except Exception:
            return AdmissionResult(AdmissionStatus.INVALID)
        if candidates is None:
            return AdmissionResult(AdmissionStatus.INVALID)

        with self._mutex:
            if self._closed:
                return AdmissionResult(AdmissionStatus.LOCAL_FAILURE)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                result = self._admit_in_transaction(candidates, repeated_count)
                if result.accepted:
                    self._connection.commit()
                else:
                    self._connection.rollback()
                return result
            except Exception as error:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.rollback()
                if isinstance(error, sqlite3.Error) and _is_contention(error):
                    return AdmissionResult(AdmissionStatus.CONTENDED)
                return AdmissionResult(AdmissionStatus.LOCAL_FAILURE)

    def read_unconfirmed(self) -> tuple[OutboxRow, ...]:
        """Return all pending/sending rows in deterministic insertion/segment order."""

        with self._mutex:
            if self._closed:
                raise OutboxReadError(OUTBOX_READ_FAILED_MESSAGE) from None
            try:
                records = self._connection.execute(
                    """
                    SELECT
                        document_id,
                        payload_hash,
                        payload_schema,
                        source_sha256,
                        segment_index,
                        segment_count,
                        content,
                        destination_fingerprint,
                        state,
                        attempt_count,
                        next_attempt_at,
                        last_error_category,
                        created_at,
                        updated_at
                    FROM outbox
                    WHERE state IN ('pending', 'sending')
                    ORDER BY created_at, source_sha256, segment_index, document_id
                    """
                ).fetchall()
                return tuple(_row_from_record(record) for record in records)
            except Exception:
                raise OutboxReadError(OUTBOX_READ_FAILED_MESSAGE) from None

    def close(self) -> None:
        """Close this connection owner idempotently without deleting durable rows."""

        with self._mutex:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _validate_candidates(
        self, segments: Sequence[RetainedSegment]
    ) -> tuple[tuple[RetainedSegment, ...] | None, int]:
        if isinstance(segments, (str, bytes, bytearray)) or not isinstance(segments, Sequence):
            return None, 0
        if not segments:
            return None, 0

        unique: dict[str, RetainedSegment] = {}
        repeated_count = 0
        for segment in segments:
            if not self._valid_segment(segment):
                return None, 0
            prior = unique.get(segment.document_id)
            if prior is None:
                unique[segment.document_id] = segment
            elif _segment_immutable_values(prior, self._destination_fingerprint) == (
                _segment_immutable_values(segment, self._destination_fingerprint)
            ):
                repeated_count += 1
            else:
                return None, 0
        candidates = tuple(sorted(unique.values(), key=lambda segment: segment.segment_index))
        first = candidates[0]
        if (
            len(candidates) != first.segment_count
            or [segment.segment_index for segment in candidates] != list(range(first.segment_count))
            or any(
                segment.payload_schema != first.payload_schema
                or segment.source_sha256 != first.source_sha256
                or segment.segment_count != first.segment_count
                for segment in candidates
            )
        ):
            return None, 0
        source = "".join(segment.content for segment in candidates).encode("utf-8")
        if hashlib.sha256(source).hexdigest() != first.source_sha256:
            return None, 0
        return candidates, repeated_count

    def _valid_segment(self, segment: object) -> bool:
        if not isinstance(segment, RetainedSegment):
            return False
        if segment.payload_schema != PAYLOAD_SCHEMA_VERSION:
            return False
        if _HASH_PATTERN.fullmatch(segment.payload_hash) is None:
            return False
        if _HASH_PATTERN.fullmatch(segment.source_sha256) is None:
            return False
        if (
            type(segment.segment_index) is not int
            or type(segment.segment_count) is not int
            or segment.segment_index < 0
            or segment.segment_count <= 0
            or segment.segment_index >= segment.segment_count
        ):
            return False
        if not isinstance(segment.content, str) or not segment.content:
            return False
        if len(segment.content.encode("utf-8")) > self._segment_max_bytes:
            return False
        expected_hash = derive_segment_payload_hash(
            payload_schema=segment.payload_schema,
            source_sha256=segment.source_sha256,
            segment_index=segment.segment_index,
            segment_count=segment.segment_count,
            content=segment.content,
        )
        return (
            segment.payload_hash == expected_hash
            and segment.document_id == DOCUMENT_ID_PREFIX + expected_hash
        )

    def _admit_in_transaction(
        self,
        candidates: tuple[RetainedSegment, ...],
        repeated_count: int,
    ) -> AdmissionResult:
        new_segments: list[RetainedSegment] = []
        duplicate_count = repeated_count
        for segment in candidates:
            existing = self._connection.execute(
                """
                SELECT
                    payload_hash,
                    payload_schema,
                    source_sha256,
                    segment_index,
                    segment_count,
                    content,
                    destination_fingerprint
                FROM outbox
                WHERE document_id = ?
                """,
                (segment.document_id,),
            ).fetchone()
            if existing is None:
                new_segments.append(segment)
                continue
            if tuple(existing) != _segment_immutable_values(segment, self._destination_fingerprint):
                return AdmissionResult(AdmissionStatus.CONFLICT)
            duplicate_count += 1

        if not new_segments:
            return AdmissionResult(
                AdmissionStatus.DUPLICATE,
                duplicate_count=duplicate_count,
            )

        usage = self._connection.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(
                    SUM(LENGTH(CAST(content AS BLOB)) + ?),
                    0
                )
            FROM outbox
            WHERE state IN ('pending', 'sending')
            """,
            (OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES,),
        ).fetchone()
        if usage is None:
            raise sqlite3.DatabaseError
        existing_rows = int(usage[0])
        existing_bytes = int(usage[1])
        new_bytes = sum(
            len(segment.content.encode("utf-8")) + OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES
            for segment in new_segments
        )
        if (
            existing_rows + len(new_segments) > self._max_pending_rows
            or existing_bytes + new_bytes > self._max_pending_bytes
        ):
            return AdmissionResult(AdmissionStatus.CAPACITY_EXCEEDED)

        timestamp = time.time()
        self._connection.executemany(
            """
            INSERT INTO outbox (
                document_id,
                payload_hash,
                payload_schema,
                source_sha256,
                segment_index,
                segment_count,
                content,
                destination_fingerprint,
                state,
                attempt_count,
                next_attempt_at,
                last_error_category,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?)
            """,
            [
                (
                    segment.document_id,
                    segment.payload_hash,
                    segment.payload_schema,
                    segment.source_sha256,
                    segment.segment_index,
                    segment.segment_count,
                    segment.content,
                    self._destination_fingerprint,
                    timestamp,
                    timestamp,
                    timestamp,
                )
                for segment in new_segments
            ],
        )
        return AdmissionResult(
            AdmissionStatus.ADMITTED,
            inserted_count=len(new_segments),
            duplicate_count=duplicate_count,
        )


def inspect_outbox(config: BetterHindsightConfig) -> OutboxInspection:
    """Read one bounded schema-v1 snapshot without initializing sender state."""

    try:
        return _inspect_outbox(config)
    except Exception:
        raise OutboxReadError(OUTBOX_READ_FAILED_MESSAGE) from None


def _inspect_outbox(config: BetterHindsightConfig) -> OutboxInspection:
    home = config.hermes_home.resolve(strict=True)
    database_path = config.outbox.path
    _require_inside(home, database_path.resolve(strict=False))
    if not os.path.lexists(database_path):
        return OutboxInspection(outbox="uninitialized")

    paths = {
        "database": database_path,
        "wal": Path(f"{database_path}-wal"),
        "shm": Path(f"{database_path}-shm"),
        "journal": Path(f"{database_path}-journal"),
    }
    sizes = {"database": _status_file_size(home, database_path)}
    for name in ("wal", "shm", "journal"):
        path = paths[name]
        _require_inside(home, path.resolve(strict=False))
        if os.path.lexists(path):
            sizes[name] = _status_file_size(home, path)

    has_wal = "wal" in sizes
    has_shm = "shm" in sizes
    if "journal" in sizes or has_wal != has_shm:
        raise OSError
    active_wal = has_wal
    if active_wal and (
        sizes["shm"] < _STATUS_SHM_REGION_BYTES or sizes["shm"] % _STATUS_SHM_REGION_BYTES != 0
    ):
        raise OSError

    connection: sqlite3.Connection | None = None
    try:
        query = "mode=ro&vfs=unix" if active_wal else "mode=ro&immutable=1&vfs=unix"
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?{query}",
            uri=True,
            timeout=config.outbox.busy_timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        inspection = _read_status_snapshot(connection, config)
        connection.close()
        connection = None

        ownership = _probe_sender_ownership(home, Path(f"{database_path}.lock"))
        return OutboxInspection(
            outbox="ready",
            mismatch_count=inspection.mismatch_count,
            pending_count=inspection.pending_count,
            retry_count=inspection.retry_count,
            sending_count=inspection.sending_count,
            logical_queued_bytes=inspection.logical_queued_bytes,
            oldest_created_at=inspection.oldest_created_at,
            last_error_category=inspection.last_error_category,
            max_attempt_count=inspection.max_attempt_count,
            next_retry_at=inspection.next_retry_at,
            error_category_counts=inspection.error_category_counts,
            sender_ownership=ownership,
        )
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()


def _read_status_snapshot(
    connection: sqlite3.Connection,
    config: BetterHindsightConfig,
) -> OutboxInspection:
    connection.execute("PRAGMA query_only=ON")
    query_only = connection.execute("PRAGMA query_only").fetchone()
    if query_only != (1,):
        raise sqlite3.DatabaseError
    connection.execute("BEGIN")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()
        if version != (OUTBOX_SCHEMA_VERSION,):
            raise _UnsupportedSchemaError
        _validate_schema(connection)
        record = connection.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE
                    WHEN destination_fingerprint <> :destination
                      OR payload_schema <> :payload_schema THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE
                    WHEN destination_fingerprint = :destination
                     AND payload_schema = :payload_schema
                     AND state = 'sending' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE
                    WHEN destination_fingerprint = :destination
                     AND payload_schema = :payload_schema
                     AND state = 'pending' AND attempt_count > 0 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE
                    WHEN destination_fingerprint = :destination
                     AND payload_schema = :payload_schema
                     AND state = 'pending' AND attempt_count = 0 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(LENGTH(CAST(content AS BLOB)) + :allowance), 0),
                MIN(created_at),
                (
                    SELECT last_error_category
                    FROM outbox
                    WHERE last_error_category IS NOT NULL
                    ORDER BY updated_at DESC, created_at DESC, document_id DESC
                    LIMIT 1
                ),
                COALESCE(SUM(CASE WHEN
                    typeof(document_id) <> 'text'
                    OR typeof(payload_hash) <> 'text'
                    OR typeof(payload_schema) <> 'text'
                    OR typeof(source_sha256) <> 'text'
                    OR typeof(segment_index) <> 'integer'
                    OR typeof(segment_count) <> 'integer'
                    OR segment_index < 0 OR segment_count <= 0 OR segment_index >= segment_count
                    OR typeof(content) <> 'text'
                    OR typeof(destination_fingerprint) <> 'text'
                    OR typeof(state) <> 'text' OR state NOT IN ('pending', 'sending')
                    OR typeof(attempt_count) <> 'integer' OR attempt_count < 0
                    OR typeof(next_attempt_at) NOT IN ('integer', 'real') OR next_attempt_at < 0
                    OR (last_error_category IS NOT NULL AND (
                        typeof(last_error_category) <> 'text'
                        OR last_error_category NOT IN (
                            'retain_timeout', 'retain_failed', 'retain_unconfirmed'
                        )
                    ))
                    OR typeof(created_at) NOT IN ('integer', 'real') OR created_at < 0
                    OR typeof(updated_at) NOT IN ('integer', 'real') OR updated_at < 0
                    THEN 1 ELSE 0 END), 0),
                MAX(ABS(created_at)),
                MAX(ABS(updated_at)),
                MAX(ABS(next_attempt_at)),
                COALESCE(MAX(attempt_count), 0),
                MIN(CASE
                    WHEN destination_fingerprint = :destination
                     AND payload_schema = :payload_schema
                     AND state = 'pending' AND attempt_count > 0
                    THEN next_attempt_at END),
                COALESCE(SUM(CASE
                    WHEN last_error_category = 'retain_timeout' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE
                    WHEN last_error_category = 'retain_failed' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE
                    WHEN last_error_category = 'retain_unconfirmed' THEN 1 ELSE 0 END), 0)
            FROM outbox
            """,
            {
                "allowance": OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES,
                "destination": config.destination_fingerprint,
                "payload_schema": config.outbox.payload_schema,
            },
        ).fetchone()
        if record is None or len(record) != 17:
            raise sqlite3.DatabaseError
        connection.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise

    integer_values = record[:6] + (record[8], record[12], record[14], record[15], record[16])
    if any(type(value) is not int or value < 0 for value in integer_values):
        raise sqlite3.DatabaseError
    total, mismatch, sending, retry, pending, logical_bytes = cast(
        tuple[int, int, int, int, int, int], record[:6]
    )
    if cast(int, record[8]) != 0 or mismatch + sending + retry + pending != total:
        raise sqlite3.DatabaseError
    for maximum in record[9:12]:
        if maximum is not None and (
            isinstance(maximum, bool)
            or not isinstance(maximum, (int, float))
            or not math.isfinite(float(maximum))
        ):
            raise sqlite3.DatabaseError

    next_retry_value = record[13]
    if next_retry_value is None:
        next_retry_at = None
    elif (
        isinstance(next_retry_value, bool)
        or not isinstance(next_retry_value, (int, float))
        or not math.isfinite(float(next_retry_value))
        or float(next_retry_value) < 0.0
    ):
        raise sqlite3.DatabaseError
    else:
        next_retry_at = float(next_retry_value)

    oldest_value = record[6]
    if oldest_value is None:
        oldest = None
    elif (
        isinstance(oldest_value, bool)
        or not isinstance(oldest_value, (int, float))
        or not math.isfinite(float(oldest_value))
        or float(oldest_value) < 0.0
    ):
        raise sqlite3.DatabaseError
    else:
        oldest = float(oldest_value)

    last_error = record[7]
    if last_error is not None and (
        type(last_error) is not str or last_error not in _STATUS_ERROR_CATEGORIES
    ):
        raise sqlite3.DatabaseError
    typed_last_error = None if last_error is None else OutboxFailureCategory(last_error)
    return OutboxInspection(
        outbox="ready",
        mismatch_count=mismatch,
        pending_count=pending,
        retry_count=retry,
        sending_count=sending,
        logical_queued_bytes=logical_bytes,
        oldest_created_at=oldest,
        last_error_category=typed_last_error,
        max_attempt_count=cast(int, record[12]),
        next_retry_at=next_retry_at,
        error_category_counts={
            "retain_timeout": cast(int, record[14]),
            "retain_failed": cast(int, record[15]),
            "retain_unconfirmed": cast(int, record[16]),
        },
    )


def _status_file_size(home: Path, path: Path) -> int:
    status = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(status.st_mode):
        raise OSError
    _require_inside(home, path.resolve(strict=True))
    return status.st_size


def _probe_sender_ownership(
    home: Path,
    lock_path: Path,
) -> Literal["held", "free", "unavailable"]:
    try:
        _require_inside(home, lock_path.resolve(strict=False))
        if not os.path.lexists(lock_path):
            return "free"
        _status_file_size(home, lock_path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(lock_path, flags)
    except Exception:
        return "unavailable"

    acquired = False
    released = False
    try:
        acquired = _try_flock_exclusive(descriptor)
        ownership: Literal["held", "free"] = "free" if acquired else "held"
        if acquired:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
            released = True
        return ownership
    except Exception:
        return "unavailable"
    finally:
        if acquired and not released:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _prepare_private_paths(
    config: BetterHindsightConfig,
) -> tuple[Path, Path, Path, tuple[int, int], tuple[int, int]]:
    home = config.hermes_home.resolve(strict=True)
    configured_path = config.outbox.path
    initially_resolved = configured_path.resolve(strict=False)
    _require_inside(home, initially_resolved)

    relative_parent = configured_path.parent.relative_to(home)
    current = home
    for component in relative_parent.parts:
        candidate = current / component
        if os.path.lexists(candidate):
            resolved = candidate.resolve(strict=True)
            _require_inside(home, resolved)
            if not resolved.is_dir():
                raise OSError
            current = resolved
            continue
        os.mkdir(candidate, mode=0o700)
        if os.name == "posix":
            os.chmod(candidate, 0o700)
        current = candidate

    database_path = configured_path.resolve(strict=False)
    _require_inside(home, database_path)
    profile_lock_path = Path(f"{database_path}.lock")
    _require_inside(home, profile_lock_path)
    database_identity = _ensure_private_regular_file(database_path)
    profile_lock_identity = _ensure_private_regular_file(profile_lock_path)
    return home, database_path, profile_lock_path, database_identity, profile_lock_identity


def _ensure_private_regular_file(path: Path) -> tuple[int, int]:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise OSError
        if os.name == "posix" and created:
            os.fchmod(descriptor, 0o600)
        return status.st_dev, status.st_ino
    finally:
        os.close(descriptor)


def _connect_existing_database(
    database_path: Path,
    config: BetterHindsightConfig,
) -> sqlite3.Connection:
    return sqlite3.connect(
        f"{database_path.as_uri()}?mode=rw",
        uri=True,
        timeout=config.outbox.busy_timeout_seconds,
        isolation_level=None,
        check_same_thread=False,
    )


def _set_private_file_mode(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o600, follow_symlinks=False)


def _revalidate_database_identity(
    home: Path,
    database_path: Path,
    expected_identity: tuple[int, int],
) -> None:
    resolved_home = home.resolve(strict=True)
    resolved_database = database_path.resolve(strict=True)
    _require_inside(resolved_home, resolved_database)
    status = database_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(status.st_mode) or (status.st_dev, status.st_ino) != expected_identity:
        raise OSError


def _revalidate_open_paths(
    home: Path,
    database_path: Path,
    profile_lock_path: Path,
    expected_database_identity: tuple[int, int],
    expected_profile_lock_identity: tuple[int, int],
) -> None:
    _revalidate_database_identity(home, database_path, expected_database_identity)
    _revalidate_profile_lock_identity(
        home,
        ProfileLockIdentity(
            path=profile_lock_path,
            device=expected_profile_lock_identity[0],
            inode=expected_profile_lock_identity[1],
        ),
        descriptor=None,
        require_private_mode=False,
    )


def _revalidate_profile_lock_identity(
    home: Path,
    identity: ProfileLockIdentity,
    *,
    descriptor: int | None,
    require_private_mode: bool,
) -> None:
    resolved_home = home.resolve(strict=True)
    resolved_lock = identity.path.resolve(strict=True)
    _require_inside(resolved_home, resolved_lock)
    path_status = identity.path.stat(follow_symlinks=False)
    _validate_profile_lock_status(path_status, identity, require_private_mode=require_private_mode)
    if descriptor is not None:
        descriptor_status = os.fstat(descriptor)
        _validate_profile_lock_status(
            descriptor_status,
            identity,
            require_private_mode=require_private_mode,
        )


def _validate_profile_lock_status(
    status: os.stat_result,
    identity: ProfileLockIdentity,
    *,
    require_private_mode: bool,
) -> None:
    if not stat.S_ISREG(status.st_mode) or (status.st_dev, status.st_ino) != (
        identity.device,
        identity.inode,
    ):
        raise OSError
    if os.name != "posix":
        return
    if status.st_uid != os.geteuid():
        raise OSError
    if require_private_mode and stat.S_IMODE(status.st_mode) != 0o600:
        raise OSError


def _try_flock_exclusive(descriptor: int) -> bool:
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _release_profile_lock_descriptor(descriptor: int) -> None:
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(descriptor)


def _valid_wall_time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if normalized < 0.0 or not math.isfinite(normalized):
        return None
    return normalized


def _valid_claim_guard(document_id: object, attempt_count: object) -> bool:
    return (
        isinstance(document_id, str)
        and bool(document_id)
        and type(attempt_count) is int
        and attempt_count > 0
    )


def _retry_delay_seconds(*, attempt_count: int, initial: float, maximum: float) -> float:
    delay = initial
    remaining = attempt_count - 1
    if delay <= 0.0 or delay >= maximum:
        return delay
    while remaining > 0 and delay < maximum:
        delay = min(delay * 2.0, maximum)
        remaining -= 1
    return delay


def _require_inside(home: Path, candidate: Path) -> None:
    try:
        candidate.relative_to(home)
    except ValueError:
        raise OSError from None


def _initialize_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise _InvalidSchemaError
    version = int(row[0])
    if version not in {0, OUTBOX_SCHEMA_VERSION}:
        raise _UnsupportedSchemaError
    if version == OUTBOX_SCHEMA_VERSION:
        _validate_schema(connection)
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise _InvalidSchemaError
        version = int(row[0])
        if version not in {0, OUTBOX_SCHEMA_VERSION}:
            raise _UnsupportedSchemaError
        if version == 0:
            connection.execute(_SCHEMA_SQL)
            _validate_schema(connection)
            connection.execute(f"PRAGMA user_version = {OUTBOX_SCHEMA_VERSION}")
        else:
            _validate_schema(connection)
        connection.commit()
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            connection.rollback()
        raise


def _validate_schema(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA table_info(outbox)").fetchall()
    column_info = tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
        )
        for row in rows
    )
    if column_info != _SCHEMA_COLUMN_INFO:
        raise _InvalidSchemaError

    schema_rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    if len(schema_rows) != 1:
        raise _InvalidSchemaError
    object_type, object_name, table_name, schema_sql = schema_rows[0]
    if (
        object_type != "table"
        or object_name != "outbox"
        or table_name != "outbox"
        or not isinstance(schema_sql, str)
    ):
        raise _InvalidSchemaError
    if " ".join(schema_sql.split()) != _SCHEMA_SQL_SIGNATURE:
        raise _InvalidSchemaError

    encoding = connection.execute("PRAGMA encoding").fetchone()
    if encoding is None or str(encoding[0]).casefold() != "utf-8":
        raise _InvalidSchemaError


def _segment_immutable_values(
    segment: RetainedSegment, destination_fingerprint: str
) -> tuple[object, ...]:
    return (
        segment.payload_hash,
        segment.payload_schema,
        segment.source_sha256,
        segment.segment_index,
        segment.segment_count,
        segment.content,
        destination_fingerprint,
    )


def _row_from_record(record: tuple[object, ...]) -> OutboxRow:
    return OutboxRow(
        document_id=str(record[0]),
        payload_hash=str(record[1]),
        payload_schema=str(record[2]),
        source_sha256=str(record[3]),
        segment_index=int(cast(int, record[4])),
        segment_count=int(cast(int, record[5])),
        content=str(record[6]),
        destination_fingerprint=str(record[7]),
        state=cast(Literal["pending", "sending"], record[8]),
        attempt_count=int(cast(int, record[9])),
        next_attempt_at=float(cast(float, record[10])),
        last_error_category=None if record[11] is None else str(record[11]),
        created_at=float(cast(float, record[12])),
        updated_at=float(cast(float, record[13])),
    )


def _is_contention(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    primary_code = code & 0xFF
    return primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


__all__ = [
    "OUTBOX_OPEN_FAILED_MESSAGE",
    "OUTBOX_READ_FAILED_MESSAGE",
    "OUTBOX_SCHEMA_UNSUPPORTED_MESSAGE",
    "OUTBOX_SCHEMA_VERSION",
    "AdmissionResult",
    "AdmissionStatus",
    "OutboxClaimResult",
    "OutboxClaimStatus",
    "OutboxFailureCategory",
    "OutboxInspection",
    "OutboxOpenError",
    "OutboxReadError",
    "OutboxRow",
    "OutboxTransitionResult",
    "OutboxTransitionStatus",
    "ProfileLockAcquisitionResult",
    "ProfileLockIdentity",
    "ProfileLockOwner",
    "ProfileLockStatus",
    "SQLiteOutbox",
    "inspect_outbox",
]
