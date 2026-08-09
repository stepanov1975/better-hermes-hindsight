"""Pinned released-Hermes proof of asynchronous Task 2 local admission."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

import pytest
from agent.memory_manager import MemoryManager

from better_hermes_hindsight.client import (
    RetainConfirmation,
)
from better_hermes_hindsight.client import (
    RetainSegment as ClientRetainSegment,
)
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.outbox import OutboxRow, SQLiteOutbox
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.runtime import (
    acquire_process_runtime,
    finalize_process_runtime,
    reset_process_runtime_for_tests,
)
from tests.hermes_compat import assert_selected_hermes

RAW_MESSAGE_SENTINEL = "synthetic-raw-message-must-be-ignored"


class _NoNetworkClient:
    def __init__(self) -> None:
        self.created_loop = asyncio.get_running_loop()
        self.operation_calls: list[str] = []
        self.close_calls = 0

    async def get_server_version(self) -> object:
        self.operation_calls.append("version")
        return object()

    async def recall(self, query: str) -> object:
        self.operation_calls.append(f"recall:{query}")
        return object()

    async def retain_segment(self, segment: ClientRetainSegment) -> RetainConfirmation:
        self.operation_calls.append(f"retain:{segment.document_id}")
        return RetainConfirmation(confirmed=True)

    async def get_bank_profile(self) -> object:
        self.operation_calls.append("profile")
        return object()

    async def get_bank_config(self) -> object:
        self.operation_calls.append("config")
        return object()

    async def update_bank_missions(self, updates: Mapping[str, str]) -> object:
        self.operation_calls.append(f"missions:{len(updates)}")
        return object()

    async def create_disposable_bank(
        self, bank_id: str, *, confirm_disposable: bool = False
    ) -> object:
        self.operation_calls.append(f"create:{bank_id}:{confirm_disposable}")
        return object()

    async def delete_disposable_bank(
        self, bank_id: str, *, confirm_disposable: bool = False
    ) -> object:
        self.operation_calls.append(f"delete:{bank_id}:{confirm_disposable}")
        return object()

    async def close(self) -> None:
        assert asyncio.get_running_loop() is self.created_loop
        self.close_calls += 1


class _NoNetworkClientFactory:
    def __init__(self) -> None:
        self.clients: list[_NoNetworkClient] = []

    def __call__(self, _config: BetterHindsightConfig) -> _NoNetworkClient:
        client = _NoNetworkClient()
        self.clients.append(client)
        return client


class _InertSender:
    def start(self) -> None:
        return None

    def wake(self) -> None:
        return None

    def request_stop(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> bool:
        del timeout
        return True


def _inert_sender_factory(
    _config: BetterHindsightConfig,
    _outbox: object,
    _client: object,
    _runner: object,
) -> _InertSender:
    return _InertSender()


def _write_retain_only_profile(hermes_home: Path) -> BetterHindsightConfig:
    config_dir = hermes_home / "better_hindsight"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "api_url": "http://127.0.0.1:9",
                "bank_id": "synthetic-bank",
                "single_principal": True,
                "recall": {"enabled": False},
                "retain": {
                    "enabled": True,
                    "segment_max_bytes": 128,
                    "tags": ["project:synthetic"],
                },
                "outbox": {
                    "max_pending_rows": 100,
                    "max_pending_bytes": 1_000_000,
                    "busy_timeout_seconds": 0.2,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return load_config(hermes_home, environ={})


def _read_rows(config: BetterHindsightConfig) -> tuple[OutboxRow, ...]:
    inspector = SQLiteOutbox.open(config)
    try:
        return inspector.read_unconfirmed()
    finally:
        inspector.close()


def _forbid_network(_socket: socket.socket, _address: object) -> NoReturn:
    raise AssertionError("Task 2 callback attempted a network connection")


def test_released_memory_manager_runs_callback_asynchronously_before_local_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Released Hermes invokes ``sync_turn`` from its own background executor.

    Better Hindsight durability begins only after that callback's complete SQLite transaction
    commits. A callback cancelled, never run, or lost before commit remains outside the guarantee.
    """

    assert_selected_hermes()

    reset_process_runtime_for_tests()
    hermes_home = tmp_path / "hermes-home"
    config = _write_retain_only_profile(hermes_home)
    client_factory = _NoNetworkClientFactory()
    bootstrap = acquire_process_runtime(
        config,
        client_factory=client_factory,
        sender_factory=_inert_sender_factory,
    )
    monkeypatch.setattr(socket.socket, "connect", _forbid_network)

    callback_started = threading.Event()
    callback_release = threading.Event()
    sync_returned = threading.Event()
    original_sync_turn = BetterHindsightMemoryProvider.sync_turn

    def gated_sync_turn(
        self: BetterHindsightMemoryProvider,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        callback_started.set()
        if not callback_release.wait(timeout=3.0):
            raise AssertionError("released callback gate was not released")
        original_sync_turn(
            self,
            user_content,
            assistant_content,
            session_id=session_id,
            messages=messages,
        )

    monkeypatch.setattr(BetterHindsightMemoryProvider, "sync_turn", gated_sync_turn)
    provider = BetterHindsightMemoryProvider()
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all(
        "initial-session",
        hermes_home=str(hermes_home),
        platform="cli",
        agent_context="primary",
    )
    assert [registered.name for registered in manager.providers] == ["better_hindsight"]
    assert provider.prefetch("retain-only provider has no recall") == ""
    assert provider.get_tool_schemas() == []

    caller_errors: list[BaseException] = []

    def invoke_sync_all() -> None:
        try:
            manager.sync_all(
                "synthetic direct user",
                "synthetic direct assistant",
                session_id="released-callback-session",
                messages=[
                    {"role": "user", "content": RAW_MESSAGE_SENTINEL},
                    {"role": "assistant", "content": RAW_MESSAGE_SENTINEL},
                ],
            )
        except BaseException as error:
            caller_errors.append(error)
        finally:
            sync_returned.set()

    caller = threading.Thread(target=invoke_sync_all)
    try:
        caller.start()
        assert callback_started.wait(timeout=2.0)
        assert sync_returned.wait(timeout=2.0)
        caller.join(timeout=2.0)
        assert caller.is_alive() is False
        assert caller_errors == []
        assert callback_release.is_set() is False
        assert _read_rows(config) == ()
        assert client_factory.clients[0].operation_calls == []

        callback_release.set()
        assert manager.flush_pending(timeout=2.0) is True

        rows = _read_rows(config)
        assert rows
        ordered = sorted(rows, key=lambda row: row.segment_index)
        assert len(ordered) == ordered[0].segment_count
        assert [row.segment_index for row in ordered] == list(range(len(ordered)))
        assert all(row.state == "pending" for row in ordered)
        canonical_source = "".join(row.content for row in ordered)
        decoded = json.loads(canonical_source)
        assert decoded["roles"] == [
            {"content": "synthetic direct user", "role": "user"},
            {"content": "synthetic direct assistant", "role": "assistant"},
        ]
        assert RAW_MESSAGE_SENTINEL not in canonical_source
        assert client_factory.clients[0].operation_calls == []
    finally:
        callback_release.set()
        caller.join(timeout=2.0)
        manager.flush_pending(timeout=2.0)
        manager.shutdown_all()
        bootstrap.close()
        finalize_process_runtime()

    assert client_factory.clients[0].operation_calls == []
    assert client_factory.clients[0].close_calls == 1
