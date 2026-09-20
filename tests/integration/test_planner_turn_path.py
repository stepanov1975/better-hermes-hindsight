"""Real Hermes staging -> companion -> prefetch -> model with fake network boundaries."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.helpers import clean_subprocess_env, materialize_standard_plugin

ROOT = Path(__file__).resolve().parents[2]

_SCRIPT = r"""
import asyncio
import importlib
import json
import socket
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace

# Deny all non-loopback sockets, including accidental auxiliary/background traffic.
def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        address = args[1] if event == "socket.connect" else args[0]
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in {"127.0.0.1", "localhost", "::1"}:
            raise AssertionError("non-loopback network forbidden")
sys.addaudithook(audit)

from tests.fakes.hindsight_server import FakeHindsightServer

home = Path(sys.argv[1])
scenario = sys.argv[2]
mode = sys.argv[3]
kind = sys.argv[4]
first_turn = sys.argv[5] == "True"
internal = scenario in {"internal", "timestamp"}
query = "[System: Delegation Closeout] Synthetic worker completed the backup review."
started, release = Event(), Event()
decisions, rewrites, models = [], [], []

def response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="fixture-model",
    )

def fake_decide(capsule, *, timeout):
    decisions.append(json.loads(capsule))
    return "recall"

def fake_auxiliary(**kwargs):
    assert kwargs["task"] == "better_hindsight_recall_planner"
    assert 0 < kwargs["timeout"] <= 1.0
    rewrites.append(kwargs)
    started.set()
    assert release.wait(10), "test did not release blocked auxiliary transport"
    return response('{"query":"synthetic shadow candidate"}')

async def run():
    server = FakeHindsightServer(bank_id="fixture-bank", disposable_bank_id="fixture-disposable",
        error_sentinel="synthetic-error-must-not-leak", expected_api_key=None)
    await server.start()
    agent = None
    try:
        config = {
            "api_url": server.base_url, "bank_id": "fixture-bank", "single_principal": True,
            "retain": {"enabled": False},
            "recall": {"timeout_seconds": 1.0},
            "planner": {"mode": mode, "rewrite": "shadow", "timeout_seconds": 1.0},
            "evaluation": {"enabled": True},
        }
        config_dir = home / "better_hindsight"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps(config))
        (home / "config.yaml").write_text('''memory:
  provider: better_hindsight
  memory_enabled: false
  user_profile_enabled: false
plugins:
  enabled: [better_hindsight]
agent:
  environment_probe: false
  parallel_tool_call_guidance: false
  task_completion_guidance: false
  tool_use_enforcement: false
sessions:
  write_json_snapshots: false
''')
        from run_agent import AIAgent
        from hermes_cli.plugins import get_plugin_manager
        from agent import auxiliary_client
        manager = get_plugin_manager()
        manager.discover_and_load()
        hook = manager._hooks["pre_llm_call"][0]
        planner = sys.modules[type(hook.__self__).__module__]
        planner.decide_memory = fake_decide
        auxiliary_client.call_llm = fake_auxiliary
        agent = AIAgent(model="fixture-model", provider="openai", api_mode="chat_completions",
            api_key="test", base_url="http://127.0.0.1:1/v1",
            max_iterations=1, enabled_toolsets=[], quiet_mode=True, verbose_logging=False,
            save_trajectories=False, skip_context_files=True, skip_memory=False,
            session_id="fixture-turn-session", platform="cli")
        assert [p.name for p in agent._memory_manager.providers] == ["better_hindsight"]
        provider = agent._memory_manager.providers[0]
        package = type(provider).__module__.rsplit(".", 1)[0]
        runtime = importlib.import_module(package + ".runtime")
        shadow = importlib.import_module(package + ".shadow_rewrite")
        evaluation = importlib.import_module(package + ".evaluation")
        mailbox = planner.InMemoryPlanMailbox(home)

        def model_call(api_kwargs, **_kwargs):
            models.append(api_kwargs)
            if scenario == "blocked":
                assert not release.is_set()
            return response("synthetic final response")
        agent._interruptible_api_call = model_call
        agent._interruptible_streaming_api_call = model_call
        history = [] if internal and first_turn else [
            {"role":"user", "content":"Remember our synthetic backup policy?"},
            {"role":"assistant", "content":"We chose synthetic daily backups."},
        ]
        if scenario in {"ordinary_same", "summary", "summary_merged"}:
            history[0] = {"role":"user", "content":query,
                          "display_kind":"delegation_closeout"}
        options = {}
        if internal:
            options = {"persist_user_display_kind":kind,
                       "persist_user_display_metadata":{"version":1}}
        api_query = query
        if scenario in {"timestamp", "ordinary_same"}:
            from datetime import datetime, timezone, timedelta
            from gateway.message_timestamps import render_user_content_with_timestamp
            timestamp = datetime(2026, 9, 20, 19, 7, 33, tzinfo=timezone.utc).timestamp()
            api_query = render_user_content_with_timestamp(
                query, timestamp, tz=timezone(timedelta(hours=2), "CEST"))
            assert api_query != query
            options.update(persist_user_message=query, persist_user_timestamp=timestamp)
        staged = []
        def observe_staging(**kwargs):
            assert kwargs["user_message"] == query
            current = kwargs["conversation_history"][-1]
            if scenario == "timestamp":
                assert current["content"] == api_query
                assert current["display_kind"] == kind
                assert kwargs["is_first_turn"] is first_turn
            if scenario in {"summary", "summary_merged"}:
                # Exercise actual compressor carrier construction, not summary text parsing.
                summary = {"role":"user", "content":"Synthetic reference summary",
                           "_compressed_summary":True,
                           "_compressed_summary_has_user_turn":False}
                if scenario == "summary_merged":
                    from agent.context_compressor import ContextCompressor
                    summary = dict(current)
                    compressor = object.__new__(ContextCompressor)
                    compressor._summary_has_user_turn = False
                    compressor._merge_summary_into_tail_row(
                        summary, "Synthetic reference summary", "user", True)
                    assert query in summary["content"]
                # The older identical typed row must never supply this carrier's origin.
                kwargs["conversation_history"][:] = [history[0], summary]
            staged.append(True)
        manager._hooks["pre_llm_call"].insert(0, observe_staging)
        if scenario != "blocked":
            release.set()
        result = await asyncio.wait_for(asyncio.to_thread(
            agent.run_conversation, api_query, None, history, **options), timeout=3)
        assert result["completed"] is True
        assert len(models) == 1
        assert staged == [True], "staging assertions must not be swallowed by hook dispatch"
        if internal:
            rows = [m for m in result["messages"] if m.get("role") == "user"]
            assert rows[-1]["display_kind"] == kind
            assert rows[-1]["content"] == query
            assert not decisions and not rewrites
            assert len(server.records) == (0 if mode == "active" else 1)
            if server.records:
                assert server.records[0].json_body["query"] == query
        elif scenario == "blocked":
            assert await asyncio.to_thread(started.wait, 3)
            assert len(decisions) == len(rewrites) == len(server.records) == 1
            assert server.records[0].json_body["query"] == query
            # Another real turn can finish while the old auxiliary ignores its timeout.
            second = await asyncio.wait_for(asyncio.to_thread(
                agent.run_conversation, query, None, result["messages"]), timeout=3)
            assert second["completed"] is True
            assert len(models) == len(decisions) == len(server.records) == 2
            assert len(rewrites) == 1
            assert mailbox.consume(source_query=query, session_id=agent.session_id) is None
        else:
            assert len(decisions) == 1 and len(server.records) == 1
        release.set()
        assert await asyncio.to_thread(shadow.drain_shadow_for_tests)
        assert mailbox.consume(source_query=query, session_id=agent.session_id) is None
        assert await asyncio.to_thread(evaluation.drain_evaluation_for_tests)
        rows = [json.loads(p.read_text()) for p in
                (home / "better_hindsight/planner_evaluation").glob("*.json")]
        if mode != "off":
            groups = {}
            for row in rows:
                groups.setdefault(row["correlation_id"], {})[row["stage"]] = row
            assert len(groups) == len(models)
            for stages in groups.values():
                assert {"input", "decision", "retrieval"} <= stages.keys()
                if internal:
                    assert stages["decision"]["outcome"] == "internal_message"
                    assert "rewrite" not in stages
            if scenario == "blocked":
                assert sum(s["rewrite"]["outcome"] == "busy" for s in groups.values()) == 1
        await asyncio.to_thread(agent.close)
        agent = None
        assert await asyncio.to_thread(runtime.finalize_process_runtime)
        return {"scenario":scenario, "mode":mode, "decisions":len(decisions),
                "rewrites":len(rewrites), "models":len(models),
                "recalls":len(server.records),
                "host":str(Path(sys.modules["run_agent"].__file__).resolve())}
    finally:
        release.set()
        if agent is not None:
            await asyncio.to_thread(agent.close)
        await server.close()

print(json.dumps(asyncio.run(run()), sort_keys=True))
"""


@pytest.mark.parametrize(
    ("scenario", "mode"),
    [
        ("internal", "active"),
        ("internal", "shadow"),
        ("internal", "off"),
        ("untyped", "active"),
        ("ordinary_same", "active"),
        ("summary", "active"),
        ("summary_merged", "active"),
        ("blocked", "active"),
    ],
)
def test_real_host_planner_turn_path(
    tmp_path: Path,
    scenario: str,
    mode: str,
    kind: str = "delegation_closeout",
    first_turn: bool = True,
) -> None:
    home = tmp_path / "hermes-home"
    materialize_standard_plugin(source=ROOT, hermes_home=home)
    completed = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(home), scenario, mode, kind, str(first_turn)],
        cwd=tmp_path,
        env=clean_subprocess_env(
            tmp_path,
            hermes_home=home,
            no_proxy="127.0.0.1,localhost",
            extra={"PYTHONPATH": str(ROOT)},
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr[-7000:]
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload["models"] == (2 if scenario == "blocked" else 1)
    print(json.dumps(payload, sort_keys=True))


@pytest.mark.parametrize("mode", ["active", "shadow", "off"])
@pytest.mark.parametrize("kind", ["delegation_closeout", "internal_notification"])
@pytest.mark.parametrize("first_turn", [True, False])
def test_timestamped_internal_real_host_turn(
    tmp_path: Path, mode: str, kind: str, first_turn: bool
) -> None:
    test_real_host_planner_turn_path(tmp_path, "timestamp", mode, kind, first_turn)
