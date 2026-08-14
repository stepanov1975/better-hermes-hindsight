"""Bounded low-noise alert evaluation over privacy-safe Better Hindsight signals."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from argparse import ArgumentParser
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import TypedDict


class _WatchdogState(TypedDict):
    active_reasons: list[str]
    recall_outcomes: list[str]


_RECALL_OUTCOMES = frozenset(
    {
        "client_error",
        "format_error",
        "empty",
        "response_invalid",
        "success",
        "timeout",
    }
)
_RETENTION_FAILURES = frozenset({"retain_failed", "retain_timeout", "retain_unconfirmed"})
_RECALL_ERROR_OUTCOMES = frozenset({"client_error", "format_error", "response_invalid"})
_ADAPTER_CONTRACT_FAILURES = frozenset(
    {
        "authentication_failed",
        "client_status",
        "endpoint_not_found",
        "malformed_json",
        "non_json",
        "redirect",
        "response_oversized",
        "schema_invalid",
        "session_closed",
        "unexpected_status",
    }
)
_LIFECYCLE_FAILURES = frozenset({"close_failed", "initialization_failed"})
_SENDER_LOOP_FAILURES = frozenset(
    {
        "claim_failed",
        "ownership_unavailable",
        "recovery_failed",
        "retry_deadline_failed",
        "transition_failed",
    }
)
_REASON_ORDER = (
    "local_status_degraded",
    "new_retention_failure",
    "new_adapter_contract_failure",
    "new_sender_loop_failure",
    "new_client_lifecycle_failure",
    "recall_timeout_rate_elevated",
    "recall_error_rate_elevated",
    "e2e_failed",
)
_MAX_STATE_BYTES = 8_192
_MAX_INPUT_BYTES = 256 * 1024
_MAX_EVENTS = 1_000


def evaluate_watchdog(
    *,
    status: Mapping[str, object],
    canary: Mapping[str, object],
    events: Iterable[Mapping[str, object]],
    state_path: Path,
    minimum_recall_samples: int = 10,
    recall_error_rate: float = 0.2,
    recall_timeout_rate: float = 0.2,
    recall_window: int = 100,
) -> dict[str, object] | None:
    """Return one alert transition/recovery, or ``None`` while state is unchanged.

    ``events`` must contain only newly collected structured events. Only fixed event and outcome
    values are retained; unknown fields are deliberately discarded.
    """

    _validate_thresholds(
        minimum_recall_samples=minimum_recall_samples,
        recall_error_rate=recall_error_rate,
        recall_timeout_rate=recall_timeout_rate,
        recall_window=recall_window,
    )
    previous = _read_state(state_path)
    recall_outcomes = list(previous["recall_outcomes"])
    new_retention_failure = False
    new_adapter_contract_failure = False
    new_sender_loop_failure = False
    new_client_lifecycle_failure = False
    for event in events:
        event_name = event.get("event")
        outcome = event.get("outcome")
        if event_name == "better_hindsight.recall" and outcome in _RECALL_OUTCOMES:
            recall_outcomes.append(str(outcome))
        elif event_name == "better_hindsight.sender_attempt" and (
            outcome in _RETENTION_FAILURES or event.get("category") in _RETENTION_FAILURES
        ):
            new_retention_failure = True
        elif (
            event_name == "better_hindsight.http_request" and outcome in _ADAPTER_CONTRACT_FAILURES
        ):
            new_adapter_contract_failure = True
        elif event_name == "better_hindsight.sender_loop" and outcome in _SENDER_LOOP_FAILURES:
            new_sender_loop_failure = True
        elif event_name == "better_hindsight.client_lifecycle" and outcome in _LIFECYCLE_FAILURES:
            new_client_lifecycle_failure = True
    recall_outcomes = recall_outcomes[-recall_window:]

    reasons: set[str] = set()
    if status.get("result") != "ok":
        reasons.add("local_status_degraded")

    sample_count = len(recall_outcomes)
    if sample_count >= minimum_recall_samples:
        timeout_count = sum(outcome == "timeout" for outcome in recall_outcomes)
        if timeout_count / sample_count >= recall_timeout_rate:
            reasons.add("recall_timeout_rate_elevated")
        error_count = sum(outcome in _RECALL_ERROR_OUTCOMES for outcome in recall_outcomes)
        if error_count / sample_count >= recall_error_rate:
            reasons.add("recall_error_rate_elevated")
    if canary.get("result") != "ok":
        reasons.add("e2e_failed")

    persistent_reasons = [reason for reason in _REASON_ORDER if reason in reasons]
    alert_reasons = set(reasons)
    if new_retention_failure:
        alert_reasons.add("new_retention_failure")
    if new_adapter_contract_failure:
        alert_reasons.add("new_adapter_contract_failure")
    if new_sender_loop_failure:
        alert_reasons.add("new_sender_loop_failure")
    if new_client_lifecycle_failure:
        alert_reasons.add("new_client_lifecycle_failure")
    ordered_reasons = [reason for reason in _REASON_ORDER if reason in alert_reasons]
    previous_reasons = previous["active_reasons"]
    _write_state(
        state_path,
        {
            "active_reasons": persistent_reasons,
            "recall_outcomes": recall_outcomes,
            "version": 1,
        },
    )
    if ordered_reasons:
        if (
            not any(
                (
                    new_retention_failure,
                    new_adapter_contract_failure,
                    new_sender_loop_failure,
                    new_client_lifecycle_failure,
                )
            )
            and persistent_reasons == previous_reasons
        ):
            return None
        result: dict[str, object] = {
            "event": "better_hindsight.watchdog",
            "reasons": ordered_reasons,
            "result": "alert",
        }
        resolved_reasons = [
            reason for reason in previous_reasons if reason not in persistent_reasons
        ]
        if resolved_reasons:
            result["resolved_reasons"] = resolved_reasons
        return result
    if previous_reasons:
        return {"event": "better_hindsight.watchdog", "result": "recovered"}
    return None


def _validate_thresholds(
    *,
    minimum_recall_samples: int,
    recall_error_rate: float,
    recall_timeout_rate: float,
    recall_window: int,
) -> None:
    if type(minimum_recall_samples) is not int or minimum_recall_samples <= 0:
        raise ValueError("minimum_recall_samples must be a positive integer")
    if type(recall_window) is not int or recall_window <= 0:
        raise ValueError("recall_window must be a positive integer")
    if minimum_recall_samples > recall_window:
        raise ValueError("minimum_recall_samples must not exceed recall_window")
    if (
        isinstance(recall_error_rate, bool)
        or not isinstance(recall_error_rate, (int, float))
        or not math.isfinite(float(recall_error_rate))
        or not 0.0 < float(recall_error_rate) <= 1.0
    ):
        raise ValueError("recall_error_rate must be in (0, 1]")
    if (
        isinstance(recall_timeout_rate, bool)
        or not isinstance(recall_timeout_rate, (int, float))
        or not math.isfinite(float(recall_timeout_rate))
        or not 0.0 < float(recall_timeout_rate) <= 1.0
    ):
        raise ValueError("recall_timeout_rate must be in (0, 1]")


def _read_state(path: Path) -> _WatchdogState:
    empty: _WatchdogState = {"active_reasons": [], "recall_outcomes": []}
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_STATE_BYTES:
            return empty
        document = json.loads(raw)
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return empty
    if not isinstance(document, dict):
        return empty
    reasons = document.get("active_reasons")
    outcomes = document.get("recall_outcomes")
    if not isinstance(reasons, list) or not isinstance(outcomes, list):
        return empty
    safe_reasons = [reason for reason in _REASON_ORDER if reason in reasons]
    safe_outcomes = [str(outcome) for outcome in outcomes if outcome in _RECALL_OUTCOMES]
    return {"active_reasons": safe_reasons, "recall_outcomes": safe_outcomes}


def _write_state(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(encoded) > _MAX_STATE_BYTES:
        raise ValueError("watchdog state exceeded its fixed bound")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _read_json_object(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("input exceeded its fixed bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("input must be one JSON object")
    return {str(key): item for key, item in value.items()}


def _read_events(path: Path) -> list[dict[str, object]]:
    raw = path.read_bytes()
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("event input exceeded its fixed bound")
    events: list[dict[str, object]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("event input must contain JSON objects")
        events.append({str(key): item for key, item in value.items()})
        if len(events) > _MAX_EVENTS:
            raise ValueError("event count exceeded its fixed bound")
    return events


def main(argv: list[str] | None = None) -> int:
    """Evaluate bounded JSON artifacts and emit only alert transitions or recovery."""

    parser = ArgumentParser(prog="better-hindsight-watchdog", allow_abbrev=False)
    parser.add_argument("--status-json", type=Path, required=True)
    parser.add_argument("--canary-json", type=Path, required=True)
    parser.add_argument("--events-jsonl", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--minimum-recall-samples", type=int, default=10)
    parser.add_argument("--recall-error-rate", type=float, default=0.2)
    parser.add_argument("--recall-timeout-rate", type=float, default=0.2)
    parser.add_argument("--recall-window", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        result = evaluate_watchdog(
            status=_read_json_object(args.status_json),
            canary=_read_json_object(args.canary_json),
            events=_read_events(args.events_jsonl),
            state_path=args.state,
            minimum_recall_samples=args.minimum_recall_samples,
            recall_error_rate=args.recall_error_rate,
            recall_timeout_rate=args.recall_timeout_rate,
            recall_window=args.recall_window,
        )
    except Exception:
        print('{"event":"better_hindsight.watchdog","result":"evaluation_failed"}')
        return 2
    if result is None:
        return 0
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 1 if result.get("result") == "alert" else 0


__all__ = ["evaluate_watchdog", "main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
