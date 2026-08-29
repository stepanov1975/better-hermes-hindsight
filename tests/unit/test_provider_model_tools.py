"""Model-facing retention and passive status tool contracts."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import pytest

import better_hermes_hindsight.provider as provider_module
from better_hermes_hindsight.management import ManagementResult
from better_hermes_hindsight.outbox import AdmissionResult, AdmissionStatus
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.runtime import reset_process_runtime_for_tests

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
    ) -> None:
        self.result = result or AdmissionResult(AdmissionStatus.ADMITTED, inserted_count=1)
        self.failure = failure
        self.admissions: list[tuple[str, str, str, int | None]] = []
        self.close_calls = 0

    def admit_turn(
        self,
        *,
        session_id: str,
        user_content: str,
        assistant_content: str,
        segment_count_limit: int | None = None,
    ) -> AdmissionResult:
        self.admissions.append((session_id, user_content, assistant_content, segment_count_limit))
        if self.failure is not None:
            raise self.failure
        return self.result

    def recall(self, _query: str, *, timeout: float) -> object:
        del timeout
        return object()

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
    retain_enabled: bool = True,
    diagnostics_enabled: bool = False,
) -> None:
    directory = home / "better_hindsight"
    directory.mkdir(parents=True, exist_ok=True)
    document: Mapping[str, object] = {
        "api_url": "http://127.0.0.1:9",
        "bank_id": "synthetic-bank",
        "single_principal": True,
        "recall": {"enabled": recall_enabled, "timeout_seconds": 0.125},
        "retain": {
            "enabled": retain_enabled,
            "segment_max_bytes": 256,
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
            "Context: user preference\n\nAlex prefers durable, verified operational changes.",
            2000,
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
    assert handle.admissions[0][2:] == ("durable fact", 2000)


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
