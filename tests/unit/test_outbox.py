"""Contract tests for the bounded, atomic profile-local SQLite outbox."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import better_hermes_hindsight.outbox as outbox_module
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.outbox import (
    OUTBOX_OPEN_FAILED_MESSAGE,
    OUTBOX_SCHEMA_UNSUPPORTED_MESSAGE,
    AdmissionStatus,
    OutboxOpenError,
    SQLiteOutbox,
)
from better_hermes_hindsight.retention import RetainedSegment, build_retained_segments


def _config(
    home: Path,
    *,
    path: str = "better_hindsight/outbox.sqlite3",
    segment_max_bytes: int = 64,
    max_pending_rows: int = 2_000,
    max_pending_bytes: int = 1_000_000,
    busy_timeout_seconds: float = 0.2,
) -> BetterHindsightConfig:
    home.mkdir(parents=True, exist_ok=True)
    return load_config(
        hermes_home=home,
        environ={},
        injected={
            "api_url": "https://service.example.test",
            "bank_id": "synthetic-bank",
            "retain": {"segment_max_bytes": segment_max_bytes},
            "outbox": {
                "path": path,
                "max_pending_rows": max_pending_rows,
                "max_pending_bytes": max_pending_bytes,
                "busy_timeout_seconds": busy_timeout_seconds,
            },
        },
    )


def _turn(seed: str, *, segment_max_bytes: int = 64) -> tuple[RetainedSegment, ...]:
    return build_retained_segments(
        session_id=f"session-{seed}",
        user_content=f"user-{seed}-" + seed * 40,
        assistant_content=f"assistant-{seed}-" + seed * 40,
        tags=("project:sample",),
        segment_max_bytes=segment_max_bytes,
    )


def _logical_bytes(segments: tuple[RetainedSegment, ...]) -> int:
    return sum(len(segment.content.encode("utf-8")) + 1024 for segment in segments)


def _execute(path: Path, statement: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def _scalar(path: Path, statement: str) -> object:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(statement).fetchone()
        assert row is not None
        return row[0]
    finally:
        connection.close()


def _insert_pending_segment(config: BetterHindsightConfig, segment: RetainedSegment) -> None:
    _execute(
        config.outbox.path,
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 1.0, NULL, 1.0, 1.0)
        """,
        (
            segment.document_id,
            segment.payload_hash,
            segment.payload_schema,
            segment.source_sha256,
            segment.segment_index,
            segment.segment_count,
            segment.content,
            config.destination_fingerprint,
        ),
    )


def test_empty_version_zero_database_creates_private_schema_v1_and_reopens(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.outbox.path.parent.mkdir(parents=True)
    sqlite3.connect(config.outbox.path).close()

    first = SQLiteOutbox.open(config)
    first.close()
    second = SQLiteOutbox.open(config)
    second.close()

    assert _scalar(config.outbox.path, "PRAGMA user_version") == 1
    columns_connection = sqlite3.connect(config.outbox.path)
    try:
        columns = {
            row[1] for row in columns_connection.execute("PRAGMA table_info(outbox)").fetchall()
        }
    finally:
        columns_connection.close()
    assert columns == {
        "document_id",
        "payload_hash",
        "payload_schema",
        "source_sha256",
        "segment_index",
        "segment_count",
        "content",
        "destination_fingerprint",
        "state",
        "attempt_count",
        "next_attempt_at",
        "last_error_category",
        "created_at",
        "updated_at",
    }


def test_unknown_nonzero_schema_version_is_rejected_without_migration(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.outbox.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(config.outbox.path)
    try:
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert str(caught.value) == OUTBOX_SCHEMA_UNSUPPORTED_MESSAGE
    assert caught.value.__cause__ is None
    assert _scalar(config.outbox.path, "PRAGMA user_version") == 7


@pytest.mark.parametrize("version", [0, 1], ids=["version-zero", "version-one"])
def test_lookalike_schema_is_rejected_without_version_mutation(
    tmp_path: Path, version: int
) -> None:
    config = _config(tmp_path)
    config.outbox.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(config.outbox.path)
    try:
        connection.execute(
            """
            CREATE TABLE outbox (
                document_id INTEGER PRIMARY KEY,
                payload_hash BLOB,
                payload_schema INTEGER,
                source_sha256 BLOB,
                segment_index TEXT,
                segment_count TEXT,
                content BLOB,
                destination_fingerprint BLOB,
                state INTEGER,
                attempt_count TEXT,
                next_attempt_at TEXT,
                last_error_category INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert caught.value.__cause__ is None
    assert _scalar(config.outbox.path, "PRAGMA user_version") == version


def test_schema_with_unexpected_trigger_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    initialized = SQLiteOutbox.open(config)
    initialized.close()
    _execute(
        config.outbox.path,
        """
        CREATE TRIGGER mutate_outbox_after_insert
        AFTER INSERT ON outbox
        BEGIN
            UPDATE outbox SET content = 'mutated' WHERE document_id = NEW.document_id;
        END
        """,
    )

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert caught.value.__cause__ is None


def test_schema_with_incompatible_check_literals_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.outbox.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(config.outbox.path)
    try:
        connection.execute(
            """
            CREATE TABLE outbox (
                document_id TEXT PRIMARY KEY NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_schema TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                segment_index INTEGER NOT NULL CHECK (segment_index >= 0),
                segment_count INTEGER NOT NULL CHECK (segment_count > 0),
                content TEXT NOT NULL,
                destination_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('PENDING', 'SENDING')),
                attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
                next_attempt_at REAL NOT NULL,
                last_error_category TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert caught.value.__cause__ is None


def test_version_zero_foreign_database_is_rejected_without_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.outbox.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(config.outbox.path)
    try:
        connection.execute("CREATE TABLE foreign_state (marker TEXT NOT NULL)")
        connection.execute("INSERT INTO foreign_state VALUES ('preserve-me')")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert _scalar(config.outbox.path, "PRAGMA user_version") == 0
    assert _scalar(config.outbox.path, "SELECT marker FROM foreign_state") == "preserve-me"
    assert (
        _scalar(
            config.outbox.path,
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='outbox'",
        )
        == 0
    )


def test_version_one_database_with_foreign_object_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    initialized = SQLiteOutbox.open(config)
    initialized.close()
    _execute(config.outbox.path, "CREATE TABLE foreign_state (marker TEXT NOT NULL)")
    _execute(config.outbox.path, "INSERT INTO foreign_state VALUES ('preserve-me')")

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert _scalar(config.outbox.path, "PRAGMA user_version") == 1
    assert _scalar(config.outbox.path, "SELECT marker FROM foreign_state") == "preserve-me"


def test_non_utf8_canonical_schema_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.outbox.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(config.outbox.path)
    try:
        connection.execute("PRAGMA encoding = 'UTF-16le'")
        connection.execute(outbox_module._SCHEMA_SQL)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        encoding = connection.execute("PRAGMA encoding").fetchone()
    finally:
        connection.close()
    assert encoding is not None
    assert str(encoding[0]).casefold() == "utf-16le"

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert _scalar(config.outbox.path, "PRAGMA user_version") == 1


@pytest.mark.skipif(os.name != "posix", reason="file mode checks are POSIX-only")
def test_rejected_foreign_file_keeps_original_bytes_and_mode(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    foreign_path = home / "foreign.db"
    original = b"synthetic non-sqlite foreign content\n"
    foreign_path.write_bytes(original)
    os.chmod(foreign_path, 0o644)
    config = _config(home, path="foreign.db")

    with pytest.raises(OutboxOpenError):
        SQLiteOutbox.open(config)

    assert foreign_path.read_bytes() == original
    assert stat.S_IMODE(foreign_path.stat().st_mode) == 0o644


def test_admission_persists_complete_minimal_pending_rows(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segments = _turn("a")
    outbox = SQLiteOutbox.open(config)
    try:
        result = outbox.admit(segments)
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert result.status is AdmissionStatus.ADMITTED
    assert result.accepted is True
    assert result.inserted_count == len(segments)
    assert result.duplicate_count == 0
    assert len(rows) == len(segments)
    assert [row.document_id for row in rows] == [segment.document_id for segment in segments]
    for row, segment in zip(rows, segments, strict=True):
        assert row.payload_hash == segment.payload_hash
        assert row.payload_schema == segment.payload_schema
        assert row.source_sha256 == segment.source_sha256
        assert row.segment_index == segment.segment_index
        assert row.segment_count == segment.segment_count
        assert row.content == segment.content
        assert row.destination_fingerprint == config.destination_fingerprint
        assert row.state == "pending"
        assert row.attempt_count == 0
        assert row.next_attempt_at == row.created_at
        assert row.last_error_category is None
        assert row.created_at > 0
        assert row.updated_at == row.created_at


def test_exact_duplicate_is_a_noop_and_preserves_mutable_row_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segments = _turn("d")
    outbox = SQLiteOutbox.open(config)
    try:
        first = outbox.admit(segments)
        _execute(
            config.outbox.path,
            "UPDATE outbox SET state='sending', attempt_count=3, next_attempt_at=99.0",
        )
        second = outbox.admit(segments)
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert first.status is AdmissionStatus.ADMITTED
    assert second.status is AdmissionStatus.DUPLICATE
    assert second.accepted is True
    assert second.inserted_count == 0
    assert second.duplicate_count == len(segments)
    assert len(rows) == len(segments)
    assert {row.state for row in rows} == {"sending"}
    assert {row.attempt_count for row in rows} == {3}
    assert {row.next_attempt_at for row in rows} == {99.0}


def test_exact_duplicate_remains_accepted_after_configured_caps_are_lowered(
    tmp_path: Path,
) -> None:
    initial_config = _config(tmp_path)
    segments = _turn("lowered-cap")
    assert len(segments) > 1
    initial = SQLiteOutbox.open(initial_config)
    try:
        assert initial.admit(segments).status is AdmissionStatus.ADMITTED
    finally:
        initial.close()

    lowered_config = _config(tmp_path, max_pending_rows=1)
    reopened = SQLiteOutbox.open(lowered_config)
    try:
        replay = reopened.admit(segments)
        rows = reopened.read_unconfirmed()
    finally:
        reopened.close()

    assert replay.status is AdmissionStatus.DUPLICATE
    assert replay.accepted is True
    assert replay.inserted_count == 0
    assert replay.duplicate_count == len(segments)
    assert len(rows) == len(segments)


def test_exact_existing_segment_is_a_noop_while_remaining_turn_inserts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segments = _turn("m")
    outbox = SQLiteOutbox.open(config)
    try:
        _insert_pending_segment(config, segments[0])
        result = outbox.admit(segments)
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert result.status is AdmissionStatus.ADMITTED
    assert result.inserted_count == len(segments) - 1
    assert result.duplicate_count == 1
    assert {row.document_id for row in rows} == {segment.document_id for segment in segments}


@pytest.mark.parametrize(
    "column,replacement",
    [
        ("payload_hash", "0" * 64),
        ("payload_schema", "other-schema"),
        ("source_sha256", "0" * 64),
        ("segment_index", 99),
        ("segment_count", 99),
        ("content", "tampered-content"),
        ("destination_fingerprint", "0" * 64),
    ],
)
def test_same_id_collision_or_immutable_row_mismatch_rejects_the_whole_turn(
    tmp_path: Path, column: str, replacement: object
) -> None:
    config = _config(tmp_path)
    segments = _turn("c")
    outbox = SQLiteOutbox.open(config)
    try:
        _insert_pending_segment(config, segments[0])
        _execute(
            config.outbox.path,
            f"UPDATE outbox SET {column}=? WHERE document_id=?",
            (replacement, segments[0].document_id),
        )

        result = outbox.admit(segments)
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert result.status is AdmissionStatus.CONFLICT
    assert result.accepted is False
    assert len(rows) == 1
    assert rows[0].document_id == segments[0].document_id


def test_capacity_uses_all_new_rows_and_utf8_bytes_plus_frozen_allowance(tmp_path: Path) -> None:
    segments = _turn("界")
    exact_bytes = _logical_bytes(segments)

    exact_config = _config(
        tmp_path / "exact",
        max_pending_rows=len(segments),
        max_pending_bytes=exact_bytes,
    )
    exact = SQLiteOutbox.open(exact_config)
    try:
        accepted = exact.admit(segments)
    finally:
        exact.close()

    below_config = _config(
        tmp_path / "below",
        max_pending_rows=len(segments),
        max_pending_bytes=exact_bytes - 1,
    )
    below = SQLiteOutbox.open(below_config)
    try:
        rejected = below.admit(segments)
        rows = below.read_unconfirmed()
    finally:
        below.close()

    assert accepted.status is AdmissionStatus.ADMITTED
    assert rejected.status is AdmissionStatus.CAPACITY_EXCEEDED
    assert rows == ()


def test_pending_and_sending_rows_both_count_toward_whole_turn_capacity(tmp_path: Path) -> None:
    first = _turn("a")
    second = _turn("b")
    third = _turn("c")
    assert len(first) == len(second) == len(third)
    byte_cap = _logical_bytes(first) + _logical_bytes(second)
    config = _config(
        tmp_path,
        max_pending_rows=len(first) + len(second),
        max_pending_bytes=byte_cap,
    )
    outbox = SQLiteOutbox.open(config)
    try:
        assert outbox.admit(first).status is AdmissionStatus.ADMITTED
        _execute(config.outbox.path, "UPDATE outbox SET state='sending'")
        assert outbox.admit(second).status is AdmissionStatus.ADMITTED
        rejected = outbox.admit(third)
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert rejected.status is AdmissionStatus.CAPACITY_EXCEEDED
    assert len(rows) == len(first) + len(second)
    ids = {row.document_id for row in rows}
    assert ids == {segment.document_id for segment in first + second}


def test_row_cap_rejects_every_new_segment_without_partial_insertion(tmp_path: Path) -> None:
    segments = _turn("r")
    assert len(segments) > 1
    config = _config(tmp_path, max_pending_rows=len(segments) - 1)
    outbox = SQLiteOutbox.open(config)
    try:
        result = outbox.admit(segments)
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert result.status is AdmissionStatus.CAPACITY_EXCEEDED
    assert rows == ()


def test_mid_insert_failure_rolls_back_every_segment(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segments = _turn("rollback")
    assert len(segments) > 1
    outbox = SQLiteOutbox.open(config)
    _execute(
        config.outbox.path,
        """
        CREATE TRIGGER abort_later_segment
        BEFORE INSERT ON outbox
        WHEN NEW.segment_index = 1
        BEGIN
            SELECT RAISE(ABORT, 'synthetic insert abort');
        END
        """,
    )
    try:
        result = outbox.admit(segments)
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert result.status is AdmissionStatus.LOCAL_FAILURE
    assert result.accepted is False
    assert rows == ()
    assert _scalar(config.outbox.path, "SELECT COUNT(*) FROM outbox") == 0


def test_incoming_identity_tampering_is_rejected_before_storage(tmp_path: Path) -> None:
    config = _config(tmp_path, segment_max_bytes=4096)
    segment = _turn("i", segment_max_bytes=4096)[0]
    tampered = replace(segment, payload_hash="0" * 64)
    outbox = SQLiteOutbox.open(config)
    try:
        result = outbox.admit((tampered,))
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert result.status is AdmissionStatus.INVALID
    assert rows == ()


def test_incomplete_segment_set_is_rejected_before_storage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    segments = _turn("p")
    assert len(segments) > 1
    outbox = SQLiteOutbox.open(config)
    try:
        result = outbox.admit(segments[:-1])
        rows = outbox.read_unconfirmed()
    finally:
        outbox.close()

    assert result.status is AdmissionStatus.INVALID
    assert rows == ()


def test_bounded_sqlite_contention_returns_one_fixed_result_promptly(tmp_path: Path) -> None:
    config = _config(tmp_path, busy_timeout_seconds=0.05)
    segments = _turn("l")
    initialized = SQLiteOutbox.open(config)
    initialized.close()
    blocker = sqlite3.connect(config.outbox.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    contender = SQLiteOutbox.open(config)
    try:
        started = time.monotonic()
        result = contender.admit(segments)
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
        contender.close()

    assert result.status is AdmissionStatus.CONTENDED
    assert result.accepted is False
    assert elapsed < 0.75
    assert _scalar(config.outbox.path, "SELECT COUNT(*) FROM outbox") == 0


def test_other_local_admission_failure_returns_one_fixed_result(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outbox = SQLiteOutbox.open(config)
    outbox.close()

    result = outbox.admit(_turn("f"))

    assert result.status is AdmissionStatus.LOCAL_FAILURE
    assert result.accepted is False
    assert "synthetic" not in repr(result).lower()


def test_separate_process_shaped_connections_cannot_partially_overfill(tmp_path: Path) -> None:
    first = _turn("x")
    second = _turn("y")
    assert len(first) == len(second)
    config = _config(
        tmp_path,
        max_pending_rows=len(first),
        max_pending_bytes=max(_logical_bytes(first), _logical_bytes(second)),
        busy_timeout_seconds=1.0,
    )
    initialized = SQLiteOutbox.open(config)
    initialized.close()
    barrier = threading.Barrier(2)

    def admit(segments: tuple[RetainedSegment, ...]) -> AdmissionStatus:
        connection_owner = SQLiteOutbox.open(config)
        try:
            barrier.wait(timeout=2.0)
            return connection_owner.admit(segments).status
        finally:
            connection_owner.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(admit, first), executor.submit(admit, second)]
        statuses = [future.result(timeout=3.0) for future in futures]

    inspect = SQLiteOutbox.open(config)
    try:
        rows = inspect.read_unconfirmed()
    finally:
        inspect.close()

    assert sorted(status.value for status in statuses) == sorted(
        [AdmissionStatus.ADMITTED.value, AdmissionStatus.CAPACITY_EXCEEDED.value]
    )
    admitted_ids = {row.document_id for row in rows}
    assert admitted_ids in (
        {segment.document_id for segment in first},
        {segment.document_id for segment in second},
    )
    assert len(rows) == len(first)


def test_open_revalidates_confinement_after_configured_parent_becomes_symlink(
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    config = _config(home, path="changing/outbox.sqlite3")
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "changing").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert caught.value.__cause__ is None
    assert not (outside / "outbox.sqlite3").exists()
    assert not (outside / "outbox.sqlite3.lock").exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink substitution proof is POSIX-only")
def test_database_path_substitution_before_connect_cannot_mutate_outside_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    config = _config(home)
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"")
    os.chmod(outside, 0o644)
    outside_bytes = outside.read_bytes()
    outside_mode = stat.S_IMODE(outside.stat().st_mode)
    displaced_inside = tmp_path / "displaced-inside.sqlite3"
    real_connect = sqlite3.connect
    real_open = outbox_module._connect_existing_database
    swapped = False

    def swap_then_connect(
        database_path: Path,
        runtime_config: BetterHindsightConfig,
    ) -> sqlite3.Connection:
        nonlocal swapped
        if not swapped:
            config.outbox.path.rename(displaced_inside)
            config.outbox.path.symlink_to(outside)
            swapped = True
        return real_open(database_path, runtime_config)

    monkeypatch.setattr(outbox_module, "_connect_existing_database", swap_then_connect)

    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)

    assert swapped is True
    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert outside.read_bytes() == outside_bytes
    assert stat.S_IMODE(outside.stat().st_mode) == outside_mode
    outside_connection = real_connect(outside)
    try:
        assert outside_connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert outside_connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='outbox'"
        ).fetchone() == (0,)
    finally:
        outside_connection.close()


@pytest.mark.skipif(os.name != "posix", reason="initial sender ownership is POSIX-only")
def test_new_paths_are_private_without_chmodding_preexisting_parents(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    home.mkdir(mode=0o755)
    preexisting = home / "preexisting"
    preexisting.mkdir(mode=0o755)
    os.chmod(preexisting, 0o755)
    config = _config(home, path="preexisting/new/outbox.sqlite3")

    outbox = SQLiteOutbox.open(config)
    lock_path = outbox.profile_lock_path
    outbox.close()

    assert stat.S_IMODE(preexisting.stat().st_mode) == 0o755
    assert stat.S_IMODE((preexisting / "new").stat().st_mode) == 0o700
    assert stat.S_IMODE(config.outbox.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

    os.chmod(config.outbox.path, 0o644)
    os.chmod(lock_path, 0o644)
    reopened = SQLiteOutbox.open(config)
    reopened.close()

    assert stat.S_IMODE(config.outbox.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_content_is_omitted_from_outbox_and_row_reprs(tmp_path: Path) -> None:
    canary = "SYNTHETIC_OUTBOX_REPR_CANARY"
    segments = build_retained_segments(
        session_id="repr-session",
        user_content=canary,
        assistant_content="safe response",
        tags=(),
        segment_max_bytes=64,
    )
    config = _config(tmp_path)
    outbox = SQLiteOutbox.open(config)
    try:
        assert outbox.admit(segments).accepted
        rows = outbox.read_unconfirmed()
        rendered = repr(outbox) + repr(rows)
    finally:
        outbox.close()

    assert canary not in rendered
    assert all(row.content not in repr(row) for row in rows)


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_profile_lock_identity_is_private_and_elects_one_nonblocking_owner(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first_outbox = SQLiteOutbox.open(config)
    second_outbox = SQLiteOutbox.open(config)
    try:
        identity = first_outbox.profile_lock_identity
        status = identity.path.stat(follow_symlinks=False)

        first = first_outbox.try_acquire_profile_lock()
        second = second_outbox.try_acquire_profile_lock()
        assert first.status is outbox_module.ProfileLockStatus.ACQUIRED
        assert first.owner is not None
        assert second.status is outbox_module.ProfileLockStatus.CONTENDED
        assert second.owner is None
        foreign_owner = second_outbox.recover_sending(first.owner, now=1.0)
        assert foreign_owner.status is outbox_module.OutboxTransitionStatus.LOCAL_FAILURE

        first.owner.release()
        successor = second_outbox.try_acquire_profile_lock()
        assert successor.status is outbox_module.ProfileLockStatus.ACQUIRED
        assert successor.owner is not None
        successor.owner.release()
    finally:
        first_outbox.close()
        second_outbox.close()

    assert identity.path == first_outbox.profile_lock_path
    assert (status.st_dev, status.st_ino) == (identity.device, identity.inode)
    assert stat.S_ISREG(status.st_mode)
    assert stat.S_IMODE(status.st_mode) == 0o600
    assert status.st_uid == os.geteuid()
    assert str(identity.path) not in repr(identity)


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_profile_lock_acquisition_is_existing_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outbox = SQLiteOutbox.open(config)
    identity = outbox.profile_lock_identity
    displaced = tmp_path / "displaced-missing.lock"
    identity.path.rename(displaced)
    try:
        acquisition = outbox.try_acquire_profile_lock()
        assert acquisition.status is outbox_module.ProfileLockStatus.LOCAL_FAILURE
        assert acquisition.owner is None
        assert not identity.path.exists()
    finally:
        displaced.rename(identity.path)
        outbox.close()


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_profile_lock_revalidates_path_identity_after_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path / "profile")
    outbox = SQLiteOutbox.open(config)
    identity = outbox.profile_lock_identity
    displaced = tmp_path / "displaced.lock"
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"synthetic outside lock\n")
    os.chmod(outside, 0o600)
    original_bytes = outside.read_bytes()
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    real_flock = outbox_module._try_flock_exclusive

    def flock_then_substitute(descriptor: int) -> bool:
        acquired = real_flock(descriptor)
        if acquired:
            identity.path.rename(displaced)
            identity.path.symlink_to(outside)
        return acquired

    monkeypatch.setattr(outbox_module, "_try_flock_exclusive", flock_then_substitute)
    try:
        failed = outbox.try_acquire_profile_lock()
        assert failed.status is outbox_module.ProfileLockStatus.LOCAL_FAILURE
        assert failed.owner is None
        assert outside.read_bytes() == original_bytes
        assert stat.S_IMODE(outside.stat().st_mode) == original_mode

        identity.path.unlink()
        displaced.rename(identity.path)
        monkeypatch.setattr(outbox_module, "_try_flock_exclusive", real_flock)
        recovered = outbox.try_acquire_profile_lock()
        assert recovered.status is outbox_module.ProfileLockStatus.ACQUIRED
        assert recovered.owner is not None
        recovered.owner.release()
    finally:
        if identity.path.is_symlink():
            identity.path.unlink()
        if displaced.exists():
            displaced.rename(identity.path)
        outbox.close()
