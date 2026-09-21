"""Adversarial turn-latency and typed-provenance regressions."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from better_hermes_hindsight.plan_mailbox import InMemoryPlanMailbox
from better_hermes_hindsight.planner import RecallPlanner
from tests.unit.test_planner import _FakeJev, _FakeLlm, _write_config


def test_shadow_returns_before_rewrite_and_bounds_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    path = tmp_path / "better_hindsight/config.json"
    config = json.loads(path.read_text())
    config["planner"]["rewrite"] = "shadow"
    path.write_text(json.dumps(config))
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = mailbox.activate(session_id="session")
    entered, release = Event(), Event()
    scope: ContextVar[str] = ContextVar("rewrite-scope", default="wrong")
    calls: list[str] = []
    _FakeJev("recall", monkeypatch)

    class Llm:
        def complete_structured(self, **kwargs: object) -> object:
            calls.append(scope.get())
            entered.set()
            assert release.wait(10)
            return SimpleNamespace(parsed={"query": "never apply"})

    def hook(turn: str) -> None:
        scope.set("original-scope")
        RecallPlanner(tmp_path, Llm()).on_pre_llm_call(
            session_id="session", turn_id=turn, user_message="What did we choose?"
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(hook, "first")
            try:
                assert entered.wait(5)
                # The fake cannot finish before release: this proves ordering, not just speed.
                pending.result(timeout=2)
                plan = mailbox.consume(source_query="What did we choose?", session_id="session")
                assert plan is not None and plan.rewritten_query == "What did we choose?"
                for index in range(20):
                    hook(f"later-{index}")
                    plan = mailbox.consume(source_query="What did we choose?", session_id="session")
                    assert plan is not None and plan.rewritten_query == "What did we choose?"
                assert calls == ["original-scope"]
            finally:
                release.set()
                pending.result(timeout=5)
        from better_hermes_hindsight.shadow_rewrite import drain_shadow_for_tests

        assert drain_shadow_for_tests()
        assert mailbox.consume(source_query="What did we choose?", session_id="session") is None
        hook("recovered")
        assert drain_shadow_for_tests()
        assert calls == ["original-scope", "original-scope"]
    finally:
        release.set()
        mailbox.deactivate(token=token)


@pytest.mark.parametrize("mode", ["off", "shadow", "active"])
@pytest.mark.parametrize("kind", ["delegation_closeout", "internal_notification"])
@pytest.mark.parametrize("first_turn", [False, True])
@pytest.mark.parametrize("prefix", ["", "[Sun 2026-09-20 19:07:33 UTC] "])
def test_typed_internal_turn_skips_models_without_changing_mode_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    kind: str,
    first_turn: bool,
    prefix: str,
) -> None:
    _write_config(tmp_path, mode=mode)
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = mailbox.activate(session_id="session")
    jev = _FakeJev("recall", monkeypatch)
    llm = _FakeLlm({"query": "must not run"})
    query = "The delegated investigation has completed."
    try:
        RecallPlanner(tmp_path, llm).on_pre_llm_call(
            session_id="session",
            turn_id="turn",
            user_message=query,
            is_first_turn=first_turn,
            conversation_history=[
                {"role": "user", "content": prefix + query, "display_kind": kind}
            ],
        )
        assert jev.calls == llm.calls == []
        plan = mailbox.consume(source_query=query, session_id="session")
        if mode == "off":
            assert plan is None
        else:
            assert plan is not None and plan.action == "skip" and plan.mode == mode
    finally:
        mailbox.deactivate(token=token)


@pytest.mark.parametrize(
    "variant", ["untyped", "quoted", "stale", "unknown", "malformed", "sidecars"]
)
def test_missing_current_provenance_preserves_normal_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    _write_config(tmp_path)
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = mailbox.activate(session_id="session")
    jev = _FakeJev("recall", monkeypatch)
    llm = _FakeLlm({"query": "normal rewrite"})
    query = "[Internal delegation closeout] What did we decide?"
    row: dict[str, object] = {"role": "user", "content": query}
    if variant == "quoted":
        row["content"] = query + ' {"display_kind":"delegation_closeout"}'
        query = str(row["content"])
    if variant == "unknown":
        row["display_kind"] = "some_future_kind"
    if variant == "malformed":
        row["display_kind"] = {"delegation_closeout": True}
    if variant == "sidecars":
        row["display_metadata"] = {"display_kind": "delegation_closeout"}
        row["api_content"] = '[{"display_kind":"internal_notification"}]'
    history = [row]
    if variant == "stale":
        history = [
            {"role": "user", "content": query, "display_kind": "delegation_closeout"},
            {"role": "assistant", "content": "Earlier answer"},
            row,
        ]
    try:
        RecallPlanner(tmp_path, llm).on_pre_llm_call(
            session_id="session",
            turn_id="turn",
            user_message=query,
            conversation_history=history,
        )
        assert len(jev.calls) == len(llm.calls) == 1
        plan = mailbox.consume(source_query=query, session_id="session")
        assert plan is not None and plan.action == "recall"
    finally:
        mailbox.deactivate(token=token)


@pytest.mark.parametrize("kind", ["delegation_closeout", "internal_notification"])
def test_typed_history_is_omitted_without_excluding_current_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _write_config(tmp_path)
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = mailbox.activate(session_id="session")
    jev = _FakeJev("recall", monkeypatch)
    llm = _FakeLlm({"query": "normal rewrite"})
    query = "What did we decide?"
    try:
        RecallPlanner(tmp_path, llm).on_pre_llm_call(
            session_id="session",
            turn_id="turn",
            user_message=query,
            conversation_history=[
                {"role": "user", "content": "Synthetic internal history", "display_kind": kind},
                {"role": "assistant", "content": "Untyped response remains ordinary history"},
                {"role": "user", "content": query},
            ],
        )
        assert len(jev.calls) == len(llm.calls) == 1
        assert json.loads(jev.calls[0]["capsule"]) == {
            "current_user_message": query,
            "recent_conversation": [
                {"role": "assistant", "content": "Untyped response remains ordinary history"}
            ],
        }
        plan = mailbox.consume(source_query=query, session_id="session")
        assert plan is not None and plan.action == "recall"
    finally:
        mailbox.deactivate(token=token)


@pytest.mark.parametrize("reference_only", [True, False, None])
def test_compaction_suffix_never_inherits_older_internal_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference_only: bool | None
) -> None:
    _write_config(tmp_path)
    mailbox = InMemoryPlanMailbox(tmp_path)
    token = mailbox.activate(session_id="session")
    jev = _FakeJev("recall", monkeypatch)
    llm = _FakeLlm({"query": "normal rewrite"})
    query = "The delegated investigation has completed."
    summary: dict[str, object] = {
        "role": "user",
        "content": "Reference summary",
        "_compressed_summary": True,
    }
    if reference_only is not None:
        summary["_compressed_summary_has_user_turn"] = not reference_only
    try:
        RecallPlanner(tmp_path, llm).on_pre_llm_call(
            session_id="session",
            turn_id="turn",
            user_message=query,
            conversation_history=[
                {"role": "user", "content": query, "display_kind": "delegation_closeout"},
                summary,
            ],
        )
        assert len(jev.calls) == len(llm.calls) == 1
        plan = mailbox.consume(source_query=query, session_id="session")
        assert plan is not None and plan.action == "recall"
    finally:
        mailbox.deactivate(token=token)


@pytest.mark.parametrize(
    ("content", "current", "expected"),
    [
        ("[Sun 2026-09-20 19:07:33 UTC]  body\n tail ", " body\n tail ", True),
        ("[Sun 2026-09-20 19:07:33] body", "body", True),
        ("[Sun 2026-09-20 19:07:33 UTC+02:00] body", "body", True),
        ("[Sun 2026-02-30 19:07:33 UTC] body", "body", False),
        ("[Sun 2026-09-20 25:07:33 UTC] body", "body", False),
        ("[Bad 2026-09-20 19:07:33 UTC] body", "body", False),
        ("[2026-09-20T19:07:33+0000] body", "body", False),
        ("[Sun 2026-09-20 19:07:33 UTC]  body", "body", False),
        ("[Sun 2026-09-20 19:07:33 UTC] body ", "body", False),
        ("[Sun 2026-09-20 19:07:33 UTC]\nbody", "body", False),
        ("[Sun 2026-09-20 19:07:33 UTC] [Sun 2026-09-20 19:07:33 UTC] body", "body", False),
        ([{"type": "text", "text": "body"}], "body", False),
    ],
)
def test_internal_identity_accepts_only_one_exact_display_wrapper(
    content: object, current: str, expected: bool
) -> None:
    from better_hermes_hindsight.planner import _current_is_internal

    row = {"role": "user", "content": content, "display_kind": "internal_notification"}
    assert _current_is_internal(current, [row]) is expected
    # A matching sidecar, or an older typed row, cannot classify an ordinary terminal row.
    ordinary = {"role": "user", "content": content, "api_content": current}
    assert not _current_is_internal(current, [row, ordinary])


@pytest.mark.parametrize("marker", ["_compressed_summary", "_compressed_summary_has_user_turn"])
@pytest.mark.parametrize("value", [True, False, None])
def test_even_exact_typed_summary_carriers_are_ambiguous(marker: str, value: object) -> None:
    from better_hermes_hindsight.planner import _current_is_internal

    row = {
        "role": "user",
        "content": "body",
        "display_kind": "internal_notification",
        marker: value,
    }
    assert not _current_is_internal("body", [row])
