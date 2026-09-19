"""Jev wire contract and total-budget tests; no external credentials or traffic."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import aiohttp
import pytest

import better_hermes_hindsight.jev as jev
from better_hermes_hindsight.config import ConfigError, load_config


@contextmanager
def server(
    body: bytes, *, status: int = 200, delay: float = 0, trickle: bool = False
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            try:
                time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Location", "/redirect-must-not-follow")
                self.end_headers()
                if trickle:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.04)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/api/alpha/decisions", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("action", ["skip", "reuse", "recall"])
def test_decision_wire_contract(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    body = json.dumps({"answers": {"action": {"choice": action, "confidence": 0}}}).encode()
    with server(body) as (url, requests):
        monkeypatch.setattr(jev, "JEV_ENDPOINT", url)
        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
        capsule = {"current_user_message": "synthetic query", "recent_conversation": []}
        assert jev.decide_memory(json.dumps(capsule), timeout=1) == action
    assert len(requests) == 1
    assert requests[0]["authorization"] == "Bearer synthetic-key"
    assert requests[0]["path"] == "/api/alpha/decisions"
    assert requests[0]["body"]["model"] == "~typesafe/jev-latest"
    assert requests[0]["body"]["state"] == capsule
    assert set(requests[0]["body"]) == {"model", "questions", "state"}
    assert set(requests[0]["body"]["questions"]["action"]["criteria"]) == {
        "skip",
        "reuse",
        "recall",
    }


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {},
        {"answers": []},
        {"answers": {"action": {"choice": "answer"}}},
        {"answers": {"action": {"choice": ["skip"]}}},
        {"answers": {"action": {"confidence": 1}}},
    ],
)
def test_malformed_choice_is_never_skip(document: object) -> None:
    with pytest.raises(jev.JevDecisionError, match="^invalid$"):
        jev.parse_action(document)


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (401, b"private upstream body", "http_error"),
        (429, b"retry later", "http_error"),
        (503, b"retry later", "http_error"),
        (302, b"", "http_error"),
        (200, b"not json", "invalid"),
        (200, b"x" * 16385, "oversized"),
    ],
)
def test_transport_failure_has_no_retry_or_redirect(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, outcome: str
) -> None:
    with server(body, status=status) as (url, requests):
        monkeypatch.setattr(jev, "JEV_ENDPOINT", url)
        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
        with pytest.raises(jev.JevDecisionError, match=f"^{outcome}$"):
            jev.decide_memory("{}", timeout=1)
    assert len(requests) == 1


@pytest.mark.parametrize("trickle", [False, True])
def test_total_timeout_bounds_headers_and_trickling_body(
    monkeypatch: pytest.MonkeyPatch, trickle: bool
) -> None:
    with server(b"a" * 100, delay=0 if trickle else 0.4, trickle=trickle) as (url, requests):
        monkeypatch.setattr(jev, "JEV_ENDPOINT", url)
        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            jev.decide_memory("{}", timeout=0.12)
        assert time.monotonic() - start < 0.4
        assert len(requests) == 1


def test_dns_cancellation_does_not_wait_for_executor_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled: list[bool] = []

    async def slow_resolve(self: object, *args: object, **kwargs: object) -> None:
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(aiohttp.AsyncResolver, "resolve", slow_resolve)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        jev.decide_memory("{}", timeout=0.05)
    assert time.monotonic() - start < 0.4
    assert cancelled == [True]


def test_missing_key_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(jev.JevDecisionError, match="^missing_key$"):
        jev.decide_memory("{}", timeout=1)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_is_rejected_before_transport(timeout: float) -> None:
    with pytest.raises(TimeoutError):
        jev.decide_memory("{}", timeout=timeout)


@pytest.mark.parametrize(
    "planner",
    [
        {"route": "unknown"},
        {"rewrite": "yes"},
        {"rewrite": "active"},
        {"rewrite": "false"},
        {"rewrite": 0},
        {"rewrite": 1},
        {"rewrite": None},
        {"rewrite": []},
    ],
)
def test_invalid_planner_config(tmp_path: Path, planner: dict[str, object]) -> None:
    path = tmp_path / "better_hindsight/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"planner": planner}))
    with pytest.raises(ConfigError):
        load_config(tmp_path)


@pytest.mark.parametrize("mode", ["off", "shadow", "active"])
@pytest.mark.parametrize("rewrite", [False, True, "shadow"])
def test_rewrite_config_combinations(
    tmp_path: Path,
    mode: str,
    rewrite: bool | str,
) -> None:
    path = tmp_path / "better_hindsight/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"planner": {"mode": mode, "rewrite": rewrite}}))
    assert load_config(tmp_path).planner.rewrite == rewrite


def test_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.planner.mode == "off"
    assert config.planner.rewrite is False


@pytest.mark.parametrize("route", ["llm", "jev", "unknown", None])
@pytest.mark.parametrize("mode", ["off", "shadow", "active"])
def test_obsolete_route_is_rejected(tmp_path: Path, route: object, mode: str) -> None:
    with pytest.raises(ConfigError, match=r"unknown.*planner.route"):
        load_config(tmp_path, environ={}, injected={"planner": {"mode": mode, "route": route}})
