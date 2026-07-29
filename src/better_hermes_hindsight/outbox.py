"""Private schema-v1 SQLite outbox with atomic bounded whole-turn admission."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from better_hermes_hindsight.config import (
    OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES,
    PAYLOAD_SCHEMA_VERSION,
    BetterHindsightConfig,
)
from better_hermes_hindsight.retention import (
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


class OutboxOpenError(RuntimeError):
    """A fixed, sanitized outbox-open failure."""


class OutboxReadError(RuntimeError):
    """A fixed, sanitized outbox-read failure."""


class _UnsupportedSchemaError(RuntimeError):
    pass


class _InvalidSchemaError(RuntimeError):
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


class SQLiteOutbox:
    """One connection owner for a profile-local private outbox."""

    __slots__ = (
        "_closed",
        "_connection",
        "_destination_fingerprint",
        "_max_pending_bytes",
        "_max_pending_rows",
        "_mutex",
        "_profile_lock_path",
        "_segment_max_bytes",
    )

    def __init__(
        self,
        *,
        config: BetterHindsightConfig,
        connection: sqlite3.Connection,
        profile_lock_path: Path,
    ) -> None:
        self._connection = connection
        self._profile_lock_path = profile_lock_path
        self._destination_fingerprint = config.destination_fingerprint
        self._max_pending_rows = config.outbox.max_pending_rows
        self._max_pending_bytes = config.outbox.max_pending_bytes
        self._segment_max_bytes = config.retain.segment_max_bytes
        self._mutex = threading.Lock()
        self._closed = False

    @classmethod
    def open(cls, config: BetterHindsightConfig) -> SQLiteOutbox:
        """Open schema v1 after revalidating the configured path inside ``hermes_home``."""

        connection: sqlite3.Connection | None = None
        try:
            database_path, profile_lock_path, database_identity = _prepare_private_paths(config)
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
            )
            _set_private_file_mode(database_path)
            _set_private_file_mode(profile_lock_path)
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
            profile_lock_path=profile_lock_path,
        )

    def __repr__(self) -> str:
        return "SQLiteOutbox()"

    @property
    def profile_lock_path(self) -> Path:
        """Return the private lock file Task 3 will acquire for sender ownership."""

        return self._profile_lock_path

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


def _prepare_private_paths(
    config: BetterHindsightConfig,
) -> tuple[Path, Path, tuple[int, int]]:
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
    _ensure_private_regular_file(profile_lock_path)
    return database_path, profile_lock_path, database_identity


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
) -> None:
    _revalidate_database_identity(home, database_path, expected_database_identity)
    resolved_home = home.resolve(strict=True)
    resolved_lock = profile_lock_path.resolve(strict=True)
    _require_inside(resolved_home, resolved_lock)
    if not resolved_lock.is_file():
        raise OSError


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
    "OutboxOpenError",
    "OutboxReadError",
    "OutboxRow",
    "SQLiteOutbox",
]
