"""Real MemoryManager -> shared runtime -> synthetic loopback HTTP pilot tests."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from agent.memory_manager import MemoryManager

from better_hermes_hindsight.config import load_config
from better_hermes_hindsight.mental_models import OUTPUT_MAX_BYTES, TOOL_NAME, stable_id
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.runtime import reset_process_runtime_for_tests

OP_ID = "550e8400-e29b-41d4-a716-446655440000"
CREATE = {
    "action": "create",
    "name": "Team preferences",
    "source_query": "What does the team prefer?",
    "reason": "Recurring planning question",
}


class Server:
    def __init__(self) -> None:
        self.models: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.version = "0.10.0"
        self.fault = ""
        self.operation_type = "refresh_mental_model"
        self.operation_model = ""
        self.status = "pending"
        self.total_override: int | None = None
        self.post_started = threading.Event()
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

            def do_GET(self) -> None:
                state.requests.append(("GET", self.path, None))
                if state.fault == "timeout":
                    time.sleep(0.4)
                if state.fault == "auth":
                    self.send({"error": "RAW-PRIVATE-SENTINEL"}, 401)
                    return
                if state.fault == "oversized":
                    self.send({"content": "x" * 300000})
                    return
                if self.path == "/version":
                    self.send({"api_version": state.version})
                    return
                parsed = urlsplit(self.path)
                if parsed.path.endswith("/operations/" + OP_ID):
                    assert parse_qs(parsed.query) == {"include_payload": ["true"]}
                    self.send(
                        {
                            "operation_id": OP_ID,
                            "status": state.status,
                            "operation_type": state.operation_type,
                            "task_payload": {
                                "mental_model_id": state.operation_model,
                                "_tenant_id": "RAW-PRIVATE-SENTINEL",
                            },
                            "error_message": "RAW-PRIVATE-SENTINEL",
                            "result_metadata": {"trace": "RAW-PRIVATE-SENTINEL"},
                        }
                    )
                    return
                if parsed.path.endswith("/mental-models"):
                    params = parse_qs(parsed.query)
                    assert params["detail"] == ["metadata"] and params["limit"] == ["20"]
                    offset = int(params["offset"][0])
                    models = list(state.models.values())[offset : offset + 20]
                    self.send(
                        {
                            "items": models,
                            "total": len(state.models)
                            if state.total_override is None
                            else state.total_override,
                            "limit": 20,
                            "offset": offset,
                        }
                    )
                    return
                assert parse_qs(parsed.query) == {"detail": ["content"]}
                model = state.models.get(parsed.path.rsplit("/", 1)[-1])
                self.send(
                    model if model is not None else {"error": "not found"}, 200 if model else 404
                )

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state.requests.append(("POST", self.path, body))
                state.post_started.set()
                assert self.path == "/v1/default/banks/synthetic-bank/mental-models"
                if state.fault != "absent_write":
                    state.models[body["id"]] = model(body["id"], source_query=body["source_query"])
                    state.operation_model = body["id"]
                if state.fault in {"ambiguous_write", "absent_write"}:
                    self.send({"error": "RAW-PRIVATE-SENTINEL"}, 500)
                elif state.fault == "slow_write":
                    time.sleep(0.4)
                    self.send({"mental_model_id": body["id"], "operation_id": OP_ID})
                else:
                    self.send({"mental_model_id": body["id"], "operation_id": OP_ID})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def close(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)


def model(model_id: str, *, source_query: str = "fixture question") -> dict[str, Any]:
    return {
        "id": model_id,
        "bank_id": "synthetic-bank",
        "name": "Fixture model",
        "source_query": source_query,
        "content": "Generated fixture answer",
        "last_refreshed_at": "2026-01-02T00:00:00Z",
        "last_memory_seen_at": "2026-01-01T00:00:00Z",
        "is_stale": True,
        "reflect_response": {"trace": "RAW-PRIVATE-SENTINEL"},
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
    value = Server()
    try:
        yield value
    finally:
        value.close()


def manager(
    home: Path,
    server: Server,
    *,
    enabled: bool = True,
    create: bool = True,
    authorized: bool = True,
    cap: int = 5,
    timeout: float = 1.0,
) -> MemoryManager:
    directory = home / "better_hindsight"
    directory.mkdir(exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps(
            {
                "api_url": server.url,
                "bank_id": "synthetic-bank",
                "single_principal": authorized,
                "recall": {"enabled": False},
                "reflect": {"enabled": False},
                "mental_models": {
                    "enabled": enabled,
                    "create_enabled": create,
                    "max_models": cap,
                    "timeout_seconds": timeout,
                },
            }
        )
    )
    result = MemoryManager()
    result.add_provider(BetterHindsightMemoryProvider())
    result.initialize_all(
        "fixture-session", hermes_home=str(home), platform="cli", agent_context="primary"
    )
    return result


def call(manager: MemoryManager, args: dict[str, Any]) -> dict[str, Any]:
    raw = manager.handle_tool_call(TOOL_NAME, args)
    assert isinstance(raw, str)
    assert len(raw.encode()) <= OUTPUT_MAX_BYTES
    assert "RAW-PRIVATE-SENTINEL" not in raw
    result = json.loads(raw)
    if "context" not in result:
        return cast(dict[str, Any], result)
    assert result["trust"] == "untrusted_generated_evidence"
    assert "[RECALLED_MEMORY_EVIDENCE_BEGIN]" in result["context"]
    return cast(
        dict[str, Any],
        json.loads(next(line for line in result["context"].splitlines() if line.startswith("{"))),
    )


def test_manager_queued_status_read_and_exact_reuse(tmp_path: Path, server: Server) -> None:
    host = manager(tmp_path, server)
    assert host.has_tool(TOOL_NAME)
    assert not server.requests  # no version check/prefetch at initialization
    assert call(host, {"action": "list"})["total"] == 0
    created = call(host, CREATE)
    assert created["result"] == "queued" and created["operation_id"] == OP_ID
    body = next(request[2] for request in server.requests if request[0] == "POST")
    assert set(body) == {"id", "name", "source_query", "tags", "max_tokens", "trigger"}
    assert body["tags"] == [] and body["max_tokens"] == 1024
    assert body["trigger"]["refresh_after_consolidation"] is False
    assert body["trigger"]["refresh_cron"] is None
    assert body["trigger"]["keep_trace"] is False
    assert body["trigger"]["exclude_mental_models"] is True
    status = {"action": "status", "id": created["id"], "operation_id": OP_ID}
    assert call(host, status)["status"] == "pending"
    server.status = "completed"
    assert call(host, status)["status"] == "completed"
    read = call(host, {"action": "read", "id": created["id"]})
    assert read["content"] == "Generated fixture answer" and read["is_stale"] is True
    assert read["last_refreshed_at"] != read["last_memory_seen_at"]
    assert read["truncated"] is False
    assert (
        call(host, {**CREATE, "source_query": "WHAT  does the team prefer?"})["result"]
        == "existing"
    )
    assert sum(request[0] == "POST" for request in server.requests) == 1
    assert not any("/refresh" in request[1] for request in server.requests)


@pytest.mark.parametrize(
    ("enabled", "create", "authorized"), [(False, False, True), (True, True, False)]
)
def test_disabled_and_unauthorized_no_io(
    tmp_path: Path, server: Server, enabled: bool, create: bool, authorized: bool
) -> None:
    host = manager(tmp_path, server, enabled=enabled, create=create, authorized=authorized)
    assert host.has_tool(TOOL_NAME)
    assert "error" in call(host, {"action": "list"})
    assert "error" in call(host, CREATE)
    assert not server.requests


def test_read_only_and_lifecycle(tmp_path: Path, server: Server) -> None:
    provider = BetterHindsightMemoryProvider()
    assert "error" in json.loads(provider.handle_tool_call(TOOL_NAME, {"action": "list"}))
    before = provider.get_tool_schemas()
    before[-1]["parameters"]["properties"].clear()
    schema = provider.get_tool_schemas()[-1]
    assert set(schema["parameters"]["properties"]) == {
        "action",
        "id",
        "operation_id",
        "offset",
        "name",
        "source_query",
        "reason",
    }
    assert schema["parameters"]["additionalProperties"] is False
    assert schema["parameters"]["properties"]["action"]["enum"] == [
        "list",
        "read",
        "create",
        "status",
    ]
    host = manager(tmp_path, server, create=False)
    assert "error" in call(host, CREATE)
    assert not server.requests
    assert call(host, {"action": "list"})["total"] == 0
    host.shutdown_all()
    count = len(server.requests)
    assert "error" in call(host, {"action": "list"})
    assert len(server.requests) == count


@pytest.mark.parametrize(
    "args",
    [
        {"action": "refresh", "id": "fixture"},
        {"action": "read", "id": "../wrong"},
        {"action": "list", "offset": True},
        {"action": "list", "offset": -1},
        {"action": "list", "offset": 100001},
        {"action": "list", "bank": "other"},
        {**CREATE, "source_query": "x" * 2001},
        {**CREATE, "reason": " "},
        {**CREATE, "trigger": {}},
        {"action": "status", "id": "fixture", "operation_id": "wrong"},
    ],
)
def test_invalid_no_io(tmp_path: Path, server: Server, args: dict[str, Any]) -> None:
    host = manager(tmp_path, server)
    assert "error" in call(host, args)
    assert not server.requests


@pytest.mark.parametrize("fault", ["auth", "timeout", "oversized"])
def test_fixed_failures(tmp_path: Path, server: Server, fault: str) -> None:
    host = manager(tmp_path, server, timeout=0.05)
    server.fault = fault
    start = time.monotonic()
    assert "error" in call(host, {"action": "list"})
    assert time.monotonic() - start < 0.3
    assert len(server.requests) == 1


def test_only_explicit_path_requires_exact_version(tmp_path: Path, server: Server) -> None:
    server.version = "0.9.2"
    host = manager(tmp_path, server)
    assert not server.requests
    assert "0.10.0" in call(host, {"action": "list"})["error"]
    assert server.requests == [("GET", "/version", None)]


@pytest.mark.parametrize("fault", ["ambiguous_write", "slow_write"])
def test_ambiguous_write_reconciles_exact_id_no_second_post(
    tmp_path: Path, server: Server, fault: str
) -> None:
    host = manager(tmp_path, server, timeout=0.1)
    server.fault = fault
    first = call(host, CREATE)
    assert first["result"] in {"ambiguous", "unconfirmed"}
    assert first["id"] in server.models
    server.fault = ""
    assert call(host, CREATE)["result"] == "existing"
    assert sum(request[0] == "POST" for request in server.requests) == 1


def test_ambiguous_absent_reserves_allowance(tmp_path: Path, server: Server) -> None:
    host = manager(tmp_path, server, cap=1)
    server.fault = "absent_write"
    assert call(host, CREATE)["result"] == "ambiguous"
    assert call(host, CREATE)["result"] == "ambiguous"
    assert "error" in call(host, {**CREATE, "source_query": "Another question?"})
    assert sum(request[0] == "POST" for request in server.requests) == 1


def test_concurrent_creation_shares_cap(tmp_path: Path, server: Server) -> None:
    host = manager(tmp_path, server, cap=1)
    other_handle = manager(tmp_path, server, cap=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda pair: call(pair[0], {**CREATE, "source_query": pair[1]}),
                [(host, "Question one?"), (other_handle, "Question two?")],
            )
        )
    assert sum(result.get("result") == "queued" for result in results) == 1
    assert sum("error" in result for result in results) == 1
    assert len(server.models) == 1


def test_pagination_cap_and_incomplete_inventory(tmp_path: Path, server: Server) -> None:
    for index in range(21):
        server.models[f"fixture-{index}"] = model(f"fixture-{index}")
    host = manager(tmp_path, server)
    page = call(host, {"action": "list"})
    assert len(page["items"]) == 20 and page["total"] == 21 and page["next_offset"] == 20
    page2 = call(host, {"action": "list", "offset": 20})
    assert len(page2["items"]) == 1 and page2["next_offset"] is None
    assert "error" in call(host, CREATE)
    server.total_override = 5
    server.models.clear()
    assert call(host, CREATE)["result"] == "unconfirmed"
    assert not any(request[0] == "POST" for request in server.requests)


def test_read_truncation_redaction_and_status_mismatch(tmp_path: Path, server: Server) -> None:
    host = manager(tmp_path, server)
    server.models["fixture"] = {
        **model("fixture"),
        "name": "x" * 121,
        "content": '"\\\n🎉' * 10000 + " api_key=supersecretvalue12345",
    }
    read = call(host, {"action": "read", "id": "fixture"})
    assert read["truncated"] is True
    assert read["name_truncated"] is True and len(read["name"]) == 120
    assert "supersecretvalue12345" not in read["content"]
    for op_type, model_id in [("retain", "fixture"), ("refresh_mental_model", "other")]:
        server.operation_type, server.operation_model = op_type, model_id
        assert "error" in call(host, {"action": "status", "id": "fixture", "operation_id": OP_ID})


@pytest.mark.parametrize(
    "scope",
    [
        {"recall": {"tags": ["private"]}},
        {"reflect": {"tags": []}},
        {"recall": {"tag_mode": "all_strict"}},
    ],
)
def test_scope_refused(tmp_path: Path, scope: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="unscoped"):
        load_config(tmp_path, environ={}, injected={**scope, "mental_models": {"enabled": True}})


def test_metadata_escaped_output_has_bounded_continuation(tmp_path: Path, server: Server) -> None:
    for index in range(20):
        key = "x" * 125 + str(index)
        server.models[key] = {**model(key), "name": "\x01" * 120}
    host = manager(tmp_path, server)
    page = call(host, {"action": "list"})
    assert page["truncated"] is True
    assert 0 < len(page["items"]) < 20
    assert page["next_offset"] == len(page["items"])
    rest = call(host, {"action": "list", "offset": page["next_offset"]})
    assert len(page["items"]) + len(rest["items"]) == 20


def test_exact_id_and_destination_validation(tmp_path: Path, server: Server) -> None:
    host = manager(tmp_path, server)
    assert "error" in call(host, {"action": "read", "id": "missing"})
    server.models["fixture"] = {**model("fixture"), "bank_id": "other-bank"}
    assert "error" in call(host, {"action": "read", "id": "fixture"})
    server.models["fixture"] = model("different-id")
    assert "error" in call(host, {"action": "read", "id": "fixture"})
    server.operation_model = "fixture"
    server.status = "failed"
    failed = call(host, {"action": "status", "id": "fixture", "operation_id": OP_ID})
    assert failed["status"] == "failed" and "error_message" not in failed


def test_config_and_destination_identity(tmp_path: Path) -> None:
    config = load_config(tmp_path, environ={})
    assert not config.mental_models.enabled and not config.mental_models.create_enabled
    for pilot in (
        {"enabled": True, "max_models": 21},
        {"create_enabled": True},
        {"enabled": True, "auto_refresh": True},
    ):
        with pytest.raises(ValueError):
            load_config(tmp_path, environ={}, injected={"mental_models": pilot})
    other = load_config(tmp_path, environ={}, injected={"bank_id": "other-bank"})
    assert stable_id(config, " Question? ") == stable_id(config, "question?")
    assert stable_id(config, "question?") != stable_id(other, "question?")
