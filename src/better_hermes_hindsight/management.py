"""Bounded operator diagnostics and explicit Hindsight mission management."""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar, cast

from better_hermes_hindsight.client import (
    HindsightClientProtocol,
    MissionClientProtocol,
    MissionSnapshot,
    MissionValue,
)
from better_hermes_hindsight.config import BetterHindsightConfig
from better_hermes_hindsight.outbox import OutboxInspection, inspect_outbox
from better_hermes_hindsight.runtime import create_operator_runtime

_T = TypeVar("_T")
_MissionField = Literal["retain_mission", "observations_mission"]
_MissionState = Literal["equal", "drift", "missing", "error"]


class _OperatorRuntime(Protocol):
    def call(
        self,
        operation: Callable[[HindsightClientProtocol], Awaitable[_T]],
        *,
        timeout: float | None,
    ) -> _T: ...

    def finalize(self) -> bool: ...


_RuntimeFactory = Callable[[BetterHindsightConfig], _OperatorRuntime]


@dataclass(frozen=True, slots=True)
class ManagementResult:
    """One already-sanitized command payload and its released-host exit status."""

    payload: dict[str, object]
    exit_code: int


def _fixed_error(command: str, error: str) -> ManagementResult:
    return ManagementResult(
        payload={"command": command, "error": error, "result": "error"},
        exit_code=3,
    )


def _authorized(config: BetterHindsightConfig) -> bool:
    return config.authorize_cli().identity_authorized


def status(
    config: BetterHindsightConfig,
    *,
    now: float | None = None,
) -> ManagementResult:
    """Return one passive, coherent local queue snapshot."""

    if not _authorized(config):
        return _fixed_error("status", "authorization_required")
    observed_at = time.time() if now is None else now
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, (int, float))
        or not math.isfinite(float(observed_at))
        or float(observed_at) < 0.0
    ):
        return _fixed_error("status", "status_unavailable")
    try:
        inspection = inspect_outbox(config)
        payload = _status_payload(inspection, now=float(observed_at))
    except Exception:
        return _fixed_error("status", "status_unavailable")
    return ManagementResult(
        payload=payload,
        exit_code=1 if inspection.mismatch_count else 0,
    )


def _status_payload(inspection: OutboxInspection, *, now: float) -> dict[str, object]:
    if inspection.oldest_created_at is None:
        age_bucket = "none"
    else:
        age = max(0.0, now - inspection.oldest_created_at)
        if age < 60.0:
            age_bucket = "lt_1m"
        elif age < 3_600.0:
            age_bucket = "1m_to_lt_1h"
        elif age < 86_400.0:
            age_bucket = "1h_to_lt_24h"
        else:
            age_bucket = "gte_24h"
    return {
        "age_bucket": age_bucket,
        "command": "status",
        "counts": {
            "mismatch": inspection.mismatch_count,
            "pending": inspection.pending_count,
            "retry": inspection.retry_count,
            "sending": inspection.sending_count,
        },
        "last_error_category": inspection.last_error_category or "none",
        "logical_queued_bytes": inspection.logical_queued_bytes,
        "outbox": inspection.outbox,
        "result": "degraded" if inspection.mismatch_count else "ok",
        "sender_ownership": inspection.sender_ownership,
    }


def check_missions(
    config: BetterHindsightConfig,
    *,
    runtime_factory: _RuntimeFactory = create_operator_runtime,
) -> ManagementResult:
    """Read the selected bank once and compare configured mission bytes exactly."""

    command = "missions_check"
    if not _authorized(config):
        return _fixed_error(command, "authorization_required")
    try:
        runtime = runtime_factory(config)
    except Exception:
        return _fixed_error(command, "mission_check_unavailable")

    result: ManagementResult
    try:
        deadline = time.monotonic() + config.retain.timeout_seconds
        try:
            snapshot = _runtime_call(
                runtime,
                lambda client: cast(MissionClientProtocol, client).get_bank_config(),
                deadline=deadline,
            )
            result = _check_result(config, snapshot)
        except Exception:
            result = _failed_check_result(config)
    finally:
        try:
            finalized = runtime.finalize()
        except Exception:
            finalized = False
    if not finalized:
        return _fixed_error(command, "runtime_cleanup_failed")
    return result


def _failed_check_result(config: BetterHindsightConfig) -> ManagementResult:
    return ManagementResult(
        payload={
            "command": "missions_check",
            "observations_mission": (
                "error" if config.missions.observations_mission is not None else "missing"
            ),
            "result": "error",
            "retain_mission": "error" if config.missions.retain_mission is not None else "missing",
        },
        exit_code=3,
    )


def _check_result(
    config: BetterHindsightConfig,
    snapshot: MissionSnapshot,
) -> ManagementResult:
    retain_state = _field_state(config.missions.retain_mission, snapshot.retain_mission)
    observations_state = _field_state(
        config.missions.observations_mission,
        snapshot.observations_mission,
    )
    states = (retain_state, observations_state)
    if "missing" in states:
        overall = "missing"
    elif "drift" in states:
        overall = "drift"
    else:
        overall = "equal"
    return ManagementResult(
        payload={
            "command": "missions_check",
            "observations_mission": observations_state,
            "result": overall,
            "retain_mission": retain_state,
        },
        exit_code=0 if overall == "equal" else 1,
    )


def _field_state(desired: str | None, remote: MissionValue) -> _MissionState:
    if desired is None:
        return "missing"
    if not remote.present or remote.value is None or not remote.value.strip():
        return "missing"
    if remote.value == desired:
        return "equal"
    return "drift"


def apply_missions(
    config: BetterHindsightConfig,
    *,
    confirmed: bool,
    runtime_factory: _RuntimeFactory = create_operator_runtime,
) -> ManagementResult:
    """Apply configured drift once, then require exact PATCH and GET confirmation."""

    if confirmed is not True:
        raise PermissionError("Better Hindsight mission apply requires explicit confirmation.")
    command = "missions_apply"
    if not _authorized(config):
        return _fixed_error(command, "authorization_required")
    desired = _desired_missions(config)
    if not desired:
        return _fixed_error(command, "mission_configuration_missing")
    try:
        runtime = runtime_factory(config)
    except Exception:
        return _fixed_error(command, "mission_prewrite_unavailable")

    write_attempted = False
    result: ManagementResult
    deadline = time.monotonic() + config.retain.timeout_seconds
    try:
        try:
            before = _runtime_call(
                runtime,
                lambda client: cast(MissionClientProtocol, client).get_bank_config(),
                deadline=deadline,
            )
            updates = _mission_updates(desired, before)
        except Exception:
            result = _fixed_error(command, "mission_prewrite_unavailable")
        else:
            if not updates:
                result = ManagementResult(
                    payload={
                        "command": command,
                        "outcome": "already_equal",
                        "result": "ok",
                    },
                    exit_code=0,
                )
            else:

                async def dispatch_patch(client: object) -> MissionSnapshot:
                    nonlocal write_attempted
                    write_attempted = True
                    return await cast(MissionClientProtocol, client).update_bank_missions(updates)

                try:
                    patched = _runtime_call(
                        runtime,
                        dispatch_patch,
                        deadline=deadline,
                    )
                    expected = _expected_snapshot(before, updates)
                    if patched != expected:
                        raise RuntimeError
                    readback = _runtime_call(
                        runtime,
                        lambda client: cast(MissionClientProtocol, client).get_bank_config(),
                        deadline=deadline,
                    )
                    if readback != expected:
                        raise RuntimeError
                except Exception:
                    result = (
                        _unknown_write_result()
                        if write_attempted
                        else _fixed_error(command, "mission_prewrite_unavailable")
                    )
                else:
                    result = ManagementResult(
                        payload={
                            "command": command,
                            "outcome": "verified_success",
                            "result": "ok",
                        },
                        exit_code=0,
                    )
    finally:
        try:
            finalized = runtime.finalize()
        except Exception:
            finalized = False
    if not finalized:
        return (
            _unknown_write_result()
            if write_attempted
            else _fixed_error(command, "runtime_cleanup_failed")
        )
    return result


def _desired_missions(config: BetterHindsightConfig) -> dict[_MissionField, str]:
    desired: dict[_MissionField, str] = {}
    if config.missions.retain_mission is not None:
        desired["retain_mission"] = config.missions.retain_mission
    if config.missions.observations_mission is not None:
        desired["observations_mission"] = config.missions.observations_mission
    return desired


def _mission_updates(
    desired: Mapping[_MissionField, str],
    before: MissionSnapshot,
) -> dict[str, str]:
    updates: dict[str, str] = {}
    for field, value in desired.items():
        if getattr(before, field) != MissionValue(present=True, value=value):
            updates[field] = value
    return updates


def _expected_snapshot(
    before: MissionSnapshot,
    updates: Mapping[str, str],
) -> MissionSnapshot:
    retain = before.retain_mission
    observations = before.observations_mission
    if "retain_mission" in updates:
        retain = MissionValue(present=True, value=updates["retain_mission"])
    if "observations_mission" in updates:
        observations = MissionValue(present=True, value=updates["observations_mission"])
    return MissionSnapshot(retain_mission=retain, observations_mission=observations)


def _unknown_write_result() -> ManagementResult:
    return ManagementResult(
        payload={
            "command": "missions_apply",
            "outcome": "write_attempted_outcome_unknown",
            "result": "error",
        },
        exit_code=4,
    )


def _runtime_call(
    runtime: _OperatorRuntime,
    operation: Callable[[HindsightClientProtocol], Awaitable[_T]],
    *,
    deadline: float,
) -> _T:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0 or not math.isfinite(remaining):
        raise TimeoutError
    return runtime.call(operation, timeout=float(remaining))


__all__ = [
    "ManagementResult",
    "apply_missions",
    "check_missions",
    "status",
]
