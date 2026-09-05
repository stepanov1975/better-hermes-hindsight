"""Process-local, consume-once handoff for contextual recall plans.

Hermes loads the standalone companion and the exclusive memory provider under
different module names, but both run in the same interpreter.  A tiny registry
stored under a stable ``sys.modules`` key bridges those import namespaces
without turning one-turn planner state into durable database state.
"""

from __future__ import annotations

import hashlib
import math
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
    query_length: int
    session_id: str
    mode: PlanMode
    expires_at: float
    owner_token: str | None = None
    publish_before: float | None = None
    action: PlanAction | None = None
    rewritten_query: str | None = None


@dataclass(slots=True)
class _HomeState:
    activations: dict[str, str] = field(default_factory=dict)
    closed_turns: dict[tuple[str, str], float] = field(default_factory=dict)
    current_turns: dict[str, tuple[str, str]] = field(default_factory=dict)
    plans: dict[str, _StoredPlan] = field(default_factory=dict)
    redirects: dict[str, tuple[str, str]] = field(default_factory=dict)
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
        with self._registry.lock:
            now = self._clock()
            state = self._home_state(create=True)
            assert state is not None
            self._purge_expired(state, now)
            state.activations[token] = session_id
        return token

    def deactivate(self, *, token: str) -> None:
        """Release exactly one provider handle and its orphaned session plans."""

        _require_text(token, "token")
        with self._registry.lock:
            now = self._clock()
            state = self._home_state(create=False)
            if state is None:
                return
            self._purge_expired(state, now)
            session_id = state.activations.pop(token, None)
            if session_id is not None and session_id not in state.activations.values():
                self._clear_session(state, session_id)
            self._drop_empty_home(state)

    def rebind(
        self,
        *,
        token: str,
        new_session_id: str,
        expected_parent_session_id: str = "",
    ) -> bool:
        """Move one activation only from the expected Hermes session."""

        _require_text(token, "token")
        _require_text(new_session_id, "new_session_id")
        if expected_parent_session_id and not isinstance(expected_parent_session_id, str):
            raise ValueError("expected_parent_session_id must be a string")
        with self._registry.lock:
            now = self._clock()
            state = self._home_state(create=False)
            if state is None or token not in state.activations:
                raise PlanMailboxError("recall plan handoff unavailable")
            self._purge_expired(state, now)
            old_session_id = state.activations[token]
            if old_session_id == new_session_id:
                return True
            if expected_parent_session_id and old_session_id != expected_parent_session_id:
                return False
            state.activations[token] = new_session_id
            if old_session_id not in state.activations.values():
                self._clear_session(state, old_session_id)
            self._clear_session(state, new_session_id)
            return True

    def is_active(self, *, session_id: str) -> bool:
        """Return whether an authorized provider handle owns this session."""

        _require_text(session_id, "session_id")
        with self._registry.lock:
            now = self._clock()
            state = self._home_state(create=False)
            if state is None:
                return False
            self._purge_expired(state, now)
            return session_id in state.activations.values()

    def begin_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        parent_session_id: str = "",
    ) -> str | None:
        """Clear prior plans and return an owner token for the current turn."""

        _require_text(session_id, "session_id")
        _require_text(turn_id, "turn_id")
        owner_token = uuid.uuid4().hex
        with self._registry.lock:
            now = self._clock()
            state = self._home_state(create=False)
            if state is None:
                return None
            self._purge_expired(state, now)
            if (session_id, turn_id) in state.closed_turns:
                return None
            session_active = session_id in state.activations.values()
            state.redirects = {
                source: redirect
                for source, redirect in state.redirects.items()
                if redirect[0] != session_id or redirect[1] == turn_id
            }
            parent_active = bool(parent_session_id) and (
                parent_session_id in state.activations.values()
            )
            if parent_active:
                state.redirects = {
                    source: redirect
                    for source, redirect in state.redirects.items()
                    if source not in {parent_session_id, session_id}
                    and redirect[0] not in {parent_session_id, session_id}
                }
                state.redirects[parent_session_id] = (session_id, turn_id)
                for token, active_session in tuple(state.activations.items()):
                    if active_session == parent_session_id:
                        state.activations[token] = session_id
                self._clear_session(state, parent_session_id)
                session_active = True
            if not session_active:
                return None
            self._clear_session(state, session_id, include_closed=False)
            state.current_turns[session_id] = (turn_id, owner_token)
            return owner_token

    def reserve(
        self,
        *,
        source_query: str,
        session_id: str,
        parent_session_id: str,
        turn_id: str,
        mode: PlanMode,
        publish_timeout_seconds: float | None = None,
        owner_token: str | None = None,
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
        with self._registry.lock:
            now = self._clock()
            state = self._home_state(create=False)
            if state is None:
                return False
            self._purge_expired(state, now)
            if session_id not in state.activations.values():
                return False
            current_turn = state.current_turns.get(session_id)
            if current_turn is not None and current_turn != (turn_id, owner_token):
                return False
            if current_turn is None and owner_token is not None:
                return False
            digest = _query_digest(source_query)
            publish_before = (
                None if publish_timeout_seconds is None else now + publish_timeout_seconds
            )
            existing = state.plans.get(turn_id)
            if existing is not None:
                return False
            state.sequence += 1
            state.plans[turn_id] = _StoredPlan(
                sequence=state.sequence,
                query_digest=digest,
                query_length=len(source_query),
                session_id=session_id,
                mode=mode,
                expires_at=now + _PLAN_MAX_AGE_SECONDS,
                owner_token=owner_token,
                publish_before=publish_before,
            )
            return True

    def finalize(
        self,
        *,
        turn_id: str,
        mode: PlanMode,
        action: PlanAction,
        rewritten_query: str | None,
        owner_token: str | None = None,
    ) -> bool:
        """Finalize an existing reservation unless it was consumed or expired."""

        _require_text(turn_id, "turn_id")
        _require_mode(mode)
        _require_action(action, rewritten_query)
        with self._registry.lock:
            now = self._clock()
            state = self._home_state(create=False)
            if state is None:
                return False
            self._purge_expired(state, now)
            plan = state.plans.get(turn_id)
            if (
                plan is None
                or plan.action is not None
                or plan.mode != mode
                or plan.owner_token != owner_token
            ):
                return False
            deadline_expired = plan.publish_before is not None and now >= plan.publish_before
            if deadline_expired and not (mode == "active" and action == "skip"):
                return False
            plan.action = action
            plan.rewritten_query = rewritten_query
            return True

    def cancel(self, *, turn_id: str, owner_token: str | None = None) -> None:
        """Cancel one reservation after a shadow planner failure."""

        _require_text(turn_id, "turn_id")
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return
            plan = state.plans.get(turn_id)
            if plan is not None and plan.owner_token == owner_token:
                del state.plans[turn_id]
            self._drop_empty_home(state)

    def consume(self, *, source_query: str, session_id: str) -> RecallPlan | None:
        """Consume the newest exact plan and fence all matching late workers."""

        _require_text(source_query, "source_query")
        _require_text(session_id, "session_id")
        with self._registry.lock:
            now = self._clock()
            state = self._home_state(create=False)
            if state is None:
                return None
            self._purge_expired(state, now)
            resolved = self._resolve_active_session(state, session_id)
            if resolved is None:
                return None
            effective_session_id, redirected_turn_id = resolved
            current_turn = state.current_turns.pop(effective_session_id, None)
            current_turn_id: str | None = None
            current_plan: _StoredPlan | None = None
            if current_turn is not None:
                current_turn_id = current_turn[0]
                current_plan = state.plans.pop(current_turn_id, None)
                state.closed_turns[(effective_session_id, current_turn_id)] = (
                    now + _PLAN_MAX_AGE_SECONDS
                )
                state.redirects = {
                    source: redirect
                    for source, redirect in state.redirects.items()
                    if redirect != (effective_session_id, current_turn_id)
                }
            query_length = len(source_query)
            if current_turn_id is not None:
                candidates = []
                if (
                    current_plan is not None
                    and current_plan.session_id == effective_session_id
                    and current_plan.query_length == query_length
                    and (redirected_turn_id is None or current_turn_id == redirected_turn_id)
                ):
                    candidates.append((current_turn_id, current_plan))
            else:
                candidates = [
                    (turn_id, plan)
                    for turn_id, plan in state.plans.items()
                    if plan.session_id == effective_session_id
                    and plan.query_length == query_length
                    and (redirected_turn_id is None or turn_id == redirected_turn_id)
                ]
            if not candidates:
                return None
            digest = _query_digest(source_query)
            matches = [
                (turn_id, plan) for turn_id, plan in candidates if plan.query_digest == digest
            ]
            if not matches:
                return None
            newest_turn_id, newest = max(matches, key=lambda item: item[1].sequence)
            for turn_id, _plan in matches:
                state.plans.pop(turn_id, None)
            if newest.action is None:
                return None
            return RecallPlan(
                mode=newest.mode,
                action=newest.action,
                rewritten_query=newest.rewritten_query,
                turn_id=newest_turn_id,
            )

    def clear_session_plans(self, *, session_id: str) -> None:
        """Invalidate pending and ready plans after a same-session reset or rewind."""

        _require_text(session_id, "session_id")
        with self._registry.lock:
            state = self._home_state(create=False)
            if state is None:
                return
            self._clear_session(state, session_id)
            self._drop_empty_home(state)

    def purge_stale(self) -> None:
        """Drop expired process-local plans for this profile."""

        with self._registry.lock:
            now = self._clock()
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
        if (
            not state.activations
            and not state.closed_turns
            and not state.current_turns
            and not state.plans
        ):
            self._registry.homes.pop(self._home_key, None)

    @staticmethod
    def _clear_session(state: _HomeState, session_id: str, *, include_closed: bool = True) -> None:
        state.current_turns.pop(session_id, None)
        stale = [turn_id for turn_id, plan in state.plans.items() if plan.session_id == session_id]
        for turn_id in stale:
            del state.plans[turn_id]
        if include_closed:
            stale_closed = [key for key in state.closed_turns if key[0] == session_id]
            for key in stale_closed:
                del state.closed_turns[key]

    @staticmethod
    def _resolve_active_session(
        state: _HomeState, session_id: str
    ) -> tuple[str, str | None] | None:
        if session_id in state.activations.values():
            return session_id, None
        redirected = state.redirects.pop(session_id, None)
        if redirected is None:
            return None
        target_session_id, turn_id = redirected
        if target_session_id not in state.activations.values():
            return None
        return target_session_id, turn_id

    @staticmethod
    def _purge_expired(state: _HomeState, now: float) -> None:
        stale = [turn_id for turn_id, plan in state.plans.items() if plan.expires_at <= now]
        for turn_id in stale:
            del state.plans[turn_id]
        stale_closed = [key for key, expires_at in state.closed_turns.items() if expires_at <= now]
        for key in stale_closed:
            del state.closed_turns[key]


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


__all__ = [
    "InMemoryPlanMailbox",
    "PlanAction",
    "PlanMailboxError",
    "PlanMode",
    "RecallPlan",
]
