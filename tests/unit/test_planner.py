"""Context-aware pre-LLM recall planner tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from better_hermes_hindsight.plan_mailbox import RecallPlan, SQLitePlanMailbox
from better_hermes_hindsight.planner import RECALL_PLANNER_TASK, RecallPlanner


def _write_config(home: Path, *, mode: str = "active", timeout_seconds: float = 1.0) -> None:
    path = home / "better_hindsight" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "single_principal": True,
                "recall": {"enabled": True, "timeout_seconds": 3.5},
                "planner": {
                    "mode": mode,
                    "timeout_seconds": timeout_seconds,
                    "history_max_exchanges": 3,
                    "history_max_chars": 2048,
                    "query_max_chars": 512,
                    "mailbox_ttl_seconds": 10.0,
                    "busy_timeout_seconds": 0.1,
                },
            }
        ),
        encoding="utf-8",
    )


class _FakeLlm:
    def __init__(self, parsed: object) -> None:
        self.parsed = parsed
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(parsed=self.parsed)


def _mailbox(home: Path) -> SQLitePlanMailbox:
    return SQLitePlanMailbox(
        home / "better_hindsight" / "recall_plans.sqlite3",
        busy_timeout_seconds=0.1,
        process_identity="fixture-process",
    )


def test_planner_uses_only_bounded_plain_user_assistant_context(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "recall", "query": "What backup policy did Alex choose previously?"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        process_identity="fixture-process",
    )

    planner.on_pre_llm_call(
        user_message="What did we decide?",
        conversation_history=[
            {"role": "system", "content": "private system instructions"},
            {"role": "user", "content": "We discussed backup retention."},
            {
                "role": "assistant",
                "content": "Keep seven daily backups.",
                "api_content": "[RECALLED_MEMORY_EVIDENCE_BEGIN]private[/...END]",
            },
            {"role": "tool", "content": "private tool output"},
            {
                "role": "assistant",
                "content": "tool-call scaffolding",
                "tool_calls": [{"id": "private-call"}],
            },
            {"role": "user", "content": "What did we decide?"},
        ],
        session_id="session-a",
        parent_session_id="",
        turn_id="turn-a",
    )

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["task"] == RECALL_PLANNER_TASK
    assert call["timeout"] == 1.0
    assert call["max_tokens"] == 128
    capsule = json.loads(call["input"][0]["text"])
    assert capsule == {
        "current_user_message": "What did we decide?",
        "recent_conversation": [
            {"role": "user", "content": "We discussed backup retention."},
            {"role": "assistant", "content": "Keep seven daily backups."},
        ],
    }
    serialized = json.dumps(capsule)
    assert "private system instructions" not in serialized
    assert "private tool output" not in serialized
    assert "private-call" not in serialized
    assert "RECALLED_MEMORY_EVIDENCE" not in serialized

    assert mailbox.consume(
        source_query="What did we decide?", session_id="session-a"
    ) == RecallPlan(
        mode="active",
        action="recall",
        rewritten_query="What backup policy did Alex choose previously?",
        turn_id="turn-a",
    )


def test_planner_strips_appended_memory_envelope_without_changing_mailbox_digest(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "skip"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        process_identity="fixture-process",
    )
    source_query = (
        "What did we decide?\n\n<memory-context>\nprivate recalled memory\n</memory-context>"
    )

    planner.on_pre_llm_call(
        user_message=source_query,
        conversation_history=[{"role": "user", "content": source_query}],
        session_id="session-a",
        turn_id="turn-a",
    )

    capsule = json.loads(llm.calls[0]["input"][0]["text"])
    assert capsule["current_user_message"] == "What did we decide?"
    assert "private recalled memory" not in json.dumps(capsule)
    assert mailbox.consume(source_query=source_query, session_id="session-a") == RecallPlan(
        mode="active",
        action="skip",
        rewritten_query=None,
        turn_id="turn-a",
    )


@pytest.mark.parametrize("invalid_query", ["   ", chr(0xD800)])
def test_active_invalid_plan_publishes_skip_instead_of_stale_recall(
    tmp_path: Path,
    invalid_query: str,
) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    mailbox.publish(
        source_query="same question",
        session_id="session-a",
        parent_session_id="",
        turn_id="old-turn",
        mode="active",
        action="recall",
        rewritten_query="stale query",
        ttl_seconds=10.0,
    )
    llm = _FakeLlm({"action": "recall", "query": invalid_query})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        process_identity="fixture-process",
    )

    planner.on_pre_llm_call(
        user_message="same question",
        conversation_history=[{"role": "user", "content": "same question"}],
        session_id="session-a",
        turn_id="new-turn",
    )

    assert mailbox.consume(source_query="same question", session_id="session-a") == RecallPlan(
        mode="active",
        action="skip",
        rewritten_query=None,
        turn_id="new-turn",
    )


def test_late_result_is_replaced_with_skip_and_never_published_as_recall(tmp_path: Path) -> None:
    _write_config(tmp_path, timeout_seconds=0.5)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "recall", "query": "late rewritten query"})
    readings = iter((10.0, 10.6, 10.6))
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        process_identity="fixture-process",
        monotonic=lambda: next(readings),
    )

    planner.on_pre_llm_call(
        user_message="question",
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert mailbox.consume(source_query="question", session_id="session-a") == RecallPlan(
        mode="active",
        action="skip",
        rewritten_query=None,
        turn_id="turn-a",
    )


def test_off_or_inactive_planner_makes_no_model_call_or_mailbox(tmp_path: Path) -> None:
    _write_config(tmp_path, mode="off")
    llm = _FakeLlm({"action": "recall", "query": "must not run"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        process_identity="fixture-process",
    )
    planner.on_pre_llm_call(
        user_message="question",
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert llm.calls == []
    assert not (tmp_path / "better_hindsight" / "recall_plans.sqlite3").exists()

    _write_config(tmp_path, mode="active")
    _mailbox(tmp_path).activate(session_id="different-session")
    planner.on_pre_llm_call(
        user_message="question",
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-b",
    )
    assert llm.calls == []


def test_shadow_plan_is_marked_for_observation_without_changing_action(tmp_path: Path) -> None:
    _write_config(tmp_path, mode="shadow")
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "reuse"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        process_identity="fixture-process",
    )

    planner.on_pre_llm_call(
        user_message="Why?",
        conversation_history=[
            {"role": "user", "content": "Use snapshots."},
            {"role": "assistant", "content": "That is the safest rollback."},
            {"role": "user", "content": "Why?"},
        ],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert mailbox.consume(source_query="Why?", session_id="session-a") == RecallPlan(
        mode="shadow",
        action="reuse",
        rewritten_query=None,
        turn_id="turn-a",
    )


def test_non_text_current_turn_is_not_planned(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "recall", "query": "must not run"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        process_identity="fixture-process",
    )

    planner.on_pre_llm_call(
        user_message=[{"type": "image", "url": "data:image/png;base64,AA=="}],
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert llm.calls == []
    assert mailbox.consume(source_query="image", session_id="session-a") is None
