"""One-shot SQLite recall-plan mailbox tests."""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from better_hermes_hindsight.plan_mailbox import (
    PlanMailboxError,
    RecallPlan,
    SQLitePlanMailbox,
)


def _publish(
    mailbox: SQLitePlanMailbox,
    *,
    source_query: str = "What did we decide?",
    session_id: str = "session-a",
    parent_session_id: str = "",
    turn_id: str = "turn-a",
    mode: str = "active",
    action: str = "recall",
    rewritten_query: str | None = "What backup policy did Alex choose?",
    ttl_seconds: float = 10.0,
) -> None:
    assert mailbox.publish(
        source_query=source_query,
        session_id=session_id,
        parent_session_id=parent_session_id,
        turn_id=turn_id,
        mode=mode,
        action=action,
        rewritten_query=rewritten_query,
        ttl_seconds=ttl_seconds,
    )


def test_publish_consume_is_exactly_once_and_profile_private(tmp_path: Path) -> None:
    path = tmp_path / "profile" / "better_hindsight" / "recall_plans.sqlite3"
    mailbox = SQLitePlanMailbox(
        path,
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    mailbox.activate(session_id="session-a")
    _publish(mailbox)

    assert mailbox.is_active(session_id="session-a") is True
    assert mailbox.consume(source_query="unrelated", session_id="session-a") is None
    assert mailbox.consume(
        source_query="What did we decide?", session_id="session-a"
    ) == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="What backup policy did Alex choose?",
        turn_id="turn-a",
    )
    assert mailbox.consume(source_query="What did we decide?", session_id="session-a") is None
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_parent_authorization_and_rebind_support_multiple_session_rotations(
    tmp_path: Path,
) -> None:
    mailbox = SQLitePlanMailbox(
        tmp_path / "mailbox.sqlite3",
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    mailbox.activate(session_id="initialized-session")
    assert not mailbox.reserve(
        source_query="What did we decide?",
        session_id="rotated-session",
        parent_session_id="initialized-session",
        turn_id="too-early",
        mode="active",
        ttl_seconds=10.0,
    )
    mailbox.rebind(
        old_session_id="initialized-session",
        new_session_id="rotated-session",
    )
    _publish(
        mailbox,
        session_id="rotated-session",
        parent_session_id="initialized-session",
    )

    assert (
        mailbox.consume(source_query="What did we decide?", session_id="initialized-session")
        is None
    )
    assert mailbox.consume(
        source_query="What did we decide?", session_id="rotated-session"
    ) == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="What backup policy did Alex choose?",
        turn_id="turn-a",
    )

    mailbox.rebind(
        old_session_id="rotated-session",
        new_session_id="new-rotation",
    )
    _publish(
        mailbox,
        session_id="new-rotation",
        parent_session_id="rotated-session",
        turn_id="turn-b",
        rewritten_query="newest",
    )
    assert mailbox.consume(source_query="What did we decide?", session_id="old-ancestor") is None
    assert mailbox.consume(
        source_query="What did we decide?", session_id="new-rotation"
    ) == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="newest",
        turn_id="turn-b",
    )


def test_sibling_children_never_fall_back_through_shared_parent(tmp_path: Path) -> None:
    mailbox = SQLitePlanMailbox(
        tmp_path / "mailbox.sqlite3",
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    mailbox.activate(session_id="root")
    mailbox.activate(session_id="child-a")
    mailbox.activate(session_id="child-b")
    _publish(
        mailbox,
        source_query="same query",
        session_id="child-a",
        parent_session_id="root",
        turn_id="turn-a",
        rewritten_query="for a",
    )
    _publish(
        mailbox,
        source_query="same query",
        session_id="child-b",
        parent_session_id="root",
        turn_id="turn-b",
        rewritten_query="for b",
    )

    assert mailbox.consume(source_query="same query", session_id="root") is None
    assert mailbox.consume(source_query="same query", session_id="child-a") == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="for a",
        turn_id="turn-a",
    )
    assert mailbox.consume(source_query="same query", session_id="child-b") == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="for b",
        turn_id="turn-b",
    )


def test_parent_lifecycle_cleanup_never_deletes_a_live_child_plan(tmp_path: Path) -> None:
    mailbox = SQLitePlanMailbox(
        tmp_path / "mailbox.sqlite3",
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    mailbox.activate(session_id="root")
    mailbox.activate(session_id="child-b")
    _publish(
        mailbox,
        source_query="after rebind",
        session_id="child-b",
        parent_session_id="root",
        turn_id="turn-rebind",
        rewritten_query="survives rebind",
    )
    mailbox.rebind(old_session_id="root", new_session_id="child-a")
    assert mailbox.consume(source_query="after rebind", session_id="child-b") == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="survives rebind",
        turn_id="turn-rebind",
    )

    mailbox.activate(session_id="root")
    _publish(
        mailbox,
        source_query="after deactivate",
        session_id="child-b",
        parent_session_id="root",
        turn_id="turn-deactivate",
        rewritten_query="survives deactivate",
    )
    mailbox.deactivate(session_id="root")
    assert mailbox.consume(source_query="after deactivate", session_id="child-b") == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="survives deactivate",
        turn_id="turn-deactivate",
    )

    mailbox.activate(session_id="root")
    _publish(
        mailbox,
        source_query="after rewind",
        session_id="child-b",
        parent_session_id="root",
        turn_id="turn-rewind",
        rewritten_query="survives rewind",
    )
    mailbox.clear_session_plans(session_id="root")
    assert mailbox.consume(source_query="after rewind", session_id="child-b") == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="survives rewind",
        turn_id="turn-rewind",
    )


def test_unknown_session_cannot_consume_same_query_plans(tmp_path: Path) -> None:
    mailbox = SQLitePlanMailbox(
        tmp_path / "mailbox.sqlite3",
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    mailbox.activate(session_id="session-a")
    mailbox.activate(session_id="session-b")
    _publish(mailbox, session_id="session-a", turn_id="turn-a", rewritten_query="for a")
    _publish(mailbox, session_id="session-b", turn_id="turn-b", rewritten_query="for b")

    assert mailbox.consume(source_query="What did we decide?", session_id="unknown") is None
    assert mailbox.consume(
        source_query="What did we decide?", session_id="session-a"
    ) == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="for a",
        turn_id="turn-a",
    )


def test_expired_and_other_process_plans_are_never_consumed(tmp_path: Path) -> None:
    now = 100.0
    path = tmp_path / "mailbox.sqlite3"
    first = SQLitePlanMailbox(
        path,
        busy_timeout_seconds=0.1,
        process_identity="process-a",
        clock=lambda: now,
    )
    first.activate(session_id="session-a")
    _publish(first, ttl_seconds=1.0)

    now = 102.0
    assert first.consume(source_query="What did we decide?", session_id="session-a") is None

    second = SQLitePlanMailbox(
        path,
        busy_timeout_seconds=0.1,
        process_identity="process-b",
        clock=lambda: now,
    )
    assert second.is_active(session_id="session-a") is False
    _publish(first, turn_id="turn-b")
    assert second.consume(source_query="What did we decide?", session_id="session-a") is None

    second.activate(session_id="session-a")
    assert first.is_active(session_id="session-a") is True
    assert second.is_active(session_id="session-a") is True
    _publish(
        second,
        source_query="Second process query",
        turn_id="turn-c",
        rewritten_query="Second process rewrite",
    )
    assert second.consume(source_query="What did we decide?", session_id="session-a") is None
    assert first.consume(source_query="What did we decide?", session_id="session-a") == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="What backup policy did Alex choose?",
        turn_id="turn-b",
    )
    assert second.consume(
        source_query="Second process query", session_id="session-a"
    ) == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="Second process rewrite",
        turn_id="turn-c",
    )


def test_activation_purges_expired_foreign_process_plans(tmp_path: Path) -> None:
    now = 100.0
    path = tmp_path / "mailbox.sqlite3"
    first = SQLitePlanMailbox(
        path,
        busy_timeout_seconds=0.1,
        process_identity="process-a",
        clock=lambda: now,
        monotonic=lambda: now,
    )
    second = SQLitePlanMailbox(
        path,
        busy_timeout_seconds=0.1,
        process_identity="process-b",
        clock=lambda: now,
        monotonic=lambda: now,
    )
    first.activate(session_id="session-a")
    _publish(
        first,
        source_query="Sensitive query",
        rewritten_query="Sensitive rewrite",
        ttl_seconds=1.0,
    )

    now = 102.0
    second.activate(session_id="session-b")

    with sqlite3.connect(path) as connection:
        plan_count = int(connection.execute("SELECT COUNT(*) FROM recall_plan").fetchone()[0])
    assert plan_count == 0
    assert first.is_active(session_id="session-a") is True
    assert second.is_active(session_id="session-b") is True


def test_duplicate_turn_cannot_overwrite_an_unconsumed_ready_plan(tmp_path: Path) -> None:
    mailbox = SQLitePlanMailbox(
        tmp_path / "mailbox.sqlite3",
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    mailbox.activate(session_id="session-a")
    _publish(mailbox, rewritten_query="old")
    assert not mailbox.publish(
        source_query="What did we decide?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
        action="skip",
        rewritten_query=None,
        ttl_seconds=10.0,
    )

    assert mailbox.consume(
        source_query="What did we decide?", session_id="session-a"
    ) == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="old",
        turn_id="turn-a",
    )
    assert mailbox.consume(source_query="What did we decide?", session_id="session-a") is None


def test_concurrent_consumers_observe_at_most_one_plan(tmp_path: Path) -> None:
    path = tmp_path / "mailbox.sqlite3"
    mailbox = SQLitePlanMailbox(
        path,
        busy_timeout_seconds=1.0,
        process_identity="process-a",
    )
    mailbox.activate(session_id="session-a")
    _publish(mailbox)

    def consume() -> RecallPlan | None:
        contender = SQLitePlanMailbox(
            path,
            busy_timeout_seconds=1.0,
            process_identity="process-a",
        )
        return contender.consume(source_query="What did we decide?", session_id="session-a")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: consume(), range(2)))

    assert sum(result is not None for result in results) == 1


def test_consumer_cancels_pending_reservation_before_late_planner_can_publish(
    tmp_path: Path,
) -> None:
    mailbox = SQLitePlanMailbox(
        tmp_path / "mailbox.sqlite3",
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    mailbox.activate(session_id="session-a")
    assert mailbox.reserve(
        source_query="What did we decide?",
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
        mode="active",
        ttl_seconds=10.0,
    )

    assert mailbox.consume(source_query="What did we decide?", session_id="session-a") is None
    assert not mailbox.finalize(
        turn_id="turn-a",
        mode="active",
        action="recall",
        rewritten_query="late result",
    )
    assert mailbox.consume(source_query="What did we decide?", session_id="session-a") is None


def test_inactive_session_cannot_reserve_or_consume_another_sessions_plan(
    tmp_path: Path,
) -> None:
    mailbox = SQLitePlanMailbox(
        tmp_path / "mailbox.sqlite3",
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    mailbox.activate(session_id="session-a")
    assert not mailbox.is_active(session_id="session-b")
    assert not mailbox.reserve(
        source_query="same query",
        session_id="session-b",
        parent_session_id="",
        turn_id="turn-b",
        mode="active",
        ttl_seconds=10.0,
    )
    _publish(mailbox, source_query="same query", session_id="session-a")
    assert mailbox.consume(source_query="same query", session_id="session-b") is None
    assert mailbox.consume(source_query="same query", session_id="session-a") is not None


def test_activation_is_reference_counted_for_sibling_provider_handles(tmp_path: Path) -> None:
    path = tmp_path / "mailbox.sqlite3"
    first = SQLitePlanMailbox(
        path,
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )
    second = SQLitePlanMailbox(
        path,
        busy_timeout_seconds=0.1,
        process_identity="process-a",
    )

    first.activate(session_id="session-a")
    second.activate(session_id="session-a")
    first.deactivate(session_id="session-a")
    assert second.is_active(session_id="session-a") is True

    second.deactivate(session_id="session-a")
    assert first.is_active(session_id="session-a") is False


def test_foreign_version_one_database_is_not_migrated_or_modified(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE foreign_state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO foreign_state VALUES ('preserve-me')")
        connection.execute("PRAGMA user_version = 1")

    mailbox = SQLitePlanMailbox(path, busy_timeout_seconds=0.1)
    with pytest.raises(PlanMailboxError, match="schema is unsupported"):
        mailbox.activate(session_id="session-a")

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT value FROM foreign_state").fetchone()[0] == "preserve-me"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {"foreign_state"}


def test_default_process_identity_is_stable_across_instances(tmp_path: Path) -> None:
    first = SQLitePlanMailbox(tmp_path / "a.sqlite3", busy_timeout_seconds=0.1)
    second = SQLitePlanMailbox(tmp_path / "b.sqlite3", busy_timeout_seconds=0.1)

    assert first.process_identity == second.process_identity
    assert str(os.getpid()) not in first.process_identity
