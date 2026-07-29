"""Deterministic Task 4 contracts for local status and explicit mission management."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pytest

import better_hermes_hindsight.management as management_module
from better_hermes_hindsight.client import (
    HindsightClientError,
    MissionSnapshot,
    MissionValue,
)
from better_hermes_hindsight.config import (
    OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES,
    BetterHindsightConfig,
    load_config,
)
from better_hermes_hindsight.management import (
    ManagementResult,
    apply_missions,
    check_missions,
    status,
)
from better_hermes_hindsight.outbox import SQLiteOutbox
from better_hermes_hindsight.runtime import create_operator_runtime

_RETAIN_DESIRED = "Retain durable synthetic preferences exactly."
_OBSERVATIONS_DESIRED = "Consolidate stable synthetic observations exactly."
_RETAIN_REMOTE = "Different synthetic retain mission."
_OBSERVATIONS_REMOTE = "Different synthetic observations mission."

_T = TypeVar("_T")


class _SyntheticFailure(RuntimeError):
    """A local-only failure whose text must never enter a management result."""


class _MissionClient:
    def __init__(
        self,
        *,
        events: list[object],
        gets: Sequence[MissionSnapshot | BaseException],
        patches: Sequence[MissionSnapshot | BaseException] = (),
    ) -> None:
        self.events = events
        self.gets = deque(gets)
        self.patches = deque(patches)

    async def get_bank_config(self) -> MissionSnapshot:
        self.events.append("get")
        return self._next(self.gets, "unexpected extra mission GET")

    async def update_bank_missions(self, updates: Mapping[str, str]) -> MissionSnapshot:
        self.events.append(("patch", dict(updates)))
        return self._next(self.patches, "unexpected extra mission PATCH")

    @staticmethod
    def _next(
        scripted: deque[MissionSnapshot | BaseException],
        empty_message: str,
    ) -> MissionSnapshot:
        if not scripted:
            raise AssertionError(empty_message)
        value = scripted.popleft()
        if isinstance(value, BaseException):
            raise value
        return value


class _FakeRuntime:
    def __init__(
        self,
        *,
        client: _MissionClient,
        events: list[object],
        finalize_error: BaseException | None = None,
    ) -> None:
        self.client = client
        self.events = events
        self.finalize_error = finalize_error
        self.timeouts: list[float | None] = []

    def call(
        self,
        operation: Callable[[Any], Awaitable[_T]],
        *,
        timeout: float | None,
    ) -> _T:
        self.timeouts.append(timeout)

        async def invoke() -> _T:
            return await operation(self.client)

        return asyncio.run(invoke())

    def finalize(self) -> bool:
        self.events.append("runtime_finalize")
        if self.finalize_error is not None:
            raise self.finalize_error
        return True


class _RuntimeFactory:
    def __init__(
        self,
        *,
        runtime: _FakeRuntime,
        events: list[object],
        creation_error: BaseException | None = None,
    ) -> None:
        self.runtime = runtime
        self.events = events
        self.creation_error = creation_error
        self.configs: list[BetterHindsightConfig] = []

    def __call__(self, config: BetterHindsightConfig) -> _FakeRuntime:
        self.events.append("runtime_create")
        self.configs.append(config)
        if self.creation_error is not None:
            raise self.creation_error
        return self.runtime


def _config(
    home: Path,
    *,
    single_principal: bool = True,
    retain_mission: str | None = None,
    observations_mission: str | None = None,
    timeout_seconds: float = 12.0,
) -> BetterHindsightConfig:
    return load_config(
        hermes_home=home,
        environ={},
        injected={
            "api_url": "https://service.example.test",
            "bank_id": "synthetic-bank",
            "single_principal": single_principal,
            "recall": {"enabled": False},
            "retain": {"enabled": False, "timeout_seconds": timeout_seconds},
            "missions": {
                "retain_mission": retain_mission,
                "observations_mission": observations_mission,
            },
        },
    )


def _value(value: str | None, *, present: bool = True) -> MissionValue:
    return MissionValue(present=present, value=value)


def _snapshot(
    retain: MissionValue,
    observations: MissionValue,
) -> MissionSnapshot:
    return MissionSnapshot(
        retain_mission=retain,
        observations_mission=observations,
    )


def _runtime_factory(
    *,
    gets: Sequence[MissionSnapshot | BaseException],
    patches: Sequence[MissionSnapshot | BaseException] = (),
    creation_error: BaseException | None = None,
    finalize_error: BaseException | None = None,
) -> tuple[_RuntimeFactory, _FakeRuntime, list[object]]:
    events: list[object] = []
    client = _MissionClient(events=events, gets=gets, patches=patches)
    runtime = _FakeRuntime(client=client, events=events, finalize_error=finalize_error)
    factory = _RuntimeFactory(
        runtime=runtime,
        events=events,
        creation_error=creation_error,
    )
    return factory, runtime, events


def _assert_result(
    result: ManagementResult,
    *,
    payload: dict[str, object],
    exit_code: int,
) -> None:
    assert type(result) is ManagementResult
    assert result.payload == payload
    assert result.exit_code == exit_code


def _assert_remote_deadlines(runtime: _FakeRuntime, *, count: int, maximum: float) -> None:
    assert len(runtime.timeouts) == count
    assert all(type(timeout) is float for timeout in runtime.timeouts)
    bounded = [timeout for timeout in runtime.timeouts if timeout is not None]
    assert all(0.0 < timeout <= maximum for timeout in bounded)
    assert bounded == sorted(bounded, reverse=True)


def _empty_status_payload(*, outbox: str, ownership: str = "free") -> dict[str, object]:
    return {
        "age_bucket": "none",
        "command": "status",
        "counts": {"mismatch": 0, "pending": 0, "retry": 0, "sending": 0},
        "last_error_category": "none",
        "logical_queued_bytes": 0,
        "outbox": outbox,
        "result": "ok",
        "sender_ownership": ownership,
    }


def _initialize_outbox(config: BetterHindsightConfig) -> Path:
    outbox = SQLiteOutbox.open(config)
    lock_path = outbox.profile_lock_path
    outbox.close()
    return lock_path


def _sqlite_sidecars(config: BetterHindsightConfig) -> tuple[Path, Path, Path]:
    database = config.outbox.path
    return Path(f"{database}-wal"), Path(f"{database}-shm"), Path(f"{database}-journal")


def _xattrs(path: Path) -> tuple[tuple[str, bytes], ...]:
    names = sorted(os.listxattr(path, follow_symlinks=False))
    return tuple((name, os.getxattr(path, name, follow_symlinks=False)) for name in names)


def _document_id(index: int) -> str:
    return f"better-hindsight-turn-v1:{index:064x}"


def _insert_row(
    config: BetterHindsightConfig,
    *,
    index: int,
    content: str = "synthetic queued content",
    destination_fingerprint: str | None = None,
    payload_schema: str | None = None,
    state: str = "pending",
    attempt_count: int = 0,
    last_error_category: str | None = None,
    created_at: float = 1_000.0,
    updated_at: float = 1_000.0,
) -> None:
    connection = sqlite3.connect(config.outbox.path)
    try:
        connection.execute(
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
            ) VALUES (?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _document_id(index),
                f"{index + 100:064x}",
                config.outbox.payload_schema if payload_schema is None else payload_schema,
                f"{index + 200:064x}",
                content,
                (
                    config.destination_fingerprint
                    if destination_fingerprint is None
                    else destination_fingerprint
                ),
                state,
                attempt_count,
                created_at,
                last_error_category,
                created_at,
                updated_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()


# Status -----------------------------------------------------------------------


def test_status_requires_exact_single_principal_before_any_local_inspection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, single_principal=False)
    config.outbox.path.parent.mkdir(parents=True)
    original = b"synthetic malformed database that must remain untouched\n"
    config.outbox.path.write_bytes(original)
    os.chmod(config.outbox.path, 0o640)
    before = config.outbox.path.stat()

    result = status(config, now=20_000.0)

    _assert_result(
        result,
        payload={
            "command": "status",
            "error": "authorization_required",
            "result": "error",
        },
        exit_code=3,
    )
    after = config.outbox.path.stat()
    assert config.outbox.path.read_bytes() == original
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o640
    assert after.st_mtime_ns == before.st_mtime_ns
    assert not Path(f"{config.outbox.path}.lock").exists()


def test_status_missing_outbox_is_successful_uninitialized_and_creates_nothing(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    outbox_parent = config.outbox.path.parent
    lock_path = Path(f"{config.outbox.path}.lock")
    assert not outbox_parent.exists()

    result = status(config, now=20_000.0)

    _assert_result(
        result,
        payload=_empty_status_payload(outbox="uninitialized"),
        exit_code=0,
    )
    assert not outbox_parent.exists()
    assert not config.outbox.path.exists()
    assert not lock_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="FIFO preflight is POSIX-only")
def test_status_rejects_fifo_before_opening_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.outbox.path.parent.mkdir(parents=True)
    os.mkfifo(config.outbox.path, mode=0o600)
    before = config.outbox.path.stat(follow_symlinks=False)
    open_calls = 0

    def forbidden_open(*_args: object, **_kwargs: object) -> int:
        nonlocal open_calls
        open_calls += 1
        raise AssertionError("FIFO reached os.open")

    monkeypatch.setattr(os, "open", forbidden_open)
    result = status(config, now=20_000.0)

    _assert_result(
        result,
        payload={"command": "status", "error": "status_unavailable", "result": "error"},
        exit_code=3,
    )
    after = config.outbox.path.stat(follow_symlinks=False)
    assert open_calls == 0
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
    )


def test_status_sidecar_free_snapshot_uses_immutable_unix_and_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    sidecars = _sqlite_sidecars(config)
    assert not any(path.exists() for path in sidecars)
    before = config.outbox.path.stat(follow_symlinks=False)
    original_connect = sqlite3.connect
    calls: list[str] = []

    def recording_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        calls.append(database)
        return cast(sqlite3.Connection, original_connect(database, *args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    result = status(config, now=20_000.0)

    assert result.exit_code == 0
    assert calls == [f"{config.outbox.path.as_uri()}?mode=ro&immutable=1&vfs=unix"]
    assert not any(path.exists() for path in sidecars)
    after = config.outbox.path.stat(follow_symlinks=False)
    assert (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


def test_status_sidecar_free_checkpointed_wal_database_uses_immutable_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    writer = sqlite3.connect(config.outbox.path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        _insert_row(config, index=1, content="checkpointed-wal-row", created_at=1_000.0)
        checkpoint = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint == (0, 0, 0)
    finally:
        writer.close()
    assert not any(path.exists() for path in _sqlite_sidecars(config))
    original_connect = sqlite3.connect
    calls: list[str] = []

    def recording_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        calls.append(database)
        return cast(sqlite3.Connection, original_connect(database, *args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    result = status(config, now=2_000.0)

    assert result.exit_code == 0
    assert result.payload["counts"] == {
        "mismatch": 0,
        "pending": 1,
        "retry": 0,
        "sending": 0,
    }
    assert calls == [f"{config.outbox.path.as_uri()}?mode=ro&immutable=1&vfs=unix"]
    assert not any(path.exists() for path in _sqlite_sidecars(config))


@pytest.mark.parametrize("present_suffix", ["-wal", "-shm", "-journal"])
def test_status_rejects_malformed_sidecar_topology_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    present_suffix: str,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    malformed = Path(f"{config.outbox.path}{present_suffix}")
    original = b"synthetic malformed sidecar bytes\n"
    malformed.write_bytes(original)
    connect_calls = 0

    def forbidden_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("malformed topology reached sqlite3.connect")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    result = status(config, now=20_000.0)

    _assert_result(
        result,
        payload={"command": "status", "error": "status_unavailable", "result": "error"},
        exit_code=3,
    )
    assert connect_calls == 0
    assert malformed.read_bytes() == original


def test_status_rejects_truncated_shm_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    wal, shm, _journal = _sqlite_sidecars(config)
    wal.write_bytes(b"synthetic WAL placeholder\n")
    shm.write_bytes(b"")
    connect_calls = 0

    def forbidden_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("truncated SHM reached sqlite3.connect")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    result = status(config, now=20_000.0)

    _assert_result(
        result,
        payload={"command": "status", "error": "status_unavailable", "result": "error"},
        exit_code=3,
    )
    assert connect_calls == 0
    assert wal.read_bytes() == b"synthetic WAL placeholder\n"
    assert shm.read_bytes() == b""


@pytest.mark.parametrize("unsupported", ["platform", "sqlite", "no_follow"])
def test_status_rejects_unsupported_runtime_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsupported: str,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    connect_calls = 0

    def forbidden_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("unsupported runtime reached sqlite3.connect")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    if unsupported == "platform":
        monkeypatch.setattr(os, "name", "nt")
    elif unsupported == "sqlite":
        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 21, 0))
    else:
        monkeypatch.delattr(os, "O_NOFOLLOW")

    result = status(config, now=20_000.0)

    _assert_result(
        result,
        payload={"command": "status", "error": "status_unavailable", "result": "error"},
        exit_code=3,
    )
    assert connect_calls == 0


def test_status_ready_snapshot_partitions_every_row_once_and_counts_logical_bytes(
    tmp_path: Path,
) -> None:
    now = 20_000.0
    config = _config(tmp_path)
    _initialize_outbox(config)
    contents = ("destination-mismatch-界", "schema-mismatch", "sending", "retry", "pending")
    _insert_row(
        config,
        index=1,
        content=contents[0],
        destination_fingerprint="f" * 64,
        state="sending",
        attempt_count=8,
        last_error_category="retain_timeout",
        created_at=now - 70.0,
        updated_at=400.0,
    )
    _insert_row(
        config,
        index=2,
        content=contents[1],
        payload_schema="synthetic-prior-schema",
        state="pending",
        attempt_count=0,
        created_at=now - 5.0,
        updated_at=700.0,
    )
    _insert_row(
        config,
        index=3,
        content=contents[2],
        state="sending",
        attempt_count=4,
        last_error_category="retain_failed",
        created_at=now - 50.0,
        updated_at=500.0,
    )
    _insert_row(
        config,
        index=4,
        content=contents[3],
        state="pending",
        attempt_count=2,
        last_error_category="retain_unconfirmed",
        created_at=now - 50.0,
        updated_at=500.0,
    )
    _insert_row(
        config,
        index=5,
        content=contents[4],
        state="pending",
        attempt_count=0,
        created_at=now - 10.0,
        updated_at=600.0,
    )

    result = status(config, now=now)

    logical_bytes = sum(
        len(content.encode("utf-8")) + OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES for content in contents
    )
    _assert_result(
        result,
        payload={
            "age_bucket": "1m_to_lt_1h",
            "command": "status",
            "counts": {"mismatch": 2, "pending": 1, "retry": 1, "sending": 1},
            "last_error_category": "retain_unconfirmed",
            "logical_queued_bytes": logical_bytes,
            "outbox": "ready",
            "result": "ok",
            "sender_ownership": "free",
        },
        exit_code=0,
    )


def test_status_reads_committed_wal_state_coherently_without_mutating_profile_files(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    _insert_row(config, index=1, content="before-wal", created_at=1_000.0, updated_at=1_000.0)
    writer = sqlite3.connect(config.outbox.path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE outbox SET content=?, updated_at=? WHERE document_id=?",
            ("committed-from-wal-界", 1_500.0, _document_id(1)),
        )
        writer.commit()
        profile_files = tuple(sorted(config.outbox.path.parent.iterdir()))
        assert Path(f"{config.outbox.path}-wal") in profile_files
        before: dict[
            str,
            tuple[
                bytes,
                tuple[int, int, int, int, int, int, int],
                tuple[int, int, int],
                tuple[tuple[str, bytes], ...],
            ],
        ] = {}
        for path in profile_files:
            path_status = path.stat(follow_symlinks=False)
            before[path.name] = (
                path.read_bytes(),
                (
                    path_status.st_dev,
                    path_status.st_ino,
                    stat.S_IFMT(path_status.st_mode),
                    path_status.st_nlink,
                    stat.S_IMODE(path_status.st_mode),
                    path_status.st_uid,
                    path_status.st_gid,
                ),
                (path_status.st_size, path_status.st_mtime_ns, path_status.st_ctime_ns),
                _xattrs(path),
            )

        result = status(config, now=2_000.0)

        assert result.exit_code == 0
        assert result.payload["counts"] == {
            "mismatch": 0,
            "pending": 1,
            "retry": 0,
            "sending": 0,
        }
        assert result.payload["logical_queued_bytes"] == (
            len("committed-from-wal-界".encode()) + OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES
        )
        after_files = tuple(sorted(config.outbox.path.parent.iterdir()))
        assert after_files == profile_files
        for path in after_files:
            path_status = path.stat(follow_symlinks=False)
            before_bytes, before_identity, before_mutable_stat, before_xattrs = before[path.name]
            assert (
                path_status.st_dev,
                path_status.st_ino,
                stat.S_IFMT(path_status.st_mode),
                path_status.st_nlink,
                stat.S_IMODE(path_status.st_mode),
                path_status.st_uid,
                path_status.st_gid,
            ) == before_identity
            assert _xattrs(path) == before_xattrs
            if path.name.endswith("-shm"):
                continue
            assert path.read_bytes() == before_bytes
            assert (
                path_status.st_size,
                path_status.st_mtime_ns,
                path_status.st_ctime_ns,
            ) == before_mutable_stat
    finally:
        writer.close()


def test_status_active_wal_sees_row_that_immutable_snapshot_misses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    writer = sqlite3.connect(config.outbox.path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            """
            INSERT INTO outbox (
                document_id, payload_hash, payload_schema, source_sha256,
                segment_index, segment_count, content, destination_fingerprint,
                state, attempt_count, next_attempt_at, last_error_category,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 0, 1, ?, ?, 'pending', 0, ?, NULL, ?, ?)
            """,
            (
                _document_id(1),
                f"{101:064x}",
                config.outbox.payload_schema,
                f"{201:064x}",
                "wal-only-row",
                config.destination_fingerprint,
                1_000.0,
                1_000.0,
                1_000.0,
            ),
        )
        writer.commit()
        wal, shm, journal = _sqlite_sidecars(config)
        assert wal.is_file() and wal.stat().st_size > 0
        assert shm.is_file()
        assert not journal.exists()

        immutable = sqlite3.connect(
            f"{config.outbox.path.as_uri()}?mode=ro&immutable=1&vfs=unix",
            uri=True,
        )
        try:
            assert immutable.execute("SELECT COUNT(*) FROM outbox").fetchone() == (0,)
        finally:
            immutable.close()

        original_connect = sqlite3.connect
        calls: list[str] = []

        def recording_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
            calls.append(database)
            return cast(sqlite3.Connection, original_connect(database, *args, **kwargs))

        monkeypatch.setattr(sqlite3, "connect", recording_connect)
        result = status(config, now=2_000.0)

        assert result.exit_code == 0
        assert result.payload["counts"] == {
            "mismatch": 0,
            "pending": 1,
            "retry": 0,
            "sending": 0,
        }
        assert calls == [f"{config.outbox.path.as_uri()}?mode=ro&vfs=unix"]
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (-5.0, "lt_1m"),
        (0.0, "lt_1m"),
        (59.999, "lt_1m"),
        (60.0, "1m_to_lt_1h"),
        (3_599.999, "1m_to_lt_1h"),
        (3_600.0, "1h_to_lt_24h"),
        (86_399.999, "1h_to_lt_24h"),
        (86_400.0, "gte_24h"),
    ],
)
def test_status_age_buckets_have_exact_boundaries_without_sleeping(
    tmp_path: Path,
    age_seconds: float,
    expected: str,
) -> None:
    now = 100_000.0
    config = _config(tmp_path)
    _initialize_outbox(config)
    _insert_row(config, index=1, created_at=now - age_seconds, updated_at=now)

    result = status(config, now=now)

    assert result.exit_code == 0
    assert result.payload["age_bucket"] == expected


def test_status_last_error_uses_updated_created_document_order(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    _insert_row(
        config,
        index=1,
        last_error_category="retain_timeout",
        created_at=10.0,
        updated_at=20.0,
    )
    _insert_row(
        config,
        index=2,
        last_error_category="retain_failed",
        created_at=30.0,
        updated_at=40.0,
    )
    _insert_row(
        config,
        index=3,
        last_error_category="retain_unconfirmed",
        created_at=30.0,
        updated_at=40.0,
    )

    result = status(config, now=50.0)

    assert result.exit_code == 0
    assert result.payload["last_error_category"] == "retain_unconfirmed"


def test_status_unknown_persisted_error_fails_with_fixed_payload(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    _insert_row(config, index=1, last_error_category="future_raw_failure")

    result = status(config, now=2_000.0)

    _assert_result(
        result,
        payload={"command": "status", "error": "status_unavailable", "result": "error"},
        exit_code=3,
    )
    assert "future_raw_failure" not in repr(result.payload)


def test_status_malformed_outbox_is_fixed_failure_without_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.outbox.path.parent.mkdir(parents=True)
    original = b"synthetic foreign bytes\n"
    config.outbox.path.write_bytes(original)
    os.chmod(config.outbox.path, 0o640)
    before = config.outbox.path.stat()

    result = status(config, now=2_000.0)

    _assert_result(
        result,
        payload={"command": "status", "error": "status_unavailable", "result": "error"},
        exit_code=3,
    )
    after = config.outbox.path.stat()
    assert config.outbox.path.read_bytes() == original
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o640
    assert after.st_mtime_ns == before.st_mtime_ns
    assert not Path(f"{config.outbox.path}.lock").exists()


def test_status_rejects_database_identity_replacement_after_read_only_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    displaced = config.outbox.path.with_name("displaced.sqlite3")
    replacement = b"synthetic replacement must remain untouched\n"
    original_connect = sqlite3.connect

    def replacing_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(database, *args, **kwargs)
        config.outbox.path.rename(displaced)
        config.outbox.path.write_bytes(replacement)
        os.chmod(config.outbox.path, 0o640)
        return cast(sqlite3.Connection, connection)

    monkeypatch.setattr(sqlite3, "connect", replacing_connect)

    result = status(config, now=2_000.0)

    _assert_result(
        result,
        payload={"command": "status", "error": "status_unavailable", "result": "error"},
        exit_code=3,
    )
    assert config.outbox.path.read_bytes() == replacement
    assert stat.S_IMODE(config.outbox.path.stat().st_mode) == 0o640
    assert displaced.is_file()


def test_status_rejects_sidecar_transition_after_read_only_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _initialize_outbox(config)
    _wal, _shm, journal = _sqlite_sidecars(config)
    original_connect = sqlite3.connect
    marker = b"synthetic concurrent rollback journal\n"

    def transitioning_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(database, *args, **kwargs)
        journal.write_bytes(marker)
        return cast(sqlite3.Connection, connection)

    monkeypatch.setattr(sqlite3, "connect", transitioning_connect)
    result = status(config, now=2_000.0)

    _assert_result(
        result,
        payload={"command": "status", "error": "status_unavailable", "result": "error"},
        exit_code=3,
    )
    assert journal.read_bytes() == marker


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_status_missing_existing_lock_is_free_for_ready_outbox(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lock_path = _initialize_outbox(config)
    lock_path.unlink()

    result = status(config, now=2_000.0)

    _assert_result(
        result,
        payload=_empty_status_payload(outbox="ready", ownership="free"),
        exit_code=0,
    )
    assert not lock_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_status_sender_ownership_probe_reports_held_without_waiting(tmp_path: Path) -> None:
    import fcntl

    config = _config(tmp_path)
    lock_path = _initialize_outbox(config)
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = status(config, now=2_000.0)

        assert result.exit_code == 0
        assert result.payload["sender_ownership"] == "held"
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="sender ownership is POSIX-only")
def test_status_sender_probe_error_is_bounded_unavailable_not_snapshot_failure(
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    config = _config(home)
    lock_path = _initialize_outbox(config)
    outside = tmp_path / "outside.lock"
    outside.write_text("synthetic lock target\n", encoding="utf-8")
    lock_path.unlink()
    lock_path.symlink_to(outside)

    result = status(config, now=2_000.0)

    _assert_result(
        result,
        payload=_empty_status_payload(outbox="ready", ownership="unavailable"),
        exit_code=0,
    )
    assert lock_path.is_symlink()
    assert outside.read_text(encoding="utf-8") == "synthetic lock target\n"


# Mission check ----------------------------------------------------------------


def test_operator_runtime_is_client_only_even_when_retention_is_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "single_principal": True,
            "retain": {"enabled": True},
        },
    )

    class ClosableClient:
        async def close(self) -> None:
            events.append("close")

    def client_factory(_config: BetterHindsightConfig) -> Any:
        events.append("client")
        return ClosableClient()

    def forbidden_outbox(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("operator runtime opened the retention outbox")

    monkeypatch.setattr(SQLiteOutbox, "open", forbidden_outbox)

    runtime = create_operator_runtime(config, client_factory=client_factory)
    assert not config.outbox.path.exists()
    assert runtime.finalize() is True
    assert events == ["client", "close"]


def test_mission_check_requires_principal_assertion_before_runtime_creation(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        single_principal=False,
        retain_mission=_RETAIN_DESIRED,
        observations_mission=_OBSERVATIONS_DESIRED,
    )
    factory, runtime, events = _runtime_factory(
        gets=[_snapshot(_value(_RETAIN_DESIRED), _value(_OBSERVATIONS_DESIRED))]
    )

    result = check_missions(config, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_check",
            "error": "authorization_required",
            "result": "error",
        },
        exit_code=3,
    )
    assert events == []
    assert factory.configs == []
    assert runtime.timeouts == []


@pytest.mark.parametrize("configured_count", [0, 1, 2])
def test_mission_check_handles_zero_one_or_two_configured_fields(
    tmp_path: Path,
    configured_count: int,
) -> None:
    retain_desired = _RETAIN_DESIRED if configured_count >= 1 else None
    observations_desired = _OBSERVATIONS_DESIRED if configured_count >= 2 else None
    config = _config(
        tmp_path,
        retain_mission=retain_desired,
        observations_mission=observations_desired,
    )
    factory, runtime, events = _runtime_factory(
        gets=[_snapshot(_value(_RETAIN_DESIRED), _value(_OBSERVATIONS_DESIRED))]
    )

    result = check_missions(config, runtime_factory=factory)

    expected_retain = "equal" if configured_count >= 1 else "missing"
    expected_observations = "equal" if configured_count >= 2 else "missing"
    expected_result = "equal" if configured_count == 2 else "missing"
    _assert_result(
        result,
        payload={
            "command": "missions_check",
            "observations_mission": expected_observations,
            "result": expected_result,
            "retain_mission": expected_retain,
        },
        exit_code=0 if configured_count == 2 else 1,
    )
    assert events == ["runtime_create", "get", "runtime_finalize"]
    assert factory.configs == [config]
    _assert_remote_deadlines(runtime, count=1, maximum=config.retain.timeout_seconds)


@pytest.mark.parametrize("configured_count", [0, 1, 2])
def test_failed_mission_get_forces_operation_error_for_every_configured_count(
    tmp_path: Path,
    configured_count: int,
) -> None:
    config = _config(
        tmp_path,
        retain_mission=_RETAIN_DESIRED if configured_count >= 1 else None,
        observations_mission=_OBSERVATIONS_DESIRED if configured_count >= 2 else None,
    )
    failure = HindsightClientError(
        "bank_config_failed",
        "Better Hindsight bank configuration read failed.",
    )
    factory, runtime, events = _runtime_factory(gets=[failure])

    result = check_missions(config, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_check",
            "observations_mission": "error" if configured_count >= 2 else "missing",
            "result": "error",
            "retain_mission": "error" if configured_count >= 1 else "missing",
        },
        exit_code=3,
    )
    assert events == ["runtime_create", "get", "runtime_finalize"]
    _assert_remote_deadlines(runtime, count=1, maximum=config.retain.timeout_seconds)


@pytest.mark.parametrize(
    ("remote_retain", "remote_observations", "retain_state", "observations_state", "overall"),
    [
        (
            _value(_RETAIN_REMOTE),
            _value(_OBSERVATIONS_DESIRED),
            "drift",
            "equal",
            "drift",
        ),
        (
            _value(None),
            _value(_OBSERVATIONS_REMOTE),
            "missing",
            "drift",
            "missing",
        ),
    ],
)
def test_mission_check_uses_frozen_field_precedence(
    tmp_path: Path,
    remote_retain: MissionValue,
    remote_observations: MissionValue,
    retain_state: str,
    observations_state: str,
    overall: str,
) -> None:
    config = _config(
        tmp_path,
        retain_mission=_RETAIN_DESIRED,
        observations_mission=_OBSERVATIONS_DESIRED,
    )
    factory, _runtime, _events = _runtime_factory(
        gets=[_snapshot(remote_retain, remote_observations)]
    )

    result = check_missions(config, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_check",
            "observations_mission": observations_state,
            "result": overall,
            "retain_mission": retain_state,
        },
        exit_code=1,
    )


@pytest.mark.parametrize(
    "remote",
    [
        _value(None, present=False),
        _value(None),
        _value(""),
        _value(" \t\n"),
    ],
    ids=["absent", "null", "empty", "blank"],
)
def test_mission_check_classifies_every_remote_missing_form(
    tmp_path: Path,
    remote: MissionValue,
) -> None:
    config = _config(
        tmp_path,
        retain_mission=_RETAIN_DESIRED,
        observations_mission=_OBSERVATIONS_DESIRED,
    )
    factory, _runtime, _events = _runtime_factory(
        gets=[_snapshot(remote, _value(_OBSERVATIONS_DESIRED))]
    )

    result = check_missions(config, runtime_factory=factory)

    assert result.exit_code == 1
    assert result.payload == {
        "command": "missions_check",
        "observations_mission": "equal",
        "result": "missing",
        "retain_mission": "missing",
    }


def test_mission_check_compares_nonblank_text_byte_for_byte(tmp_path: Path) -> None:
    desired = "  Preserve these outer spaces exactly.  "
    config = _config(
        tmp_path,
        retain_mission=desired,
        observations_mission=_OBSERVATIONS_DESIRED,
    )
    factory, _runtime, _events = _runtime_factory(
        gets=[_snapshot(_value(desired.strip()), _value(_OBSERVATIONS_DESIRED))]
    )

    result = check_missions(config, runtime_factory=factory)

    assert result.exit_code == 1
    assert result.payload["retain_mission"] == "drift"
    assert desired not in repr(result.payload)


def test_mission_check_runtime_construction_failure_is_fixed_and_sanitized(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC_RUNTIME_CONSTRUCTION_DETAIL"
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    factory, runtime, events = _runtime_factory(
        gets=[],
        creation_error=_SyntheticFailure(marker),
    )

    result = check_missions(config, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_check",
            "error": "mission_check_unavailable",
            "result": "error",
        },
        exit_code=3,
    )
    assert marker not in repr(result.payload)
    assert events == ["runtime_create"]
    assert runtime.timeouts == []


def test_mission_check_cleanup_failure_overrides_completed_check(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC_CHECK_CLEANUP_DETAIL"
    config = _config(
        tmp_path,
        retain_mission=_RETAIN_DESIRED,
        observations_mission=_OBSERVATIONS_DESIRED,
    )
    factory, _runtime, events = _runtime_factory(
        gets=[_snapshot(_value(_RETAIN_DESIRED), _value(_OBSERVATIONS_DESIRED))],
        finalize_error=_SyntheticFailure(marker),
    )

    result = check_missions(config, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_check",
            "error": "runtime_cleanup_failed",
            "result": "error",
        },
        exit_code=3,
    )
    assert marker not in repr(result.payload)
    assert events == ["runtime_create", "get", "runtime_finalize"]


# Mission apply ----------------------------------------------------------------


def test_mission_apply_requires_principal_assertion_before_runtime_creation(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        single_principal=False,
        retain_mission=_RETAIN_DESIRED,
    )
    factory, runtime, events = _runtime_factory(gets=[])

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "error": "authorization_required",
            "result": "error",
        },
        exit_code=3,
    )
    assert events == []
    assert factory.configs == []
    assert runtime.timeouts == []


def test_mission_apply_refuses_unconfirmed_direct_invocation_before_runtime(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    factory, runtime, events = _runtime_factory(gets=[])

    with pytest.raises(PermissionError):
        apply_missions(config, confirmed=False, runtime_factory=factory)

    assert events == []
    assert factory.configs == []
    assert runtime.timeouts == []


def test_mission_apply_with_no_desired_fields_performs_no_remote_work(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    factory, runtime, events = _runtime_factory(gets=[])

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "error": "mission_configuration_missing",
            "result": "error",
        },
        exit_code=3,
    )
    assert events == []
    assert factory.configs == []
    assert runtime.timeouts == []


def test_mission_apply_runtime_construction_failure_is_prewrite_failure(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC_APPLY_CONSTRUCTION_DETAIL"
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    factory, runtime, events = _runtime_factory(
        gets=[],
        creation_error=_SyntheticFailure(marker),
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "error": "mission_prewrite_unavailable",
            "result": "error",
        },
        exit_code=3,
    )
    assert marker not in repr(result.payload)
    assert events == ["runtime_create"]
    assert runtime.timeouts == []


def test_mission_apply_failed_preread_is_not_reported_as_ambiguous_write(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC_PREWRITE_GET_DETAIL"
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    factory, runtime, events = _runtime_factory(gets=[_SyntheticFailure(marker)])

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "error": "mission_prewrite_unavailable",
            "result": "error",
        },
        exit_code=3,
    )
    assert marker not in repr(result.payload)
    assert events == ["runtime_create", "get", "runtime_finalize"]
    _assert_remote_deadlines(runtime, count=1, maximum=config.retain.timeout_seconds)


def test_mission_apply_deadline_expiry_before_patch_is_prewrite_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    before = _snapshot(_value(_RETAIN_REMOTE), _value(None, present=False))
    factory, runtime, events = _runtime_factory(gets=[before])
    monotonic_values = iter((100.0, 100.0, 100.0 + config.retain.timeout_seconds))
    monkeypatch.setattr(
        management_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "error": "mission_prewrite_unavailable",
            "result": "error",
        },
        exit_code=3,
    )
    assert events == ["runtime_create", "get", "runtime_finalize"]
    _assert_remote_deadlines(runtime, count=1, maximum=config.retain.timeout_seconds)


def test_mission_apply_already_equal_uses_one_get_and_no_patch_or_readback(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        retain_mission=_RETAIN_DESIRED,
        observations_mission=_OBSERVATIONS_DESIRED,
    )
    current = _snapshot(_value(_RETAIN_DESIRED), _value(_OBSERVATIONS_DESIRED))
    factory, runtime, events = _runtime_factory(gets=[current])

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "outcome": "already_equal",
            "result": "ok",
        },
        exit_code=0,
    )
    assert events == ["runtime_create", "get", "runtime_finalize"]
    _assert_remote_deadlines(runtime, count=1, maximum=config.retain.timeout_seconds)


def test_mission_apply_partial_configuration_patches_only_drift_and_preserves_unconfigured(
    tmp_path: Path,
) -> None:
    desired = "  Keep exact configured mission bytes.  "
    untouched = _value("Preserve this unconfigured remote mission exactly.")
    config = _config(tmp_path, retain_mission=desired)
    before = _snapshot(_value(_RETAIN_REMOTE), untouched)
    after_patch = _snapshot(_value(desired), untouched)
    readback = _snapshot(_value(desired), untouched)
    factory, runtime, events = _runtime_factory(
        gets=[before, readback],
        patches=[after_patch],
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "outcome": "verified_success",
            "result": "ok",
        },
        exit_code=0,
    )
    assert events == [
        "runtime_create",
        "get",
        ("patch", {"retain_mission": desired}),
        "get",
        "runtime_finalize",
    ]
    _assert_remote_deadlines(runtime, count=3, maximum=config.retain.timeout_seconds)
    assert desired not in repr(result.payload)
    assert untouched.value is not None
    assert untouched.value not in repr(result.payload)


def test_mission_apply_one_patch_contains_only_the_configured_drifted_subset(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        retain_mission=_RETAIN_DESIRED,
        observations_mission=_OBSERVATIONS_DESIRED,
    )
    before = _snapshot(_value(_RETAIN_DESIRED), _value(_OBSERVATIONS_REMOTE))
    after = _snapshot(_value(_RETAIN_DESIRED), _value(_OBSERVATIONS_DESIRED))
    factory, runtime, events = _runtime_factory(gets=[before, after], patches=[after])

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    assert result.exit_code == 0
    assert result.payload["outcome"] == "verified_success"
    assert events == [
        "runtime_create",
        "get",
        ("patch", {"observations_mission": _OBSERVATIONS_DESIRED}),
        "get",
        "runtime_finalize",
    ]
    _assert_remote_deadlines(runtime, count=3, maximum=config.retain.timeout_seconds)


def test_mission_apply_patches_remotely_missing_configured_field_once(
    tmp_path: Path,
) -> None:
    absent = _value(None, present=False)
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    before = _snapshot(absent, absent)
    after = _snapshot(_value(_RETAIN_DESIRED), absent)
    factory, _runtime, events = _runtime_factory(gets=[before, after], patches=[after])

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    assert result.exit_code == 0
    assert events.count(("patch", {"retain_mission": _RETAIN_DESIRED})) == 1
    assert events.count("get") == 2


def test_mission_apply_patch_failure_is_write_attempted_outcome_unknown(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC_PATCH_FAILURE_DETAIL"
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    before = _snapshot(_value(_RETAIN_REMOTE), _value(None, present=False))
    factory, runtime, events = _runtime_factory(
        gets=[before],
        patches=[_SyntheticFailure(marker)],
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "outcome": "write_attempted_outcome_unknown",
            "result": "error",
        },
        exit_code=4,
    )
    assert marker not in repr(result.payload)
    assert events == [
        "runtime_create",
        "get",
        ("patch", {"retain_mission": _RETAIN_DESIRED}),
        "runtime_finalize",
    ]
    _assert_remote_deadlines(runtime, count=2, maximum=config.retain.timeout_seconds)


@pytest.mark.parametrize(
    "mutated_untouched",
    [
        _value(None, present=False),
        _value("Unexpected mutation of untouched mission."),
    ],
    ids=["presence-changed", "value-changed"],
)
def test_mission_apply_rejects_patch_response_that_changes_untouched_field_without_readback(
    tmp_path: Path,
    mutated_untouched: MissionValue,
) -> None:
    untouched = _value("Preserve untouched mission exactly.")
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    before = _snapshot(_value(_RETAIN_REMOTE), untouched)
    invalid_patch = _snapshot(_value(_RETAIN_DESIRED), mutated_untouched)
    factory, _runtime, events = _runtime_factory(gets=[before], patches=[invalid_patch])

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "outcome": "write_attempted_outcome_unknown",
            "result": "error",
        },
        exit_code=4,
    )
    assert events == [
        "runtime_create",
        "get",
        ("patch", {"retain_mission": _RETAIN_DESIRED}),
        "runtime_finalize",
    ]


@pytest.mark.parametrize(
    "readback",
    [
        _snapshot(_value(_RETAIN_REMOTE), _value("Preserve untouched mission exactly.")),
        _snapshot(_value(_RETAIN_DESIRED), _value("Mutated after PATCH.")),
    ],
    ids=["changed-field-mismatch", "untouched-field-mismatch"],
)
def test_mission_apply_failed_exact_readback_never_retries_or_rolls_back(
    tmp_path: Path,
    readback: MissionSnapshot,
) -> None:
    untouched = _value("Preserve untouched mission exactly.")
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    before = _snapshot(_value(_RETAIN_REMOTE), untouched)
    valid_patch = _snapshot(_value(_RETAIN_DESIRED), untouched)
    factory, _runtime, events = _runtime_factory(
        gets=[before, readback],
        patches=[valid_patch],
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    assert result.payload == {
        "command": "missions_apply",
        "outcome": "write_attempted_outcome_unknown",
        "result": "error",
    }
    assert result.exit_code == 4
    assert events == [
        "runtime_create",
        "get",
        ("patch", {"retain_mission": _RETAIN_DESIRED}),
        "get",
        "runtime_finalize",
    ]


def test_mission_apply_readback_transport_failure_is_unknown_without_second_patch(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC_READBACK_FAILURE_DETAIL"
    untouched = _value(None, present=False)
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    before = _snapshot(_value(_RETAIN_REMOTE), untouched)
    valid_patch = _snapshot(_value(_RETAIN_DESIRED), untouched)
    factory, _runtime, events = _runtime_factory(
        gets=[before, _SyntheticFailure(marker)],
        patches=[valid_patch],
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    assert result.exit_code == 4
    assert result.payload["outcome"] == "write_attempted_outcome_unknown"
    assert marker not in repr(result.payload)
    assert sum(isinstance(event, tuple) and event[0] == "patch" for event in events) == 1
    assert events.count("get") == 2


def test_mission_apply_prewrite_cleanup_failure_has_fixed_exit_three(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC_PREWRITE_CLEANUP_DETAIL"
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    factory, _runtime, events = _runtime_factory(
        gets=[_SyntheticFailure("synthetic preread failure")],
        finalize_error=_SyntheticFailure(marker),
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "error": "runtime_cleanup_failed",
            "result": "error",
        },
        exit_code=3,
    )
    assert marker not in repr(result.payload)
    assert events == ["runtime_create", "get", "runtime_finalize"]


def test_mission_apply_noop_cleanup_failure_has_fixed_exit_three(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    current = _snapshot(_value(_RETAIN_DESIRED), _value(None, present=False))
    factory, _runtime, events = _runtime_factory(
        gets=[current],
        finalize_error=_SyntheticFailure("synthetic cleanup failure"),
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "error": "runtime_cleanup_failed",
            "result": "error",
        },
        exit_code=3,
    )
    assert events == ["runtime_create", "get", "runtime_finalize"]


def test_mission_apply_postwrite_cleanup_failure_is_ambiguous_exit_four(
    tmp_path: Path,
) -> None:
    untouched = _value(None, present=False)
    config = _config(tmp_path, retain_mission=_RETAIN_DESIRED)
    before = _snapshot(_value(_RETAIN_REMOTE), untouched)
    after = _snapshot(_value(_RETAIN_DESIRED), untouched)
    factory, _runtime, events = _runtime_factory(
        gets=[before, after],
        patches=[after],
        finalize_error=_SyntheticFailure("synthetic postwrite cleanup failure"),
    )

    result = apply_missions(config, confirmed=True, runtime_factory=factory)

    _assert_result(
        result,
        payload={
            "command": "missions_apply",
            "outcome": "write_attempted_outcome_unknown",
            "result": "error",
        },
        exit_code=4,
    )
    assert events == [
        "runtime_create",
        "get",
        ("patch", {"retain_mission": _RETAIN_DESIRED}),
        "get",
        "runtime_finalize",
    ]
