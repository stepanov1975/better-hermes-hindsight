"""Privacy-safe production-minimum structured event contracts."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

import better_hermes_hindsight.provider as provider_module
import better_hermes_hindsight.telemetry as telemetry_module
from better_hermes_hindsight import __version__
from better_hermes_hindsight.client import HindsightClientError
from better_hermes_hindsight.outbox import AdmissionResult, AdmissionStatus
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.runtime import AsyncCallTimeoutError
from better_hermes_hindsight.telemetry import deployed_identity
from tests.unit.test_provider_recall import _base_config, _recall_response, _write_config

_PRIVATE = "private-query-sentinel"


def _write_install_metadata(hermes_home: Path, revision: object) -> None:
    path = hermes_home / "plugins/.install-metadata.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"better_hindsight": {"revision": revision}}),
        encoding="utf-8",
    )


def test_deployed_identity_uses_standard_plugin_metadata_and_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BETTER_HINDSIGHT_COMMIT", raising=False)
    _write_install_metadata(tmp_path, "B" * 40)
    assert deployed_identity(tmp_path) == {"commit": "b" * 40, "version": __version__}

    monkeypatch.setenv("BETTER_HINDSIGHT_COMMIT", "not-a-commit")
    assert deployed_identity(tmp_path)["commit"] == "b" * 40

    monkeypatch.setenv("BETTER_HINDSIGHT_COMMIT", "A" * 40)
    assert deployed_identity(tmp_path)["commit"] == "a" * 40


def test_deployed_identity_falls_back_to_standard_git_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BETTER_HINDSIGHT_COMMIT", raising=False)
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=plugin_root, check=True)
    subprocess.run(["git", "config", "user.name", "Identity fixture"], cwd=plugin_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "identity@example.invalid"],
        cwd=plugin_root,
        check=True,
    )
    (plugin_root / "plugin.yaml").write_text("name: fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "plugin.yaml"], cwd=plugin_root, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "fixture plugin"], cwd=plugin_root, check=True
    )
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=plugin_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(telemetry_module, "_plugin_root", lambda: plugin_root)

    assert deployed_identity(tmp_path)["commit"] == expected

    subprocess.run(["git", "pack-refs", "--all", "--prune"], cwd=plugin_root, check=True)
    assert deployed_identity(tmp_path)["commit"] == expected


def test_deployed_identity_returns_unknown_when_sources_are_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BETTER_HINDSIGHT_COMMIT", "not-a-commit")
    metadata_path = tmp_path / "plugins/.install-metadata.json"
    _write_install_metadata(tmp_path, "also-not-a-commit")
    monkeypatch.setattr(telemetry_module, "_checkout_commit", lambda _path: None)

    assert deployed_identity(tmp_path) == {"commit": "unknown", "version": __version__}

    metadata_path.write_text("[]\n", encoding="utf-8")
    assert deployed_identity(tmp_path) == {"commit": "unknown", "version": __version__}


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
