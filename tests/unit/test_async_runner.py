"""Unit tests for the shared process runtime and sync-to-async runner."""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import weakref
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from better_hermes_hindsight.client import RetainSegment
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.runtime import (
    AsyncCallTimeoutError,
    AsyncRunner,
    AsyncRunnerReentrancyError,
    RuntimeConfigurationConflict,
    acquire_process_runtime,
    finalize_process_runtime,
    reset_process_runtime_for_tests,
)


class _FakeClient:
    def __init__(self) -> None:
        self.created_loop = asyncio.get_running_loop()
        self.close_loops: list[asyncio.AbstractEventLoop] = []
        self.calls: list[str] = []

    async def recall(self, query: str) -> object:
        self.calls.append(f"recall:{query}")
        return {"query": query}

    async def get_server_version(self) -> object:
        self.calls.append("version")
        return object()

    async def retain_segment(self, segment: RetainSegment) -> object:
        self.calls.append(f"retain:{segment.document_id}")
        return {"document_id": segment.document_id}

    async def get_bank_profile(self) -> object:
        self.calls.append("profile")
        return object()

    async def get_bank_config(self) -> object:
        self.calls.append("config")
        return object()

    async def update_bank_missions(self, updates: Mapping[str, str]) -> object:
        self.calls.append(f"missions:{len(updates)}")
        return object()

    async def create_disposable_bank(
        self, bank_id: str, *, confirm_disposable: bool = False
    ) -> object:
        self.calls.append(f"create:{bank_id}:{confirm_disposable}")
        return object()

    async def delete_disposable_bank(
        self, bank_id: str, *, confirm_disposable: bool = False
    ) -> object:
        self.calls.append(f"delete:{bank_id}:{confirm_disposable}")
        return object()

    async def close(self) -> None:
        self.close_loops.append(asyncio.get_running_loop())


class _TimeoutThenSuccessClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.recall_attempts = 0
        self.cancelled = threading.Event()

    async def recall(self, query: str) -> object:
        self.recall_attempts += 1
        if self.recall_attempts == 1:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
        return {"query": query, "attempt": self.recall_attempts}


class _RecordingFactory:
    def __init__(self, client_type: type[_FakeClient] = _FakeClient) -> None:
        self.client_type = client_type
        self.clients: list[_FakeClient] = []
        self.loops: list[asyncio.AbstractEventLoop] = []

    def __call__(self, _config: BetterHindsightConfig) -> _FakeClient:
        self.loops.append(asyncio.get_running_loop())
        client = self.client_type()
        self.clients.append(client)
        return client


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    reset_process_runtime_for_tests()
    yield
    reset_process_runtime_for_tests()


def _config(
    tmp_path: Path,
    *,
    hermes_home: Path | None = None,
    injected: Mapping[str, object] | None = None,
    api_key: str = "sample-credential-one",
) -> BetterHindsightConfig:
    return load_config(
        hermes_home=tmp_path if hermes_home is None else hermes_home,
        environ={"HINDSIGHT_API_KEY": api_key},
        injected={} if injected is None else injected,
    )


def test_async_runner_timeout_cancels_task_and_next_call_succeeds() -> None:
    runner = AsyncRunner()
    cancelled = threading.Event()

    async def never_finishes() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def pending_task_count() -> int:
        return sum(not task.done() for task in asyncio.all_tasks())

    async def success() -> str:
        return "next-call-succeeded"

    try:
        started = time.monotonic()
        with pytest.raises(
            AsyncCallTimeoutError,
            match="Better Hindsight operation exceeded its total deadline",
        ):
            runner.run(never_finishes, timeout=0.03)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert cancelled.is_set()
        assert runner.run(pending_task_count, timeout=1.0) == 1
        assert runner.run(success, timeout=1.0) == "next-call-succeeded"
    finally:
        runner.shutdown()


def test_async_runner_timeout_does_not_wait_unbounded_for_slow_cancellation() -> None:
    runner = AsyncRunner()

    async def slow_cancellation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)

    try:
        started = time.monotonic()
        with pytest.raises(AsyncCallTimeoutError):
            runner.run(slow_cancellation, timeout=0.01)
        assert time.monotonic() - started < 0.15

        async def success() -> str:
            return "still-responsive"

        assert runner.run(success, timeout=0.1) == "still-responsive"
    finally:
        runner.shutdown()


def test_async_runner_rejects_owning_loop_reentrancy_without_creating_work() -> None:
    runner = AsyncRunner()
    nested_factory_called = False

    async def nested_result() -> str:
        nonlocal nested_factory_called
        nested_factory_called = True
        return "unexpected"

    async def reentrant_call() -> str:
        with pytest.raises(
            AsyncRunnerReentrancyError,
            match="cannot be called from its owning event loop",
        ):
            runner.run(nested_result, timeout=1.0)
        return "reentrancy-rejected"

    try:
        assert runner.run(reentrant_call, timeout=1.0) == "reentrancy-rejected"
        assert nested_factory_called is False
    finally:
        runner.shutdown()


def test_async_runner_propagates_asyncio_cancelled_error() -> None:
    runner = AsyncRunner()

    async def cancelled() -> None:
        raise asyncio.CancelledError

    try:
        with pytest.raises(asyncio.CancelledError):
            runner.run(cancelled, timeout=1.0)
    finally:
        runner.shutdown()


def test_process_runtime_rejects_reentrant_finalization_without_losing_ownership(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    factory = _RecordingFactory()
    handle = acquire_process_runtime(config, client_factory=factory)

    async def attempt_finalization() -> str:
        with pytest.raises(
            AsyncRunnerReentrancyError,
            match="cannot finalize from its owning event loop",
        ):
            finalize_process_runtime()
        return "rejected"

    assert handle.call(lambda _client: attempt_finalization(), timeout=1.0) == "rejected"
    assert factory.clients[0].close_loops == []
    assert finalize_process_runtime() is True
    assert factory.clients[0].close_loops == [factory.loops[0]]


def test_equal_profile_configs_share_one_runtime_loop_and_client_until_finalization(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    equal_config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    factory = _RecordingFactory()

    first = acquire_process_runtime(config, client_factory=factory)
    second = acquire_process_runtime(equal_config, client_factory=factory)
    runtime_ref = weakref.ref(first.runtime)

    assert first.runtime is second.runtime
    assert len(factory.clients) == 1
    assert len(factory.loops) == 1

    first.close()
    del first
    del second
    gc.collect()

    replacement = acquire_process_runtime(equal_config, client_factory=factory)
    assert replacement.runtime is runtime_ref()
    assert len(factory.clients) == 1
    assert factory.clients[0].close_loops == []

    async def current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    operation_loop = replacement.call(lambda _client: current_loop(), timeout=1.0)
    assert operation_loop is factory.loops[0]

    assert finalize_process_runtime() is True
    assert finalize_process_runtime() is False
    assert factory.clients[0].close_loops == [factory.loops[0]]


@pytest.mark.parametrize(
    "second_home, second_injected, second_api_key",
    [
        (None, {"bank_id": "different-bank"}, "sample-credential-one"),
        (
            None,
            {"api_url": "https://different.example.test", "bank_id": "sample-bank"},
            "sample-credential-one",
        ),
        (None, {"bank_id": "sample-bank"}, "sample-credential-two"),
        (
            None,
            {"bank_id": "sample-bank", "recall": {"budget": "low"}},
            "sample-credential-one",
        ),
        (
            Path("other-profile"),
            {"bank_id": "sample-bank"},
            "sample-credential-one",
        ),
        (
            None,
            {
                "bank_id": "sample-bank",
                "single_principal": True,
                "allowed_principals": [
                    {
                        "platform": "sample-platform",
                        "identifier_kind": "user_id",
                        "identifier": "sample-principal",
                    }
                ],
            },
            "sample-credential-one",
        ),
    ],
)
def test_conflicting_process_config_requires_restart_with_fixed_sanitized_diagnostic(
    tmp_path: Path,
    second_home: Path | None,
    second_injected: Mapping[str, object],
    second_api_key: str,
) -> None:
    first_config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    alternate_home = tmp_path / second_home if second_home is not None else tmp_path
    second_config = _config(
        tmp_path,
        hermes_home=alternate_home,
        injected=second_injected,
        api_key=second_api_key,
    )
    factory = _RecordingFactory()
    acquire_process_runtime(first_config, client_factory=factory)

    with pytest.raises(RuntimeConfigurationConflict) as caught:
        acquire_process_runtime(second_config, client_factory=factory)

    message = str(caught.value)
    assert message == "Better Hindsight process runtime configuration conflict; restart required."
    assert caught.value.__cause__ is None
    assert not any(
        forbidden in message.casefold()
        for forbidden in ("endpoint", "bank", "key", "path", "principal")
    )
    assert "sample-credential" not in message
    assert "different.example.test" not in message
    assert str(tmp_path) not in message
    assert len(factory.clients) == 1


def test_runtime_timeout_cancellation_has_no_pending_task_and_next_recall_succeeds(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    factory = _RecordingFactory(_TimeoutThenSuccessClient)
    handle = acquire_process_runtime(config, client_factory=factory)

    with pytest.raises(AsyncCallTimeoutError):
        handle.recall("first query", timeout=0.03)

    client = factory.clients[0]
    assert isinstance(client, _TimeoutThenSuccessClient)
    assert client.cancelled.is_set()

    result = handle.recall("second query", timeout=1.0)

    assert result == {"query": "second query", "attempt": 2}

    async def pending_task_count() -> int:
        return sum(not task.done() for task in asyncio.all_tasks())

    assert handle.call(lambda _client: pending_task_count(), timeout=1.0) == 1


def test_only_explicit_process_finalization_closes_client_once_on_owning_loop(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    factory = _RecordingFactory()
    handle = acquire_process_runtime(config, client_factory=factory)

    handle.close()
    assert factory.clients[0].close_loops == []

    assert reset_process_runtime_for_tests() is True
    assert reset_process_runtime_for_tests() is False
    assert factory.clients[0].close_loops == [factory.loops[0]]
