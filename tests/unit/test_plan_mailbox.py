"""Contract tests for the process-local contextual-plan handoff."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from better_hermes_hindsight.plan_mailbox import (
    InMemoryPlanMailbox,
    PlanMailboxError,
    RecallPlan,
    remove_legacy_plan_mailbox,
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


def test_legacy_sqlite_mailbox_and_sidecars_are_removed(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE active_session (session_id TEXT NOT NULL);
            CREATE TABLE recall_plan (
                turn_id TEXT NOT NULL,
                query_digest TEXT NOT NULL,
                mode TEXT NOT NULL,
                action TEXT,
                rewritten_query TEXT,
                expires_at REAL NOT NULL
            );
            PRAGMA user_version = 3;
            """
        )
    candidates = [
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ]
    for candidate in candidates[1:]:
        candidate.write_bytes(b"private rewritten query")

    assert remove_legacy_plan_mailbox(path)
    assert all(not candidate.exists() for candidate in candidates)
    assert not remove_legacy_plan_mailbox(path)


def test_legacy_cleanup_refuses_an_unverified_file(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.sqlite3"
    path.write_bytes(b"unrelated profile data")

    with pytest.raises(PlanMailboxError, match="unverified"):
        remove_legacy_plan_mailbox(path)
    assert path.read_bytes() == b"unrelated profile data"


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
