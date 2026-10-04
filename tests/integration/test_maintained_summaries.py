"""Real provider/shared-runtime and operator CLI lifecycle against synthetic HTTP only."""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
from argparse import ArgumentParser
from collections.abc import Iterator
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from agent.memory_manager import MemoryManager

from better_hermes_hindsight.config import load_config
from better_hermes_hindsight.mental_models import OUTPUT_MAX_BYTES, TOOL_NAME, stable_id
from better_hermes_hindsight.operator_cli import better_hindsight_command, register_cli
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.runtime import reset_process_runtime_for_tests
from better_hermes_hindsight.summary_policy import SummaryPolicy

PAGE = "kp-550e8400e29b41d4a716446655440010"
FOLDER = "kf-550e8400e29b41d4a716446655440011"
OP = "550e8400-e29b-41d4-a716-446655440012"
QUESTION = "What are the durable deployment decisions?"


class Server:
    def __init__(self) -> None:
        self.version = "0.10.2"
        self.models: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.ops: dict[str, Any] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.fault = ""
        self.status = "pending"
        self.auto_complete = False
        self.status_entered = threading.Event()
        self.status_release = threading.Event()
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def send(self, payload: object, status: int = 200) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(raw)

            def body(self) -> Any:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                return json.loads(raw) if raw else None

            def do_GET(self) -> None:
                state.requests.append(("GET", self.path, None))
                if state.fault == "auth":
                    self.send({"error": "RAW-PRIVATE-SENTINEL"}, 403)
                    return
                path = urlsplit(self.path).path
                key = path.rsplit("/", 1)[-1]
                if path == "/version":
                    self.send({"api_version": state.version})
                elif "/operations/" in path:
                    if state.fault == "status_deadline":
                        state.status_entered.set()
                        state.status_release.wait(timeout=5)
                    if state.fault == "status_unavailable":
                        self.send({"error": "RAW-PRIVATE-SENTINEL"}, 503)
                        return
                    self.send(
                        {
                            "operation_id": OP,
                            "operation_type": "refresh_mental_model",
                            "status": state.status,
                            "task_payload": {
                                "mental_model_id": "wrong"
                                if state.fault == "operation_binding"
                                else state.ops["model"]
                            },
                            "error_message": "RAW-PRIVATE-SENTINEL",
                        }
                    )
                elif path.endswith("/mental-models"):
                    self.send(
                        {
                            "items": list(state.models.values()),
                            "total": len(state.models),
                            "offset": 0,
                            "limit": 20,
                        }
                    )
                elif "/mental-models/" in path:
                    if state.fault == "model_readback_unavailable" and key in state.models:
                        self.send({"error": "RAW-PRIVATE-SENTINEL"}, 503)
                        return
                    if key not in state.models:
                        self.send({"error": "absent"}, 404)
                    else:
                        model = state.models[key]
                        self.send(
                            {**model, "bank_id": "wrong"}
                            if state.fault == "bank_binding"
                            else model
                        )
                elif path.endswith("/tree"):
                    roots = list(state.nodes.values())
                    if state.fault == "tree_oversized":
                        roots = roots * 201
                    if state.fault == "tree_schema":
                        roots = [{"id": PAGE, "kind": "page", "name": "P", "children": []}]
                    self.send({"roots": roots})
                elif path.endswith("/search"):
                    params = parse_qs(urlsplit(self.path).query)
                    assert params["limit"] == ["10"]
                    hits = [
                        {
                            "id": node["id"],
                            "name": node["name"],
                            "type": "knowledge-page",
                            "tags": [],
                            "snippet": "page answer",
                            "score": 0.02,
                            "mental_model_id": node["mental_model_id"],
                        }
                        for node in state.nodes.values()
                        if node["kind"] == "page"
                    ]
                    if state.fault == "search_schema":
                        hits[0]["score"] = float("nan")
                    self.send({"results": hits, "total": len(hits)})
                elif "/pages/" in path:
                    if state.fault == "page_readback_unavailable":
                        self.send({"error": "RAW-PRIVATE-SENTINEL"}, 503)
                        return
                    node = state.nodes.get(key)
                    if node is None:
                        self.send({"error": "absent"}, 404)
                    else:
                        model = state.models[node["mental_model_id"]]
                        self.send(
                            {
                                "id": "wrong" if state.fault == "page_binding" else key,
                                "name": node["name"],
                                "type": "knowledge-page",
                                "tags": [],
                                "body": model["content"],
                                "markdown": "Content not yet generated"
                                if not model["content"]
                                else model["content"],
                                "history": [{"trace": "RAW-PRIVATE-SENTINEL"}],
                            }
                        )
                else:
                    self.send({"error": "unknown"}, 404)

            def do_POST(self) -> None:
                body = self.body()
                state.requests.append(("POST", self.path, body))
                if state.fault == "reject":
                    self.send({"error": "RAW-PRIVATE-SENTINEL"}, 429)
                    return
                if state.fault == "page_conflict" and self.path.endswith("/knowledge-base/pages"):
                    self.send({"detail": "RAW-PRIVATE-SENTINEL"}, 409)
                    return
                if self.path.endswith("/refresh"):
                    assert body is None
                    key = self.path.split("/")[-2]
                    state.ops["model"] = key
                    if state.auto_complete:
                        from datetime import datetime

                        state.models[key]["content"] = (
                            "Synthetic Northstar recovery: cobalt lantern"
                        )
                        state.models[key]["last_refreshed_at"] = datetime.now(UTC).isoformat()
                    if state.fault == "ambiguous":
                        self.send({"error": "RAW-PRIVATE-SENTINEL"}, 500)
                    else:
                        ack = "pending" if state.fault == "bad_refresh_ack" else "queued"
                        self.send({"operation_id": OP, "status": ack})
                    return
                assert isinstance(body, dict)
                is_page = self.path.endswith("/knowledge-base/pages")
                key = "page-backing" if is_page else body["id"]
                state.models[key] = fixture_model(key, body["source_query"])
                state.models[key].update(
                    {field: body[field] for field in ("name", "tags", "max_tokens", "trigger")}
                )
                state.models[key]["content"] = ""
                if state.auto_complete:
                    from datetime import datetime

                    state.models[key]["content"] = "Synthetic Northstar recovery: cobalt lantern"
                    state.models[key]["last_refreshed_at"] = datetime.now(UTC).isoformat()
                if is_page:
                    state.nodes[PAGE] = {
                        "id": PAGE,
                        "kind": "page",
                        "name": body["name"],
                        "parent_id": body["parent_id"],
                        "mental_model_id": key,
                        "children": [],
                        "tags": [],
                    }
                state.ops["model"] = key
                if state.fault == "ambiguous":
                    self.send({"error": "RAW-PRIVATE-SENTINEL"}, 500)
                else:
                    self.send(
                        {
                            "mental_model_id": key,
                            "operation_id": OP,
                            **({"page_id": PAGE} if is_page else {}),
                        },
                        201 if is_page else 200,
                    )

            def do_PATCH(self) -> None:
                body = self.body()
                state.requests.append(("PATCH", self.path, body))
                key = self.path.rsplit("/", 1)[-1]
                assert "/mental-models/" in self.path and "/nodes/" not in self.path
                if state.fault != "patch_noop":
                    model = state.models[key]
                    for field, value in body.items():
                        if field == "trigger":
                            model[field].update(value)
                        else:
                            model[field] = value
                self.send(state.models[key])

            def do_DELETE(self) -> None:
                state.requests.append(("DELETE", self.path, None))
                key = self.path.rsplit("/", 1)[-1]
                assert "/mental-models/" in self.path
                if state.fault != "delete_noop":
                    state.models.pop(key)
                    state.nodes = {
                        page: node
                        for page, node in state.nodes.items()
                        if node.get("mental_model_id") != key
                    }
                self.send({"status": "deleted"})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def close(self) -> None:
        self.status_release.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)


def fixture_model(key: str, question: str = QUESTION) -> dict[str, Any]:
    return {
        "id": key,
        "bank_id": "synthetic-bank",
        "name": "Deployment decisions",
        "source_query": question,
        "content": "Generated fixture decisions",
        "tags": [],
        "max_tokens": 1024,
        "trigger": SummaryPolicy().trigger(),
        "last_refreshed_at": None,
        "last_memory_seen_at": None,
        "is_stale": True,
    }


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_process_runtime_for_tests()
    for name in tuple(os.environ):
        if name.startswith("HINDSIGHT_"):
            monkeypatch.delenv(name)
    yield
    reset_process_runtime_for_tests()


@pytest.fixture
def server() -> Iterator[Server]:
    state = Server()
    try:
        yield state
    finally:
        state.close()


def config_home(home: Path, server: Server, **mental: object) -> None:
    path = home / "better_hindsight" / "config.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "api_url": server.url,
                "bank_id": "synthetic-bank",
                "single_principal": True,
                "recall": {"enabled": False},
                "reflect": {"enabled": False},
                "mental_models": {
                    "enabled": True,
                    "create_enabled": True,
                    "refresh_enabled": True,
                    "pages_enabled": True,
                    "timeout_seconds": 2,
                    **mental,
                },
            }
        )
    )


def unwrap(raw: str) -> dict[str, Any]:
    assert len(raw.encode()) <= OUTPUT_MAX_BYTES
    assert "RAW-PRIVATE-SENTINEL" not in raw
    envelope = json.loads(raw)
    if "context" not in envelope:
        return cast(dict[str, Any], envelope)
    assert envelope["trust"] == "untrusted_generated_evidence"
    return cast(
        dict[str, Any],
        json.loads(next(line for line in envelope["context"].splitlines() if line.startswith("{"))),
    )


def cli(home: Path, words: list[str]) -> tuple[int, dict[str, Any]]:
    parser = ArgumentParser(allow_abbrev=False)
    register_cli(parser)
    args = parser.parse_args(words)
    stdout, stderr = io.StringIO(), io.StringIO()
    from unittest.mock import patch as mock_patch

    code = 0
    with (
        mock_patch.dict(os.environ, {"HERMES_HOME": str(home)}),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        try:
            better_hindsight_command(args)
        except SystemExit as error:
            code = int(error.code) if isinstance(error.code, int) else 3
    assert not stderr.getvalue()
    return code, unwrap(stdout.getvalue())


def host(home: Path) -> MemoryManager:
    result = MemoryManager()
    result.add_provider(BetterHindsightMemoryProvider())
    result.initialize_all(
        "synthetic-maintenance", hermes_home=str(home), platform="cli", agent_context="primary"
    )
    return result


def provider_call(manager: MemoryManager, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("action") == "refresh":
        args = {**args, "reason": "Explicit durable decision refresh"}
    raw = manager.handle_tool_call(TOOL_NAME, args)
    assert isinstance(raw, str)
    return unwrap(raw)


def test_complete_operator_and_provider_page_lifecycle(tmp_path: Path, server: Server) -> None:
    config_home(tmp_path, server)
    manager = host(tmp_path)
    assert server.requests == []  # Initialization never writes settings.
    code, created = cli(
        tmp_path,
        [
            "pages",
            "create",
            "--name",
            "Deployment decisions",
            "--source-query",
            QUESTION,
            "--mode",
            "delta",
            "--budget",
            "mid",
            "--refresh-cron",
            "0 4 * * *",
            "--min-refresh-interval-seconds",
            "300",
            "--confirm",
        ],
    )
    assert code == 0 and created["result"] == "queued"
    assert created["page_id"] == PAGE and created["mental_model_id"] == "page-backing"
    request = next(body for method, path, body in server.requests if method == "POST")
    assert request["trigger"]["mode"] == "delta" and request["trigger"]["budget"] == "mid"
    assert request["trigger"]["refresh_cron"] == "0 4 * * *"
    assert request["trigger"]["refresh_after_consolidation"] is False
    assert (
        request["trigger"]["keep_trace"] is False
        and request["trigger"]["exclude_mental_models"] is True
    )
    assert request["tags"] == []
    assert (
        provider_call(manager, {"action": "page_browse"})["items"][0]["mental_model_id"]
        == "page-backing"
    )
    assert (
        provider_call(manager, {"action": "page_search", "query": "deployment"})["returned_hits"]
        == 1
    )
    assert (
        provider_call(manager, {"action": "page_read", "id": PAGE})["generated_content_present"]
        is False
    )
    server.status = "completed"
    server.models["page-backing"]["content"] = "Generated deployment decisions"
    assert cli(tmp_path, ["summaries", "status", "page-backing", OP])[1]["status"] == "completed"
    assert (
        provider_call(manager, {"action": "page_read", "id": PAGE})["generated_content_present"]
        is True
    )
    before_posts = len([r for r in server.requests if r[0] == "POST"])
    code, edited = cli(
        tmp_path,
        [
            "summaries",
            "edit",
            "page-backing",
            "--source-query",
            "What changed in deployment decisions?",
            "--after-consolidation",
            "true",
            "--budget",
            "high",
            "--confirm",
            "page-backing",
        ],
    )
    assert code == 0 and edited["result"] == "definition_verified"
    assert len([r for r in server.requests if r[0] == "POST"]) == before_posts
    assert server.models["page-backing"]["trigger"]["refresh_cron"] is None
    assert server.models["page-backing"]["trigger"]["refresh_after_consolidation"] is True
    assert provider_call(manager, {"action": "refresh", "id": "page-backing"})["result"] == "queued"
    assert server.requests[-2][0] == "POST" and server.requests[-2][2] is None
    code, deleted = cli(
        tmp_path, ["summaries", "delete", "page-backing", "--confirm", "page-backing"]
    )
    assert code == 0 and deleted["result"] == "deleted_verified"
    assert deleted["deleted_page_ids"] == [PAGE] and server.models == {} and server.nodes == {}
    assert cli(tmp_path, ["pages", "browse"])[1]["items"] == []
    manager.shutdown_all()


def test_standalone_create_readback_reuse_and_edited_question_mismatch(
    tmp_path: Path, server: Server
) -> None:
    config_home(tmp_path, server)
    code, created = cli(
        tmp_path,
        [
            "summaries",
            "create",
            "--name",
            "Decision summary",
            "--source-query",
            QUESTION,
            "--confirm",
        ],
    )
    assert code == 0 and created["result"] == "queued"
    key = stable_id(load_config(tmp_path), QUESTION)
    assert created["id"] == key
    assert cli(tmp_path, ["summaries", "inspect", key])[0] == 0
    assert (
        cli(
            tmp_path,
            [
                "summaries",
                "create",
                "--name",
                "Decision summary",
                "--source-query",
                QUESTION,
                "--confirm",
            ],
        )[1]["result"]
        == "existing"
    )
    assert cli(tmp_path, ["pages", "search", "decisions"])[1]["items"] == []
    assert (
        cli(
            tmp_path,
            ["summaries", "edit", key, "--source-query", "Different question?", "--confirm", key],
        )[0]
        == 0
    )
    before = len([r for r in server.requests if r[0] == "POST"])
    assert (
        cli(
            tmp_path,
            [
                "summaries",
                "create",
                "--name",
                "Decision summary",
                "--source-query",
                QUESTION,
                "--confirm",
            ],
        )[0]
        == 3
    )
    assert len([r for r in server.requests if r[0] == "POST"]) == before


@pytest.mark.parametrize("action", ["refresh", "page_browse", "page_search", "page_read"])
def test_new_operations_require_exact_version(tmp_path: Path, server: Server, action: str) -> None:
    config_home(tmp_path, server)
    server.version = "0.10.0"
    args: dict[str, Any] = {"action": action}
    if action in {"refresh", "page_read"}:
        args["id"] = PAGE if action == "page_read" else "fixture"
    if action == "page_search":
        args["query"] = "deploy"
    manager = host(tmp_path)
    assert "error" in provider_call(manager, args)
    assert server.requests == [("GET", "/version", None)]
    manager.shutdown_all()


@pytest.mark.parametrize(
    "flag,action",
    [
        ("refresh_enabled", {"action": "refresh", "id": "fixture"}),
        ("pages_enabled", {"action": "page_browse"}),
    ],
)
def test_separate_optins_fail_before_io(
    tmp_path: Path, server: Server, flag: str, action: dict[str, Any]
) -> None:
    config_home(tmp_path, server, **{flag: False})
    manager = host(tmp_path)
    assert "error" in provider_call(manager, action) and not server.requests
    manager.shutdown_all()


@pytest.mark.parametrize(
    "fault,action",
    [
        ("bank_binding", {"action": "refresh", "id": "fixture"}),
        ("tree_schema", {"action": "page_browse"}),
        ("tree_oversized", {"action": "page_browse"}),
        ("search_schema", {"action": "page_search", "query": "deploy"}),
        ("page_binding", {"action": "page_read", "id": PAGE}),
        ("auth", {"action": "page_browse"}),
    ],
)
def test_provider_failure_redacted_and_no_writes(
    tmp_path: Path, server: Server, fault: str, action: dict[str, Any]
) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    server.nodes[PAGE] = {
        "id": PAGE,
        "kind": "page",
        "name": "Page",
        "parent_id": None,
        "mental_model_id": "fixture",
        "children": [],
    }
    server.fault = fault
    manager = host(tmp_path)
    assert "error" in provider_call(manager, action)
    assert not any(method != "GET" for method, _, _ in server.requests)
    manager.shutdown_all()


@pytest.mark.parametrize(
    "fault,words",
    [
        (
            "patch_noop",
            ["summaries", "edit", "fixture", "--name", "Changed", "--confirm", "fixture"],
        ),
        ("delete_noop", ["summaries", "delete", "fixture", "--confirm", "fixture"]),
        ("operation_binding", ["summaries", "refresh", "fixture", "--confirm", "fixture"]),
    ],
)
def test_operator_does_not_claim_success_from_ack(
    tmp_path: Path, server: Server, fault: str, words: list[str]
) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    server.fault = fault
    code, result = cli(tmp_path, words)
    assert code == 3 and result["result"] == "unconfirmed"


def test_confirmation_invalid_policy_and_shutdown_no_io(tmp_path: Path, server: Server) -> None:
    config_home(tmp_path, server)
    for words in (
        ["summaries", "delete", "fixture", "--confirm", "other"],
        ["summaries", "edit", "fixture", "--refresh-cron", "@hourly", "--confirm", "fixture"],
        [
            "pages",
            "create",
            "--name",
            "P",
            "--source-query",
            QUESTION,
            "--refresh-cron",
            "0 0 * * * *",
            "--confirm",
        ],
    ):
        assert cli(tmp_path, words)[0] == 2
    assert not server.requests
    manager = host(tmp_path)
    manager.shutdown_all()
    assert "error" in provider_call(manager, {"action": "page_browse"})
    assert not server.requests


def test_ambiguous_refresh_not_retried_by_runtime(tmp_path: Path, server: Server) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    server.fault = "ambiguous"
    manager = host(tmp_path)
    assert provider_call(manager, {"action": "refresh", "id": "fixture"})["result"] == "ambiguous"
    assert provider_call(manager, {"action": "refresh", "id": "fixture"})["result"] == "ambiguous"
    assert len([r for r in server.requests if r[0] == "POST"]) == 1
    manager.shutdown_all()


def test_ambiguous_page_create_is_one_write_and_actionable(tmp_path: Path, server: Server) -> None:
    config_home(tmp_path, server)
    server.fault = "ambiguous"
    code, result = cli(
        tmp_path, ["pages", "create", "--name", "P", "--source-query", QUESTION, "--confirm"]
    )
    assert code == 3 and result["result"] == "ambiguous"
    assert len([r for r in server.requests if r[0] == "POST"]) == 1
    assert PAGE in server.nodes


@pytest.mark.parametrize(
    "field,value",
    [
        ("keep_trace", True),
        ("response_schema", {"private": "RAW-PRIVATE-SENTINEL"}),
        ("tag_groups", [{"tags": ["private"]}]),
        ("fact_types", ["observation"]),
        ("include_chunks", True),
        ("exclude_mental_models", False),
    ],
)
def test_refresh_and_edit_refuse_inherited_policy(
    tmp_path: Path, server: Server, field: str, value: object
) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    server.models["fixture"]["trigger"][field] = value
    manager = host(tmp_path)
    assert "error" in provider_call(manager, {"action": "refresh", "id": "fixture"})
    assert (
        cli(tmp_path, ["summaries", "edit", "fixture", "--budget", "low", "--confirm", "fixture"])[
            0
        ]
        == 3
    )
    assert all(method == "GET" for method, _, _ in server.requests)
    manager.shutdown_all()


@pytest.mark.parametrize(
    "bad_id",
    [
        "550e8400-e29b-41d4-a716-446655440010",
        "kp-short",
        "../page",
        "kp-" + "A" * 32,
    ],
)
def test_malformed_page_identifier_has_no_io(tmp_path: Path, server: Server, bad_id: str) -> None:
    config_home(tmp_path, server)
    manager = host(tmp_path)
    assert "error" in provider_call(manager, {"action": "page_read", "id": bad_id})
    assert not server.requests
    manager.shutdown_all()


def test_live_maintenance_harness_exercised_against_loopback(
    tmp_path: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_isolated_hindsight import (
        _assert_live_mental_models,
        _assert_live_summary_maintenance,
    )

    config_home(tmp_path, server)
    server.auto_complete = True
    server.status = "completed"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = host(tmp_path)
    # Use the real provider surface; no monkeypatched lifecycle results.
    provider = BetterHindsightMemoryProvider()
    provider.initialize(
        "synthetic-maintenance", hermes_home=str(tmp_path), platform="cli", agent_context="primary"
    )
    _assert_live_mental_models(provider)
    _assert_live_summary_maintenance(tmp_path, provider)
    assert server.models == {} and server.nodes == {}
    assert len([r for r in server.requests if r[0] == "DELETE"]) == 2
    provider.shutdown()
    manager.shutdown_all()


def test_refresh_wrong_wire_ack_never_claims_queued(tmp_path: Path, server: Server) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    server.fault = "bad_refresh_ack"
    manager = host(tmp_path)
    first = provider_call(manager, {"action": "refresh", "id": "fixture"})
    second = provider_call(manager, {"action": "refresh", "id": "fixture"})
    assert first["result"] == second["result"] == "ambiguous"
    assert len([r for r in server.requests if r[0] == "POST"]) == 1
    manager.shutdown_all()


def test_conflicting_operator_triggers_no_io(tmp_path: Path, server: Server) -> None:
    config_home(tmp_path, server)
    with pytest.raises(SystemExit) as error:
        cli(
            tmp_path,
            [
                "summaries",
                "edit",
                "fixture",
                "--refresh-cron",
                "0 4 * * *",
                "--after-consolidation",
                "true",
                "--confirm",
                "fixture",
            ],
        )
    assert error.value.code == 2
    assert server.requests == []


@pytest.mark.parametrize("surface", ["provider", "operator"])
def test_refresh_ack_survives_failed_status(tmp_path: Path, server: Server, surface: str) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    server.fault = "status_unavailable"
    if surface == "operator":
        code, result = cli(tmp_path, ["summaries", "refresh", "fixture", "--confirm", "fixture"])
        assert code == 3
    else:
        manager = host(tmp_path)
        result = provider_call(manager, {"action": "refresh", "id": "fixture"})
        # Even failed model reads after the ACK cannot erase known IDs or permit a retry.
        server.fault = "model_readback_unavailable"
        duplicate = provider_call(manager, {"action": "refresh", "id": "fixture"})
        assert duplicate == result
        manager.shutdown_all()
    assert result["result"] == "unconfirmed"
    assert result["id"] == "fixture" and result["operation_id"] == OP
    assert "No retry" in result["verification"]
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "fault", ["model_readback_unavailable", "page_readback_unavailable", "status_unavailable"]
)
def test_page_create_ack_survives_failed_readback(
    tmp_path: Path, server: Server, fault: str
) -> None:
    config_home(tmp_path, server)
    server.fault = fault
    code, result = cli(
        tmp_path, ["pages", "create", "--name", "P", "--source-query", QUESTION, "--confirm"]
    )
    assert code == 3 and result["result"] == "unconfirmed"
    assert result["page_id"] == PAGE and result["mental_model_id"] == "page-backing"
    assert result["operation_id"] == OP and "No retry" in result["verification"]
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "fault", ["model_readback_unavailable", "page_readback_unavailable", "status_unavailable"]
)
def test_page_create_unconfirmed_duplicate_sends_no_write(
    tmp_path: Path, server: Server, fault: str
) -> None:
    from better_hermes_hindsight.client import HindsightClientProtocol
    from better_hermes_hindsight.mental_models import MentalModelClient, MentalModels
    from better_hermes_hindsight.runtime import create_operator_runtime
    from better_hermes_hindsight.summary_management import operator_call

    config_home(tmp_path, server)
    config = load_config(tmp_path)
    models = MentalModels(config)
    runtime = create_operator_runtime(config)
    server.fault = fault
    args = {"action": "page_create", "name": "P", "source_query": QUESTION, "confirm": True}

    async def operation(client: HindsightClientProtocol) -> str:
        first = await operator_call(models, cast(MentalModelClient, client), args)
        second = await operator_call(models, cast(MentalModelClient, client), args)
        assert unwrap(first) == unwrap(second)
        return second

    try:
        result = unwrap(runtime.call(operation, timeout=2))
    finally:
        runtime.finalize()
    assert result["result"] == "unconfirmed"
    assert result["page_id"] == PAGE and result["mental_model_id"] == "page-backing"
    assert result["operation_id"] == OP
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


@pytest.mark.parametrize("action", ["refresh", "page_create"])
def test_operator_cancelled_verification_retains_ack_ids(
    tmp_path: Path, server: Server, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    import asyncio

    from better_hermes_hindsight.mental_models import MentalModelClient, MentalModels

    async def cancelled(
        self: MentalModels, client: MentalModelClient, model_id: str, op_id: str
    ) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(MentalModels, "status", cancelled)
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    words = (
        ["summaries", "refresh", "fixture", "--confirm", "fixture"]
        if action == "refresh"
        else ["pages", "create", "--name", "P", "--source-query", QUESTION, "--confirm"]
    )
    code, result = cli(tmp_path, words)
    assert code == 3 and result["result"] == "unconfirmed"
    assert result["operation_id"] == OP
    if action == "refresh":
        assert result["id"] == "fixture"
    else:
        assert result["page_id"] == PAGE and result["mental_model_id"] == "page-backing"
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


@pytest.mark.parametrize("tokens", [1, 128, 255, 256])
@pytest.mark.parametrize("action", ["create", "edit", "page_create"])
def test_operator_output_token_boundary(
    tmp_path: Path, server: Server, tokens: int, action: str
) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    if action == "edit":
        words = ["summaries", "edit", "fixture", "--confirm", "fixture"]
    else:
        words = [
            "pages" if action == "page_create" else "summaries",
            "create",
            "--name",
            "P",
            "--source-query",
            QUESTION,
            "--confirm",
        ]
    code, result = cli(tmp_path, [*words, "--max-tokens", str(tokens)])
    if tokens < 256:
        assert code == 2 and result["error"] == "arguments_invalid"
        assert not server.requests
    else:
        assert code == 0
        writes = [body for method, _, body in server.requests if method in {"POST", "PATCH"}]
        assert len(writes) == 1 and writes[0]["max_tokens"] == 256


def test_inspection_omits_unknown_trigger_artifacts(tmp_path: Path, server: Server) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    server.models["fixture"]["trigger"]["backend_private"] = "RAW-PRIVATE-SENTINEL"
    code, result = cli(tmp_path, ["summaries", "inspect", "fixture"])
    assert code == 0 and "RAW-PRIVATE-SENTINEL" not in json.dumps(result)
    assert "backend_private" not in result["definition"]["trigger"]


@pytest.mark.parametrize("fault", ["model_readback_unavailable", "status_unavailable"])
def test_standalone_create_readback_failure_retains_ack_ids(
    tmp_path: Path, server: Server, fault: str
) -> None:
    config_home(tmp_path, server)
    expected_id = stable_id(load_config(tmp_path), QUESTION)
    server.fault = fault
    words = ["summaries", "create", "--name", "P", "--source-query", QUESTION, "--confirm"]
    code, result = cli(tmp_path, words)
    assert code == 3 and result["result"] == "unconfirmed"
    assert result["id"] == expected_id and result["operation_id"] == OP
    assert len([r for r in server.requests if r[0] == "POST"]) == 1
    server.fault = ""
    code, reconciled = cli(tmp_path, words)
    assert code == 0 and reconciled["result"] == "existing"
    assert reconciled["id"] == expected_id
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


def test_standalone_create_cancelled_verification_retains_ack_ids(
    tmp_path: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from better_hermes_hindsight.mental_models import MentalModelClient, MentalModels

    async def cancelled(
        self: MentalModels, client: MentalModelClient, model_id: str, op_id: str
    ) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(MentalModels, "status", cancelled)
    config_home(tmp_path, server)
    expected_id = stable_id(load_config(tmp_path), QUESTION)
    words = ["summaries", "create", "--name", "P", "--source-query", QUESTION, "--confirm"]
    code, result = cli(tmp_path, words)
    assert code == 3 and result["result"] == "unconfirmed"
    assert result["id"] == expected_id and result["operation_id"] == OP
    code, reconciled = cli(tmp_path, words)
    assert code == 0 and reconciled["result"] == "existing"
    assert reconciled["id"] == expected_id
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled"])
@pytest.mark.parametrize("action", ["refresh", "create", "page_create"])
def test_immediate_terminal_generation_state(
    tmp_path: Path, server: Server, status: str, action: str
) -> None:
    config_home(tmp_path, server)
    server.status = status
    server.models["fixture"] = fixture_model("fixture")
    words = (
        ["summaries", "refresh", "fixture", "--confirm", "fixture"]
        if action == "refresh"
        else [
            "pages" if action == "page_create" else "summaries",
            "create",
            "--name",
            "P",
            "--source-query",
            QUESTION,
            "--confirm",
        ]
    )
    code, result = cli(tmp_path, words)
    assert code == 3 and result["result"] == result["status"] == status
    assert result["operation_id"] == OP
    key = result.get("id", result.get("mental_model_id"))
    assert isinstance(key, str)
    assert cli(tmp_path, ["summaries", "status", key, OP])[0] == 3
    if action == "refresh":
        manager = host(tmp_path)
        try:
            model_result = provider_call(manager, {"action": "refresh", "id": "fixture"})
            assert model_result["result"] == model_result["status"] == status
            assert model_result["operation_id"] == OP
        finally:
            manager.shutdown_all()


@pytest.mark.parametrize("surface", ["provider", "operator"])
def test_real_refresh_deadline_keeps_ack(tmp_path: Path, server: Server, surface: str) -> None:
    config_home(tmp_path, server, timeout_seconds=0.5)
    server.models["fixture"] = fixture_model("fixture")
    server.fault = "status_deadline"
    if surface == "operator":
        code, result = cli(tmp_path, ["summaries", "refresh", "fixture", "--confirm", "fixture"])
        assert code == 3
    else:
        manager = host(tmp_path)
        try:
            result = provider_call(manager, {"action": "refresh", "id": "fixture"})
            assert server.status_entered.is_set()
            duplicate = provider_call(manager, {"action": "refresh", "id": "fixture"})
            assert duplicate == result
        finally:
            server.status_release.set()
            manager.shutdown_all()
    assert server.status_entered.is_set()
    assert result["result"] == "unconfirmed"
    assert result["id"] == "fixture" and result["operation_id"] == OP
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


@pytest.mark.parametrize("cron", [" 0 4 * * *", "0 4 * * * ", "\t0 4 * * *\n"])
@pytest.mark.parametrize("action", ["create", "edit", "page_create"])
def test_cron_surrounding_whitespace_rejected_before_io(
    tmp_path: Path, server: Server, cron: str, action: str
) -> None:
    config_home(tmp_path, server)
    words = (
        ["summaries", "edit", "fixture", "--confirm", "fixture"]
        if action == "edit"
        else [
            "pages" if action == "page_create" else "summaries",
            "create",
            "--name",
            "P",
            "--source-query",
            QUESTION,
            "--confirm",
        ]
    )
    code, result = cli(tmp_path, [*words, "--refresh-cron", cron])
    assert code == 2 and result["error"] == "arguments_invalid"
    assert not server.requests


@pytest.mark.parametrize(
    "body,generated",
    [
        ("Generating content...", False),
        ("", False),
        ("   ", False),
        ("Generating content... with details", True),
        (" Generating content... ", True),
        ("Generated answer", True),
    ],
)
def test_legacy_placeholder_projection(
    tmp_path: Path, server: Server, body: str, generated: bool
) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    server.models["fixture"]["content"] = body
    server.nodes[PAGE] = {
        "id": PAGE,
        "kind": "page",
        "name": "P",
        "parent_id": None,
        "mental_model_id": "fixture",
        "children": [],
    }
    manager = host(tmp_path)
    try:
        result = provider_call(manager, {"action": "page_read", "id": PAGE})
        assert result["generated_content_present"] is generated
        assert result["content"] == ("" if body == "Generating content..." else body)
    finally:
        manager.shutdown_all()


def test_page_conflict_is_rejection_and_does_not_reserve(tmp_path: Path, server: Server) -> None:
    from better_hermes_hindsight.client import HindsightClientProtocol
    from better_hermes_hindsight.mental_models import MentalModelClient, MentalModels
    from better_hermes_hindsight.runtime import create_operator_runtime
    from better_hermes_hindsight.summary_management import operator_call

    config_home(tmp_path, server)
    server.fault = "page_conflict"
    words = ["pages", "create", "--name", "P", "--source-query", QUESTION, "--confirm"]
    code, result = cli(tmp_path, words)
    assert code == 3 and result["result"] == "rejected"
    assert result["error"] == "page_name_conflict"
    assert not server.models and not server.nodes and not server.ops
    models = MentalModels(load_config(tmp_path))
    runtime = create_operator_runtime(models.config)
    args = {"action": "page_create", "name": "P", "source_query": QUESTION, "confirm": True}

    async def operation(client: HindsightClientProtocol) -> str:
        first = await operator_call(models, cast(MentalModelClient, client), args)
        assert unwrap(first)["result"] == "rejected"
        assert models.page_creation_ambiguous is False
        assert models.page_creation_unconfirmed is None
        server.fault = ""
        return await operator_call(models, cast(MentalModelClient, client), args)

    try:
        assert unwrap(runtime.call(operation, timeout=2))["result"] == "queued"
    finally:
        runtime.finalize()


@pytest.mark.parametrize("surface", ["provider", "operator"])
def test_browse_continuation_discovers_entire_bounded_tree(
    tmp_path: Path, server: Server, surface: str
) -> None:
    config_home(tmp_path, server)
    for n in range(200):
        key = f"kp-{n:032x}"
        server.nodes[key] = {
            "id": key,
            "kind": "page",
            "name": "決" * 120,
            "parent_id": None,
            "mental_model_id": "fixture",
            "children": [],
        }
    manager = host(tmp_path)
    offset = 0
    seen: list[str] = []
    try:
        for _ in range(200):
            if surface == "provider":
                result = provider_call(manager, {"action": "page_browse", "offset": offset})
            else:
                code, result = cli(tmp_path, ["pages", "browse", "--offset", str(offset)])
                assert code == 0
            assert result["offset"] == offset and result["total"] == 200
            seen.extend(item["page_id"] for item in result["items"])
            next_offset = result["next_offset"]
            if next_offset is None:
                break
            assert result["truncated"] and next_offset == offset + len(result["items"])
            assert next_offset > offset
            offset = next_offset
        assert seen == list(server.nodes)
        assert len(set(seen)) == 200
        assert not any(method != "GET" for method, _, _ in server.requests)
    finally:
        manager.shutdown_all()


@pytest.mark.parametrize("page_backed", [False, True])
def test_summary_rename_never_desynchronizes_page_title(
    tmp_path: Path, server: Server, page_backed: bool
) -> None:
    config_home(tmp_path, server)
    server.models["fixture"] = fixture_model("fixture")
    original_name = server.models["fixture"]["name"]
    if page_backed:
        server.nodes[PAGE] = {
            "id": PAGE,
            "kind": "page",
            "name": original_name,
            "parent_id": None,
            "mental_model_id": "fixture",
            "children": [],
        }
    code, result = cli(
        tmp_path,
        ["summaries", "edit", "fixture", "--name", "Changed", "--confirm", "fixture"],
    )
    if page_backed:
        assert code == 3 and "Page-backed summary renames" in result["error"]
        assert server.models["fixture"]["name"] == original_name
        assert server.nodes[PAGE]["name"] == original_name
        assert not any(method != "GET" for method, _, _ in server.requests)
        code, edited = cli(
            tmp_path,
            ["summaries", "edit", "fixture", "--budget", "low", "--confirm", "fixture"],
        )
        assert code == 0 and edited["result"] == "definition_verified"
        assert server.models["fixture"]["trigger"]["budget"] == "low"
        assert server.nodes[PAGE]["name"] == original_name
    else:
        assert code == 0 and result["result"] == "definition_verified"
        assert server.models["fixture"]["name"] == "Changed"
