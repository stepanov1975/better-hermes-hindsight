"""Task 2 provider admission and process-runtime lifecycle contract tests."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import NoReturn, cast

import pytest

import better_hermes_hindsight.provider as provider_module
import better_hermes_hindsight.runtime as runtime_module
from better_hermes_hindsight.client import RetainSegment as ClientRetainSegment
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.outbox import AdmissionResult, AdmissionStatus, SQLiteOutbox
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

    async def retain_segment(self, segment: ClientRetainSegment) -> object:
        self.operation_calls.append(f"retain:{segment.document_id}")
        return object()

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

    def __call__(self, config: BetterHindsightConfig) -> _RecordingOutbox:
        self.configs.append(config)
        return self.outbox


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
                "segment_max_bytes": 128,
                "tags": ["project:synthetic"],
            },
            "outbox": {"max_pending_bytes": 1_000_000},
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
    handle = acquire_process_runtime(config, client_factory=client_factory)

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
