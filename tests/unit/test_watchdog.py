"""Low-noise aggregate alert evaluator contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from better_hermes_hindsight.watchdog import evaluate_watchdog, main


def _status(*, result: str = "ok") -> dict[str, object]:
    return {"command": "status", "result": result}


def _canary(*, result: str = "ok", error: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"result": result}
    if error is not None:
        payload["error"] = error
    return payload


def _recall(outcome: str) -> dict[str, object]:
    return {"event": "better_hindsight.recall", "outcome": outcome}


def test_watchdog_emits_alert_once_then_one_recovery(tmp_path: Path) -> None:
    state = tmp_path / "watchdog.json"

    first = evaluate_watchdog(
        status=_status(result="degraded"),
        canary=_canary(),
        events=(),
        state_path=state,
    )
    repeated = evaluate_watchdog(
        status=_status(result="degraded"),
        canary=_canary(),
        events=(),
        state_path=state,
    )
    recovery = evaluate_watchdog(
        status=_status(),
        canary=_canary(),
        events=(),
        state_path=state,
    )
    healthy = evaluate_watchdog(
        status=_status(),
        canary=_canary(),
        events=(),
        state_path=state,
    )

    assert first == {
        "event": "better_hindsight.watchdog",
        "reasons": ["local_status_degraded"],
        "result": "alert",
    }
    assert repeated is None
    assert recovery == {"event": "better_hindsight.watchdog", "result": "recovered"}
    assert healthy is None
    assert state.stat().st_mode & 0o777 == 0o600


def test_watchdog_alerts_on_new_retention_failure_and_e2e_failure(tmp_path: Path) -> None:
    state = tmp_path / "watchdog.json"
    events = ({"event": "better_hindsight.sender_attempt", "outcome": "retain_timeout"},)

    result = evaluate_watchdog(
        status=_status(),
        canary=_canary(result="error", error="recall_timeout"),
        events=events,
        state_path=state,
    )

    assert result == {
        "event": "better_hindsight.watchdog",
        "reasons": ["new_retention_failure", "e2e_failed"],
        "result": "alert",
    }
    assert "recall_timeout" not in json.dumps(result)


def test_watchdog_emits_each_new_retention_failure_without_false_recovery(
    tmp_path: Path,
) -> None:
    state = tmp_path / "watchdog.json"
    failure = ({"event": "better_hindsight.sender_attempt", "outcome": "retain_timeout"},)

    first = evaluate_watchdog(status=_status(), canary=_canary(), events=failure, state_path=state)
    quiet = evaluate_watchdog(status=_status(), canary=_canary(), events=(), state_path=state)
    second = evaluate_watchdog(status=_status(), canary=_canary(), events=failure, state_path=state)

    expected = {
        "event": "better_hindsight.watchdog",
        "reasons": ["new_retention_failure"],
        "result": "alert",
    }
    assert first == expected
    assert quiet is None
    assert second == expected


def test_watchdog_uses_bounded_rolling_recall_timeout_rate(tmp_path: Path) -> None:
    state = tmp_path / "watchdog.json"
    events = tuple(_recall("timeout" if index < 2 else "success") for index in range(10))

    result = evaluate_watchdog(
        status=_status(),
        canary=_canary(),
        events=events,
        state_path=state,
        minimum_recall_samples=10,
        recall_timeout_rate=0.2,
        recall_window=20,
    )

    assert result == {
        "event": "better_hindsight.watchdog",
        "reasons": ["recall_timeout_rate_elevated"],
        "result": "alert",
    }
    persisted = json.loads(state.read_text())
    assert persisted["recall_outcomes"] == ["timeout", "timeout"] + ["success"] * 8


def test_watchdog_counts_empty_recall_as_successful_sample(tmp_path: Path) -> None:
    state = tmp_path / "watchdog.json"
    events = tuple(_recall("timeout" if index < 2 else "empty") for index in range(10))

    result = evaluate_watchdog(
        status=_status(),
        canary=_canary(),
        events=events,
        state_path=state,
        minimum_recall_samples=10,
        recall_timeout_rate=0.2,
    )

    assert result is not None
    assert result["reasons"] == ["recall_timeout_rate_elevated"]


def test_watchdog_alerts_on_rolling_recall_error_rate(tmp_path: Path) -> None:
    state = tmp_path / "watchdog.json"
    events = tuple(_recall("client_error" if index < 2 else "success") for index in range(10))

    result = evaluate_watchdog(
        status=_status(),
        canary=_canary(),
        events=events,
        state_path=state,
        minimum_recall_samples=10,
        recall_error_rate=0.2,
    )

    assert result is not None
    assert result["reasons"] == ["recall_error_rate_elevated"]


def test_watchdog_alerts_on_each_new_adapter_sender_and_lifecycle_failure(
    tmp_path: Path,
) -> None:
    state = tmp_path / "watchdog.json"
    failures = (
        {"event": "better_hindsight.http_request", "outcome": "schema_invalid"},
        {"event": "better_hindsight.sender_loop", "outcome": "claim_failed"},
        {"event": "better_hindsight.client_lifecycle", "outcome": "initialization_failed"},
    )

    first = evaluate_watchdog(status=_status(), canary=_canary(), events=failures, state_path=state)
    quiet = evaluate_watchdog(status=_status(), canary=_canary(), events=(), state_path=state)
    second = evaluate_watchdog(
        status=_status(), canary=_canary(), events=failures, state_path=state
    )

    expected = {
        "event": "better_hindsight.watchdog",
        "reasons": [
            "new_adapter_contract_failure",
            "new_sender_loop_failure",
            "new_client_lifecycle_failure",
        ],
        "result": "alert",
    }
    assert first == expected
    assert quiet is None
    assert second == expected


def test_watchdog_reports_persistent_recovery_that_coincides_with_edge_alert(
    tmp_path: Path,
) -> None:
    state = tmp_path / "watchdog.json"
    initial = evaluate_watchdog(
        status=_status(result="degraded"),
        canary=_canary(),
        events=(),
        state_path=state,
    )
    collision = evaluate_watchdog(
        status=_status(),
        canary=_canary(),
        events=(
            {"event": "better_hindsight.client_lifecycle", "outcome": "initialization_failed"},
        ),
        state_path=state,
    )
    quiet = evaluate_watchdog(status=_status(), canary=_canary(), events=(), state_path=state)

    assert initial is not None
    assert collision == {
        "event": "better_hindsight.watchdog",
        "reasons": ["new_client_lifecycle_failure"],
        "resolved_reasons": ["local_status_degraded"],
        "result": "alert",
    }
    assert quiet is None


def test_watchdog_ignores_private_or_unknown_event_fields(tmp_path: Path) -> None:
    state = tmp_path / "watchdog.json"
    private = "private-memory-sentinel"

    result = evaluate_watchdog(
        status=_status(),
        canary=_canary(),
        events=(
            {"event": "better_hindsight.recall", "outcome": "success", "query": private},
            {"event": "unknown", "outcome": private},
        ),
        state_path=state,
    )

    assert result is None
    assert private not in state.read_text()


def test_watchdog_bounds_persisted_recall_window(tmp_path: Path) -> None:
    state = tmp_path / "watchdog.json"

    evaluate_watchdog(
        status=_status(),
        canary=_canary(),
        events=tuple(_recall("success") for _ in range(30)),
        state_path=state,
        recall_window=12,
    )

    persisted = json.loads(state.read_text())
    assert persisted["recall_outcomes"] == ["success"] * 12


def test_watchdog_cli_is_silent_while_healthy_and_emits_transitions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status_path = tmp_path / "status.json"
    canary_path = tmp_path / "canary.json"
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    status_path.write_text('{"result":"ok"}')
    canary_path.write_text('{"result":"ok"}')
    events_path.write_text("")
    arguments = [
        "--status-json",
        str(status_path),
        "--canary-json",
        str(canary_path),
        "--events-jsonl",
        str(events_path),
        "--state",
        str(state_path),
    ]

    assert main(arguments) == 0
    assert capsys.readouterr().out == ""
    status_path.write_text('{"result":"degraded"}')
    assert main(arguments) == 1
    alert = json.loads(capsys.readouterr().out)
    assert alert["result"] == "alert"
    assert main(arguments) == 0
    assert capsys.readouterr().out == ""
    status_path.write_text('{"result":"ok"}')
    assert main(arguments) == 0
    recovery = json.loads(capsys.readouterr().out)
    assert recovery == {"event": "better_hindsight.watchdog", "result": "recovered"}
