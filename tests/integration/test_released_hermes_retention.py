"""Released-Hermes callback-to-real-SDK retention proofs against the loopback fake."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, TypeVar

import pytest
from agent.memory_manager import MemoryManager  # type: ignore[import-untyped]

from better_hermes_hindsight.client import HINDSIGHT_SDK_VERSION
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.outbox import OutboxRow, SQLiteOutbox
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.retention import build_retained_segments
from better_hermes_hindsight.runtime import (
    finalize_process_runtime,
    reset_process_runtime_for_tests,
)
from tests.fakes.hindsight_server import FakeHindsightServer, RequestRecord

RELEASE_COMMIT = "3ef6bbd201263d354fd83ec55b3c306ded2eb72a"
RELEASE_VERSION = "0.19.0"
FIXTURE_BANK_ID = "released-retention-fixture-bank"
FIXTURE_API_KEY = "synthetic-released-retention-api-key"
FIXTURE_ERROR_SENTINEL = "synthetic-released-retention-error"
FIXTURE_TAGS = ("kind:released-proof", "source:fixture")

_T = TypeVar("_T")


class _DedicatedLoop:
    """Own one asyncio loop on a dedicated thread for the aiohttp fake."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._serve,
            name="released-retention-fake-loop",
            daemon=True,
        )

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()
        if not self._started.wait(timeout=2.0):
            raise RuntimeError("Dedicated fake-server loop did not start.")

    def run(self, operation: Coroutine[Any, Any, _T], *, timeout: float = 3.0) -> _T:
        future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        try:
            return future.result(timeout=timeout)
        except BaseException:
            future.cancel()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3.0)
        if self._thread.is_alive() or not self._stopped.is_set():
            raise RuntimeError("Dedicated fake-server loop did not stop.")

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._stopped.set()


@dataclass(frozen=True, slots=True)
class _RetentionHarness:
    config: BetterHindsightConfig
    loop: _DedicatedLoop
    manager: MemoryManager
    server: FakeHindsightServer


async def _start_server() -> FakeHindsightServer:
    server = FakeHindsightServer(
        bank_id=FIXTURE_BANK_ID,
        disposable_bank_id="disposable-released-retention-fixture",
        error_sentinel=FIXTURE_ERROR_SENTINEL,
        expected_api_key=FIXTURE_API_KEY,
    )
    await server.start()
    return server


async def _arm_retain_delay(server: FakeHindsightServer) -> None:
    server.arm_retain_fault("delay")


async def _wait_for_retain_delay(server: FakeHindsightServer) -> None:
    await server.wait_for_retain_delay_entered()


async def _release_retain_delay(server: FakeHindsightServer) -> None:
    server.release_retain_delay()
    await server.wait_for_retain_delay_finished()


async def _retain_records(server: FakeHindsightServer) -> tuple[RequestRecord, ...]:
    return tuple(
        record
        for record in server.records
        if record.method == "POST" and record.path.endswith("/memories")
    )


async def _close_server(server: FakeHindsightServer) -> None:
    await server.close()


def _assert_pinned_release_identity() -> None:
    release = metadata.distribution("hermes-agent")
    assert release.version == RELEASE_VERSION
    direct_url_text = release.read_text("direct_url.json")
    assert direct_url_text is not None
    assert json.loads(direct_url_text)["vcs_info"]["commit_id"] == RELEASE_COMMIT
    assert MemoryManager.__module__ == "agent.memory_manager"

    source = inspect.getsourcefile(MemoryManager)
    files = release.files
    assert source is not None
    assert files is not None
    source_entry = next(entry for entry in files if str(entry) == "agent/memory_manager.py")
    assert Path(source).resolve() == Path(str(release.locate_file(source_entry))).resolve()
    assert metadata.version("hindsight-client") == HINDSIGHT_SDK_VERSION


def _isolate_environment(
    monkeypatch: pytest.MonkeyPatch,
    hermes_home: Path,
) -> None:
    for name in ("HINDSIGHT_API_URL", "HINDSIGHT_API_KEY", "HINDSIGHT_BANK_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HINDSIGHT_API_KEY", FIXTURE_API_KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _write_profile(
    hermes_home: Path,
    *,
    base_url: str,
    retain_timeout_seconds: float,
    retry_initial_seconds: float,
) -> BetterHindsightConfig:
    config_dir = hermes_home / "better_hindsight"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "api_url": base_url,
                "bank_id": FIXTURE_BANK_ID,
                "single_principal": True,
                "recall": {"enabled": False},
                "retain": {
                    "enabled": True,
                    "timeout_seconds": retain_timeout_seconds,
                    "segment_max_bytes": 4096,
                    "tags": list(FIXTURE_TAGS),
                },
                "outbox": {
                    "max_pending_rows": 100,
                    "max_pending_bytes": 1_000_000,
                    "busy_timeout_seconds": 0.2,
                    "poll_interval_seconds": 0.1,
                    "retry_initial_seconds": retry_initial_seconds,
                    "retry_max_seconds": retry_initial_seconds,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return load_config(
        hermes_home,
        environ={"HINDSIGHT_API_KEY": FIXTURE_API_KEY},
    )


@contextlib.contextmanager
def _released_retention_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    retain_timeout_seconds: float,
    retry_initial_seconds: float,
) -> Iterator[_RetentionHarness]:
    _assert_pinned_release_identity()
    reset_process_runtime_for_tests()

    hermes_home = tmp_path / "hermes-home"
    _isolate_environment(monkeypatch, hermes_home)
    loop = _DedicatedLoop()
    loop.start()
    server = loop.run(_start_server())
    manager = MemoryManager()
    finalized = False
    try:
        config = _write_profile(
            hermes_home,
            base_url=server.base_url,
            retain_timeout_seconds=retain_timeout_seconds,
            retry_initial_seconds=retry_initial_seconds,
        )
        provider = BetterHindsightMemoryProvider()
        manager.add_provider(provider)
        manager.initialize_all(
            "released-retention-initial-session",
            hermes_home=str(hermes_home),
            platform="cli",
            agent_context="primary",
        )
        assert [registered.name for registered in manager.providers] == ["better_hindsight"]
        assert provider.get_tool_schemas() == []

        yield _RetentionHarness(config=config, loop=loop, manager=manager, server=server)
    finally:
        try:
            loop.run(_release_retain_delay(server))
            manager.shutdown_all()
            finalized = finalize_process_runtime()
        finally:
            try:
                loop.run(_close_server(server))
            finally:
                loop.close()

    assert finalized is True
    assert finalize_process_runtime() is False
    assert server.closed is True
    assert loop.is_alive is False


def _read_rows(config: BetterHindsightConfig) -> tuple[OutboxRow, ...]:
    inspector = SQLiteOutbox.open(config)
    try:
        return inspector.read_unconfirmed()
    finally:
        inspector.close()


def _wait_for_rows(
    config: BetterHindsightConfig,
    predicate: Callable[[tuple[OutboxRow, ...]], bool],
    *,
    timeout: float = 3.0,
) -> tuple[OutboxRow, ...]:
    deadline = time.monotonic() + timeout
    while True:
        rows = _read_rows(config)
        if predicate(rows):
            return rows
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("Timed out waiting for the expected durable outbox state.")
        threading.Event().wait(timeout=min(0.02, remaining))


def _request_identity(record: RequestRecord) -> tuple[str, str]:
    body = record.json_body
    assert isinstance(body, dict)
    assert body.get("async") is False
    items = body.get("items")
    assert isinstance(items, list) and len(items) == 1
    item = items[0]
    assert isinstance(item, dict)
    assert item.get("update_mode") == "replace"
    document_id = item.get("document_id")
    content = item.get("content")
    assert isinstance(document_id, str)
    assert isinstance(content, str)
    return document_id, content


def _expected_segment(
    *,
    session_id: str,
    user_content: str,
    assistant_content: str,
) -> tuple[str, str]:
    segments = build_retained_segments(
        session_id=session_id,
        user_content=user_content,
        assistant_content=assistant_content,
        tags=FIXTURE_TAGS,
        segment_max_bytes=4096,
    )
    assert len(segments) == 1
    return segments[0].document_id, segments[0].content


def test_released_callback_flush_finishes_while_real_sdk_response_is_gated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _released_retention_harness(
        tmp_path,
        monkeypatch,
        retain_timeout_seconds=2.0,
        retry_initial_seconds=0.2,
    ) as harness:
        session_id = "released-gated-session"
        user_content = "synthetic released user turn"
        assistant_content = "synthetic released assistant turn"
        expected_identity = _expected_segment(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
        )

        harness.loop.run(_arm_retain_delay(harness.server))
        harness.manager.sync_all(
            user_content,
            assistant_content,
            session_id=session_id,
        )
        assert harness.manager.flush_pending(timeout=2.0) is True
        harness.loop.run(_wait_for_retain_delay(harness.server))

        rows = _read_rows(harness.config)
        assert len(rows) == 1
        assert rows[0].state == "sending"
        assert rows[0].attempt_count == 1
        assert (rows[0].document_id, rows[0].content) == expected_identity

        records = harness.loop.run(_retain_records(harness.server))
        assert len(records) == 1
        assert records[0].authorization == "valid_bearer"
        assert _request_identity(records[0]) == expected_identity

        harness.loop.run(_release_retain_delay(harness.server))
        assert _wait_for_rows(harness.config, lambda current: not current) == ()
        assert harness.loop.run(_retain_records(harness.server)) == records


def test_released_response_loss_replays_same_persisted_identity_until_typed_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _released_retention_harness(
        tmp_path,
        monkeypatch,
        retain_timeout_seconds=0.15,
        retry_initial_seconds=0.5,
    ) as harness:
        session_id = "released-response-loss-session"
        user_content = "synthetic response-loss user turn"
        assistant_content = "synthetic response-loss assistant turn"
        expected_identity = _expected_segment(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
        )

        harness.loop.run(_arm_retain_delay(harness.server))
        harness.manager.sync_all(
            user_content,
            assistant_content,
            session_id=session_id,
        )
        assert harness.manager.flush_pending(timeout=2.0) is True
        harness.loop.run(_wait_for_retain_delay(harness.server))

        sending = _read_rows(harness.config)
        assert len(sending) == 1
        assert sending[0].state == "sending"
        assert (sending[0].document_id, sending[0].content) == expected_identity
        first_records = harness.loop.run(_retain_records(harness.server))
        assert len(first_records) == 1
        assert _request_identity(first_records[0]) == expected_identity

        pending = _wait_for_rows(
            harness.config,
            lambda current: (
                len(current) == 1
                and current[0].state == "pending"
                and current[0].last_error_category == "retain_timeout"
            ),
        )
        assert pending[0].attempt_count == 1
        assert (pending[0].document_id, pending[0].content) == expected_identity

        harness.loop.run(_release_retain_delay(harness.server))
        assert _wait_for_rows(harness.config, lambda current: not current) == ()
        replay_records = harness.loop.run(_retain_records(harness.server))
        assert len(replay_records) == 2
        assert [_request_identity(record) for record in replay_records] == [
            expected_identity,
            expected_identity,
        ]
        assert all(record.authorization == "valid_bearer" for record in replay_records)
