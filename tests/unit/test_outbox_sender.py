"""Deterministic data/ownership contract tests for the Task 3 outbox sender seam."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Protocol, cast

import pytest

import better_hermes_hindsight.outbox as outbox_module
import better_hermes_hindsight.retention as retention_module
import better_hermes_hindsight.runtime as runtime_module
from better_hermes_hindsight.client import (
    HindsightClientError,
    HindsightClientProtocol,
    RetainConfirmation,
)
from better_hermes_hindsight.client import (
    RetainSegment as ClientRetainSegment,
)
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.outbox import SQLiteOutbox
from better_hermes_hindsight.retention import (
    RetainedSegment,
    build_retained_segments,
    derive_segment_payload_hash,
    retained_event_timestamp,
)
from better_hermes_hindsight.runtime import AsyncCallTimeoutError, AsyncRunner


def _config(
    home: Path,
    *,
    bank_id: str = "synthetic-bank",
    segment_max_bytes: int = 4096,
    retain_timeout_seconds: float = 0.01,
    poll_interval_seconds: float = 0.1,
    retry_initial_seconds: float = 2.0,
    retry_max_seconds: float = 10.0,
) -> BetterHindsightConfig:
    home.mkdir(parents=True, exist_ok=True)
    return load_config(
        hermes_home=home,
        environ={},
        injected={
            "api_url": "https://service.example.test",
            "bank_id": bank_id,
            "retain": {
                "enabled": True,
                "timeout_seconds": retain_timeout_seconds,
                "segment_max_bytes": segment_max_bytes,
            },
            "outbox": {
                "path": "better_hindsight/outbox.sqlite3",
                "max_pending_rows": 2_000,
                "max_pending_bytes": 1_000_000,
                "busy_timeout_seconds": 0.2,
                "poll_interval_seconds": poll_interval_seconds,
                "retry_initial_seconds": retry_initial_seconds,
                "retry_max_seconds": retry_max_seconds,
            },
        },
    )


def _turn(seed: str, *, segment_max_bytes: int = 4096) -> tuple[RetainedSegment, ...]:
    return build_retained_segments(
        session_id=f"session-{seed}",
        user_content=f"user-{seed}-" + seed * 40,
        assistant_content=f"assistant-{seed}-" + seed * 40,
        tags=("project:sample",),
        segment_max_bytes=segment_max_bytes,
    )


def _single_segment(seed: str) -> RetainedSegment:
    segments = _turn(seed)
    assert len(segments) == 1
    return segments[0]


def _legacy_segment() -> RetainedSegment:
    content = 'turn-v1","roles":[{"content":"legacy pending fragment'
    source_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    payload_hash = derive_segment_payload_hash(
        payload_schema="better-hindsight-turn-v1",
        source_sha256=source_sha256,
        segment_index=0,
        segment_count=1,
        content=content,
    )
    return RetainedSegment(
        document_id="better-hindsight-turn-v1:" + payload_hash,
        payload_hash=payload_hash,
        payload_schema="better-hindsight-turn-v1",
        source_sha256=source_sha256,
        segment_index=0,
        segment_count=1,
        content=content,
    )


def _client_segment(segment: RetainedSegment) -> ClientRetainSegment:
    return ClientRetainSegment(
        content=segment.content,
        document_id=segment.document_id,
        payload_schema=segment.payload_schema,
        source_sha256=segment.source_sha256,
        segment_index=segment.segment_index,
        segment_count=segment.segment_count,
        timestamp=retained_event_timestamp(segment.content),
    )


def _execute(path: Path, statement: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def _insert_legacy_segment(config: BetterHindsightConfig, segment: RetainedSegment) -> None:
    _execute(
        config.outbox.path,
        """
        INSERT INTO outbox (
            document_id, payload_hash, payload_schema, source_sha256,
            segment_index, segment_count, content, destination_fingerprint,
            state, attempt_count, next_attempt_at, last_error_category,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 90.0, NULL, 80.0, 80.0)
        """,
        (
            segment.document_id,
            segment.payload_hash,
            segment.payload_schema,
            segment.source_sha256,
            segment.segment_index,
            segment.segment_count,
            segment.content,
            config.legacy_destination_fingerprint,
        ),
    )


def _schema_contract(path: Path) -> tuple[int, tuple[tuple[object, ...], ...]]:
    connection = sqlite3.connect(path)
    try:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        assert version_row is not None
        inventory = tuple(
            connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
        )
        return int(version_row[0]), inventory
    finally:
        connection.close()


def _acquire(outbox: SQLiteOutbox) -> outbox_module.ProfileLockOwner:
    acquisition = outbox.try_acquire_profile_lock()
    assert acquisition.status is outbox_module.ProfileLockStatus.ACQUIRED
    assert acquisition.owner is not None
    return acquisition.owner


def _immutable_values(row: outbox_module.OutboxRow) -> tuple[object, ...]:
    return (
        row.document_id,
        row.payload_hash,
        row.payload_schema,
        row.source_sha256,
        row.segment_index,
        row.segment_count,
        row.content,
        row.destination_fingerprint,
        row.created_at,
    )


class _WallClock:
    def __init__(self, value: float) -> None:
        self._condition = threading.Condition()
        self._value = value

    def __call__(self) -> float:
        with self._condition:
            return self._value

    def set(self, value: float) -> None:
        with self._condition:
            self._value = value
            self._condition.notify_all()


class _ObservedOutbox:
    """Observe sender transitions while delegating every ownership check to the real outbox."""

    def __init__(self, delegate: SQLiteOutbox) -> None:
        self.delegate = delegate
        self.operations: list[str] = []
        self._lock = threading.Lock()
        self.acquire_seen = threading.Event()
        self.contended_seen = threading.Event()
        self.recovered = threading.Event()
        self.queried = threading.Event()
        self.claimed = threading.Event()
        self.rescheduled = threading.Event()
        self.completed = threading.Event()
        self.completion_failed = threading.Event()
        self.fail_next_completion = False
        self.allow_reacquire = threading.Event()
        self.allow_reacquire.set()
        self._acquisition_count = 0

    @property
    def profile_lock_path(self) -> Path:
        return self.delegate.profile_lock_path

    def admit(self, segments: tuple[RetainedSegment, ...]) -> outbox_module.AdmissionResult:
        return self.delegate.admit(segments)

    def read_unconfirmed(self) -> tuple[outbox_module.OutboxRow, ...]:
        return self.delegate.read_unconfirmed()

    def try_acquire_profile_lock(self) -> outbox_module.ProfileLockAcquisitionResult:
        with self._lock:
            acquisition_index = self._acquisition_count
            self._acquisition_count += 1
        if acquisition_index > 0 and not self.allow_reacquire.wait(timeout=3.0):
            return outbox_module.ProfileLockAcquisitionResult(
                outbox_module.ProfileLockStatus.LOCAL_FAILURE
            )
        result = self.delegate.try_acquire_profile_lock()
        self._record(f"acquire:{result.status.value}")
        self.acquire_seen.set()
        if result.status is outbox_module.ProfileLockStatus.CONTENDED:
            self.contended_seen.set()
        return result

    def recover_sending(
        self,
        owner: outbox_module.ProfileLockOwner,
        *,
        now: float,
    ) -> outbox_module.OutboxTransitionResult:
        result = self.delegate.recover_sending(owner, now=now)
        self._record(f"recover:{result.status.value}")
        self.recovered.set()
        return result

    def claim_due(
        self,
        owner: outbox_module.ProfileLockOwner,
        *,
        now: float,
    ) -> outbox_module.OutboxClaimResult:
        result = self.delegate.claim_due(owner, now=now)
        self._record(f"claim:{result.status.value}")
        self.queried.set()
        if result.claimed:
            self.claimed.set()
        return result

    def complete_claim(
        self,
        owner: outbox_module.ProfileLockOwner,
        *,
        document_id: str,
        attempt_count: int,
    ) -> outbox_module.OutboxTransitionResult:
        if self.fail_next_completion:
            self.fail_next_completion = False
            self._record("complete:local_failure")
            self.allow_reacquire.clear()
            self.completion_failed.set()
            return outbox_module.OutboxTransitionResult(
                outbox_module.OutboxTransitionStatus.LOCAL_FAILURE
            )
        result = self.delegate.complete_claim(
            owner,
            document_id=document_id,
            attempt_count=attempt_count,
        )
        self._record(f"complete:{result.status.value}")
        if result.applied:
            self.completed.set()
        return result

    def reschedule_claim(
        self,
        owner: outbox_module.ProfileLockOwner,
        *,
        document_id: str,
        attempt_count: int,
        category: outbox_module.OutboxFailureCategory,
        completed_at: float,
    ) -> outbox_module.OutboxTransitionResult:
        result = self.delegate.reschedule_claim(
            owner,
            document_id=document_id,
            attempt_count=attempt_count,
            category=category,
            completed_at=completed_at,
        )
        self._record(f"reschedule:{category.value}:{result.status.value}")
        if result.applied:
            self.rescheduled.set()
        return result

    def next_matching_retry_deadline(self) -> float | None:
        result = self.delegate.next_matching_retry_deadline()
        self._record("deadline")
        return result

    def close(self) -> None:
        self.delegate.close()

    def snapshot_operations(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self.operations)

    def _record(self, operation: str) -> None:
        with self._lock:
            self.operations.append(operation)


_RetainBehavior = Callable[[ClientRetainSegment, int], Awaitable[RetainConfirmation]]


class _ScriptedRetainClient:
    def __init__(self, behavior: _RetainBehavior) -> None:
        self._behavior = behavior
        self._condition = threading.Condition()
        self.factory_segments: list[ClientRetainSegment] = []
        self.active_calls = 0
        self.close_calls = 0
        self.max_active_calls = 0

    def retain_segment(self, segment: ClientRetainSegment) -> Awaitable[RetainConfirmation]:
        with self._condition:
            attempt_index = len(self.factory_segments)
            self.factory_segments.append(segment)
            self._condition.notify_all()

        async def invoke() -> RetainConfirmation:
            with self._condition:
                self.active_calls += 1
                self.max_active_calls = max(self.max_active_calls, self.active_calls)
            try:
                return await self._behavior(segment, attempt_index)
            finally:
                with self._condition:
                    self.active_calls -= 1
                    self._condition.notify_all()

        return invoke()

    async def close(self) -> None:
        self.close_calls += 1

    def wait_for_factory_calls(self, count: int, *, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.factory_segments) >= count,
                timeout=timeout,
            )


class _CrossProcessEvent(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def _send_nonblocking_lock_result(path: str, connection: Connection) -> None:
    import fcntl

    descriptor = os.open(path, os.O_RDWR)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        connection.send(acquired)
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        connection.close()


def _hold_profile_lock(
    path: str,
    ready: _CrossProcessEvent,
    release: _CrossProcessEvent,
) -> None:
    import fcntl

    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        if not release.wait(timeout=5.0):
            raise RuntimeError("synthetic profile-lock holder timed out")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _admit_from_non_owner_process(
    config: BetterHindsightConfig,
    segment: RetainedSegment,
    connection: Connection,
) -> None:
    outbox: SQLiteOutbox | None = None
    try:
        outbox = SQLiteOutbox.open(config)
        ownership = outbox.try_acquire_profile_lock()
        if ownership.owner is not None:
            ownership.owner.release()
        if ownership.status is not outbox_module.ProfileLockStatus.CONTENDED:
            raise AssertionError("process B unexpectedly acquired the process A sender lock")

        # Process B deliberately performs only a passive ownership probe and local admission.
        # It constructs no runtime, sender, runner, or client and makes no sender transition.
        result = outbox.admit((segment,))
        connection.send(
            (
                "ok",
                result.status.value,
                ownership.status.value,
                os.getpid(),
                time.monotonic(),
            )
        )
    except BaseException as error:
        connection.send(("error", repr(error)))
        raise
    finally:
        if outbox is not None:
            outbox.close()
        connection.close()


def _spawn_can_acquire_profile_lock(path: Path) -> bool:
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(
        target=_send_nonblocking_lock_result,
        args=(str(path), writer),
    )
    process.start()
    writer.close()
    try:
        assert reader.poll(5.0)
        acquired = reader.recv()
    finally:
        reader.close()
        process.join(timeout=5.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
    assert process.exitcode == 0
    assert type(acquired) is bool
    return acquired


def _make_sender(
    *,
    config: BetterHindsightConfig,
    outbox: object,
    client: _ScriptedRetainClient,
    runner: AsyncRunner,
    wall_time: _WallClock,
) -> runtime_module.OutboxSender:
    return runtime_module.OutboxSender(
        config=config,
        outbox=cast(runtime_module.OutboxProtocol, outbox),
        client=cast(HindsightClientProtocol, client),
        runner=runner,
        wall_time=wall_time,
    )


def test_recovery_requires_live_owner_and_resets_every_sending_row(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outbox = SQLiteOutbox.open(config)
    segments = (_single_segment("recover-a"), _single_segment("recover-b"))
    try:
        for segment in segments:
            assert outbox.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            """
            UPDATE outbox
            SET state='sending', attempt_count=3, next_attempt_at=900.0,
                last_error_category='retain_failed', updated_at=12.0
            WHERE document_id=?
            """,
            (segments[0].document_id,),
        )
        _execute(
            config.outbox.path,
            """
            UPDATE outbox
            SET destination_fingerprint=?, state='sending', attempt_count=7,
                next_attempt_at=901.0, last_error_category='retain_timeout', updated_at=13.0
            WHERE document_id=?
            """,
            ("0" * 64, segments[1].document_id),
        )
        before = {row.document_id: row for row in outbox.read_unconfirmed()}

        released_owner = _acquire(outbox)
        released_owner.release()
        denied = outbox.recover_sending(released_owner, now=50.0)
        assert denied.status is outbox_module.OutboxTransitionStatus.LOCAL_FAILURE
        assert outbox.read_unconfirmed() == tuple(before.values())

        owner = _acquire(outbox)
        try:
            recovered = outbox.recover_sending(owner, now=50.0)
        finally:
            owner.release()
        after = {row.document_id: row for row in outbox.read_unconfirmed()}
    finally:
        outbox.close()

    assert recovered.status is outbox_module.OutboxTransitionStatus.APPLIED
    assert recovered.affected_count == 2
    assert set(after) == set(before)
    for document_id, prior in before.items():
        current = after[document_id]
        assert _immutable_values(current) == _immutable_values(prior)
        assert current.state == "pending"
        assert current.attempt_count == prior.attempt_count
        assert current.last_error_category == prior.last_error_category
        assert current.next_attempt_at == 50.0
        assert current.updated_at == 50.0


def test_recovery_later_row_failure_rolls_back_every_row(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outbox = SQLiteOutbox.open(config)
    try:
        for seed in ("rollback-a", "rollback-b", "rollback-c"):
            assert outbox.admit((_single_segment(seed),)).accepted
        _execute(
            config.outbox.path,
            """
            UPDATE outbox
            SET state='sending', attempt_count=4, next_attempt_at=91.0,
                last_error_category='retain_unconfirmed', updated_at=11.0
            """,
        )
        _execute(
            config.outbox.path,
            """
            CREATE TRIGGER abort_after_one_recovery
            BEFORE UPDATE OF state, next_attempt_at, updated_at ON outbox
            WHEN NEW.state='pending' AND (
                SELECT COUNT(*) FROM outbox
                WHERE state='pending' AND updated_at=75.0
            ) > 0
            BEGIN
                SELECT RAISE(ABORT, 'synthetic later-row recovery failure');
            END
            """,
        )
        owner = _acquire(outbox)
        try:
            result = outbox.recover_sending(owner, now=75.0)
        finally:
            owner.release()
        rows = outbox.read_unconfirmed()
        _execute(config.outbox.path, "DROP TRIGGER abort_after_one_recovery")
    finally:
        outbox.close()

    assert result.status is outbox_module.OutboxTransitionStatus.LOCAL_FAILURE
    assert result.affected_count == 0
    assert len(rows) == 3
    assert {row.state for row in rows} == {"sending"}
    assert {row.attempt_count for row in rows} == {4}
    assert {row.next_attempt_at for row in rows} == {91.0}
    assert {row.updated_at for row in rows} == {11.0}
    assert {row.last_error_category for row in rows} == {"retain_unconfirmed"}


def test_claim_selects_one_due_matching_row_in_frozen_order(tmp_path: Path) -> None:
    segment_max_bytes = 4096
    config = _config(tmp_path, segment_max_bytes=segment_max_bytes)
    outbox = SQLiteOutbox.open(config)
    first_turn = _turn("claim-a", segment_max_bytes=segment_max_bytes)
    second_turn = _turn("claim-b", segment_max_bytes=segment_max_bytes)
    mismatch_turn = _turn("claim-mismatch", segment_max_bytes=segment_max_bytes)
    schema_mismatch_turn = _turn("claim-schema-mismatch", segment_max_bytes=segment_max_bytes)
    delayed = _turn("claim-delayed", segment_max_bytes=segment_max_bytes)
    try:
        for turn in (
            first_turn,
            second_turn,
            mismatch_turn,
            schema_mismatch_turn,
            delayed,
        ):
            assert outbox.admit(turn).accepted
        matching_ids = {segment.document_id for segment in first_turn + second_turn}
        mismatch_ids = {segment.document_id for segment in mismatch_turn}
        schema_mismatch_ids = {segment.document_id for segment in schema_mismatch_turn}
        delayed_ids = {segment.document_id for segment in delayed}
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=10.0, created_at=20.0, updated_at=20.0",
        )
        for document_id in mismatch_ids:
            _execute(
                config.outbox.path,
                """
                UPDATE outbox
                SET destination_fingerprint=?, next_attempt_at=1.0
                WHERE document_id=?
                """,
                ("0" * 64, document_id),
            )
        for document_id in schema_mismatch_ids:
            _execute(
                config.outbox.path,
                """
                UPDATE outbox
                SET payload_schema='other-schema', next_attempt_at=1.0
                WHERE document_id=?
                """,
                (document_id,),
            )
        for document_id in delayed_ids:
            _execute(
                config.outbox.path,
                "UPDATE outbox SET next_attempt_at=30.0 WHERE document_id=?",
                (document_id,),
            )
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=5.0, created_at=99.0 WHERE document_id=?",
            (first_turn[-1].document_id,),
        )
        _execute(
            config.outbox.path,
            "UPDATE outbox SET created_at=1.0 WHERE document_id=?",
            (second_turn[-1].document_id,),
        )
        before = {row.document_id: row for row in outbox.read_unconfirmed()}
        expected_order = sorted(
            (before[document_id] for document_id in matching_ids),
            key=lambda row: (
                row.next_attempt_at,
                row.created_at,
                row.source_sha256,
                row.segment_index,
                row.document_id,
            ),
        )

        owner = _acquire(outbox)
        claimed_rows: list[outbox_module.OutboxRow] = []
        try:
            while True:
                claim = outbox.claim_due(owner, now=10.0)
                if claim.status is outbox_module.OutboxClaimStatus.EMPTY:
                    break
                assert claim.status is outbox_module.OutboxClaimStatus.CLAIMED
                assert claim.row is not None
                claimed_rows.append(claim.row)
                persisted = outbox.read_unconfirmed()
                sending = [row for row in persisted if row.state == "sending"]
                assert sending == [claim.row]
                completed = outbox.complete_claim(
                    owner,
                    document_id=claim.row.document_id,
                    attempt_count=claim.row.attempt_count,
                )
                assert completed.status is outbox_module.OutboxTransitionStatus.APPLIED
        finally:
            owner.release()
        remaining = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert [row.document_id for row in claimed_rows] == [row.document_id for row in expected_order]
    assert all(row.state == "sending" for row in claimed_rows)
    assert all(row.attempt_count == 1 for row in claimed_rows)
    assert all(row.updated_at == 10.0 for row in claimed_rows)
    assert {row.document_id for row in remaining} == (
        mismatch_ids | schema_mismatch_ids | delayed_ids
    )
    assert {row.state for row in remaining} == {"pending"}


def test_claim_increments_attempt_before_io_and_preserves_prior_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segment = _single_segment("attempt")
    outbox = SQLiteOutbox.open(config)
    try:
        assert outbox.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            """
            UPDATE outbox
            SET attempt_count=4, next_attempt_at=5.0,
                last_error_category='retain_timeout', updated_at=4.0
            """,
        )
        owner = _acquire(outbox)
        try:
            claim = outbox.claim_due(owner, now=5.0)
            persisted = outbox.read_unconfirmed()
        finally:
            owner.release()
    finally:
        outbox.close()

    assert claim.status is outbox_module.OutboxClaimStatus.CLAIMED
    assert claim.row is not None
    assert claim.row.document_id == segment.document_id
    assert claim.row.state == "sending"
    assert claim.row.attempt_count == 5
    assert claim.row.last_error_category == "retain_timeout"
    assert claim.row.updated_at == 5.0
    assert persisted == (claim.row,)


def test_guarded_completion_deletes_only_the_exact_sending_attempt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segment = _single_segment("complete")
    outbox = SQLiteOutbox.open(config)
    try:
        assert outbox.admit((segment,)).accepted
        _execute(config.outbox.path, "UPDATE outbox SET next_attempt_at=100.0")
        owner = _acquire(outbox)
        try:
            claim = outbox.claim_due(owner, now=100.0)
            assert claim.row is not None
            stale = outbox.complete_claim(
                owner,
                document_id=claim.row.document_id,
                attempt_count=claim.row.attempt_count + 1,
            )
            assert stale.status is outbox_module.OutboxTransitionStatus.LOCAL_FAILURE
            assert outbox.read_unconfirmed() == (claim.row,)

            completed = outbox.complete_claim(
                owner,
                document_id=claim.row.document_id,
                attempt_count=claim.row.attempt_count,
            )
            repeated = outbox.complete_claim(
                owner,
                document_id=claim.row.document_id,
                attempt_count=claim.row.attempt_count,
            )
        finally:
            owner.release()
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert completed.status is outbox_module.OutboxTransitionStatus.APPLIED
    assert completed.affected_count == 1
    assert repeated.status is outbox_module.OutboxTransitionStatus.LOCAL_FAILURE
    assert repeated.affected_count == 0
    assert rows == ()


def test_confirmed_completion_overwrites_payload_bytes_in_the_outbox_database(
    tmp_path: Path,
) -> None:
    marker = "better-hindsight-secure-delete-probe-91c90e7b"
    config = _config(tmp_path, segment_max_bytes=65_536)
    segments = build_retained_segments(
        session_id="secure-delete-probe",
        user_content=(marker + " synthetic payload ") * 120,
        assistant_content="synthetic acknowledgement",
        tags=("project:synthetic",),
        segment_max_bytes=65_536,
    )
    assert len(segments) == 1
    outbox = SQLiteOutbox.open(config)
    try:
        assert outbox.admit(segments).accepted
        marker_bytes = marker.encode("utf-8")
        assert marker_bytes in config.outbox.path.read_bytes()
        owner = _acquire(outbox)
        try:
            claim = outbox.claim_due(owner, now=time.time() + 1.0)
            assert claim.row is not None
            completed = outbox.complete_claim(
                owner,
                document_id=claim.row.document_id,
                attempt_count=claim.row.attempt_count,
            )
        finally:
            owner.release()
    finally:
        outbox.close()

    assert completed.status is outbox_module.OutboxTransitionStatus.APPLIED
    assert marker_bytes not in config.outbox.path.read_bytes()


def test_guarded_failure_reschedule_uses_fixed_category_and_due_deadline(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segment = _single_segment("retry")
    outbox = SQLiteOutbox.open(config)
    try:
        assert outbox.admit((segment,)).accepted
        _execute(config.outbox.path, "UPDATE outbox SET next_attempt_at=100.0")
        owner = _acquire(outbox)
        try:
            first_claim = outbox.claim_due(owner, now=100.0)
            assert first_claim.row is not None
            stale = outbox.reschedule_claim(
                owner,
                document_id=first_claim.row.document_id,
                attempt_count=first_claim.row.attempt_count + 1,
                category=outbox_module.OutboxFailureCategory.RETAIN_TIMEOUT,
                completed_at=200.0,
            )
            invalid = outbox.reschedule_claim(
                owner,
                document_id=first_claim.row.document_id,
                attempt_count=first_claim.row.attempt_count,
                category=cast(
                    outbox_module.OutboxFailureCategory,
                    "synthetic-payload-bearing-category",
                ),
                completed_at=200.0,
            )
            first_retry = outbox.reschedule_claim(
                owner,
                document_id=first_claim.row.document_id,
                attempt_count=first_claim.row.attempt_count,
                category=outbox_module.OutboxFailureCategory.RETAIN_TIMEOUT,
                completed_at=200.0,
            )
            deadline = outbox.next_matching_retry_deadline()
            early = outbox.claim_due(owner, now=201.999)
            second_claim = outbox.claim_due(owner, now=202.0)
            assert second_claim.row is not None
            second_retry = outbox.reschedule_claim(
                owner,
                document_id=second_claim.row.document_id,
                attempt_count=second_claim.row.attempt_count,
                category=outbox_module.OutboxFailureCategory.RETAIN_FAILED,
                completed_at=300.0,
            )
        finally:
            owner.release()
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert stale.status is outbox_module.OutboxTransitionStatus.LOCAL_FAILURE
    assert invalid.status is outbox_module.OutboxTransitionStatus.LOCAL_FAILURE
    assert first_retry.status is outbox_module.OutboxTransitionStatus.APPLIED
    assert deadline == 202.0
    assert early.status is outbox_module.OutboxClaimStatus.EMPTY
    assert second_claim.status is outbox_module.OutboxClaimStatus.CLAIMED
    assert second_claim.row.attempt_count == 2
    assert second_claim.row.last_error_category == "retain_timeout"
    assert second_retry.status is outbox_module.OutboxTransitionStatus.APPLIED
    assert len(rows) == 1
    assert rows[0].state == "pending"
    assert rows[0].attempt_count == 2
    assert rows[0].last_error_category == "retain_failed"
    assert rows[0].next_attempt_at == 304.0
    assert rows[0].updated_at == 300.0


@pytest.mark.parametrize(
    ("prior_attempt_count", "expected_attempt_count", "expected_delay"),
    [
        (0, 1, 2.0),
        (2, 3, 8.0),
        (3, 4, 10.0),
        (9_999, 10_000, 10.0),
    ],
    ids=("attempt-one", "immediately-before-cap", "at-cap", "attempt-ten-thousand"),
)
def test_retry_delay_is_cap_first_and_saturating(
    tmp_path: Path,
    prior_attempt_count: int,
    expected_attempt_count: int,
    expected_delay: float,
) -> None:
    config = _config(tmp_path)
    segment = _single_segment(f"delay-{prior_attempt_count}")
    outbox = SQLiteOutbox.open(config)
    try:
        assert outbox.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET attempt_count=?, next_attempt_at=1.0",
            (prior_attempt_count,),
        )
        owner = _acquire(outbox)
        try:
            claim = outbox.claim_due(owner, now=1.0)
            assert claim.row is not None
            result = outbox.reschedule_claim(
                owner,
                document_id=claim.row.document_id,
                attempt_count=claim.row.attempt_count,
                category=outbox_module.OutboxFailureCategory.RETAIN_UNCONFIRMED,
                completed_at=50.0,
            )
        finally:
            owner.release()
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert claim.row.attempt_count == expected_attempt_count
    assert result.status is outbox_module.OutboxTransitionStatus.APPLIED
    assert len(rows) == 1
    assert rows[0].attempt_count == expected_attempt_count
    assert rows[0].next_attempt_at == 50.0 + expected_delay


def test_next_deadline_filters_destination_schema_and_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segments = [_single_segment(f"deadline-{index}") for index in range(5)]
    outbox = SQLiteOutbox.open(config)
    try:
        for segment in segments:
            assert outbox.admit((segment,)).accepted
        updates = (
            (30.0, config.destination_fingerprint, config.outbox.payload_schema, "pending"),
            (20.0, config.destination_fingerprint, config.outbox.payload_schema, "pending"),
            (5.0, "0" * 64, config.outbox.payload_schema, "pending"),
            (4.0, config.destination_fingerprint, "other-schema", "pending"),
            (1.0, config.destination_fingerprint, config.outbox.payload_schema, "sending"),
        )
        for segment, (deadline, destination, schema, state) in zip(segments, updates, strict=True):
            _execute(
                config.outbox.path,
                """
                UPDATE outbox
                SET next_attempt_at=?, destination_fingerprint=?, payload_schema=?, state=?
                WHERE document_id=?
                """,
                (deadline, destination, schema, state, segment.document_id),
            )

        assert outbox.next_matching_retry_deadline() == 20.0
        _execute(
            config.outbox.path,
            """
            UPDATE outbox SET state='sending'
            WHERE destination_fingerprint=? AND payload_schema=?
            """,
            (config.destination_fingerprint, config.outbox.payload_schema),
        )
        assert outbox.next_matching_retry_deadline() is None
    finally:
        outbox.close()


def test_sender_data_methods_leave_schema_v1_inventory_unchanged(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outbox = SQLiteOutbox.open(config)
    before = _schema_contract(config.outbox.path)
    segment = _single_segment("schema")
    try:
        assert outbox.admit((segment,)).accepted
        _execute(config.outbox.path, "UPDATE outbox SET next_attempt_at=2.0")
        owner = _acquire(outbox)
        try:
            recovered = outbox.recover_sending(owner, now=1.0)
            claim = outbox.claim_due(owner, now=2.0)
            assert claim.row is not None
            retried = outbox.reschedule_claim(
                owner,
                document_id=claim.row.document_id,
                attempt_count=claim.row.attempt_count,
                category=outbox_module.OutboxFailureCategory.RETAIN_UNCONFIRMED,
                completed_at=3.0,
            )
        finally:
            owner.release()
    finally:
        outbox.close()
    after = _schema_contract(config.outbox.path)

    assert recovered.status is outbox_module.OutboxTransitionStatus.APPLIED
    assert retried.status is outbox_module.OutboxTransitionStatus.APPLIED
    assert before == after
    assert after[0] == 1
    assert [(row[0], row[1]) for row in after[1]] == [("table", "outbox")]


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_sender_eagerly_recovers_preexisting_sending_and_deletes_typed_success(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    segment = _single_segment("cold-start")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    runner = AsyncRunner()
    wall_time = _WallClock(100.0)

    async def confirm(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=wall_time,
    )
    try:
        assert delegate.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=90.0 WHERE document_id=?",
            (segment.document_id,),
        )
        owner = _acquire(delegate)
        claim = delegate.claim_due(owner, now=90.0)
        owner.release()
        assert claim.claimed
        assert delegate.read_unconfirmed()[0].state == "sending"

        sender.start()

        assert observed.completed.wait(timeout=2.0)
        assert client.wait_for_factory_calls(1)
        assert delegate.read_unconfirmed() == ()
    finally:
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()

    assert client.factory_segments == [_client_segment(segment)]
    assert observed.snapshot_operations()[:4] == (
        "acquire:acquired",
        "recover:applied",
        "claim:claimed",
        "complete:applied",
    )


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_delayed_restart_sends_the_captured_occurrence_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurred_at = "2026-08-31T04:00:00.123456+00:00"
    monkeypatch.setattr(retention_module, "_new_event_id", lambda: "9" * 32)
    monkeypatch.setattr(retention_module, "_capture_occurrence_time", lambda: occurred_at)
    config = _config(tmp_path)
    segment = build_retained_segments(
        session_id="session-delayed-restart",
        user_content="I paid the fee today.",
        assistant_content="The payment was recorded.",
        tags=("project:sample",),
        segment_max_bytes=config.retain.segment_max_bytes,
    )[0]

    admitting = SQLiteOutbox.open(config)
    try:
        assert admitting.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=900.0 WHERE document_id=?",
            (segment.document_id,),
        )
    finally:
        admitting.close()

    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    runner = AsyncRunner()

    async def confirm(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=_WallClock(1_000.0),
    )
    try:
        sender.start()
        assert observed.completed.wait(timeout=2.0)
        assert delegate.read_unconfirmed() == ()
    finally:
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()

    assert len(client.factory_segments) == 1
    assert client.factory_segments[0].timestamp == occurred_at
    assert json.loads(client.factory_segments[0].content)["occurred_at"] == occurred_at


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_timestamped_v2_rows_do_not_match_the_legacy_sender_identity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segment = _single_segment("timestamp-schema")
    outbox = SQLiteOutbox.open(config)
    try:
        assert outbox.admit((segment,)).accepted
        (row,) = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert row.payload_schema == "better-hindsight-turn-v2"
    assert row.destination_fingerprint == config.destination_fingerprint
    assert row.payload_schema != "better-hindsight-turn-v1"
    assert row.destination_fingerprint != config.legacy_destination_fingerprint


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_legacy_pending_v1_row_delivers_with_null_timestamp(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segment = _legacy_segment()
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    runner = AsyncRunner()

    async def confirm(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=_WallClock(100.0),
    )
    try:
        _insert_legacy_segment(config, segment)
        sender.start()
        assert observed.completed.wait(timeout=2.0)
        assert delegate.read_unconfirmed() == ()
    finally:
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()

    assert client.factory_segments == [_client_segment(segment)]
    assert client.factory_segments[0].timestamp is None


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_sender_preserves_remote_metadata_needed_to_reconstruct_segmented_source(
    tmp_path: Path,
) -> None:
    segment_max_bytes = 460
    config = _config(tmp_path, segment_max_bytes=segment_max_bytes)
    segments = build_retained_segments(
        session_id="session-remote-reconstruction",
        user_content=("Mira visited Tokyo. She filed the report. " * 2)
        + "\n\n"
        + ("The second paragraph stays independently useful. " * 2),
        assistant_content="The report was recorded for Mira. " * 2,
        tags=("project:sample",),
        segment_max_bytes=segment_max_bytes,
    )
    assert len(segments) > 1
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    runner = AsyncRunner()

    async def confirm(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=_WallClock(100.0),
    )
    try:
        assert delegate.admit(segments).accepted
        _execute(config.outbox.path, "UPDATE outbox SET next_attempt_at=90.0")
        sender.start()
        assert client.wait_for_factory_calls(len(segments), timeout=2.0)
        deadline = time.monotonic() + 2.0
        while delegate.read_unconfirmed() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert delegate.read_unconfirmed() == ()
    finally:
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()

    remote_documents = list(reversed(client.factory_segments))
    assert {segment.source_sha256 for segment in remote_documents} == {segments[0].source_sha256}
    ordered = sorted(remote_documents, key=lambda segment: segment.segment_index)
    assert [segment.segment_index for segment in ordered] == list(range(len(segments)))
    assert {segment.segment_count for segment in ordered} == {len(segments)}
    assert {segment.payload_schema for segment in ordered} == {"better-hindsight-turn-v2"}
    reconstructed = "".join(segment.content for segment in ordered)
    assert hashlib.sha256(reconstructed.encode("utf-8")).hexdigest() == segments[0].source_sha256


@pytest.mark.parametrize(
    ("failure_kind", "expected_category"),
    [
        ("timeout", outbox_module.OutboxFailureCategory.RETAIN_TIMEOUT),
        ("client-error", outbox_module.OutboxFailureCategory.RETAIN_FAILED),
        ("unconfirmed", outbox_module.OutboxFailureCategory.RETAIN_UNCONFIRMED),
    ],
)
@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_sender_maps_remote_failures_to_fixed_retry_categories(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
    expected_category: outbox_module.OutboxFailureCategory,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path)
    segment = _single_segment(f"failure-{failure_kind}")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    runner = AsyncRunner()
    wall_time = _WallClock(200.0)

    async def fail(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        if failure_kind == "timeout":
            await asyncio.Future()
            raise AssertionError("synthetic timeout operation unexpectedly resumed")
        if failure_kind == "client-error":
            raise HindsightClientError("retain_failed", "Better Hindsight retain failed.")
        return RetainConfirmation(confirmed=False)

    client = _ScriptedRetainClient(fail)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=wall_time,
    )
    try:
        assert delegate.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=200.0 WHERE document_id=?",
            (segment.document_id,),
        )
        sender.start()

        assert observed.rescheduled.wait(timeout=2.0)
        rows = delegate.read_unconfirmed()
    finally:
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()

    assert len(rows) == 1
    assert rows[0].state == "pending"
    assert rows[0].attempt_count == 1
    assert rows[0].last_error_category == expected_category.value
    assert rows[0].next_attempt_at == 202.0
    assert rows[0].content == segment.content
    assert rows[0].document_id == segment.document_id
    assert client.factory_segments == [_client_segment(segment)]
    assert f"reschedule:{expected_category.value}:applied" in observed.snapshot_operations()
    events = [
        json.loads(record.message) for record in caplog.records if record.message.startswith("{")
    ]
    expected_reason = {
        "timeout": "timeout",
        "client-error": "retain_failed",
        "unconfirmed": "unconfirmed",
    }[failure_kind]
    assert events[-1] == {
        "attempt_count": 1,
        "elapsed_ms": events[-1]["elapsed_ms"],
        "event": "better_hindsight.sender_attempt",
        "outcome": expected_category.value,
        "reason": expected_reason,
        "retry_delay_ms": 2_000,
    }
    assert segment.content not in caplog.text
    assert segment.document_id not in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_late_valid_retain_stays_timeout_holds_lock_and_replays_stable_segment(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    segment = _single_segment("late-valid")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    runner = AsyncRunner()
    wall_time = _WallClock(300.0)
    cancellation_seen = threading.Event()
    release_late_result = threading.Event()

    async def late_then_confirm(
        _segment: ClientRetainSegment,
        attempt: int,
    ) -> RetainConfirmation:
        if attempt == 0:
            try:
                await asyncio.Future()
                raise AssertionError("synthetic resistant retain unexpectedly resumed")
            except asyncio.CancelledError:
                cancellation_seen.set()
                await asyncio.to_thread(release_late_result.wait)
                return RetainConfirmation(confirmed=True)
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(late_then_confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=wall_time,
    )
    try:
        assert delegate.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=300.0 WHERE document_id=?",
            (segment.document_id,),
        )
        sender.start()

        assert cancellation_seen.wait(timeout=2.0)
        assert observed.rescheduled.is_set() is False
        assert observed.completed.is_set() is False
        live_rows = delegate.read_unconfirmed()
        assert len(live_rows) == 1
        assert live_rows[0].state == "sending"
        assert _spawn_can_acquire_profile_lock(observed.profile_lock_path) is False
        assert len(client.factory_segments) == 1
        assert client.max_active_calls == 1

        release_late_result.set()
        assert observed.rescheduled.wait(timeout=2.0)
        retry_rows = delegate.read_unconfirmed()
        assert len(retry_rows) == 1
        assert retry_rows[0].state == "pending"
        assert retry_rows[0].last_error_category == "retain_timeout"
        assert retry_rows[0].next_attempt_at == 302.0
        assert observed.completed.is_set() is False

        wall_time.set(302.0)
        sender.wake()
        assert observed.completed.wait(timeout=2.0)
        assert client.wait_for_factory_calls(2)
        assert delegate.read_unconfirmed() == ()
    finally:
        release_late_result.set()
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.wait_for_settlement(timeout=1.0)
        runner.shutdown()
        delegate.close()

    expected = _client_segment(segment)
    assert client.factory_segments == [expected, expected]
    assert client.max_active_calls == 1


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_claim_submission_race_keeps_lock_and_reschedules_without_invoking_retain_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    segment = _single_segment("claim-race")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    runner = AsyncRunner()
    wall_time = _WallClock(400.0)
    claim_paused = threading.Event()
    allow_submission = threading.Event()
    sender_submission_finished = threading.Event()
    prior_started = threading.Event()
    prior_cancelled = threading.Event()
    release_prior = threading.Event()
    prior_errors: list[BaseException] = []

    async def unexpected_retain(
        _segment: ClientRetainSegment,
        _attempt: int,
    ) -> RetainConfirmation:
        raise AssertionError(
            "retain operation factory must not be invoked while runner is unsettled"
        )

    client = _ScriptedRetainClient(unexpected_retain)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=wall_time,
    )

    real_deliver_claim = sender._deliver_claim

    def pause_before_submit(
        owner: outbox_module.ProfileLockOwner,
        row: outbox_module.OutboxRow,
    ) -> outbox_module.OutboxTransitionResult:
        claim_paused.set()
        assert allow_submission.wait(timeout=3.0)
        return real_deliver_claim(owner, row)

    monkeypatch.setattr(sender, "_deliver_claim", pause_before_submit)
    real_run = runner.run

    def observe_sender_run(
        operation: Callable[[], Awaitable[object]],
        *,
        timeout: float | None = None,
    ) -> object:
        is_sender = threading.current_thread().name == "better-hindsight-outbox-sender"
        try:
            return real_run(operation, timeout=timeout)
        finally:
            if is_sender:
                sender_submission_finished.set()

    monkeypatch.setattr(runner, "run", observe_sender_run)

    async def resistant_prior_operation() -> None:
        prior_started.set()
        try:
            await asyncio.Future()
            raise AssertionError("synthetic prior operation unexpectedly resumed")
        except asyncio.CancelledError:
            prior_cancelled.set()
            await asyncio.to_thread(release_prior.wait)

    def run_prior_operation() -> None:
        try:
            runner.run(resistant_prior_operation, timeout=0.01)
        except BaseException as error:
            prior_errors.append(error)

    prior_thread = threading.Thread(target=run_prior_operation)
    try:
        assert delegate.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=400.0 WHERE document_id=?",
            (segment.document_id,),
        )
        sender.start()
        assert claim_paused.wait(timeout=2.0)

        prior_thread.start()
        assert prior_started.wait(timeout=1.0)
        prior_thread.join(timeout=1.0)
        assert prior_thread.is_alive() is False
        assert len(prior_errors) == 1
        assert isinstance(prior_errors[0], AsyncCallTimeoutError)
        assert prior_cancelled.is_set()
        assert runner.wait_for_settlement(timeout=0.0) is False

        allow_submission.set()
        assert sender_submission_finished.wait(timeout=1.0)
        assert client.factory_segments == []
        assert observed.rescheduled.is_set() is False
        claimed_rows = delegate.read_unconfirmed()
        assert len(claimed_rows) == 1
        assert claimed_rows[0].state == "sending"
        assert _spawn_can_acquire_profile_lock(observed.profile_lock_path) is False

        release_prior.set()
        assert observed.rescheduled.wait(timeout=2.0)
        rows = delegate.read_unconfirmed()
    finally:
        allow_submission.set()
        release_prior.set()
        prior_thread.join(timeout=1.0)
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.wait_for_settlement(timeout=1.0)
        runner.shutdown()
        delegate.close()

    assert client.factory_segments == []
    assert len(rows) == 1
    assert rows[0].state == "pending"
    assert rows[0].attempt_count == 1
    assert rows[0].last_error_category == "retain_failed"
    assert rows[0].next_attempt_at == 402.0
    operations = observed.snapshot_operations()
    assert operations.count("recover:applied") == 1
    assert operations.count("claim:claimed") == 1
    assert "reschedule:retain_failed:applied" in operations


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_non_owner_is_passive_then_takes_over_and_recovers_after_owner_exit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    segment = _single_segment("takeover")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    runner = AsyncRunner()
    wall_time = _WallClock(500.0)

    async def confirm(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=wall_time,
    )
    context = multiprocessing.get_context("spawn")
    owner_ready = context.Event()
    release_owner = context.Event()
    holder = context.Process(
        target=_hold_profile_lock,
        args=(str(observed.profile_lock_path), owner_ready, release_owner),
    )
    try:
        assert delegate.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=490.0 WHERE document_id=?",
            (segment.document_id,),
        )
        owner = _acquire(delegate)
        claim = delegate.claim_due(owner, now=490.0)
        owner.release()
        assert claim.claimed

        holder.start()
        assert owner_ready.wait(timeout=3.0)
        sender.start()
        assert observed.contended_seen.wait(timeout=2.0)
        assert observed.recovered.is_set() is False
        assert observed.claimed.is_set() is False
        assert client.factory_segments == []
        assert delegate.read_unconfirmed()[0].state == "sending"

        release_owner.set()
        holder.join(timeout=5.0)
        assert holder.exitcode == 0
        sender.wake()
        assert observed.completed.wait(timeout=2.0)
        assert delegate.read_unconfirmed() == ()
    finally:
        release_owner.set()
        holder.join(timeout=5.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()

    operations = observed.snapshot_operations()
    assert operations[0] == "acquire:contended"
    assert "acquire:acquired" in operations
    assert "recover:applied" in operations
    assert client.factory_segments == [_client_segment(segment)]


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_local_completion_failure_releases_lock_then_recovers_and_replays(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    segment = _single_segment("local-transition")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    observed.fail_next_completion = True
    runner = AsyncRunner()
    wall_time = _WallClock(600.0)

    async def confirm(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=wall_time,
    )
    try:
        assert delegate.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=600.0 WHERE document_id=?",
            (segment.document_id,),
        )
        sender.start()

        assert observed.completion_failed.wait(timeout=2.0)
        failed_rows = delegate.read_unconfirmed()
        assert len(failed_rows) == 1
        assert failed_rows[0].state == "sending"
        assert _spawn_can_acquire_profile_lock(observed.profile_lock_path) is True

        observed.allow_reacquire.set()
        sender.wake()
        assert observed.completed.wait(timeout=2.0)
        assert client.wait_for_factory_calls(2)
        assert delegate.read_unconfirmed() == ()
    finally:
        observed.allow_reacquire.set()
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()

    expected = _client_segment(segment)
    assert client.factory_segments == [expected, expected]
    operations = observed.snapshot_operations()
    assert operations.count("acquire:acquired") == 2
    assert operations.count("recover:applied") == 2
    assert "complete:local_failure" in operations
    assert "complete:applied" in operations
    assert operations.index("complete:local_failure") < operations.index("complete:applied")


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_idle_owner_polls_cross_connection_admission_without_process_local_wake(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, poll_interval_seconds=0.1)
    segment = _single_segment("cross-connection")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    admitting_outbox = SQLiteOutbox.open(config)
    runner = AsyncRunner()
    wall_time = _WallClock(700.0)

    async def confirm(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=wall_time,
    )
    try:
        sender.start()
        assert observed.recovered.wait(timeout=2.0)
        assert observed.queried.wait(timeout=2.0)
        assert client.factory_segments == []

        assert admitting_outbox.admit((segment,)).accepted
        _execute(
            config.outbox.path,
            "UPDATE outbox SET next_attempt_at=700.0 WHERE document_id=?",
            (segment.document_id,),
        )

        # Deliberately do not call sender.wake(): bounded polling is the cross-process signal.
        assert observed.completed.wait(timeout=2.0)
        assert delegate.read_unconfirmed() == ()
    finally:
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        admitting_outbox.close()
        delegate.close()

    assert client.factory_segments == [_client_segment(segment)]


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_production_runtime_polls_spawned_non_owner_admission_without_local_wake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poll_interval = 0.2
    config = _config(tmp_path, poll_interval_seconds=poll_interval)
    segment = _single_segment("spawned-cross-process")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)

    async def confirm(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    client = _ScriptedRetainClient(confirm)
    senders: list[runtime_module.OutboxSender] = []
    local_wakes: list[str] = []

    def client_factory(_config: BetterHindsightConfig) -> HindsightClientProtocol:
        return cast(HindsightClientProtocol, client)

    def outbox_factory(_config: BetterHindsightConfig) -> runtime_module.OutboxProtocol:
        return cast(runtime_module.OutboxProtocol, observed)

    def sender_factory(
        sender_config: BetterHindsightConfig,
        sender_outbox: runtime_module.OutboxProtocol,
        sender_client: HindsightClientProtocol,
        runner: AsyncRunner,
    ) -> runtime_module.SenderProtocol:
        sender = runtime_module.OutboxSender(
            config=sender_config,
            outbox=sender_outbox,
            client=sender_client,
            runner=runner,
        )
        original_wake = sender.wake

        def record_local_wake() -> None:
            local_wakes.append(threading.current_thread().name)
            original_wake()

        monkeypatch.setattr(sender, "wake", record_local_wake)
        senders.append(sender)
        return sender

    runtime_module.reset_process_runtime_for_tests()
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    admitting_process = context.Process(
        target=_admit_from_non_owner_process,
        args=(config, segment, writer),
    )
    handle: runtime_module.ProcessRuntimeHandle | None = None
    process_started = False
    try:
        handle = runtime_module.acquire_process_runtime(
            config,
            client_factory=client_factory,
            outbox_factory=outbox_factory,
            sender_factory=sender_factory,
        )
        assert len(senders) == 1
        assert observed.recovered.wait(timeout=2.0)
        assert observed.queried.wait(timeout=2.0)
        assert delegate.read_unconfirmed() == ()
        assert senders[0]._wake.is_set() is False
        assert local_wakes == []

        admitting_process.start()
        process_started = True
        writer.close()
        assert reader.poll(5.0)
        child_result = reader.recv()
        assert child_result[:3] == ("ok", "admitted", "contended")
        assert type(child_result[3]) is int
        assert child_result[3] != os.getpid()
        assert type(child_result[4]) is float

        delivery_bound = poll_interval + 1.0  # One poll plus process-scheduling allowance.
        assert observed.completed.wait(timeout=delivery_bound)
        assert time.monotonic() - child_result[4] <= delivery_bound
        assert client.wait_for_factory_calls(1, timeout=delivery_bound)
        assert delegate.read_unconfirmed() == ()
        assert client.factory_segments == [_client_segment(segment)]
        # Process B has no reference to A's process-local event; bounded polling did the work.
        assert local_wakes == []

        admitting_process.join(timeout=5.0)
        assert admitting_process.is_alive() is False
        assert admitting_process.exitcode == 0
    finally:
        writer.close()
        reader.close()
        if process_started:
            admitting_process.join(timeout=5.0)
            if admitting_process.is_alive():
                admitting_process.terminate()
                admitting_process.join(timeout=5.0)
        if handle is not None:
            handle.close()
            assert runtime_module.finalize_process_runtime() is True
        else:
            delegate.close()

    assert admitting_process.is_alive() is False
    assert client.close_calls == 1
    assert len(local_wakes) == 1  # The only public wake was deterministic finalization.
    assert runtime_module.finalize_process_runtime() is False


def test_duplicate_wakes_are_cleared_before_query_then_wait_uses_poll_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, poll_interval_seconds=0.25)
    order: list[str] = []
    wait_entered = threading.Event()
    release_wait = threading.Event()

    class _Owner:
        def release(self) -> None:
            order.append("release")

    owner = _Owner()

    class _EmptyOutbox:
        def try_acquire_profile_lock(self) -> outbox_module.ProfileLockAcquisitionResult:
            order.append("acquire")
            return outbox_module.ProfileLockAcquisitionResult(
                outbox_module.ProfileLockStatus.ACQUIRED,
                cast(outbox_module.ProfileLockOwner, owner),
            )

        def recover_sending(
            self,
            _owner: outbox_module.ProfileLockOwner,
            *,
            now: float,
        ) -> outbox_module.OutboxTransitionResult:
            assert now == 800.0
            order.append("recover")
            return outbox_module.OutboxTransitionResult(
                outbox_module.OutboxTransitionStatus.APPLIED
            )

        def claim_due(
            self,
            _owner: outbox_module.ProfileLockOwner,
            *,
            now: float,
        ) -> outbox_module.OutboxClaimResult:
            assert now == 800.0
            order.append("query")
            return outbox_module.OutboxClaimResult(outbox_module.OutboxClaimStatus.EMPTY)

        def next_matching_retry_deadline(self) -> float | None:
            order.append("deadline")
            return None

    class _WakeEvent:
        def set(self) -> None:
            order.append("set")
            release_wait.set()

        def clear(self) -> None:
            order.append("clear")
            release_wait.clear()

        def wait(self, timeout: float | None = None) -> bool:
            order.append(f"wait:{timeout}")
            wait_entered.set()
            return release_wait.wait(timeout=2.0)

    async def unexpected(_segment: ClientRetainSegment, _attempt: int) -> RetainConfirmation:
        raise AssertionError("empty sender must not call the retain client")

    runner = AsyncRunner()
    client = _ScriptedRetainClient(unexpected)
    sender = _make_sender(
        config=config,
        outbox=_EmptyOutbox(),
        client=client,
        runner=runner,
        wall_time=_WallClock(800.0),
    )
    monkeypatch.setattr(sender, "_wake", _WakeEvent())
    try:
        sender.wake()
        sender.wake()
        sender.start()
        assert wait_entered.wait(timeout=2.0)
        assert order[:2] == ["set", "set"]
        clear_index = order.index("clear")
        assert order[clear_index : clear_index + 4] == [
            "clear",
            "query",
            "deadline",
            "wait:0.25",
        ]
        assert client.factory_segments == []
    finally:
        sender.request_stop()
        release_wait.set()
        assert sender.join(timeout=2.0)
        runner.shutdown()

    assert order[-1] == "release"


def test_stop_signal_does_not_block_behind_an_active_claim(tmp_path: Path) -> None:
    config = _config(tmp_path)
    claim_entered = threading.Event()
    release_claim = threading.Event()
    stop_returned = threading.Event()

    class _Owner:
        def release(self) -> None:
            return None

    owner = cast(outbox_module.ProfileLockOwner, _Owner())

    class _BlockingClaimOutbox:
        def try_acquire_profile_lock(self) -> outbox_module.ProfileLockAcquisitionResult:
            return outbox_module.ProfileLockAcquisitionResult(
                outbox_module.ProfileLockStatus.ACQUIRED,
                owner,
            )

        def recover_sending(
            self,
            _owner: outbox_module.ProfileLockOwner,
            *,
            now: float,
        ) -> outbox_module.OutboxTransitionResult:
            del now
            return outbox_module.OutboxTransitionResult(
                outbox_module.OutboxTransitionStatus.APPLIED
            )

        def claim_due(
            self,
            _owner: outbox_module.ProfileLockOwner,
            *,
            now: float,
        ) -> outbox_module.OutboxClaimResult:
            del now
            claim_entered.set()
            assert release_claim.wait(timeout=3.0)
            return outbox_module.OutboxClaimResult(outbox_module.OutboxClaimStatus.EMPTY)

        def next_matching_retry_deadline(self) -> float | None:
            return None

    async def confirm(
        _segment: ClientRetainSegment,
        _attempt: int,
    ) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    runner = AsyncRunner()
    sender = _make_sender(
        config=config,
        outbox=cast(runtime_module.OutboxProtocol, _BlockingClaimOutbox()),
        client=_ScriptedRetainClient(confirm),
        runner=runner,
        wall_time=_WallClock(900.0),
    )

    def request_stop() -> None:
        sender.request_stop()
        stop_returned.set()

    stopping_thread = threading.Thread(target=request_stop)
    try:
        sender.start()
        assert claim_entered.wait(timeout=2.0)
        stopping_thread.start()
        assert stop_returned.wait(timeout=1.0)
        assert release_claim.is_set() is False
    finally:
        release_claim.set()
        stopping_thread.join(timeout=2.0)
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()


def test_failed_thread_start_does_not_mark_sender_started(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class _StartFailsOnce:
        def __init__(self) -> None:
            self.start_calls = 0

        def start(self) -> None:
            self.start_calls += 1
            if self.start_calls == 1:
                raise RuntimeError("synthetic thread start failure")

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return False

    async def confirm(
        _segment: ClientRetainSegment,
        _attempt: int,
    ) -> RetainConfirmation:
        return RetainConfirmation(confirmed=True)

    runner = AsyncRunner()
    sender = _make_sender(
        config=config,
        outbox=cast(runtime_module.OutboxProtocol, object()),
        client=_ScriptedRetainClient(confirm),
        runner=runner,
        wall_time=_WallClock(901.0),
    )
    fake_thread = _StartFailsOnce()
    sender._thread = cast(threading.Thread, fake_thread)
    try:
        with pytest.raises(RuntimeError, match="synthetic thread start failure"):
            sender.start()
        sender.start()
    finally:
        runner.shutdown()

    assert fake_thread.start_calls == 2


def test_sender_performs_remote_work_after_claim_transaction_commits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segment = _single_segment("network-outside-transaction")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    assert delegate.admit((segment,)).accepted
    _execute(
        config.outbox.path,
        "UPDATE outbox SET next_attempt_at=100.0 WHERE document_id=?",
        (segment.document_id,),
    )
    concurrent_write_committed = threading.Event()

    async def confirm(
        retained: ClientRetainSegment,
        _attempt: int,
    ) -> RetainConfirmation:
        connection = sqlite3.connect(config.outbox.path, timeout=0.0)
        try:
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute(
                "UPDATE outbox SET updated_at=updated_at WHERE document_id=?",
                (retained.document_id,),
            )
            connection.commit()
            concurrent_write_committed.set()
        finally:
            connection.close()
        return RetainConfirmation(confirmed=True)

    runner = AsyncRunner()
    client = _ScriptedRetainClient(confirm)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=_WallClock(100.0),
    )
    try:
        sender.start()
        assert observed.completed.wait(timeout=2.0)
        assert concurrent_write_committed.is_set()
        assert delegate.read_unconfirmed() == ()
    finally:
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()


def test_delayed_failed_row_does_not_block_other_ready_row(tmp_path: Path) -> None:
    config = _config(tmp_path, retry_initial_seconds=2.0, retry_max_seconds=10.0)
    first = _single_segment("first-fails")
    second = _single_segment("second-ready")
    delegate = SQLiteOutbox.open(config)
    observed = _ObservedOutbox(delegate)
    assert delegate.admit((first,)).accepted
    assert delegate.admit((second,)).accepted
    _execute(
        config.outbox.path,
        """
        UPDATE outbox
        SET created_at=CASE document_id WHEN ? THEN 1.0 ELSE 2.0 END,
            next_attempt_at=100.0
        WHERE document_id IN (?, ?)
        """,
        (first.document_id, first.document_id, second.document_id),
    )

    async def classify(
        retained: ClientRetainSegment,
        _attempt: int,
    ) -> RetainConfirmation:
        if retained.document_id == first.document_id:
            return RetainConfirmation(confirmed=False)
        return RetainConfirmation(confirmed=True)

    runner = AsyncRunner()
    client = _ScriptedRetainClient(classify)
    sender = _make_sender(
        config=config,
        outbox=observed,
        client=client,
        runner=runner,
        wall_time=_WallClock(100.0),
    )
    try:
        sender.start()
        assert observed.rescheduled.wait(timeout=2.0)
        assert observed.completed.wait(timeout=2.0)
        rows = delegate.read_unconfirmed()
    finally:
        sender.request_stop()
        assert sender.join(timeout=2.0)
        runner.shutdown()
        delegate.close()

    assert client.factory_segments == [
        _client_segment(first),
        _client_segment(second),
    ]
    assert len(rows) == 1
    assert rows[0].document_id == first.document_id
    assert rows[0].state == "pending"
    assert rows[0].attempt_count == 1
    assert rows[0].last_error_category == "retain_unconfirmed"
    assert rows[0].next_attempt_at == 102.0


def test_stop_wake_cannot_be_cleared_after_ownership_failure(tmp_path: Path) -> None:
    config = _config(tmp_path, poll_interval_seconds=60.0)
    clear_entered = threading.Event()
    release_clear = threading.Event()
    clear_finished = threading.Event()
    owner_released = threading.Event()

    class _Owner:
        def release(self) -> None:
            owner_released.set()

    owner = cast(outbox_module.ProfileLockOwner, _Owner())

    class _RecoveryFails:
        def try_acquire_profile_lock(self) -> outbox_module.ProfileLockAcquisitionResult:
            return outbox_module.ProfileLockAcquisitionResult(
                outbox_module.ProfileLockStatus.ACQUIRED,
                owner,
            )

        def recover_sending(
            self,
            _owner: outbox_module.ProfileLockOwner,
            *,
            now: float,
        ) -> outbox_module.OutboxTransitionResult:
            del now
            return outbox_module.OutboxTransitionResult(
                outbox_module.OutboxTransitionStatus.LOCAL_FAILURE
            )

    class _BlockingClearWake:
        def __init__(self) -> None:
            self._event = threading.Event()
            self.wait_timeouts: list[float | None] = []

        def set(self) -> None:
            self._event.set()

        def clear(self) -> None:
            clear_entered.set()
            assert release_clear.wait(timeout=2.0)
            self._event.clear()
            clear_finished.set()

        def wait(self, timeout: float | None = None) -> bool:
            self.wait_timeouts.append(timeout)
            return self._event.wait(timeout=timeout)

        def is_set(self) -> bool:
            return self._event.is_set()

    async def unexpected(
        _segment: ClientRetainSegment,
        _attempt: int,
    ) -> RetainConfirmation:
        raise AssertionError("ownership recovery failure must not call the client")

    runner = AsyncRunner()
    sender = _make_sender(
        config=config,
        outbox=cast(runtime_module.OutboxProtocol, _RecoveryFails()),
        client=_ScriptedRetainClient(unexpected),
        runner=runner,
        wall_time=_WallClock(902.0),
    )
    wake = _BlockingClearWake()
    sender._wake = cast(threading.Event, wake)
    try:
        sender.start()
        assert clear_entered.wait(timeout=2.0)
        sender.request_stop()
        release_clear.set()
        assert clear_finished.wait(timeout=2.0)
        assert sender.join(timeout=0.5)
        assert wake.wait_timeouts == []
        assert owner_released.is_set()
    finally:
        release_clear.set()
        assert clear_finished.wait(timeout=2.0)
        sender.request_stop()
        sender.wake()
        assert sender.join(timeout=2.0)
        runner.shutdown()
