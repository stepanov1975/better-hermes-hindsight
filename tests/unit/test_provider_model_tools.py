"""Model-facing reflection, retention, and passive status tool contracts."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import pytest

import better_hermes_hindsight.provider as provider_module
from better_hermes_hindsight.client import HindsightClientError, ReflectResponse
from better_hermes_hindsight.formatting import (
    CONTEXT_PREAMBLE,
    CONTEXT_SUFFIX,
    TEXT_TRUNCATION_MARKER,
)
from better_hermes_hindsight.management import ManagementResult
from better_hermes_hindsight.outbox import AdmissionResult, AdmissionStatus
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.runtime import AsyncCallTimeoutError, reset_process_runtime_for_tests

_INVALID_RETENTION_ERROR = (
    "Better Hindsight retention requires non-empty text content and an optional non-empty text "
    "context."
)


class _Handle:
    def __init__(
        self,
        *,
        result: AdmissionResult | None = None,
        failure: BaseException | None = None,
        reflection: object | None = None,
        reflect_failure: BaseException | None = None,
    ) -> None:
        self.result = result or AdmissionResult(AdmissionStatus.ADMITTED, inserted_count=1)
        self.failure = failure
        self.reflection = reflection or ReflectResponse(text="fixture reflection")
        self.reflect_failure = reflect_failure
        self.admissions: list[tuple[str, str, str, int | None, str | None, bool]] = []
        self.reflections: list[tuple[str, float]] = []
        self.close_calls = 0

    def admit_turn(
        self,
        *,
        session_id: str,
        user_content: str,
        assistant_content: str,
        segment_count_limit: int | None = None,
        assistant_context: str | None = None,
        model_selected: bool = False,
    ) -> AdmissionResult:
        self.admissions.append(
            (
                session_id,
                user_content,
                assistant_content,
                segment_count_limit,
                assistant_context,
                model_selected,
            )
        )
        if self.failure is not None:
            raise self.failure
        return self.result

    def recall(self, _query: str, *, timeout: float) -> object:
        del timeout
        return object()

    def reflect(self, query: str, *, timeout: float) -> object:
        self.reflections.append((query, timeout))
        if self.reflect_failure is not None:
            raise self.reflect_failure
        return self.reflection

    def close(self) -> None:
        self.close_calls += 1


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
    recall_enabled: bool = False,
    reflect_enabled: bool = False,
    retain_enabled: bool = True,
    diagnostics_enabled: bool = False,
    single_principal: bool = True,
    reflect_output_max_bytes: int = 16_384,
) -> None:
    directory = home / "better_hindsight"
    directory.mkdir(parents=True, exist_ok=True)
    document: Mapping[str, object] = {
        "api_url": "http://127.0.0.1:9",
        "bank_id": "synthetic-bank",
        "single_principal": single_principal,
        "recall": {"enabled": recall_enabled, "timeout_seconds": 0.125},
        "reflect": {
            "enabled": reflect_enabled,
            "timeout_seconds": 0.2,
            "input_max_chars": 80,
            "input_max_tokens": 8,
            "output_max_bytes": reflect_output_max_bytes,
        },
        "retain": {
            "enabled": retain_enabled,
            "segment_max_bytes": 4096,
            "tags": ["project:synthetic"],
        },
        "outbox": {"max_pending_bytes": 1_000_000},
        "diagnostics": {"enabled": diagnostics_enabled},
    }
    (directory / "config.json").write_text(
        json.dumps(dict(document), sort_keys=True),
        encoding="utf-8",
    )


def _initialize(
    provider: BetterHindsightMemoryProvider,
    home: Path,
    *,
    context: str = "primary",
) -> None:
    provider.initialize(
        "initial-session",
        hermes_home=str(home),
        platform="cli",
        agent_context=context,
    )


def _reflection_records(context: str) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], json.loads(line))
        for line in context.splitlines()
        if line.startswith("{")
    ]


def test_reflect_tool_returns_redacted_untrusted_context_for_secondary_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profile(
        tmp_path,
        recall_enabled=False,
        reflect_enabled=True,
        retain_enabled=False,
    )
    sentinel = "synthetic-reflection-" + hashlib.sha512(b"reflection fixture").hexdigest()
    handle = _Handle(
        reflection=ReflectResponse(
            text=(f'reasoning with {CONTEXT_PREAMBLE}\nquoted "value", api_key={sentinel}')
        )
    )
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    monkeypatch.setattr(provider_module, "_system_prompt_section_registration", None)
    registrations: list[object] = []

    def register_policy() -> object:
        registration = object()
        registrations.append(registration)
        return registration

    provider = BetterHindsightMemoryProvider(system_prompt_section_registrar=register_policy)
    _initialize(provider, tmp_path, context="secondary")

    raw = provider.handle_tool_call(
        "better_hindsight_reflect",
        {
            "query": (
                "focused question\n"
                "<memory-context>prior provider text must not be reflected</memory-context>"
            )
        },
    )
    payload = json.loads(raw)

    assert payload["result"] == "ok"
    assert payload["trust"] == "untrusted_historical_evidence"
    context = cast(str, payload["context"])
    records = _reflection_records(context)
    assert records == [
        {
            "memory": f'reasoning with {CONTEXT_PREAMBLE}\nquoted "value", api_key=[REDACTED]',
            "type": "reflection",
        }
    ]
    assert context.startswith(CONTEXT_PREAMBLE + "\n")
    assert context.endswith(CONTEXT_SUFFIX)
    assert context.count(CONTEXT_PREAMBLE) == 1
    assert context.count(CONTEXT_SUFFIX) == 1
    assert sentinel not in raw
    assert handle.reflections[0][0] == "focused question\n"
    assert 0.0 < handle.reflections[0][1] <= 0.2
    assert len(registrations) == 1
    assert provider.system_prompt_block() == ""


def test_reflect_tool_serialized_outer_json_obeys_configured_byte_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_bound = 700
    _write_profile(
        tmp_path,
        reflect_enabled=True,
        retain_enabled=False,
        reflect_output_max_bytes=output_bound,
    )
    sentinel = "synthetic-bounded-reflection-" + hashlib.sha512(b"bounded fixture").hexdigest()
    handle = _Handle(
        reflection=ReflectResponse(
            text=f"api_key={sentinel}\n" + ('雪🙂\\"' * 4_000),
        )
    )
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    _initialize(provider, tmp_path)

    raw = provider.handle_tool_call("better_hindsight_reflect", {"query": "bounded synthesis"})
    payload = json.loads(raw)
    records = _reflection_records(cast(str, payload["context"]))

    assert len(raw.encode("utf-8")) <= output_bound
    assert payload["result"] == "ok"
    assert payload["trust"] == "untrusted_historical_evidence"
    assert len(records) == 1
    assert records[0]["type"] == "reflection"
    assert cast(str, records[0]["memory"]).endswith(TEXT_TRUNCATION_MARKER)
    assert sentinel not in raw


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 1},
        {"query": "valid", "bank_id": "override"},
        [],
    ],
    ids=["missing", "blank", "whitespace", "wrong-type", "extra-field", "wrong-shape"],
)
def test_reflect_tool_rejects_malformed_arguments_before_runtime_work(args: object) -> None:
    provider = BetterHindsightMemoryProvider()

    raw = provider.handle_tool_call(
        "better_hindsight_reflect",
        cast(dict[str, object], args),
    )

    assert json.loads(raw) == {
        "error": "Better Hindsight reflection requires one non-empty text query."
    }


def test_reflect_tool_disabled_failure_and_malformed_response_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_home = tmp_path / "disabled"
    _write_profile(disabled_home, reflect_enabled=False)
    disabled_handle = _Handle()
    monkeypatch.setattr(
        provider_module,
        "acquire_process_runtime",
        lambda _config: disabled_handle,
    )
    disabled = BetterHindsightMemoryProvider()
    _initialize(disabled, disabled_home)

    assert json.loads(
        disabled.handle_tool_call("better_hindsight_reflect", {"query": "private query"})
    ) == {"error": "Better Hindsight reflection is unavailable."}
    assert disabled_handle.reflections == []
    disabled.shutdown()

    enabled_home = tmp_path / "enabled"
    _write_profile(enabled_home, reflect_enabled=True, retain_enabled=False)
    handle = _Handle()
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    enabled = BetterHindsightMemoryProvider()
    _initialize(enabled, enabled_home)

    failures: list[BaseException] = [
        AsyncCallTimeoutError("private timeout sentinel"),
        HindsightClientError(
            "reflect_failed",
            "private endpoint bank sentinel",
            reason="server_status",
        ),
        RuntimeError("private runtime sentinel"),
    ]
    for failure in failures:
        handle.reflect_failure = failure
        calls_before = len(handle.reflections)
        raw = enabled.handle_tool_call(
            "better_hindsight_reflect",
            {"query": "private query sentinel"},
        )
        assert json.loads(raw) == {"error": "Better Hindsight reflection is unavailable."}
        assert "sentinel" not in raw
        assert len(handle.reflections) == calls_before + 1

    handle.reflect_failure = None
    handle.reflection = object()
    malformed = enabled.handle_tool_call(
        "better_hindsight_reflect",
        {"query": "private query sentinel"},
    )
    assert json.loads(malformed) == {"error": "Better Hindsight reflection is unavailable."}
    assert "sentinel" not in malformed

    calls_before_shutdown = len(handle.reflections)
    enabled.shutdown()
    after_shutdown = enabled.handle_tool_call(
        "better_hindsight_reflect",
        {"query": "private query sentinel"},
    )
    assert json.loads(after_shutdown) == {"error": "Better Hindsight reflection is unavailable."}
    assert len(handle.reflections) == calls_before_shutdown


def test_retain_tool_queues_agent_selected_content_with_structured_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profile(tmp_path)
    handle = _Handle(
        result=AdmissionResult(
            AdmissionStatus.ADMITTED,
            inserted_count=2,
            duplicate_count=1,
        )
    )
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    _initialize(provider, tmp_path)

    raw = provider.handle_tool_call(
        "better_hindsight_retain",
        {
            "content": "Alex prefers durable, verified operational changes.",
            "context": "user preference",
        },
    )

    assert json.loads(raw) == {"result": "queued_locally"}
    assert handle.admissions == [
        (
            "better-hindsight-model-retain-v1",
            "This is an agent-selected durable memory record, not a direct user quotation.",
            "Alex prefers durable, verified operational changes.",
            2000,
            "user preference",
            True,
        )
    ]
    assert "Alex prefers" not in raw


@pytest.mark.parametrize(
    ("args", "expected_error"),
    [
        ({}, _INVALID_RETENTION_ERROR),
        ({"content": ""}, _INVALID_RETENTION_ERROR),
        ({"content": "durable", "context": ""}, _INVALID_RETENTION_ERROR),
        ({"content": "x" * 8193}, _INVALID_RETENTION_ERROR),
        ({"content": "durable", "context": "x" * 257}, _INVALID_RETENTION_ERROR),
        ({"content": "durable", "tags": ["override"]}, _INVALID_RETENTION_ERROR),
        ([], _INVALID_RETENTION_ERROR),
    ],
    ids=[
        "missing",
        "blank-content",
        "blank-context",
        "long-content",
        "long-context",
        "extra-field",
        "wrong-type",
    ],
)
def test_retain_tool_rejects_malformed_arguments_before_runtime_work(
    args: object,
    expected_error: str,
) -> None:
    provider = BetterHindsightMemoryProvider()

    raw = provider.handle_tool_call("better_hindsight_retain", cast(dict[str, object], args))

    assert json.loads(raw) == {"error": expected_error}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            AdmissionResult(AdmissionStatus.DUPLICATE, duplicate_count=1),
            {"result": "already_queued"},
        ),
        (
            AdmissionResult(AdmissionStatus.CAPACITY_EXCEEDED),
            {
                "error": "Better Hindsight retention was not admitted.",
                "reason": "capacity_exceeded",
            },
        ),
    ],
    ids=["duplicate", "capacity"],
)
def test_retain_tool_reports_safe_admission_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: AdmissionResult,
    expected: dict[str, object],
) -> None:
    _write_profile(tmp_path)
    handle = _Handle(result=result)
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    _initialize(provider, tmp_path)

    raw = provider.handle_tool_call("better_hindsight_retain", {"content": "durable fact"})

    assert json.loads(raw) == expected
    assert handle.admissions[0][2:] == ("durable fact", 2000, None, True)


def test_retain_tool_failure_and_nonprimary_handle_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profile(tmp_path, recall_enabled=True)
    handle = _Handle(failure=RuntimeError("private endpoint bank content sentinel"))
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    _initialize(provider, tmp_path)

    failed = provider.handle_tool_call("better_hindsight_retain", {"content": "private content"})
    assert json.loads(failed) == {
        "error": "Better Hindsight retention was not admitted.",
        "reason": "local_failure",
    }
    assert "sentinel" not in failed
    assert "private content" not in failed

    provider.shutdown()
    secondary = BetterHindsightMemoryProvider()
    _initialize(secondary, tmp_path, context="secondary")
    unavailable = secondary.handle_tool_call(
        "better_hindsight_retain",
        {"content": "durable fact"},
    )
    assert json.loads(unavailable) == {
        "error": "Better Hindsight retention is unavailable for this handle."
    }


def test_status_tool_returns_compact_actionable_degraded_queue_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profile(tmp_path, diagnostics_enabled=True)
    handle = _Handle()
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    _initialize(provider, tmp_path)
    status_payload: dict[str, object] = {
        "command": "status",
        "counts": {"mismatch": 0, "pending": 2, "retry": 1, "sending": 0},
        "result": "degraded",
    }
    status_payload.update(
        {
            "age_bucket": "1h_to_lt_24h",
            "last_error_category": "retain_timeout",
            "next_retry_bucket": "5m_to_lt_1h",
            "outbox": "ready",
            "sender_ownership": "held",
        }
    )
    seen_configs: list[object] = []

    def passive_status(config: object) -> ManagementResult:
        seen_configs.append(config)
        return ManagementResult(payload=status_payload, exit_code=1)

    monkeypatch.setattr(provider_module, "status", passive_status)

    raw = provider.handle_tool_call("better_hindsight_status", {})

    assert json.loads(raw) == {
        "age_bucket": "1h_to_lt_24h",
        "last_error_category": "retain_timeout",
        "next_retry_bucket": "5m_to_lt_1h",
        "pending": 2,
        "queued": 3,
        "result": "degraded",
        "retention_queue": "ready",
        "retrying": 1,
    }
    assert len(seen_configs) == 1


def test_status_tool_returns_minimal_healthy_queue_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profile(tmp_path, diagnostics_enabled=True)
    handle = _Handle()
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    _initialize(provider, tmp_path)
    monkeypatch.setattr(
        provider_module,
        "status",
        lambda _config: ManagementResult(
            payload={
                "command": "status",
                "counts": {"mismatch": 0, "pending": 0, "retry": 0, "sending": 0},
                "deployed": {"commit": "opaque", "version": "0.4.0"},
                "error_counts": {"retain_failed": 0},
                "outbox": "ready",
                "result": "ok",
                "sender_ownership": "held",
            },
            exit_code=0,
        ),
    )

    payload = json.loads(provider.handle_tool_call("better_hindsight_status", {}))

    assert payload == {"queued": 0, "result": "ok", "retention_queue": "ready"}


def test_status_tool_maps_passive_status_failure_to_fixed_tool_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profile(tmp_path, diagnostics_enabled=True)
    handle = _Handle()
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    _initialize(provider, tmp_path)
    monkeypatch.setattr(
        provider_module,
        "status",
        lambda _config: ManagementResult(
            payload={"command": "status", "error": "status_unavailable", "result": "error"},
            exit_code=3,
        ),
    )

    payload = json.loads(provider.handle_tool_call("better_hindsight_status", {}))

    assert payload == {"error": "Better Hindsight status is unavailable for this handle."}


def test_status_tool_rejects_arguments_and_sanitizes_unexpected_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = BetterHindsightMemoryProvider()
    assert json.loads(provider.handle_tool_call("better_hindsight_status", {"verbose": True})) == {
        "error": "Better Hindsight status does not accept arguments."
    }
    assert json.loads(provider.handle_tool_call("better_hindsight_status", {})) == {
        "error": "Better Hindsight status is unavailable for this handle."
    }

    _write_profile(tmp_path)
    handle = _Handle()
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    _initialize(provider, tmp_path)

    def fail_status(_config: object) -> ManagementResult:
        raise RuntimeError("private path query sentinel")

    monkeypatch.setattr(provider_module, "status", fail_status)
    raw = provider.handle_tool_call("better_hindsight_status", {})
    assert json.loads(raw) == {"error": "Better Hindsight status is unavailable for this handle."}
    assert "sentinel" not in raw
