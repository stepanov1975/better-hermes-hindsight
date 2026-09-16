"""Deterministic harness regressions; fake responses are NOT live-server evidence."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientSession

from better_hermes_hindsight.config import load_config
from better_hermes_hindsight.formatting import count_query_tokens
from tests.integration import test_isolated_hindsight as live


def test_live_child_imports_parametrized_tests_without_pytest_dependency() -> None:
    script = live._LIVE_CHILD_SCRIPT.replace(
        "raise SystemExit(_run_live_child())", "print('import-ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=live.ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "import-ok"


@pytest.fixture
def inputs() -> live.DevelopmentInputs:
    return live.DevelopmentInputs(
        api_url="http://127.0.0.1:9",
        api_key="synthetic-live-key",
        expected_version="0.10.0",
        hermes_python=Path("/synthetic/python"),
        allowed_endpoints=(),
        bank_id="better-hindsight-live-synthetic",
        ownership_name="Better Hindsight live test synthetic",
    )


def _page(inputs: live.DevelopmentInputs, banks: list[dict[str, Any]]) -> dict[str, Any]:
    if inputs.expected_version in {"0.8.5", "0.9.1"}:
        return {"banks": banks}
    return {"banks": banks, "total": len(banks), "limit": 100, "offset": 0}


@pytest.mark.parametrize("version", ["0.8.5", "0.9.1", "0.9.2", "0.10.0"])
def test_bank_creation_and_cleanup_use_exact_listing_ownership(
    monkeypatch: pytest.MonkeyPatch, inputs: live.DevelopmentInputs, version: str
) -> None:
    inputs = replace(inputs, expected_version=version)
    banks: list[dict[str, Any]] = []
    calls: list[str] = []

    async def request(
        session: ClientSession, method: str, url: str, *, json_body: object = None
    ) -> tuple[int, dict[str, Any]]:
        calls.append(method)
        path = urlsplit(url).path
        assert not path.endswith(("/profile", "/config"))
        if path == "/version":
            return 200, {"api_version": version}
        if path == "/v1/default/banks":
            query = parse_qs(urlsplit(url).query)
            if version in {"0.9.2", "0.10.0"}:
                assert query == {"q": [inputs.bank_id], "limit": ["100"], "offset": ["0"]}
            else:
                assert not query
            return 200, _page(inputs, list(banks))
        assert path == f"/v1/default/banks/{inputs.bank_id}"
        if method == "PUT":
            assert json_body == {"name": inputs.ownership_name}
            banks.append({"bank_id": inputs.bank_id, "name": inputs.ownership_name})
            return 200, banks[0]
        assert method == "DELETE"
        banks.clear()
        return 200, {"success": True}

    monkeypatch.setattr(live, "_raw_json", request)
    live._create_disposable_bank(inputs)
    assert len(banks) == 1
    live._delete_disposable_bank(inputs)
    assert banks == []
    assert calls == ["GET", "GET", "PUT", "GET", "GET", "DELETE", "GET"]


@pytest.mark.parametrize("version", ["0.8.5", "0.9.1", "0.9.2", "0.10.0"])
def test_existing_foreign_bank_is_neither_overwritten_nor_deleted(
    monkeypatch: pytest.MonkeyPatch, inputs: live.DevelopmentInputs, version: str
) -> None:
    inputs = replace(inputs, expected_version=version)
    calls: list[str] = []

    async def request(
        session: ClientSession, method: str, url: str, *, json_body: object = None
    ) -> tuple[int, dict[str, Any]]:
        calls.append(method)
        assert method == "GET"
        if url.endswith("/version"):
            return 200, {"api_version": version}
        return 200, _page(inputs, [{"bank_id": inputs.bank_id, "name": "someone else"}])

    monkeypatch.setattr(live, "_raw_json", request)
    with pytest.raises(AssertionError, match="already exists"):
        live._create_disposable_bank(inputs)
    with pytest.raises(AssertionError, match="cleanup failed") as error:
        live._delete_disposable_bank(inputs)
    assert "ownership marker" in str(error.value.__cause__)
    assert set(calls) == {"GET"}


@pytest.mark.parametrize("readback_name", [None, "someone else"])
def test_create_requires_ownership_readback(
    monkeypatch: pytest.MonkeyPatch, inputs: live.DevelopmentInputs, readback_name: str | None
) -> None:
    replies = iter(
        [
            {"api_version": inputs.expected_version},
            _page(inputs, []),
            {"bank_id": inputs.bank_id, "name": inputs.ownership_name},
            _page(inputs, [{"bank_id": inputs.bank_id, "name": readback_name}]),
        ]
    )

    async def request(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return 200, next(replies)

    monkeypatch.setattr(live, "_raw_json", request)
    with pytest.raises(AssertionError, match="ownership marker"):
        live._create_disposable_bank(inputs)


@pytest.mark.parametrize("version", ["0.9.2", "0.10.0"])
@pytest.mark.parametrize("present", [False, True])
def test_paginated_bank_lookup_exhausts_substring_matches(
    monkeypatch: pytest.MonkeyPatch, inputs: live.DevelopmentInputs, version: str, present: bool
) -> None:
    inputs = replace(inputs, expected_version=version)
    offsets: list[int] = []
    banks = [{"bank_id": f"{inputs.bank_id}-{index}"} for index in range(101)]
    if present:
        banks[-1] = {"bank_id": inputs.bank_id, "name": inputs.ownership_name}

    async def request(session: ClientSession, method: str, url: str) -> tuple[int, dict[str, Any]]:
        offset = int(parse_qs(urlsplit(url).query)["offset"][0])
        offsets.append(offset)
        return 200, {
            "banks": banks[offset : offset + 100],
            "total": 101,
            "limit": 100,
            "offset": offset,
        }

    monkeypatch.setattr(live, "_raw_json", request)
    bank = asyncio.run(live._listed_bank(cast(ClientSession, None), inputs))
    assert bank == (banks[-1] if present else None)
    assert offsets == [0, 100]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"banks": []},
        {"banks": [], "total": 1, "limit": 100, "offset": 0},
        {"banks": [], "total": False, "limit": 100, "offset": 0},
        {"banks": [], "total": 0, "limit": 100, "offset": 1},
        {"banks": [{"name": "missing identity"}], "total": 1, "limit": 100, "offset": 0},
        {"banks": [{"bank_id": "x"}] * 2, "total": 2, "limit": 100, "offset": 0},
        {"banks": [{"bank_id": "x"}], "total": 0, "limit": 100, "offset": 0},
    ],
)
def test_incomplete_or_malformed_listing_never_proves_absence(
    monkeypatch: pytest.MonkeyPatch, inputs: live.DevelopmentInputs, payload: dict[str, Any]
) -> None:
    async def request(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return 200, payload

    monkeypatch.setattr(live, "_raw_json", request)
    with pytest.raises(AssertionError):
        asyncio.run(live._listed_bank(cast(ClientSession, None), inputs))


def test_bank_list_404_does_not_authorize_create_or_cleanup(
    monkeypatch: pytest.MonkeyPatch, inputs: live.DevelopmentInputs
) -> None:
    async def request(
        session: ClientSession, method: str, url: str, *, json_body: object = None
    ) -> tuple[int, dict[str, Any] | None]:
        assert method == "GET"
        if url.endswith("/version"):
            return 200, {"api_version": inputs.expected_version}
        return 404, None

    monkeypatch.setattr(live, "_raw_json", request)
    with pytest.raises(AssertionError, match="complete bank list"):
        live._create_disposable_bank(inputs)
    with pytest.raises(AssertionError, match="cleanup failed"):
        live._delete_disposable_bank(inputs)


@pytest.mark.parametrize(
    ("context", "memory", "fact_type", "passes"),
    [
        ("cobalt lantern", "cobalt lantern", "world", True),
        ("unrelated", "cobalt lantern", "world", False),
        ("cobalt lantern", "unrelated", "world", False),
        ("cobalt lantern", "cobalt lantern", "", False),
        ("cobalt lantern", "cobalt lantern", None, False),
    ],
)
def test_useful_recall_requires_both_real_provider_surfaces(
    monkeypatch: pytest.MonkeyPatch,
    context: str,
    memory: str,
    fact_type: str | None,
    passes: bool,
) -> None:
    class Provider:
        def prefetch(self, query: str) -> str:
            return context

        def handle_tool_call(self, name: str, arguments: dict[str, str]) -> str:
            assert name == "better_hindsight_recall"
            return json.dumps({"result": "ok", "memories": [{"memory": memory, "type": fact_type}]})

    monkeypatch.setattr(live, "_DRAIN_TIMEOUT_SECONDS", 0)
    if passes:
        live._wait_for_useful_recall(Provider())
    else:
        with pytest.raises(AssertionError, match="not useful"):
            live._wait_for_useful_recall(Provider())


@pytest.fixture
def fake_recall_server() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"queries": [], "reject_status": 400, "reject_count": 502}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query = body["query"]
            state["queries"].append(query)
            assert self.path.endswith("/memories/recall")
            assert self.headers["Authorization"] == "Bearer synthetic-live-key"
            status = 200
            payload: dict[str, Any] = {"results": []}
            assert "word" in query
            if query == "word " + "😀" * 251:
                status = state["reject_status"]
                payload = {
                    "detail": f"Query too long: {state['reject_count']} tokens exceeds maximum of "
                    "500. Please shorten your query."
                }
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["compatible", "wrong_tokenizer", "wrong_count"])
def test_tokenizer_gate_checks_wire_query_and_exact_server_rejection(
    inputs: live.DevelopmentInputs,
    tmp_path: Path,
    fake_recall_server: tuple[str, dict[str, Any]],
    mode: str,
) -> None:
    url, state = fake_recall_server
    inputs = replace(inputs, api_url=url)
    directory = tmp_path / "better_hindsight"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps(live._profile_document(inputs)))
    config = load_config(tmp_path, environ={"HINDSIGHT_API_KEY": inputs.api_key})
    if mode == "wrong_tokenizer":
        state["reject_status"] = 200
    elif mode == "wrong_count":
        state["reject_count"] = 501
    if mode == "compatible":
        live._assert_live_tokenizer_boundary(inputs, config)
    else:
        with pytest.raises(AssertionError):
            live._assert_live_tokenizer_boundary(inputs, config)
    assert len(state["queries"]) == 2
    assert [count_query_tokens(query) for query in state["queries"]] == [500, 502]
