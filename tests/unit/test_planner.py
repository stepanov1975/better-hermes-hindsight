"""Context-aware pre-LLM recall planner tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import better_hermes_hindsight.planner as planner_module
from better_hermes_hindsight.config import PlannerConfig, load_config
from better_hermes_hindsight.plan_mailbox import InMemoryPlanMailbox, RecallPlan
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


def _mailbox(home: Path) -> InMemoryPlanMailbox:
    return InMemoryPlanMailbox(home)


def test_planner_uses_only_bounded_plain_user_assistant_context(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "recall", "query": "What backup policy did Alex choose previously?"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
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
    assert call["timeout"] == pytest.approx(1.0, abs=0.01)
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


def test_history_scan_has_a_hard_row_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "skip"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
    )
    inspected = 0
    original = planner_module._safe_history_message

    def counted(message: object, *, maximum: int) -> tuple[str, str] | None:
        nonlocal inspected
        inspected += 1
        return original(message, maximum=maximum)

    monkeypatch.setattr(planner_module, "_safe_history_message", counted)
    planner.on_pre_llm_call(
        user_message="current",
        conversation_history=[
            {"role": "user", "content": "too old"},
            *({"role": "tool", "content": "filtered"} for _ in range(1_000)),
            {"role": "user", "content": "current"},
        ],
        session_id="session-a",
        turn_id="turn-a",
    )

    capsule = json.loads(llm.calls[0]["input"][0]["text"])
    assert capsule["recent_conversation"] == []
    assert inspected == 3 * planner_module._HISTORY_INSPECTED_ROWS_PER_EXCHANGE


def test_history_message_is_bounded_before_strip_or_marker_search(tmp_path: Path) -> None:
    class GuardedText(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("raw oversized content was stripped")

        def __contains__(self, item: object) -> bool:
            raise AssertionError("raw oversized content was searched")

    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "skip"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
    )
    planner.on_pre_llm_call(
        user_message="current",
        conversation_history=[
            {"role": "user", "content": GuardedText("x" * 100_000)},
            {"role": "user", "content": "current"},
        ],
        session_id="session-a",
        turn_id="turn-a",
    )

    capsule = json.loads(llm.calls[0]["input"][0]["text"])
    assert len(capsule["recent_conversation"][0]["content"]) <= 2048


def test_capsule_build_time_is_charged_to_the_planner_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, timeout_seconds=2.0)
    now = [100.0]
    mailbox = InMemoryPlanMailbox(tmp_path, monotonic=lambda: now[0])
    mailbox.activate(session_id="session-a")
    original_build = planner_module._build_capsule

    def delayed_build(
        current_user_message: str,
        conversation_history: object,
        config: PlannerConfig,
    ) -> dict[str, object]:
        now[0] += 0.5
        return original_build(current_user_message, conversation_history, config)

    monkeypatch.setattr(planner_module, "_build_capsule", delayed_build)
    llm = _FakeLlm({"action": "reuse"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        monotonic=lambda: now[0],
    )

    planner.on_pre_llm_call(
        user_message="Why?",
        conversation_history=[{"role": "user", "content": "Why?"}],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert llm.calls[0]["timeout"] == pytest.approx(1.5)


def test_serialized_capsule_rejects_content_beyond_derived_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "recall", "query": "unused"})

    def oversized_capsule(
        current_user_message: str,
        conversation_history: object,
        config: PlannerConfig,
    ) -> dict[str, object]:
        del current_user_message, conversation_history, config
        return {"current_user_message": "x" * 100_000, "recent_conversation": []}

    monkeypatch.setattr(planner_module, "_build_capsule", oversized_capsule)
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
    )
    planner.on_pre_llm_call(
        user_message="Current direct query",
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert llm.calls == []
    assert mailbox.consume(
        source_query="Current direct query", session_id="session-a"
    ) == RecallPlan(mode="active", action="skip", rewritten_query=None, turn_id="turn-a")


def test_valid_capsule_stays_within_derived_utf8_byte_limit(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "skip"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
    )
    source_query = "\x00" * 2048

    planner.on_pre_llm_call(
        user_message=source_query,
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    serialized = llm.calls[0]["input"][0]["text"]
    config = load_config(tmp_path).planner
    assert len(serialized.encode("utf-8")) <= planner_module._capsule_byte_limit(config)


def test_latest_current_copy_is_removed_when_a_later_user_row_exists(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "reuse"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
    )

    planner.on_pre_llm_call(
        user_message="Why?",
        conversation_history=[
            {"role": "user", "content": "Why?"},
            {"role": "assistant", "content": "Because snapshots make rollback deterministic."},
            {"role": "user", "content": "Why?"},
            {"role": "user", "content": "Restored post-current user row."},
        ],
        session_id="session-a",
        turn_id="turn-a",
    )

    capsule = json.loads(llm.calls[0]["input"][0]["text"])
    assert capsule["recent_conversation"] == [
        {"role": "user", "content": "Why?"},
        {"role": "assistant", "content": "Because snapshots make rollback deterministic."},
        {"role": "user", "content": "Restored post-current user row."},
    ]


def test_planner_preserves_user_authored_memory_context_literal(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "skip"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
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
    assert capsule["current_user_message"] == source_query
    assert "private recalled memory" in json.dumps(capsule)
    assert mailbox.consume(source_query=source_query, session_id="session-a") == RecallPlan(
        mode="active",
        action="skip",
        rewritten_query=None,
        turn_id="turn-a",
    )


def test_user_authored_memory_marker_in_clean_history_is_preserved(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "reuse"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
    )
    literal = "Analyze <memory-context>literal XML</memory-context>."

    planner.on_pre_llm_call(
        user_message="What does it mean?",
        conversation_history=[
            {
                "role": "user",
                "content": literal,
                "api_content": "<memory-context>provider secret</memory-context>",
            },
            {"role": "assistant", "content": "It is literal markup."},
            {"role": "user", "content": "What does it mean?"},
        ],
        session_id="session-a",
        turn_id="turn-a",
    )

    capsule = json.loads(llm.calls[0]["input"][0]["text"])
    assert capsule["recent_conversation"][0]["content"] == literal
    assert "provider secret" not in json.dumps(capsule)


@pytest.mark.parametrize("invalid_query", ["   ", chr(0xD800)])
def test_active_invalid_plan_publishes_skip_instead_of_stale_recall(
    tmp_path: Path,
    invalid_query: str,
) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    assert mailbox.reserve(
        source_query="same question",
        session_id="session-a",
        parent_session_id="",
        turn_id="old-turn",
        mode="active",
    )
    assert mailbox.finalize(
        turn_id="old-turn",
        mode="active",
        action="recall",
        rewritten_query="stale query",
    )
    llm = _FakeLlm({"action": "recall", "query": invalid_query})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
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


def test_active_timeout_publishes_skip_without_using_late_model_result(tmp_path: Path) -> None:
    _write_config(tmp_path, timeout_seconds=0.5)
    now = [10.0]
    mailbox = InMemoryPlanMailbox(tmp_path, monotonic=lambda: now[0])
    mailbox.activate(session_id="session-a")

    class _LateLlm(_FakeLlm):
        def complete_structured(self, **kwargs: Any) -> object:
            result = super().complete_structured(**kwargs)
            now[0] = 10.6
            return result

    llm = _LateLlm({"action": "recall", "query": "late rewritten query"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
        monotonic=lambda: now[0],
    )

    planner.on_pre_llm_call(
        user_message="question",
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert len(llm.calls) == 1
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
    )
    planner.on_pre_llm_call(
        user_message="question",
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert llm.calls == []

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


def test_oversized_current_turn_falls_back_without_planning(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "recall", "query": "must not run"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
    )

    planner.on_pre_llm_call(
        user_message="x" * 2_049,
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert llm.calls == []
    assert mailbox.consume(source_query="x" * 2_049, session_id="session-a") is None


def test_non_text_current_turn_is_not_planned(tmp_path: Path) -> None:
    _write_config(tmp_path)
    mailbox = _mailbox(tmp_path)
    mailbox.activate(session_id="session-a")
    llm = _FakeLlm({"action": "recall", "query": "must not run"})
    planner = RecallPlanner(
        hermes_home=tmp_path,
        llm=llm,
    )

    planner.on_pre_llm_call(
        user_message=[{"type": "image", "url": "data:image/png;base64,AA=="}],
        conversation_history=[],
        session_id="session-a",
        turn_id="turn-a",
    )

    assert llm.calls == []
    assert mailbox.consume(source_query="image", session_id="session-a") is None
