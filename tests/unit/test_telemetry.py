"""Privacy-safe production-minimum structured event contracts."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import better_hermes_hindsight.provider as provider_module
from better_hermes_hindsight.client import HindsightClientError
from better_hermes_hindsight.outbox import AdmissionResult, AdmissionStatus
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.runtime import AsyncCallTimeoutError
from tests.unit.test_provider_recall import _base_config, _recall_response, _write_config

_PRIVATE = "private-query-sentinel"


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(record.message) for record in caplog.records if record.message.startswith("{")
    ]


class _Handle:
    def __init__(
        self, *, response: object | None = None, failure: BaseException | None = None
    ) -> None:
        self.response = _recall_response() if response is None else response
        self.failure = failure
        self.result = AdmissionResult(AdmissionStatus.ADMITTED, inserted_count=1)

    def recall(self, _query: str, *, timeout: float) -> object:
        del timeout
        if self.failure is not None:
            raise self.failure
        return self.response

    def admit_turn(self, **_kwargs: object) -> AdmissionResult:
        return self.result

    def close(self) -> None:
        return None


def _initialized_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handle: _Handle,
) -> BetterHindsightMemoryProvider:
    _write_config(tmp_path, _base_config())
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, platform="cli")
    return provider


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (AsyncCallTimeoutError("private timeout"), "timeout"),
        (HindsightClientError("recall_failed", "private client"), "client_error"),
    ],
)
def test_recall_events_separate_failures_and_never_log_private_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: BaseException,
    outcome: str,
) -> None:
    caplog.set_level(logging.INFO)
    provider = _initialized_provider(tmp_path, monkeypatch, _Handle(failure=failure))

    assert provider.prefetch(_PRIVATE) == ""

    event = _events(caplog)[-1]
    assert event["event"] == "better_hindsight.recall"
    assert event["outcome"] == outcome
    assert type(event["elapsed_ms"]) is int
    assert _PRIVATE not in caplog.text


def test_recall_events_report_success_counts_bytes_and_format_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    handle = _Handle()
    provider = _initialized_provider(tmp_path, monkeypatch, handle)
    context = provider.prefetch("safe query")
    success = _events(caplog)[-1]
    assert success == {
        "elapsed_ms": success["elapsed_ms"],
        "event": "better_hindsight.recall",
        "formatted_bytes": len(context.encode()),
        "outcome": "success",
        "result_count": 1,
    }

    handle.response = object()
    assert provider.prefetch("another safe query") == ""
    assert _events(caplog)[-1]["outcome"] == "response_invalid"


def test_admission_event_uses_fixed_outcome_and_bounded_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = _base_config()
    document["retain"] = {"enabled": True}
    _write_config(tmp_path, document)
    handle = _Handle()
    handle.result = AdmissionResult(AdmissionStatus.CAPACITY_EXCEEDED)
    monkeypatch.setattr(provider_module, "acquire_process_runtime", lambda _config: handle)
    provider = BetterHindsightMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, platform="cli", agent_context="primary")
    caplog.set_level(logging.INFO)

    provider.sync_turn(_PRIVATE, "private-assistant-sentinel", session_id="private-id")

    event = _events(caplog)[-1]
    assert event == {
        "duplicate_count": 0,
        "event": "better_hindsight.admission",
        "inserted_count": 0,
        "outcome": "capacity_exceeded",
    }
    assert "private" not in caplog.text
