"""Process-local, consume-once handoff for contextual recall plans.

Hermes loads the standalone companion and the exclusive memory provider under
different module names, but both run in the same interpreter.  A tiny registry
stored under a stable ``sys.modules`` key bridges those import namespaces
without turning one-turn planner state into durable database state.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import sqlite3
import sys
import threading
import time
import types
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

PlanMode = Literal["shadow", "active"]
PlanAction = Literal["skip", "reuse", "recall"]

_PLAN_MAX_AGE_SECONDS = 10.0
_SHARED_REGISTRY_MODULE = "_better_hermes_hindsight_plan_registry_v1"
_LEGACY_SCHEMA_VERSIONS = frozenset({1, 2, 3})
_LEGACY_REQUIRED_COLUMNS = {
    "active_session": frozenset({"session_id"}),
    "recall_plan": frozenset(
        {"turn_id", "query_digest", "mode", "action", "rewritten_query", "expires_at"}
    ),
}


class PlanMailboxError(RuntimeError):
    """The process-local recall-plan handoff is unavailable."""


@dataclass(frozen=True, slots=True)
class RecallPlan:
    """One validated planner decision consumed by the provider."""

    mode: PlanMode
    action: PlanAction
    rewritten_query: str | None
    turn_id: str


@dataclass(slots=True)
class _StoredPlan:
    sequence: int
    query_digest: str
    session_id: str
    mode: PlanMode
    expires_at: float
    publish_before: float | None = None
    action: PlanAction | None = None
    rewritten_query: str | None = None


@dataclass(slots=True)
class _HomeState:
    activations: dict[str, str] = field(default_factory=dict)
    plans: dict[str, _StoredPlan] = field(default_factory=dict)
    sequence: int = 0


@dataclass(slots=True)
class _RegistryState:
    lock: threading.RLock
    homes: dict[str, _HomeState]


def _shared_registry() -> _RegistryState:
    """Return one registry even when this file has multiple import names."""

    candidate = types.ModuleType(_SHARED_REGISTRY_MODULE)
    candidate.__dict__["state"] = _RegistryState(lock=threading.RLock(), homes={})
    shared = sys.modules.setdefault(_SHARED_REGISTRY_MODULE, candidate)
    state = getattr(shared, "state", None)
    lock = getattr(state, "lock", None)
    homes = getattr(state, "homes", None)
    if lock is None or not hasattr(lock, "__enter__") or not isinstance(homes, dict):
        raise PlanMailboxError("recall plan handoff unavailable")
    return cast(_RegistryState, state)


def _query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8", errors="surrogatepass")).hexdigest()


class InMemoryPlanMailbox:
    """A profile-scoped, thread-safe, consume-once plan rendezvous."""

    __slots__ = ("_clock", "_home_key", "_registry")

    def __init__(
        self,
        hermes_home: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            home = hermes_home.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise PlanMailboxError("recall plan handoff unavailable") from None
        if not home.is_absolute():
            raise PlanMailboxError("recall plan handoff unavailable")
        self._home_key = str(home)
        self._clock = monotonic
        self._registry = _shared_registry()

    def activate(self, *, session_id: str) -> str:
        """Activate one provider handle and return its exact release token."""

        _require_text(session_id, "session_id")
        token = uuid.uuid4().hex
        now = self._clock()
        with self._registry.lock:
            state = self._home_state(create=True)
            assert state is not None
            self._purge_expired(state, now)
            state.activations[token] = session_id
        return token

    def deactivate(self, *, token: str) -> None:
        """Release exactly one provider handle and its orphaned session plans."""

        _require_text(token, "token")
        now = self._clock()
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return
            self._purge_expired(state, now)
            session_id = state.activations.pop(token, None)
            if session_id is not None and session_id not in state.activations.values():
                self._clear_session(state, session_id)
            self._drop_empty_home(state)

    def rebind(self, *, token: str, new_session_id: str) -> None:
        """Move one provider activation to a new Hermes session atomically."""

        _require_text(token, "token")
        _require_text(new_session_id, "new_session_id")
        now = self._clock()
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None or token not in state.activations:
                raise PlanMailboxError("recall plan handoff unavailable")
            self._purge_expired(state, now)
            old_session_id = state.activations[token]
            state.activations[token] = new_session_id
            if old_session_id not in state.activations.values():
                self._clear_session(state, old_session_id)
            self._clear_session(state, new_session_id)

    def is_active(self, *, session_id: str) -> bool:
        """Return whether an authorized provider handle owns this session."""

        _require_text(session_id, "session_id")
        now = self._clock()
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return False
            self._purge_expired(state, now)
            return session_id in state.activations.values()

    def reserve(
        self,
        *,
        source_query: str,
        session_id: str,
        parent_session_id: str,
        turn_id: str,
        mode: PlanMode,
        publish_timeout_seconds: float | None = None,
    ) -> bool:
        """Reserve one active turn before starting its bounded planner call."""

        del parent_session_id
        _require_text(source_query, "source_query")
        _require_text(session_id, "session_id")
        _require_text(turn_id, "turn_id")
        _require_mode(mode)
        if publish_timeout_seconds is not None and (
            not math.isfinite(publish_timeout_seconds) or publish_timeout_seconds <= 0
        ):
            raise ValueError("publish_timeout_seconds must be finite and positive")
        now = self._clock()
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return False
            self._purge_expired(state, now)
            if session_id not in state.activations.values() or turn_id in state.plans:
                return False
            state.sequence += 1
            state.plans[turn_id] = _StoredPlan(
                sequence=state.sequence,
                query_digest=_query_digest(source_query),
                session_id=session_id,
                mode=mode,
                expires_at=now + _PLAN_MAX_AGE_SECONDS,
                publish_before=(
                    None if publish_timeout_seconds is None else now + publish_timeout_seconds
                ),
            )
            return True

    def finalize(
        self,
        *,
        turn_id: str,
        mode: PlanMode,
        action: PlanAction,
        rewritten_query: str | None,
    ) -> bool:
        """Finalize an existing reservation unless it was consumed or expired."""

        _require_text(turn_id, "turn_id")
        _require_mode(mode)
        _require_action(action, rewritten_query)
        now = self._clock()
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return False
            self._purge_expired(state, now)
            plan = state.plans.get(turn_id)
            if plan is None or plan.action is not None or plan.mode != mode:
                return False
            if plan.publish_before is not None and now >= plan.publish_before:
                return False
            plan.action = action
            plan.rewritten_query = rewritten_query
            return True

    def cancel(self, *, turn_id: str) -> None:
        """Cancel one reservation after a shadow planner failure."""

        _require_text(turn_id, "turn_id")
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return
            state.plans.pop(turn_id, None)
            self._drop_empty_home(state)

    def consume(self, *, source_query: str, session_id: str) -> RecallPlan | None:
        """Consume the newest exact plan and fence all matching late workers."""

        _require_text(source_query, "source_query")
        _require_text(session_id, "session_id")
        now = self._clock()
        digest = _query_digest(source_query)
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return None
            self._purge_expired(state, now)
            if session_id not in state.activations.values():
                return None
            matches = [
                (turn_id, plan)
                for turn_id, plan in state.plans.items()
                if plan.session_id == session_id and plan.query_digest == digest
            ]
            if not matches:
                return None
            newest_turn_id, newest = max(matches, key=lambda item: item[1].sequence)
            for turn_id, _plan in matches:
                del state.plans[turn_id]
            if newest.action is None:
                return None
            return RecallPlan(
                mode=newest.mode,
                action=newest.action,
                rewritten_query=newest.rewritten_query,
                turn_id=newest_turn_id,
            )

    def clear_session_plans(self, *, session_id: str) -> None:
        """Invalidate pending and ready plans after a same-session rewind."""

        _require_text(session_id, "session_id")
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return
            self._clear_session(state, session_id)
            self._drop_empty_home(state)

    def purge_stale(self) -> None:
        """Drop expired process-local plans for this profile."""

        now = self._clock()
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return
            self._purge_expired(state, now)
            self._drop_empty_home(state)

    def _home_state(self, *, create: bool) -> _HomeState | None:
        state = self._registry.homes.get(self._home_key)
        if state is None and create:
            state = _HomeState()
            self._registry.homes[self._home_key] = state
        return state

    def _drop_empty_home(self, state: _HomeState) -> None:
        if not state.activations and not state.plans:
            self._registry.homes.pop(self._home_key, None)

    @staticmethod
    def _clear_session(state: _HomeState, session_id: str) -> None:
        stale = [turn_id for turn_id, plan in state.plans.items() if plan.session_id == session_id]
        for turn_id in stale:
            del state.plans[turn_id]

    @staticmethod
    def _purge_expired(state: _HomeState, now: float) -> None:
        stale = [turn_id for turn_id, plan in state.plans.items() if plan.expires_at <= now]
        for turn_id in stale:
            del state.plans[turn_id]


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_mode(mode: str) -> None:
    if mode not in {"shadow", "active"}:
        raise ValueError("mode must be shadow or active")


def _require_action(action: str, rewritten_query: str | None) -> None:
    if action not in {"skip", "reuse", "recall"}:
        raise ValueError("action must be skip, reuse, or recall")
    if action == "recall":
        if not isinstance(rewritten_query, str) or not rewritten_query:
            raise ValueError("recall action requires rewritten_query")
    elif rewritten_query is not None:
        raise ValueError("only recall action accepts rewritten_query")


def remove_legacy_plan_mailbox(path: Path) -> bool:
    """Remove a verified obsolete SQLite mailbox and its sidecars."""

    if not path.is_absolute():
        raise PlanMailboxError("legacy recall plan mailbox path is not absolute")
    if not path.exists():
        return False
    if not _is_legacy_plan_mailbox(path):
        raise PlanMailboxError("legacy recall plan mailbox cleanup refused an unverified file")
    removed = False
    failed = False
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            failed = True
        else:
            removed = True
    if failed:
        raise PlanMailboxError("legacy recall plan mailbox cleanup failed")
    return removed


def _is_legacy_plan_mailbox(path: Path) -> bool:
    try:
        uri = f"{path.as_uri()}?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.0)) as connection:
            version_row = connection.execute("PRAGMA user_version").fetchone()
            if version_row is None or int(version_row[0]) not in _LEGACY_SCHEMA_VERSIONS:
                return False
            for table, required in _LEGACY_REQUIRED_COLUMNS.items():
                columns = {
                    str(row[1])
                    for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                if not required.issubset(columns):
                    return False
    except (OSError, ValueError, sqlite3.Error):
        return False
    return True


__all__ = [
    "InMemoryPlanMailbox",
    "PlanAction",
    "PlanMailboxError",
    "PlanMode",
    "RecallPlan",
    "remove_legacy_plan_mailbox",
]
