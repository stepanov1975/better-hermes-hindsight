"""Task 2 provider admission and process-runtime lifecycle contract tests."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import NoReturn, cast

import pytest

import better_hermes_hindsight.provider as provider_module
import better_hermes_hindsight.runtime as runtime_module
from better_hermes_hindsight.client import (
    RetainConfirmation,
)
from better_hermes_hindsight.client import (
    RetainSegment as ClientRetainSegment,
)
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.outbox import (
    AdmissionResult,
    AdmissionStatus,
    OutboxOpenError,
    OutboxReadError,
    OutboxRow,
    SQLiteOutbox,
)
from better_hermes_hindsight.provider import (
    RUNTIME_INACTIVE_DIAGNOSTIC,
    BetterHindsightMemoryProvider,
)
from better_hermes_hindsight.retention import (
    RETENTION_REJECTED_MESSAGE,
    RetainedSegment,
    RetentionConstructionError,
    build_retained_segments,
)
from better_hermes_hindsight.runtime import (
    ProcessRuntimeHandle,
    RuntimeFinalizedError,
    acquire_process_runtime,
    finalize_process_runtime,
    reset_process_runtime_for_tests,
)

EXPECTED_RETENTION_WARNING = "Better Hindsight local retention admission was rejected."


class _ProviderHandle:
    def __init__(
        self,
        *,
        result: AdmissionResult | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.result = result or AdmissionResult(AdmissionStatus.ADMITTED, inserted_count=1)
        self.failure = failure
        self.admissions: list[tuple[str, str, str]] = []
        self.recalls: list[tuple[str, float]] = []
        self.close_calls = 0

    def admit_turn(
        self,
        *,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> AdmissionResult:
        self.admissions.append((session_id, user_content, assistant_content))
        if self.failure is not None:
            raise self.failure
        return self.result

    def recall(self, query: str, *, timeout: float) -> object:
        self.recalls.append((query, timeout))
        return object()

    def close(self) -> None:
        self.close_calls += 1


class _RecordingClient:
    def __init__(self, close_order: list[str] | None = None, *, fail_close: bool = False) -> None:
        self.created_loop = asyncio.get_running_loop()
        self.close_order = close_order
        self.fail_close = fail_close
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
        if self.close_order is not None:
            self.close_order.append("client")
        if self.fail_close:
            raise RuntimeError("synthetic client close failure")


class _ClientFactory:
    def __init__(self, close_order: list[str] | None = None, *, fail_close: bool = False) -> None:
        self.close_order = close_order
        self.fail_close = fail_close
        self.clients: list[_RecordingClient] = []

    def __call__(self, _config: BetterHindsightConfig) -> _RecordingClient:
        client = _RecordingClient(self.close_order, fail_close=self.fail_close)
        self.clients.append(client)
        return client


class _CancellationResistantRetainClient(_RecordingClient):
    def __init__(self, close_order: list[str]) -> None:
        super().__init__(close_order)
        self.cancellation_seen = threading.Event()
        self.release_late_success = threading.Event()
        self.segments: list[ClientRetainSegment] = []

    async def retain_segment(self, segment: ClientRetainSegment) -> RetainConfirmation:
        self.operation_calls.append(f"retain:{segment.document_id}")
        self.segments.append(segment)
        try:
            await asyncio.Future()
            raise AssertionError("synthetic resistant retain unexpectedly resumed")
        except asyncio.CancelledError:
            self.cancellation_seen.set()
            await asyncio.to_thread(self.release_late_success.wait)
            return RetainConfirmation(confirmed=True)


class _CancellationResistantClientFactory:
    def __init__(self, close_order: list[str]) -> None:
        self.close_order = close_order
        self.clients: list[_CancellationResistantRetainClient] = []

    def __call__(self, _config: BetterHindsightConfig) -> _CancellationResistantRetainClient:
        client = _CancellationResistantRetainClient(self.close_order)
        self.clients.append(client)
        return client


class _CountingOutbox:
    def __init__(self, delegate: SQLiteOutbox, close_order: list[str]) -> None:
        self.delegate = delegate
        self.close_order = close_order
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.close_order.append("outbox")
        self.delegate.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


class _RecordingOutbox:
    def __init__(
        self,
        *,
        result: AdmissionResult | None = None,
        close_order: list[str] | None = None,
        fail_close: bool = False,
    ) -> None:
        self.result = result or AdmissionResult(AdmissionStatus.ADMITTED, inserted_count=1)
        self.close_order = close_order
        self.fail_close = fail_close
        self.admissions: list[tuple[RetainedSegment, ...]] = []
        self.close_calls = 0

    def admit(self, segments: Sequence[RetainedSegment]) -> AdmissionResult:
        self.admissions.append(tuple(segments))
        return self.result

    def close(self) -> None:
        self.close_calls += 1
        if self.close_order is not None:
            self.close_order.append("outbox")
        if self.fail_close:
            raise RuntimeError("synthetic outbox close failure")


class _OutboxFactory:
    def __init__(self, outbox: _RecordingOutbox) -> None:
        self.outbox = outbox
        self.configs: list[BetterHindsightConfig] = []

    def __call__(self, config: BetterHindsightConfig) -> runtime_module.OutboxProtocol:
        self.configs.append(config)
        return cast(runtime_module.OutboxProtocol, self.outbox)


class _InertSender:
    def __init__(self) -> None:
        self.start_calls = 0
        self.wake_calls = 0
        self.stop_calls = 0
        self.join_timeouts: list[float | None] = []

    def start(self) -> None:
        self.start_calls += 1

    def wake(self) -> None:
        self.wake_calls += 1

    def request_stop(self) -> None:
        self.stop_calls += 1

    def join(self, timeout: float | None = None) -> bool:
        self.join_timeouts.append(timeout)
        return True


class _ControllableSender(_InertSender):
    def __init__(self, *, initially_stopped: bool) -> None:
        super().__init__()
        self.stopped = threading.Event()
        if initially_stopped:
            self.stopped.set()

    def join(self, timeout: float | None = None) -> bool:
        self.join_timeouts.append(timeout)
        return self.stopped.is_set()


class _SenderFactory:
    def __init__(self, sender: _InertSender) -> None:
        self.sender = sender
        self.calls = 0

    def __call__(
        self,
        _config: BetterHindsightConfig,
        _outbox: runtime_module.OutboxProtocol,
        _client: object,
        _runner: runtime_module.AsyncRunner,
    ) -> _InertSender:
        self.calls += 1
        return self.sender


def _inert_sender_factory(
    _config: BetterHindsightConfig,
    _outbox: runtime_module.OutboxProtocol,
    _client: object,
    _runner: runtime_module.AsyncRunner,
) -> _InertSender:
    return _InertSender()


class _BlockingFirstOutbox(_RecordingOutbox):
    def __init__(
        self, entered: threading.Event, release: threading.Event, order: list[str]
    ) -> None:
        super().__init__(close_order=order)
        self.entered = entered
        self.release = release
        self._admit_lock = threading.Lock()
        self.admit_calls = 0

    def admit(self, segments: Sequence[RetainedSegment]) -> AdmissionResult:
        with self._admit_lock:
            self.admit_calls += 1
            first = self.admit_calls == 1
        if first:
            self.entered.set()
            if not self.release.wait(timeout=3.0):
                raise AssertionError("synthetic admission gate was not released")
        return super().admit(segments)


@pytest.fixture(autouse=True)
def _isolated_runtime_and_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_process_runtime_for_tests()
    for name in tuple(os.environ):
        if name.startswith("HINDSIGHT_"):
            monkeypatch.delenv(name, raising=False)
    yield
    reset_process_runtime_for_tests()


def _write_profile(
    home: Path,
    *,
    recall_enabled: bool,
    retain_enabled: bool,
    single_principal: bool = True,
) -> None:
    directory = home / "better_hindsight"
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "api_url": "http://127.0.0.1:9",
        "bank_id": "synthetic-bank",
        "single_principal": single_principal,
        "recall": {"enabled": recall_enabled, "timeout_seconds": 0.125},
        "retain": {
            "enabled": retain_enabled,
            "segment_max_bytes": 256,
            "tags": ["project:synthetic"],
        },
        "outbox": {"max_pending_bytes": 1_000_000},
    }
    (directory / "config.json").write_text(
        json.dumps(document, sort_keys=True),
        encoding="utf-8",
    )


def _initialize_cli(provider: BetterHindsightMemoryProvider, home: Path, *, context: str) -> None:
    provider.initialize(
        "initial-session",
        hermes_home=str(home),
        platform="cli",
        agent_context=context,
    )


def _runtime_config(
    home: Path,
    *,
    recall_enabled: bool = False,
    retain_enabled: bool = True,
    retain_timeout_seconds: float = 0.01,
    busy_timeout_seconds: float = 0.01,
) -> BetterHindsightConfig:
    home.mkdir(parents=True, exist_ok=True)
    return load_config(
        home,
        environ={},
        injected={
            "api_url": "https://service.example.test",
            "bank_id": "synthetic-bank",
            "single_principal": True,
            "recall": {"enabled": recall_enabled},
            "retain": {
                "enabled": retain_enabled,
                "timeout_seconds": retain_timeout_seconds,
                "segment_max_bytes": 128,
                "tags": ["project:synthetic"],
            },
            "outbox": {
                "max_pending_bytes": 1_000_000,
                "busy_timeout_seconds": busy_timeout_seconds,
            },
        },
    )


def _admit(handle: ProcessRuntimeHandle, seed: str = "one") -> AdmissionResult:
    return handle.admit_turn(
        session_id=f"session-{seed}",
        user_content=f"user-{seed}",
        assistant_content=f"assistant-{seed}",
    )


def _forbidden_acquire(_config: BetterHindsightConfig) -> NoReturn:
    raise AssertionError("inert provider acquired a process runtime")


def test_retain_only_primary_acquires_runtime_admits_and_keeps_prefetch_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_profile(tmp_path, recall_enabled=False, retain_enabled=True)
    handle = _ProviderHandle()
    configs: list[BetterHindsightConfig] = []

    def acquire(config: BetterHindsightConfig) -> _ProviderHandle:
        configs.append(config)
        return handle

    monkeypatch.setattr(provider_module, "acquire_process_runtime", acquire)
    provider = BetterHindsightMemoryProvider()
    caplog.set_level(logging.WARNING)

    _initialize_cli(provider, tmp_path, context="primary")
    provider.queue_prefetch("must stay inert")
    provider.sync_turn(
        "synthetic direct user",
        "synthetic direct assistant",
        session_id="callback-session",
        messages=[{"role": "user", "content": "raw-message-must-be-ignored"}],
    )

    assert len(configs) == 1
    assert provider.prefetch("recall stays disabled") == ""
    assert provider.get_tool_schemas() == []
    assert handle.admissions == [
        ("callback-session", "synthetic direct user", "synthetic direct assistant")
    ]
    assert caplog.messages == []
    provider.shutdown()
    provider.shutdown()
    assert handle.close_calls == 1


@pytest.mark.parametrize(
    ("recall_enabled", "retain_enabled", "single_principal", "context"),
    [
        (False, False, True, "primary"),
        (False, True, True, "secondary"),
        (False, True, False, "primary"),
    ],
    ids=["retention-disabled", "non-primary", "unauthorized"],
)
def test_disabled_nonprimary_and_unauthorized_retain_only_handles_are_inert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    recall_enabled: bool,
    retain_enabled: bool,
    single_principal: bool,
    context: str,
) -> None:
    _write_profile(
        tmp_path,
        recall_enabled=recall_enabled,
        retain_enabled=retain_enabled,
        single_principal=single_principal,
    )
    monkeypatch.setattr(provider_module, "acquire_process_runtime", _forbidden_acquire)
    provider = BetterHindsightMemoryProvider()
    caplog.set_level(logging.WARNING)

    _initialize_cli(provider, tmp_path, context=context)
    provider.sync_turn("synthetic user", "synthetic assistant", session_id="session")

    assert provider.prefetch("query") == ""
    assert caplog.messages == []


def test_secondary_recall_handle_acquires_runtime_but_retention_stays_inert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_profile(tmp_path, recall_enabled=True, retain_enabled=True)
    handle = _ProviderHandle()
    configs: list[BetterHindsightConfig] = []

    def acquire(config: BetterHindsightConfig) -> _ProviderHandle:
        configs.append(config)
        return handle

    monkeypatch.setattr(provider_module, "acquire_process_runtime", acquire)
    provider = BetterHindsightMemoryProvider()
    caplog.set_level(logging.WARNING)

    _initialize_cli(provider, tmp_path, context="secondary")
    provider.sync_turn("synthetic user", "synthetic assistant", session_id="session")

    assert len(configs) == 1
    assert handle.admissions == []
    assert caplog.messages == []


@pytest.mark.parametrize(
    ("user_content", "assistant_content"),
    [
        (None, "assistant"),
        ("", "assistant"),
        (" \t\n", "assistant"),
        ("user", None),
        ("user", ""),
        ("user", " \t\n"),
    ],
    ids=[
        "non-string-user",
        "empty-user",
        "whitespace-user",
        "non-string-assistant",
        "empty-assistant",
        "whitespace-assistant",
    ],
)
def test_invalid_direct_callback_content_is_deliberately_inert_without_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    user_content: object,
    assistant_content: object,
) -> None:
    _write_profile(tmp_path, recall_enabled=False, retain_enabled=True)
    handle = _ProviderHandle()
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    caplog.set_level(logging.WARNING)
    _initialize_cli(provider, tmp_path, context="primary")

    provider.sync_turn(
        cast(str, user_content),
        cast(str, assistant_content),
        session_id="session",
        messages=[{"role": "assistant", "content": "raw-message-is-not-a-fallback"}],
    )

    assert handle.admissions == []
    assert caplog.messages == []


@pytest.mark.parametrize(
    "outcome",
    [
        AdmissionResult(AdmissionStatus.CAPACITY_EXCEEDED),
        AdmissionResult(AdmissionStatus.CONTENDED),
        AdmissionResult(AdmissionStatus.CONFLICT),
        AdmissionResult(AdmissionStatus.LOCAL_FAILURE),
        AdmissionResult(AdmissionStatus.INVALID),
        RetentionConstructionError(RETENTION_REJECTED_MESSAGE),
        RuntimeFinalizedError("synthetic finalized detail"),
    ],
    ids=[
        "capacity",
        "contention",
        "collision",
        "local-failure",
        "invalid",
        "construction",
        "finalized",
    ],
)
def test_every_construction_admission_or_runtime_rejection_uses_one_fixed_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: AdmissionResult | BaseException,
) -> None:
    _write_profile(tmp_path, recall_enabled=False, retain_enabled=True)
    failure = outcome if isinstance(outcome, BaseException) else None
    result = outcome if isinstance(outcome, AdmissionResult) else None
    handle = _ProviderHandle(result=result, failure=failure)
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    caplog.set_level(logging.WARNING)
    _initialize_cli(provider, tmp_path, context="primary")

    provider.sync_turn(
        "synthetic user payload canary",
        "synthetic assistant payload canary",
        session_id="synthetic-session-canary",
    )

    assert caplog.messages == [EXPECTED_RETENTION_WARNING]
    for forbidden in (
        "payload canary",
        "session-canary",
        "endpoint",
        "bank",
        "key",
        "path",
        "finalized detail",
    ):
        assert forbidden not in caplog.text.casefold()


@pytest.mark.parametrize(
    "status",
    [AdmissionStatus.ADMITTED, AdmissionStatus.DUPLICATE],
    ids=["admitted", "duplicate"],
)
def test_successful_local_admission_does_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: AdmissionStatus,
) -> None:
    _write_profile(tmp_path, recall_enabled=False, retain_enabled=True)
    handle = _ProviderHandle(result=AdmissionResult(status))
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    caplog.set_level(logging.WARNING)
    _initialize_cli(provider, tmp_path, context="primary")

    provider.sync_turn("synthetic user", "synthetic assistant", session_id="session")

    assert caplog.messages == []


def test_runtime_construction_failure_stays_inactive_with_existing_fixed_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_profile(tmp_path, recall_enabled=False, retain_enabled=True)

    def fail_acquire(_config: BetterHindsightConfig) -> NoReturn:
        raise RuntimeError("synthetic endpoint bank key path payload session detail")

    monkeypatch.setattr(provider_module, "acquire_process_runtime", fail_acquire)
    provider = BetterHindsightMemoryProvider()
    caplog.set_level(logging.WARNING)

    _initialize_cli(provider, tmp_path, context="primary")
    provider.sync_turn("synthetic user", "synthetic assistant")

    assert caplog.messages == [RUNTIME_INACTIVE_DIAGNOSTIC]
    assert "synthetic endpoint" not in caplog.text


def test_outbox_factory_failure_closes_constructed_client_and_runner(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    client_factory = _ClientFactory()
    existing_loop_threads = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name == "better-hindsight-event-loop"
    }

    def fail_outbox(_config: BetterHindsightConfig) -> NoReturn:
        raise RuntimeError("synthetic outbox construction failure")

    with pytest.raises(RuntimeError, match="synthetic outbox construction failure"):
        acquire_process_runtime(
            config,
            client_factory=client_factory,
            outbox_factory=fail_outbox,
            sender_factory=_inert_sender_factory,
        )

    assert len(client_factory.clients) == 1
    assert client_factory.clients[0].close_calls == 1
    assert finalize_process_runtime() is False
    remaining_loop_threads = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name == "better-hindsight-event-loop"
    }
    assert remaining_loop_threads == existing_loop_threads


def test_retain_disabled_runtime_never_opens_an_outbox(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path, recall_enabled=True, retain_enabled=False)
    client_factory = _ClientFactory()
    outbox = _RecordingOutbox()
    outbox_factory = _OutboxFactory(outbox)

    handle = acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=outbox_factory,
        sender_factory=_inert_sender_factory,
    )

    assert len(client_factory.clients) == 1
    assert outbox_factory.configs == []
    handle.close()
    assert finalize_process_runtime() is True
    assert outbox.close_calls == 0
    assert client_factory.clients[0].close_calls == 1


def test_equal_configs_share_exactly_one_runtime_client_and_outbox(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    equal_config = _runtime_config(tmp_path)
    close_order: list[str] = []
    client_factory = _ClientFactory(close_order)
    outbox = _RecordingOutbox(close_order=close_order)
    outbox_factory = _OutboxFactory(outbox)

    first = acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=outbox_factory,
        sender_factory=_inert_sender_factory,
    )
    second = acquire_process_runtime(
        equal_config,
        client_factory=client_factory,
        outbox_factory=outbox_factory,
    )

    assert first.runtime is second.runtime
    assert len(client_factory.clients) == 1
    assert outbox_factory.configs == [config]
    first.close()
    second.close()
    assert outbox.close_calls == 0
    assert client_factory.clients[0].close_calls == 0

    assert finalize_process_runtime() is True
    assert close_order == ["outbox", "client"]
    assert outbox.close_calls == 1
    assert client_factory.clients[0].close_calls == 1


def test_runtime_admission_constructs_locally_without_any_client_operation(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    client_factory = _ClientFactory()
    outbox = _RecordingOutbox()
    handle = acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=_OutboxFactory(outbox),
        sender_factory=_inert_sender_factory,
    )

    result = _admit(handle)

    assert result.status is AdmissionStatus.ADMITTED
    assert len(outbox.admissions) == 1
    source = "".join(segment.content for segment in outbox.admissions[0])
    decoded = json.loads(source)
    assert decoded["roles"] == [
        {"content": "user-one", "role": "user"},
        {"content": "assistant-one", "role": "assistant"},
    ]
    assert client_factory.clients[0].operation_calls == []


def _wait_for_finalized_rejection(handle: ProcessRuntimeHandle) -> None:
    deadline = time.monotonic() + 2.0
    while True:
        try:
            _admit(handle, "late")
        except RuntimeFinalizedError:
            return
        if time.monotonic() >= deadline:
            pytest.fail("runtime did not reject new admission after finalization started")
        time.sleep(0.001)


def test_active_outbox_admission_blocks_finalization_and_cleanup_order_is_exact(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finalized = threading.Event()
    order: list[str] = []
    outbox = _BlockingFirstOutbox(entered, release, order)
    client_factory = _ClientFactory(order)
    first = acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=_OutboxFactory(outbox),
        sender_factory=_inert_sender_factory,
    )
    second = acquire_process_runtime(config)
    admission_results: list[AdmissionResult] = []
    admission_errors: list[BaseException] = []
    finalization_results: list[bool] = []

    def run_admission() -> None:
        try:
            admission_results.append(_admit(first))
        except BaseException as error:
            admission_errors.append(error)

    def run_finalization() -> None:
        finalization_results.append(finalize_process_runtime())
        finalized.set()

    admitting_thread = threading.Thread(target=run_admission)
    finalizing_thread = threading.Thread(target=run_finalization)
    admitting_thread.start()
    assert entered.wait(timeout=2.0)
    finalizing_thread.start()
    _wait_for_finalized_rejection(second)

    assert finalized.is_set() is False
    assert order == []
    release.set()
    admitting_thread.join(timeout=2.0)
    finalizing_thread.join(timeout=2.0)

    assert admitting_thread.is_alive() is False
    assert finalizing_thread.is_alive() is False
    assert admission_errors == []
    assert [result.status for result in admission_results] == [AdmissionStatus.ADMITTED]
    assert finalization_results == [True]
    assert order == ["outbox", "client"]
    assert outbox.close_calls == 1
    assert client_factory.clients[0].close_calls == 1
    assert finalize_process_runtime() is False


def test_lifecycle_counter_starts_before_deterministic_turn_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _runtime_config(tmp_path)
    construction_entered = threading.Event()
    construction_release = threading.Event()
    finalized = threading.Event()
    construction_lock = threading.Lock()
    construction_calls = 0
    outbox = _RecordingOutbox()
    handle = acquire_process_runtime(
        config,
        client_factory=_ClientFactory(),
        outbox_factory=_OutboxFactory(outbox),
        sender_factory=_inert_sender_factory,
    )
    sibling = acquire_process_runtime(config)

    def blocking_builder(
        *,
        session_id: object,
        user_content: object,
        assistant_content: object,
        tags: object,
        segment_max_bytes: object,
    ) -> tuple[RetainedSegment, ...]:
        nonlocal construction_calls
        with construction_lock:
            construction_calls += 1
            first = construction_calls == 1
        if first:
            construction_entered.set()
            if not construction_release.wait(timeout=3.0):
                raise AssertionError("synthetic construction gate was not released")
        return build_retained_segments(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
            tags=tags,
            segment_max_bytes=segment_max_bytes,
        )

    monkeypatch.setattr(
        runtime_module,
        "build_retained_segments",
        blocking_builder,
        raising=False,
    )
    admission_results: list[AdmissionResult] = []

    def run_finalization() -> None:
        finalize_process_runtime()
        finalized.set()

    admitting_thread = threading.Thread(target=lambda: admission_results.append(_admit(handle)))
    finalizing_thread = threading.Thread(target=run_finalization)
    admitting_thread.start()
    assert construction_entered.wait(timeout=2.0)
    finalizing_thread.start()
    _wait_for_finalized_rejection(sibling)

    assert finalized.is_set() is False
    assert outbox.admissions == []
    construction_release.set()
    admitting_thread.join(timeout=2.0)
    finalizing_thread.join(timeout=2.0)

    assert [result.status for result in admission_results] == [AdmissionStatus.ADMITTED]
    assert len(outbox.admissions) == 1
    assert finalized.is_set() is True


def test_outbox_close_failure_still_closes_client_and_shuts_down_runtime(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    order: list[str] = []
    outbox = _RecordingOutbox(close_order=order, fail_close=True)
    client_factory = _ClientFactory(order)
    acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=_OutboxFactory(outbox),
        sender_factory=_inert_sender_factory,
    )

    with pytest.raises(RuntimeError, match="synthetic outbox close failure"):
        finalize_process_runtime()

    assert order == ["outbox", "client"]
    assert outbox.close_calls == 1
    assert client_factory.clients[0].close_calls == 1
    assert finalize_process_runtime() is False


def test_real_sqlite_rows_survive_handle_close_and_runtime_finalization(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    client_factory = _ClientFactory()
    handle = acquire_process_runtime(
        config,
        client_factory=client_factory,
        sender_factory=_inert_sender_factory,
    )

    result = _admit(handle, "durable")
    handle.close()
    inspector = SQLiteOutbox.open(config)
    try:
        before_finalize = inspector.read_unconfirmed()
    finally:
        inspector.close()

    assert result.status is AdmissionStatus.ADMITTED
    assert len(before_finalize) == before_finalize[0].segment_count
    assert [row.segment_index for row in before_finalize] == list(range(len(before_finalize)))
    assert all(row.state == "pending" for row in before_finalize)
    assert client_factory.clients[0].operation_calls == []

    assert finalize_process_runtime() is True
    reopened = SQLiteOutbox.open(config)
    try:
        after_finalize = reopened.read_unconfirmed()
    finally:
        reopened.close()

    assert after_finalize == before_finalize
    assert client_factory.clients[0].close_calls == 1


def test_retain_runtime_starts_one_sender_before_publication_and_wakes_duplicate_admission(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    client_factory = _ClientFactory()
    outbox = _RecordingOutbox()
    outbox_factory = _OutboxFactory(outbox)
    sender = _InertSender()
    sender_factory = _SenderFactory(sender)

    first = acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=outbox_factory,
        sender_factory=sender_factory,
    )
    second = acquire_process_runtime(config)

    assert sender_factory.calls == 1
    assert sender.start_calls == 1
    assert first.runtime is second.runtime
    assert len(client_factory.clients) == 1
    assert outbox_factory.configs == [config]

    assert _admit(first, "admitted").status is AdmissionStatus.ADMITTED
    outbox.result = AdmissionResult(AdmissionStatus.DUPLICATE, duplicate_count=1)
    assert _admit(second, "duplicate").status is AdmissionStatus.DUPLICATE
    assert sender.wake_calls == 2

    assert finalize_process_runtime() is True
    assert sender.stop_calls == 1
    assert len(sender.join_timeouts) == 1


def test_blocked_sender_preserves_runtime_and_zero_closes_until_repeated_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _runtime_config(tmp_path)
    order: list[str] = []
    outbox = _RecordingOutbox(close_order=order)
    client_factory = _ClientFactory(order)
    sender = _ControllableSender(initially_stopped=False)
    sender_factory = _SenderFactory(sender)
    runner_shutdown_calls = 0
    real_shutdown = runtime_module.AsyncRunner.shutdown

    def shutdown_with_order(runner: runtime_module.AsyncRunner) -> bool:
        nonlocal runner_shutdown_calls
        runner_shutdown_calls += 1
        order.append("runner")
        return real_shutdown(runner)

    monkeypatch.setattr(runtime_module.AsyncRunner, "shutdown", shutdown_with_order)
    monkeypatch.setattr(runtime_module, "_monotonic_now", lambda: 100.0)
    handle = acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=_OutboxFactory(outbox),
        sender_factory=sender_factory,
    )
    expected_deadline = (
        config.retain.timeout_seconds
        + config.outbox.busy_timeout_seconds
        + runtime_module.ASYNC_CANCELLATION_DRAIN_SECONDS
        + 1.0
    )
    try:
        with pytest.raises(runtime_module.SenderStopError) as caught:
            finalize_process_runtime()

        assert str(caught.value) == (
            "Better Hindsight sender could not stop before shutdown deadline."
        )
        assert caught.value.__cause__ is None
        assert sender.stop_calls == 1
        assert sender.join_timeouts == pytest.approx([expected_deadline])
        assert outbox.close_calls == 0
        assert client_factory.clients[0].close_calls == 0
        assert runner_shutdown_calls == 0
        assert order == []
        with pytest.raises(RuntimeFinalizedError):
            _admit(handle, "after-stop-timeout")
        with pytest.raises(RuntimeFinalizedError):
            acquire_process_runtime(config)
        assert len(client_factory.clients) == 1

        sender.stopped.set()
        assert finalize_process_runtime() is True
        assert sender.stop_calls == 2
        assert sender.join_timeouts == pytest.approx([expected_deadline, expected_deadline])
        assert order == ["outbox", "client", "runner"]
        assert outbox.close_calls == 1
        assert client_factory.clients[0].close_calls == 1
        assert runner_shutdown_calls == 1
        assert finalize_process_runtime() is False
    finally:
        sender.stopped.set()
        finalize_process_runtime()


def test_idle_joined_sender_still_waits_for_unrelated_runner_settlement_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _runtime_config(tmp_path)
    order: list[str] = []
    outbox = _RecordingOutbox(close_order=order)
    client_factory = _ClientFactory(order)
    sender = _ControllableSender(initially_stopped=True)
    sender_factory = _SenderFactory(sender)
    runner_shutdown_calls = 0
    real_shutdown = runtime_module.AsyncRunner.shutdown
    operation_started = threading.Event()
    cancellation_seen = threading.Event()
    release_operation = threading.Event()
    operation_errors: list[BaseException] = []

    def shutdown_with_order(runner: runtime_module.AsyncRunner) -> bool:
        nonlocal runner_shutdown_calls
        runner_shutdown_calls += 1
        order.append("runner")
        return real_shutdown(runner)

    monkeypatch.setattr(runtime_module.AsyncRunner, "shutdown", shutdown_with_order)
    handle = acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=_OutboxFactory(outbox),
        sender_factory=sender_factory,
    )

    async def cancellation_resistant() -> None:
        operation_started.set()
        try:
            await asyncio.Future()
            raise AssertionError("synthetic unrelated operation unexpectedly resumed")
        except asyncio.CancelledError:
            cancellation_seen.set()
            await asyncio.to_thread(release_operation.wait)

    def run_operation() -> None:
        try:
            handle.call(lambda _client: cancellation_resistant(), timeout=0.01)
        except BaseException as error:
            operation_errors.append(error)

    operation_thread = threading.Thread(target=run_operation)
    expected_deadline = (
        config.retain.timeout_seconds
        + config.outbox.busy_timeout_seconds
        + runtime_module.ASYNC_CANCELLATION_DRAIN_SECONDS
        + 1.0
    )
    clock_values = iter((10.0, 10.0, 10.0 + expected_deadline))
    last_clock = 10.0 + expected_deadline

    def expiring_clock() -> float:
        return next(clock_values, last_clock)

    try:
        operation_thread.start()
        assert operation_started.wait(timeout=1.0)
        operation_thread.join(timeout=1.0)
        assert operation_thread.is_alive() is False
        assert len(operation_errors) == 1
        assert isinstance(operation_errors[0], runtime_module.AsyncCallTimeoutError)
        assert cancellation_seen.is_set()
        assert handle.runtime._runner.wait_for_settlement(timeout=0.0) is False

        monkeypatch.setattr(runtime_module, "_monotonic_now", expiring_clock)
        with pytest.raises(runtime_module.SenderStopError):
            finalize_process_runtime()

        assert sender.join_timeouts == pytest.approx([expected_deadline])
        assert outbox.close_calls == 0
        assert client_factory.clients[0].close_calls == 0
        assert runner_shutdown_calls == 0
        assert order == []
        with pytest.raises(RuntimeFinalizedError):
            acquire_process_runtime(config)

        release_operation.set()
        assert handle.runtime._runner.wait_for_settlement(timeout=1.0) is True
        monkeypatch.setattr(runtime_module, "_monotonic_now", lambda: 20.0)
        assert finalize_process_runtime() is True
        assert order == ["outbox", "client", "runner"]
        assert outbox.close_calls == 1
        assert client_factory.clients[0].close_calls == 1
        assert runner_shutdown_calls == 1
    finally:
        release_operation.set()
        operation_thread.join(timeout=1.0)
        sender.stopped.set()
        finalize_process_runtime()


def test_actual_sender_late_success_preserves_runtime_then_replays_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(
        tmp_path,
        environ={},
        injected={
            "api_url": "https://service.example.test",
            "bank_id": "synthetic-bank",
            "single_principal": True,
            "recall": {"enabled": False},
            "retain": {
                "enabled": True,
                "timeout_seconds": 0.01,
                "segment_max_bytes": 4096,
            },
            "outbox": {
                "max_pending_bytes": 1_000_000,
                "busy_timeout_seconds": 0.01,
                "poll_interval_seconds": 0.1,
                "retry_initial_seconds": 0.05,
                "retry_max_seconds": 0.05,
            },
        },
    )
    order: list[str] = []
    client_factory = _CancellationResistantClientFactory(order)
    outboxes: list[_CountingOutbox] = []
    runner_shutdown_calls = 0
    real_shutdown = runtime_module.AsyncRunner.shutdown
    real_clock = runtime_module._monotonic_now

    def outbox_factory(sender_config: BetterHindsightConfig) -> runtime_module.OutboxProtocol:
        outbox = _CountingOutbox(SQLiteOutbox.open(sender_config), order)
        outboxes.append(outbox)
        return cast(runtime_module.OutboxProtocol, outbox)

    def shutdown_with_order(runner: runtime_module.AsyncRunner) -> bool:
        nonlocal runner_shutdown_calls
        runner_shutdown_calls += 1
        order.append("runner")
        return real_shutdown(runner)

    def read_rows() -> tuple[OutboxRow, ...]:
        inspector = SQLiteOutbox.open(config)
        try:
            return inspector.read_unconfirmed()
        finally:
            inspector.close()

    def wait_for_rows(
        predicate: Callable[[tuple[OutboxRow, ...]], bool],
    ) -> tuple[OutboxRow, ...]:
        deadline = time.monotonic() + 3.0
        while True:
            try:
                rows = read_rows()
            except (OutboxOpenError, OutboxReadError):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pytest.fail("durable outbox could not be reopened before the deadline")
                threading.Event().wait(timeout=min(0.02, remaining))
                continue
            if predicate(rows):
                return rows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail("durable outbox state did not reach the expected value")
            threading.Event().wait(timeout=min(0.02, remaining))

    monkeypatch.setattr(runtime_module.AsyncRunner, "shutdown", shutdown_with_order)
    handle = acquire_process_runtime(
        config,
        client_factory=client_factory,
        outbox_factory=outbox_factory,
    )
    first_client = client_factory.clients[0]
    expected_deadline = (
        config.retain.timeout_seconds
        + config.outbox.busy_timeout_seconds
        + runtime_module.ASYNC_CANCELLATION_DRAIN_SECONDS
        + 1.0
    )

    try:
        result = handle.admit_turn(
            session_id="late-success-session",
            user_content="late-success-user",
            assistant_content="late-success-assistant",
        )
        assert result.status is AdmissionStatus.ADMITTED
        assert first_client.cancellation_seen.wait(timeout=2.0)
        sending = wait_for_rows(lambda rows: len(rows) == 1 and rows[0].state == "sending")
        first_identity = (sending[0].document_id, sending[0].content)
        assert len(first_client.segments) == 1
        assert (
            first_client.segments[0].document_id,
            first_client.segments[0].content,
        ) == first_identity

        clock_values = iter((100.0, 100.0 + expected_deadline))
        monkeypatch.setattr(
            runtime_module,
            "_monotonic_now",
            lambda: next(clock_values, 100.0 + expected_deadline),
        )
        with pytest.raises(runtime_module.SenderStopError):
            finalize_process_runtime()

        assert outboxes[0].close_calls == 0
        assert first_client.close_calls == 0
        assert runner_shutdown_calls == 0
        assert order == []
        with pytest.raises(RuntimeFinalizedError):
            acquire_process_runtime(config)

        first_client.release_late_success.set()
        pending = wait_for_rows(
            lambda rows: (
                len(rows) == 1
                and rows[0].state == "pending"
                and rows[0].last_error_category == "retain_timeout"
            )
        )
        assert (pending[0].document_id, pending[0].content) == first_identity

        monkeypatch.setattr(runtime_module, "_monotonic_now", real_clock)
        assert finalize_process_runtime() is True
        assert outboxes[0].close_calls == 1
        assert first_client.close_calls == 1
        assert runner_shutdown_calls == 1
        assert order == ["outbox", "client", "runner"]

        class _ReplayClient(_RecordingClient):
            def __init__(self) -> None:
                super().__init__()
                self.segments: list[ClientRetainSegment] = []

            async def retain_segment(self, segment: ClientRetainSegment) -> RetainConfirmation:
                self.segments.append(segment)
                return RetainConfirmation(confirmed=True)

        replay_clients: list[_ReplayClient] = []

        def replay_factory(_config: BetterHindsightConfig) -> _ReplayClient:
            client = _ReplayClient()
            replay_clients.append(client)
            return client

        monkeypatch.setattr(runtime_module.AsyncRunner, "shutdown", real_shutdown)
        replay_handle = acquire_process_runtime(config, client_factory=replay_factory)
        assert wait_for_rows(lambda rows: not rows) == ()
        replay_handle.close()
        assert finalize_process_runtime() is True
        assert len(replay_clients) == 1
        assert len(replay_clients[0].segments) == 1
        assert (
            replay_clients[0].segments[0].document_id,
            replay_clients[0].segments[0].content,
        ) == first_identity
    finally:
        first_client.release_late_success.set()
        handle.close()
        monkeypatch.setattr(runtime_module, "_monotonic_now", real_clock)
        monkeypatch.setattr(runtime_module.AsyncRunner, "shutdown", real_shutdown)
        finalize_process_runtime()
