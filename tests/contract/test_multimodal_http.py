"""Real loopback wire/restart proof: commit, lose response, replay original bytes."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from better_hermes_hindsight.client import (
    HindsightClientAdapter,
    HindsightClientError,
    JsonResponse,
    RetainSegment,
)
from better_hermes_hindsight.config import load_config
from better_hermes_hindsight.multimodal import build_multimodal_segment
from better_hermes_hindsight.outbox import SQLiteOutbox
from tests.unit.test_multimodal import descriptor


class Transport:
    def __init__(self, version: str = "0.10.2") -> None:
        self.version = version
        self.calls: list[tuple[str, str, Mapping[str, object] | None]] = []
        self.reply: dict[str, Any] = {"results": []}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
    ) -> JsonResponse:
        self.calls.append((method, path, json_body))
        payload = {"api_version": self.version} if path == "/version" else self.reply
        return JsonResponse(payload=payload, response_bytes=512, status=200)

    async def close(self) -> None:
        pass


def test_attachment_recall_and_reflection_exact_wire(tmp_path: Path) -> None:
    config = load_config(
        tmp_path,
        injected={
            "bank_id": "test",
            "recall": {"include_attachments": True},
            "reflect": {"enabled": True, "include_attachments": True, "max_tokens": 512},
        },
        environ={},
    )
    transport = Transport()
    client = HindsightClientAdapter(config=config, transport=transport)
    transport.reply = {
        "results": [{"id": "fact", "text": "Caption", "attachments": [descriptor()]}]
    }
    response = asyncio.run(client.recall("Caption"))
    assert response.results[0].attachments == (descriptor(),)
    assert transport.calls[0][1] == "/version"
    assert transport.calls[1][1] == "/v1/default/banks/test/memories/recall"
    assert (transport.calls[1][2] or {})["include"] == {
        "entities": None,
        "chunks": None,
        "source_facts": None,
    }
    transport.reply = {
        "text": "Generated answer",
        "based_on": {
            "memories": [{"id": "fact", "attachments": [descriptor()]}, {"attachments": "invalid"}]
        },
    }
    response2 = asyncio.run(client.reflect("Question"))
    assert response2.attachments == (descriptor(),)
    assert transport.calls[-1][2] == {
        "query": "Question",
        "budget": "low",
        "max_tokens": 512,
        "tags": None,
        "tags_match": "any",
        "include": {"facts": {}, "tool_calls": None},
    }
    assert [call[1] for call in transport.calls].count("/version") == 1


@pytest.mark.parametrize("prefix_count", [12, 4095, 4096])
def test_reflection_collects_valid_handles_after_attachmentless_sources(
    tmp_path: Path, prefix_count: int
) -> None:
    config = load_config(
        tmp_path,
        injected={
            "bank_id": "test",
            "reflect": {"enabled": True, "include_attachments": True},
        },
        environ={},
    )
    transport = Transport()
    client = HindsightClientAdapter(config=config, transport=transport)
    handles = [descriptor(f"att_{index}") for index in range(10)]
    memories: list[object] = [
        {"id": f"text_{index}", "attachments": [None, {"hash": "invalid"}]}
        for index in range(prefix_count)
    ]
    memories.append({"attachments": [None] * 12 + handles})
    transport.reply = {"text": "Generated answer", "based_on": {"memories": memories}}
    assert len(json.dumps(transport.reply).encode()) < 1024 * 1024
    response = asyncio.run(client.reflect("Question"))
    assert response.text == "Generated answer"
    assert response.attachments == (tuple(handles[:8]) if prefix_count < 4096 else ())


@pytest.mark.parametrize("version", ["0.10.0", "0.9.0", "0.10.3", "unknown"])
def test_unsupported_multimodal_never_sends_caption(tmp_path: Path, version: str) -> None:
    config = load_config(
        tmp_path,
        injected={
            "retain": {"enabled": True},
            "multimodal": {"enabled": True, "allowed_roots": [str(tmp_path)]},
        },
        environ={},
    )
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    segment = build_multimodal_segment(
        config,
        content="caption",
        context=None,
        attachments=[{"path": str(source), "kind": "image", "media_type": "image/png"}],
    )
    transport = Transport(version)
    client = HindsightClientAdapter(config=config, transport=transport)
    with pytest.raises(HindsightClientError):
        asyncio.run(
            client.retain_segment(
                RetainSegment(
                    content=segment.content,
                    document_id=segment.document_id,
                    payload_schema=segment.payload_schema,
                    source_sha256=segment.source_sha256,
                    segment_index=0,
                    segment_count=1,
                )
            )
        )
    assert all(call[0] != "POST" for call in transport.calls)


class Server(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[dict[str, Any]] = []
        self.committed = threading.Event()
        self.release = threading.Event()
        self.confirmed = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        assert self.path == "/version"
        self._reply({"api_version": "0.10.2"})

    def do_POST(self) -> None:
        server: Any = self.server
        assert self.path == "/v1/default/banks/restart/memories"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.requests.append(body)
        if len(server.requests) == 1:
            # Store complete item but deliberately withhold the response until the process dies.
            server.committed.set()
            assert server.release.wait(15)
            self.close_connection = True
            return
        self._reply({"success": True, "bank_id": "restart", "items_count": 1, "async": False})
        server.confirmed.set()

    def _reply(self, value: object) -> None:
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextlib.contextmanager
def loopback() -> Iterator[Server]:
    server = Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


CHILD = """
import json,sys,time
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
p=BetterHindsightMemoryProvider()
p.initialize('synthetic',hermes_home=sys.argv[1],platform='cli',agent_context='primary')
if sys.argv[2]=='admit':
    result=p._handle_retain_tool({'content':'Synthetic caption','context':'fixture','attachments':[
        {'path':sys.argv[3],'kind':'image','media_type':'image/png'},
        {'path':sys.argv[4],'kind':'file','media_type':'application/pdf'}]})
    print(result,flush=True)
while True: time.sleep(.05)
"""


def test_real_model_tool_process_restart_replays_immutable_original_bytes(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    image, pdf = tmp_path / "image.png", tmp_path / "manual.pdf"
    image.write_bytes(b"synthetic-png")
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    with loopback() as server:
        config_dir = tmp_path / "better_hindsight"
        config_dir.mkdir()
        config_dir.joinpath("config.json").write_text(
            json.dumps(
                {
                    "api_url": f"http://127.0.0.1:{server.server_port}",
                    "bank_id": "restart",
                    "single_principal": True,
                    "recall": {"enabled": False},
                    "retain": {"enabled": True},
                    "multimodal": {"enabled": True, "allowed_roots": [str(tmp_path)]},
                    "outbox": {"retry_initial_seconds": 0.05, "retry_max_seconds": 0.05},
                }
            )
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("HINDSIGHT_")}
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        env["PYTHONPATH"] = str(root)
        env["HERMES_HOME"] = str(tmp_path)
        first = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(tmp_path), "admit", str(image), str(pdf)],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert server.committed.wait(10), first.poll()
        finally:
            first.kill()
            stdout, stderr = first.communicate(timeout=5)
        assert json.loads(stdout.splitlines()[-1])["result"] == "queued_locally", stderr.decode()
        image.write_bytes(b"replacement")
        pdf.unlink()
        server.release.set()
        second = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(tmp_path), "replay"],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert server.confirmed.wait(10), second.poll()
            config = load_config(tmp_path, environ={})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                box = SQLiteOutbox.open(config)
                rows = box.read_unconfirmed()
                box.close()
                if not rows:
                    break
                time.sleep(0.01)
            assert not rows
        finally:
            second.kill()
            second.communicate(timeout=5)
        assert len(server.requests) == 2
        assert server.requests[0] == server.requests[1]
        body = server.requests[1]
        assert body["async"] is False
        item = body["items"][0]
        assert item["update_mode"] == "replace"
        assert [block["type"] for block in item["content"]] == ["text", "image", "file"]
        assert base64.b64decode(item["content"][1]["source"]["data"]) == b"synthetic-png"
        assert base64.b64decode(item["content"][2]["source"]["data"]) == b"%PDF-1.4 synthetic"
        assert item["content"][2]["filename"] == "manual.pdf"
        assert item["context"] == "fixture"
        assert item["timestamp"]


@pytest.mark.parametrize("version", ["0.8.5", "0.9.1", "0.9.2", "0.10.0", "0.10.3"])
def test_attachment_reads_gate_exact_version_without_changing_default_text(
    tmp_path: Path, version: str
) -> None:
    config = load_config(tmp_path, injected={"bank_id": "test"}, environ={})
    transport = Transport(version)
    client = HindsightClientAdapter(config=config, transport=transport)
    transport.reply = {
        "results": [{"id": "fact", "text": "caption", "attachments": [descriptor()]}]
    }
    assert not asyncio.run(client.recall("caption")).results[0].attachments
    assert [call[1] for call in transport.calls] == ["/v1/default/banks/test/memories/recall"]
    for operation in ("recall", "reflect"):
        enabled = load_config(
            tmp_path,
            injected={
                "recall": {"include_attachments": True},
                "reflect": {"enabled": True, "include_attachments": True},
            },
            environ={},
        )
        transport = Transport(version)
        client = HindsightClientAdapter(config=enabled, transport=transport)
        with pytest.raises(HindsightClientError):
            asyncio.run(getattr(client, operation)("synthetic"))
        assert [call[1] for call in transport.calls] == ["/version"]


@pytest.mark.parametrize("include_attachments", [False, True])
@pytest.mark.parametrize("direct_attachment", [False, True])
def test_linked_source_fact_attachment_provenance(
    tmp_path: Path, include_attachments: bool, direct_attachment: bool
) -> None:
    config = load_config(
        tmp_path,
        injected={
            "bank_id": "test",
            "recall": {
                "include_attachments": include_attachments,
                "include_source_facts": True,
            },
        },
        environ={},
    )
    transport = Transport()
    transport.reply = {
        "results": [
            {
                "id": "observation",
                "type": "observation",
                "text": "Usable observation",
                "source_fact_ids": ["source", "missing", "source", "second_source"],
                "attachments": [descriptor("att_direct")] if direct_attachment else None,
            },
            {"id": "unlinked", "type": "observation", "text": "Unlinked observation"},
            {
                "id": "world",
                "type": "world",
                "text": "Not an observation",
                "source_fact_ids": ["source"],
            },
        ],
        "source_facts": {
            "source": {"id": "source", "text": "Source caption", "attachments": [descriptor()]},
            "second_source": {
                "id": "second_source",
                "text": "Second source caption",
                "attachments": [descriptor(f"att_{index}") for index in range(8)],
            },
            "unrelated": {
                "id": "unrelated",
                "text": "Unrelated caption",
                "attachments": [descriptor("att_unrelated")],
            },
        },
        "source_facts_truncated": True,
        "chunks": {"chunk": {"attachments": [descriptor("att_chunk")]}},
    }
    response = asyncio.run(
        HindsightClientAdapter(config=config, transport=transport).recall("caption")
    )
    assert response.results[0].text == "Usable observation"
    assert response.results[0].source_fact_ids == ["source", "missing", "source", "second_source"]
    expected = ([descriptor("att_direct")] if direct_attachment else []) + [descriptor()]
    expected += [descriptor(f"att_{index}") for index in range(8)]
    assert response.results[0].attachments == (tuple(expected[:8]) if include_attachments else ())
    assert not response.results[1].attachments
    assert not response.results[2].attachments
    assert response.source_facts is not None
    assert response.source_facts["unrelated"].attachments == (
        (descriptor("att_unrelated"),) if include_attachments else ()
    )


def test_malformed_optional_metadata_preserves_text_and_no_chunk_attribution(
    tmp_path: Path,
) -> None:
    config = load_config(
        tmp_path,
        injected={
            "bank_id": "test",
            "recall": {"include_attachments": True, "include_source_facts": True},
        },
        environ={},
    )
    transport = Transport()
    transport.reply = {
        "results": [
            {
                "id": "observation",
                "type": "observation",
                "text": "usable",
                "attachments": [{"bad": "metadata"}, descriptor()],
            }
        ],
        "source_facts": {"source": {"id": "source", "text": "source text", "attachments": "bad"}},
        "chunks": {"chunk": {"attachments": [descriptor("not-a-fact")]}},
    }
    response = asyncio.run(
        HindsightClientAdapter(config=config, transport=transport).recall("usable")
    )
    assert response.results[0].text == "usable"
    assert response.results[0].attachments == (descriptor(),)
    assert response.source_facts is not None
    assert response.source_facts["source"].text == "source text"
    assert not response.source_facts["source"].attachments
