"""Selective retry contracts for concurrent durable outbox inspection."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import NoReturn

import pytest

import better_hermes_hindsight.outbox as outbox_module
from better_hermes_hindsight.config import BetterHindsightConfig
from better_hermes_hindsight.outbox import (
    OUTBOX_OPEN_FAILED_MESSAGE,
    OUTBOX_READ_FAILED_MESSAGE,
    OutboxOpenError,
    OutboxReadError,
    SQLiteOutbox,
)
from tests import outbox_inspection as inspection
from tests.integration import test_isolated_hindsight as live
from tests.integration import test_released_hermes_retention as released
from tests.unit.test_outbox import _config


@pytest.mark.parametrize("waiter", ["live", "released"])
@pytest.mark.parametrize("stage", ["open", "read"])
def test_live_waiter_retries_real_contention_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiter: str, stage: str
) -> None:
    module = live if waiter == "live" else released
    config = _config(tmp_path, busy_timeout_seconds=0.01)
    inspector = SQLiteOutbox.open(config)
    blocker = sqlite3.connect(config.outbox.path)
    original_read = module._read_rows
    failures = 0
    observations = 0

    def read_rows(config: BetterHindsightConfig) -> tuple[outbox_module.OutboxRow, ...]:
        nonlocal failures
        try:
            return original_read(config) if stage == "open" else inspector.read_unconfirmed()
        except (OutboxOpenError, OutboxReadError):
            failures += 1
            blocker.rollback()
            raise

    def empty(rows: tuple[outbox_module.OutboxRow, ...]) -> bool:
        nonlocal observations
        observations += 1
        return not rows

    monkeypatch.setattr(module, "_read_rows", read_rows)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        assert module._wait_for_rows(config, empty) == ()
        assert failures == 1
        assert observations == 1
    finally:
        blocker.rollback()
        blocker.close()
        inspector.close()


@pytest.mark.parametrize("stage", ["open", "read", "retry_deadline"])
def test_real_exclusive_lock_is_classified_without_exposing_sqlite_text(
    tmp_path: Path, stage: str
) -> None:
    config = _config(tmp_path, busy_timeout_seconds=0.01)
    inspector = SQLiteOutbox.open(config)
    blocker = sqlite3.connect(config.outbox.path)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        error_type = OutboxOpenError if stage == "open" else OutboxReadError
        with pytest.raises(error_type) as caught:
            if stage == "open":
                SQLiteOutbox.open(config)
            elif stage == "read":
                inspector.read_unconfirmed()
            else:
                inspector.next_matching_retry_deadline()
        assert caught.value.category == "sqlite_contention"
        assert str(caught.value) == (
            OUTBOX_OPEN_FAILED_MESSAGE if stage == "open" else OUTBOX_READ_FAILED_MESSAGE
        )
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__
    finally:
        blocker.rollback()
        blocker.close()
        inspector.close()


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (sqlite3.SQLITE_BUSY, "sqlite_contention"),
        (sqlite3.SQLITE_BUSY_SNAPSHOT, "sqlite_contention"),
        (sqlite3.SQLITE_LOCKED, "sqlite_contention"),
        (sqlite3.SQLITE_LOCKED_SHAREDCACHE, "sqlite_contention"),
        (sqlite3.SQLITE_CANTOPEN, "local_failure"),
        (sqlite3.SQLITE_PERM, "local_failure"),
        (sqlite3.SQLITE_CORRUPT, "local_failure"),
        (sqlite3.SQLITE_SCHEMA, "local_failure"),
        (None, "local_failure"),
    ],
)
def test_open_classification_uses_numeric_sqlite_codes_not_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int | None, category: str
) -> None:
    config = _config(tmp_path)
    error = sqlite3.OperationalError("database is locked: private-path secret-token")
    if code is not None:
        error.sqlite_errorcode = code

    def fail_connect(*args: object) -> NoReturn:
        raise error

    monkeypatch.setattr(outbox_module, "_connect_existing_database", fail_connect)
    with pytest.raises(OutboxOpenError) as caught:
        SQLiteOutbox.open(config)
    assert caught.value.category == category
    assert str(caught.value) == OUTBOX_OPEN_FAILED_MESSAGE
    assert "secret-token" not in repr(caught.value)


@pytest.mark.parametrize("fault", ["schema", "unsupported", "path", "permission"])
def test_invalid_open_is_permanent_and_not_repaired(tmp_path: Path, fault: str) -> None:
    config = _config(tmp_path)
    SQLiteOutbox.open(config).close()
    if fault in {"schema", "unsupported"}:
        with sqlite3.connect(config.outbox.path) as connection:
            connection.execute(
                "DROP TABLE outbox" if fault == "schema" else "PRAGMA user_version=99"
            )
        connection.close()
    elif fault == "path":
        config.outbox.path.unlink()
        config.outbox.path.mkdir()
    else:
        config.outbox.path.chmod(0)
    try:
        with pytest.raises(OutboxOpenError) as caught:
            SQLiteOutbox.open(config)
        assert caught.value.category == "local_failure"
        assert str(caught.value) == (
            outbox_module.OUTBOX_SCHEMA_UNSUPPORTED_MESSAGE
            if fault == "unsupported"
            else OUTBOX_OPEN_FAILED_MESSAGE
        )
        if fault == "permission":
            assert config.outbox.path.stat().st_mode & 0o777 == 0
        if fault == "schema":
            with sqlite3.connect(config.outbox.path) as connection:
                assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == []
            connection.close()
    finally:
        if fault == "permission":
            config.outbox.path.chmod(0o600)


@pytest.mark.parametrize("method", ["read_unconfirmed", "next_matching_retry_deadline"])
@pytest.mark.parametrize("fault", ["closed", "schema"])
def test_invalid_read_is_permanent(tmp_path: Path, method: str, fault: str) -> None:
    config = _config(tmp_path)
    inspector = SQLiteOutbox.open(config)
    try:
        if fault == "closed":
            inspector.close()
        else:
            with sqlite3.connect(config.outbox.path) as connection:
                connection.execute("DROP TABLE outbox")
            connection.close()
        with pytest.raises(OutboxReadError) as caught:
            getattr(inspector, method)()
        assert caught.value.category == "local_failure"
        assert str(caught.value) == OUTBOX_READ_FAILED_MESSAGE
    finally:
        inspector.close()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(inspection, "time", clock)
    return clock


@pytest.mark.parametrize("error_type", [OutboxOpenError, OutboxReadError])
def test_permanent_failure_is_immediate_and_never_calls_predicate(
    error_type: type[OutboxOpenError] | type[OutboxReadError], clock: _Clock
) -> None:
    calls = 0

    def fail_read() -> NoReturn:
        nonlocal calls
        calls += 1
        # Legacy message-only constructors must remain non-retryable.
        raise error_type("private-path secret-token")

    def predicate(rows: tuple[outbox_module.OutboxRow, ...]) -> NoReturn:
        pytest.fail("failed inspection reached predicate")

    with pytest.raises(inspection.OutboxWaitError) as caught:
        inspection.wait_for_rows(fail_read, predicate, timeout=1.0, poll_interval=0.02)
    assert calls == 1
    assert clock.sleeps == []
    assert caught.value.category == "local_failure"
    assert caught.value.stage == ("open" if error_type is OutboxOpenError else "read")
    assert "secret-token" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("error_type", [OutboxOpenError, OutboxReadError])
def test_contention_exhausts_one_deadline_without_observations(
    error_type: type[OutboxOpenError] | type[OutboxReadError], clock: _Clock
) -> None:
    calls = 0

    def fail_read() -> NoReturn:
        nonlocal calls
        calls += 1
        raise error_type(category="sqlite_contention")

    def predicate(rows: tuple[outbox_module.OutboxRow, ...]) -> NoReturn:
        pytest.fail("failed inspection reached predicate")

    with pytest.raises(inspection.OutboxWaitError) as caught:
        inspection.wait_for_rows(fail_read, predicate, timeout=0.05, poll_interval=0.02)
    assert calls == 3
    assert clock.now == 0.05
    assert clock.sleeps == pytest.approx([0.02, 0.02, 0.01])
    assert caught.value.category == "sqlite_contention"
    assert caught.value.stage == ("open" if error_type is OutboxOpenError else "read")


@pytest.mark.parametrize("stage", ["open", "read"])
def test_real_contention_remaining_locked_reaches_deadline(
    tmp_path: Path, clock: _Clock, stage: str
) -> None:
    config = _config(tmp_path, busy_timeout_seconds=0.01)
    inspector = SQLiteOutbox.open(config)
    blocker = sqlite3.connect(config.outbox.path)

    def read_rows() -> tuple[outbox_module.OutboxRow, ...]:
        return live._read_rows(config) if stage == "open" else inspector.read_unconfirmed()

    def predicate(rows: tuple[outbox_module.OutboxRow, ...]) -> NoReturn:
        pytest.fail("locked database was mistaken for an empty queue")

    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(inspection.OutboxWaitError) as caught:
            inspection.wait_for_rows(read_rows, predicate, timeout=0.05, poll_interval=0.02)
        assert caught.value.category == "sqlite_contention"
        assert caught.value.stage == stage
        assert clock.now == 0.05
        assert blocker.in_transaction
    finally:
        blocker.rollback()
        blocker.close()
        inspector.close()


def test_contention_and_predicate_misses_share_the_deadline(clock: _Clock) -> None:
    calls = 0
    observations = 0

    def read_rows() -> tuple[outbox_module.OutboxRow, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OutboxReadError(category="sqlite_contention")
        return ()

    def predicate(rows: tuple[outbox_module.OutboxRow, ...]) -> bool:
        nonlocal observations
        observations += 1
        return False

    with pytest.raises(inspection.OutboxWaitError) as caught:
        inspection.wait_for_rows(read_rows, predicate, timeout=0.05, poll_interval=0.02)
    assert calls == 3
    assert observations == 2
    assert clock.now == 0.05
    assert caught.value.category == "predicate_not_met"
    assert caught.value.stage == "predicate"


def test_late_success_is_not_accepted_or_given_to_predicate(clock: _Clock) -> None:
    def read_rows() -> tuple[outbox_module.OutboxRow, ...]:
        clock.now = 1.0
        return ()

    def predicate(rows: tuple[outbox_module.OutboxRow, ...]) -> NoReturn:
        pytest.fail("late inspection reached predicate")

    with pytest.raises(inspection.OutboxWaitError):
        inspection.wait_for_rows(read_rows, predicate, timeout=0.05, poll_interval=0.02)
    assert clock.sleeps == []


@pytest.mark.parametrize("stage", ["open", "read"])
def test_live_child_reports_safe_failure_category_and_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stage: str
) -> None:
    for key in (
        "API_URL",
        "API_KEY",
        "EXPECTED_VERSION",
        "HERMES_PYTHON",
        "BANK_ID",
        "OWNERSHIP_NAME",
    ):
        monkeypatch.setenv(f"BETTER_HINDSIGHT_CHILD_{key}", "synthetic-unused")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HINDSIGHT_API_KEY", "synthetic-unused")
    error_type = OutboxOpenError if stage == "open" else OutboxReadError

    def fail_prepare(*args: object) -> NoReturn:
        raise error_type("private-path secret-token", category="sqlite_contention")

    monkeypatch.setattr(live, "_prepare_home", fail_prepare)
    assert live._run_live_child() == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert "secret-token" not in output.out
    report = json.loads(output.out)
    assert report["outbox_category"] == "sqlite_contention"
    assert report["outbox_stage"] == stage
