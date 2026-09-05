"""Contract tests for the process-local contextual-plan handoff."""

from __future__ import annotations

import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from better_hermes_hindsight.plan_mailbox import (
    InMemoryPlanMailbox,
    RecallPlan,
)


class _Clock:
    def __init__(self, value: float = 1.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _ready_plan(
    mailbox: InMemoryPlanMailbox,
    *,
    session_id: str = "session-a",
    turn_id: str = "turn-a",
    source_query: str = "Why?",
    mode: str = "active",
    action: str = "recall",
    rewritten_query: str | None = "Why was the backup policy chosen?",
) -> str:
    token = mailbox.activate(session_id=session_id)
    assert mailbox.reserve(
        source_query=source_query,
        session_id=session_id,
        parent_session_id="",
        turn_id=turn_id,
        mode=mode,  # type: ignore[arg-type]
    )
    assert mailbox.finalize(
        turn_id=turn_id,
        mode=mode,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        rewritten_query=rewritten_query,
    )
    return token


def test_ready_plan_is_shared_and_consumed_once(tmp_path: Path) -> None:
    writer = InMemoryPlanMailbox(tmp_path)
    reader = InMemoryPlanMailbox(tmp_path)
    token = _ready_plan(writer)

    assert reader.consume(source_query="Why?", session_id="session-a") == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="Why was the backup policy chosen?",
        turn_id="turn-a",
    )
    assert writer.consume(source_query="Why?", session_id="session-a") is None
    writer.deactivate(token=token)


def test_distinct_import_namespaces_share_the_process_registry(tmp_path: Path) -> None:
    module_path = (
        Path(__file__).resolve().parents[2] / "better_hermes_hindsight" / "plan_mailbox.py"
    )
    spec = importlib.util.spec_from_file_location("_alternate_better_plan_mailbox", module_path)
    assert spec is not None and spec.loader is not None
    alternate: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = alternate
    try:
        spec.loader.exec_module(alternate)
        writer = InMemoryPlanMailbox(tmp_path)
        reader = alternate.InMemoryPlanMailbox(tmp_path)
        token = _ready_plan(writer)

        plan = reader.consume(source_query="Why?", session_id="session-a")
        assert plan.action == "recall"
        assert plan.rewritten_query == "Why was the backup policy chosen?"
        assert plan.turn_id == "turn-a"
        writer.deactivate(token=token)
    finally:
        sys.modules.pop(spec.name, None)


def test_activation_tokens_preserve_sibling_provider_handles(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    first = mailbox.activate(session_id="session-a")
    second = mailbox.activate(session_id="session-a")

    mailbox.deactivate(token=first)
    assert mailbox.is_active(session_id="session-a")

    mailbox.deactivate(token=second)
    assert not mailbox.is_active(session_id="session-a")


def test_real_rebind_clears_existing_target_session_plans(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    source = mailbox.activate(session_id="source")
    target = mailbox.activate(session_id="target")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="target",
        parent_session_id="",
        turn_id="target-turn",
        mode="active",
    )
    assert mailbox.finalize(
        turn_id="target-turn",
        mode="active",
        action="skip",
        rewritten_query=None,
    )

    mailbox.rebind(token=source, new_session_id="target")

    assert mailbox.consume(source_query="Why?", session_id="target") is None
    mailbox.deactivate(token=source)
    mailbox.deactivate(token=target)


def test_rebind_is_atomic_and_invalidates_old_session_plans(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = _ready_plan(mailbox)

    mailbox.rebind(token=token, new_session_id="session-b")

    assert not mailbox.is_active(session_id="session-a")
    assert mailbox.is_active(session_id="session-b")
    assert mailbox.consume(source_query="Why?", session_id="session-a") is None
    assert mailbox.reserve(
        source_query="Which one?",
        session_id="session-b",
        parent_session_id="session-a",
        turn_id="turn-b",
        mode="active",
    )
    mailbox.deactivate(token=token)


def test_turn_start_bridges_an_asynchronous_session_rotation(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    first_activation = mailbox.activate(session_id="parent")
    delayed_activation = mailbox.activate(session_id="parent")
    mailbox.rebind(token=first_activation, new_session_id="child")
    owner = mailbox.begin_turn(session_id="child", turn_id="turn-child", parent_session_id="parent")
    assert owner is not None
    assert not mailbox.is_active(session_id="parent")
    assert mailbox.is_active(session_id="child")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="child",
        parent_session_id="parent",
        turn_id="turn-child",
        mode="active",
        owner_token=owner,
    )
    assert mailbox.finalize(
        turn_id="turn-child",
        mode="active",
        action="skip",
        rewritten_query=None,
        owner_token=owner,
    )
    assert mailbox.consume(source_query="Why?", session_id="parent") is not None
    mailbox.rebind(token=delayed_activation, new_session_id="child")

    next_owner = mailbox.begin_turn(session_id="child", turn_id="turn-next")
    assert next_owner is not None
    assert mailbox.reserve(
        source_query="Why?",
        session_id="child",
        parent_session_id="parent",
        turn_id="turn-next",
        mode="active",
        owner_token=next_owner,
    )
    assert mailbox.finalize(
        turn_id="turn-next",
        mode="active",
        action="skip",
        rewritten_query=None,
        owner_token=next_owner,
    )
    assert mailbox.consume(source_query="Why?", session_id="parent") is None
    assert mailbox.consume(source_query="Why?", session_id="child") is not None

    mailbox.deactivate(token=first_activation)
    mailbox.deactivate(token=delayed_activation)


def test_rotation_redirect_is_removed_when_callback_precedes_prefetch(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    activation = mailbox.activate(session_id="parent")
    owner = mailbox.begin_turn(session_id="child", turn_id="turn-child", parent_session_id="parent")
    assert owner is not None
    assert mailbox.reserve(
        source_query="Why?",
        session_id="child",
        parent_session_id="parent",
        turn_id="turn-child",
        mode="active",
        owner_token=owner,
    )
    assert mailbox.finalize(
        turn_id="turn-child",
        mode="active",
        action="skip",
        rewritten_query=None,
        owner_token=owner,
    )
    mailbox.rebind(token=activation, new_session_id="child")
    assert mailbox.consume(source_query="Why?", session_id="child") is not None
    state = mailbox._home_state(create=False)
    assert state is not None
    assert state.redirects == {}
    mailbox.deactivate(token=activation)


def test_new_rotation_replaces_an_unconsumed_redirect(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    activation = mailbox.activate(session_id="session-a")
    assert mailbox.begin_turn(
        session_id="session-b", turn_id="turn-b", parent_session_id="session-a"
    )
    assert mailbox.begin_turn(
        session_id="session-c", turn_id="turn-c", parent_session_id="session-b"
    )
    state = mailbox._home_state(create=False)
    assert state is not None
    assert state.redirects == {"session-b": ("session-c", "turn-c")}
    mailbox.deactivate(token=activation)


def test_pending_reservation_fences_late_planner_publication(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = mailbox.activate(session_id="session-a")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
    )

    assert mailbox.consume(source_query="Why?", session_id="session-a") is None
    assert not mailbox.finalize(
        turn_id="turn-a",
        mode="active",
        action="recall",
        rewritten_query="late rewritten query",
    )
    mailbox.deactivate(token=token)


def test_publication_deadline_is_enforced_inside_finalize(tmp_path: Path) -> None:
    clock = _Clock(10.0)
    mailbox = InMemoryPlanMailbox(tmp_path, monotonic=clock)
    token = mailbox.activate(session_id="session-a")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-deadline",
        mode="active",
        publish_timeout_seconds=0.5,
    )

    clock.value = 10.5
    assert not mailbox.finalize(
        turn_id="turn-deadline",
        mode="active",
        action="recall",
        rewritten_query="late rewritten query",
    )
    assert mailbox.consume(source_query="Why?", session_id="session-a") is None
    mailbox.deactivate(token=token)


def test_finalize_samples_deadline_after_acquiring_registry_lock(tmp_path: Path) -> None:
    clock = _Clock(10.0)
    sampled = Event()
    arm_sample = False

    def monotonic() -> float:
        if arm_sample:
            sampled.set()
        return clock()

    mailbox = InMemoryPlanMailbox(tmp_path, monotonic=monotonic)
    token = mailbox.activate(session_id="session-a")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
        publish_timeout_seconds=0.5,
    )
    arm_sample = True
    started = Event()

    def finalize() -> bool:
        started.set()
        return mailbox.finalize(
            turn_id="turn-a",
            mode="active",
            action="recall",
            rewritten_query="rewritten",
        )

    pool = ThreadPoolExecutor(max_workers=1)
    lock_held = True
    mailbox._registry.lock.acquire()
    try:
        future = pool.submit(finalize)
        assert started.wait(timeout=1.0)
        assert not sampled.wait(timeout=0.1)
        clock.value = 10.5
        mailbox._registry.lock.release()
        lock_held = False
        assert not future.result(timeout=1.0)
    finally:
        if lock_held:
            mailbox._registry.lock.release()
        pool.shutdown(wait=True)
    mailbox.deactivate(token=token)


def test_turn_start_clears_an_abandoned_matching_plan(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = _ready_plan(mailbox, rewritten_query="stale rewrite")

    assert mailbox.begin_turn(session_id="session-a", turn_id="turn-current")
    assert mailbox.consume(source_query="Why?", session_id="session-a") is None
    mailbox.deactivate(token=token)


def test_turn_start_fences_an_abandoned_reservation(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = mailbox.activate(session_id="session-a")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
    )
    owner_token = mailbox.begin_turn(session_id="session-a", turn_id="turn-b")
    assert owner_token is not None
    assert not mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
    )
    assert not mailbox.finalize(
        turn_id="turn-a",
        mode="active",
        action="recall",
        rewritten_query="first turn rewrite",
    )
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-b",
        mode="active",
        owner_token=owner_token,
    )
    assert mailbox.finalize(
        turn_id="turn-b",
        mode="active",
        action="skip",
        rewritten_query=None,
        owner_token=owner_token,
    )
    assert mailbox.consume(source_query="Why?", session_id="session-a") == RecallPlan(
        mode="active",
        action="skip",
        rewritten_query=None,
        turn_id="turn-b",
    )
    mailbox.deactivate(token=token)


def test_new_owner_generation_fences_a_duplicate_same_turn_worker(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    activation = mailbox.activate(session_id="session-a")
    first_owner = mailbox.begin_turn(session_id="session-a", turn_id="turn-a")
    assert first_owner is not None
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
        owner_token=first_owner,
    )

    second_owner = mailbox.begin_turn(session_id="session-a", turn_id="turn-a")
    assert second_owner is not None and second_owner != first_owner
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
        owner_token=second_owner,
    )
    assert not mailbox.finalize(
        turn_id="turn-a",
        mode="active",
        action="recall",
        rewritten_query="stale",
        owner_token=first_owner,
    )
    assert mailbox.finalize(
        turn_id="turn-a",
        mode="active",
        action="recall",
        rewritten_query="current",
        owner_token=second_owner,
    )
    mailbox.deactivate(token=activation)


def test_consumed_turn_cannot_be_reserved_again(tmp_path: Path) -> None:
    clock = _Clock(10.0)
    mailbox = InMemoryPlanMailbox(tmp_path, monotonic=clock)
    activation = mailbox.activate(session_id="session-a")
    owner_token = mailbox.begin_turn(session_id="session-a", turn_id="turn-a")
    assert owner_token is not None
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
        owner_token=owner_token,
    )
    assert mailbox.finalize(
        turn_id="turn-a",
        mode="active",
        action="skip",
        rewritten_query=None,
        owner_token=owner_token,
    )
    assert mailbox.consume(source_query="Why?", session_id="session-a") is not None
    assert mailbox.begin_turn(session_id="session-a", turn_id="turn-a") is None
    clock.value = 131.0
    next_owner = mailbox.begin_turn(session_id="session-a", turn_id="turn-a")
    assert next_owner is not None
    assert not mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
        owner_token=owner_token,
    )
    mailbox.deactivate(token=activation)


def test_query_mismatch_closes_and_removes_the_current_reservation(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    activation = mailbox.activate(session_id="session-a")
    owner = mailbox.begin_turn(session_id="session-a", turn_id="turn-a")
    assert owner is not None
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
        owner_token=owner,
    )
    assert mailbox.consume(source_query="Different", session_id="session-a") is None
    assert not mailbox.finalize(
        turn_id="turn-a",
        mode="active",
        action="skip",
        rewritten_query=None,
        owner_token=owner,
    )
    assert mailbox.begin_turn(session_id="session-a", turn_id="turn-a") is None
    mailbox.deactivate(token=activation)


def test_active_skip_fallback_can_finalize_after_planner_deadline(tmp_path: Path) -> None:
    clock = _Clock(10.0)
    mailbox = InMemoryPlanMailbox(tmp_path, monotonic=clock)

    token = mailbox.activate(session_id="session-a")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-timeout",
        mode="active",
        publish_timeout_seconds=0.5,
    )

    clock.value = 10.5
    assert mailbox.finalize(
        turn_id="turn-timeout",
        mode="active",
        action="skip",
        rewritten_query=None,
    )
    assert mailbox.consume(source_query="Why?", session_id="session-a") == RecallPlan(
        mode="active",
        action="skip",
        rewritten_query=None,
        turn_id="turn-timeout",
    )
    mailbox.deactivate(token=token)


def test_newest_matching_pending_turn_fences_an_older_ready_plan(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = _ready_plan(mailbox, turn_id="turn-old")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-new",
        mode="active",
    )

    assert mailbox.consume(source_query="Why?", session_id="session-a") is None
    assert not mailbox.finalize(
        turn_id="turn-new",
        mode="active",
        action="recall",
        rewritten_query="late",
    )
    mailbox.deactivate(token=token)


def test_query_session_and_profile_are_isolated(tmp_path: Path) -> None:
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    mailbox_a = InMemoryPlanMailbox(home_a)
    mailbox_b = InMemoryPlanMailbox(home_b)
    token = _ready_plan(mailbox_a)

    assert mailbox_a.consume(source_query="When?", session_id="session-a") is None
    assert mailbox_a.consume(source_query="Why?", session_id="session-b") is None
    assert mailbox_b.consume(source_query="Why?", session_id="session-a") is None
    assert mailbox_a.consume(source_query="Why?", session_id="session-a") is not None
    mailbox_a.deactivate(token=token)


def test_source_query_digest_accepts_non_unicode_scalar_text(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    source_query = "why" + chr(0xD800)
    token = _ready_plan(mailbox, source_query=source_query)

    assert mailbox.consume(source_query=source_query, session_id="session-a") is not None
    mailbox.deactivate(token=token)


def test_stale_plans_expire_on_monotonic_time(tmp_path: Path) -> None:
    clock = _Clock()
    mailbox = InMemoryPlanMailbox(tmp_path, monotonic=clock)
    token = _ready_plan(mailbox)

    clock.value += 10.1

    assert mailbox.consume(source_query="Why?", session_id="session-a") is None
    mailbox.purge_stale()
    mailbox.deactivate(token=token)


def test_clear_and_deactivate_remove_session_plans(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = _ready_plan(mailbox)
    mailbox.clear_session_plans(session_id="session-a")
    assert mailbox.consume(source_query="Why?", session_id="session-a") is None

    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-b",
        mode="active",
    )
    mailbox.deactivate(token=token)
    assert mailbox.consume(source_query="Why?", session_id="session-a") is None


def test_only_one_concurrent_consumer_wins(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = _ready_plan(mailbox)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: mailbox.consume(source_query="Why?", session_id="session-a"),
                range(32),
            )
        )

    assert sum(result is not None for result in results) == 1
    mailbox.deactivate(token=token)


@pytest.mark.parametrize(
    ("action", "rewritten_query"),
    [("skip", None), ("reuse", None), ("recall", "standalone query")],
)
def test_supported_decisions_round_trip(
    tmp_path: Path,
    action: str,
    rewritten_query: str | None,
) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path / action)
    token = _ready_plan(
        mailbox,
        action=action,
        rewritten_query=rewritten_query,
    )

    plan = mailbox.consume(source_query="Why?", session_id="session-a")
    assert plan is not None
    assert (plan.action, plan.rewritten_query) == (action, rewritten_query)
    mailbox.deactivate(token=token)


def test_invalid_decision_shape_is_rejected(tmp_path: Path) -> None:
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = mailbox.activate(session_id="session-a")
    assert mailbox.reserve(
        source_query="Why?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
    )

    with pytest.raises(ValueError, match="requires rewritten_query"):
        mailbox.finalize(
            turn_id="turn-a",
            mode="active",
            action="recall",
            rewritten_query=None,
        )
    mailbox.deactivate(token=token)
