"""Real provider/runtime admission and evidence against a fake HTTP adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import better_hermes_hindsight.provider as provider_module
from better_hermes_hindsight.client import HindsightClientAdapter
from better_hermes_hindsight.config import load_config
from better_hermes_hindsight.multimodal import decode_envelope
from better_hermes_hindsight.outbox import SQLiteOutbox
from better_hermes_hindsight.provider import BetterHindsightMemoryProvider
from better_hermes_hindsight.runtime import acquire_process_runtime, reset_process_runtime_for_tests
from tests.contract.test_multimodal_http import Transport
from tests.unit.test_multimodal import descriptor
from tests.unit.test_provider_retention import _inert_sender_factory


def test_provider_local_snapshot_and_attachment_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reset_process_runtime_for_tests()
    for key in os.environ:
        if key.startswith("HINDSIGHT_"):
            monkeypatch.delenv(key)
    directory = tmp_path / "better_hindsight"
    directory.mkdir()
    directory.joinpath("config.json").write_text(
        json.dumps(
            {
                "single_principal": True,
                "bank_id": "test",
                "recall": {"include_attachments": True},
                "reflect": {"enabled": True, "include_attachments": True},
                "retain": {"enabled": True},
                "multimodal": {"enabled": True, "allowed_roots": [str(tmp_path)]},
            }
        )
    )
    transport = Transport()

    def acquire(config: Any) -> Any:
        return acquire_process_runtime(
            config,
            client_factory=lambda cfg: HindsightClientAdapter(config=cfg, transport=transport),
            sender_factory=_inert_sender_factory,
        )

    monkeypatch.setattr(provider_module, "acquire_process_runtime", acquire)
    provider = BetterHindsightMemoryProvider()
    provider.initialize(
        "synthetic", hermes_home=str(tmp_path), platform="cli", agent_context="primary"
    )
    image = tmp_path / "synthetic.png"
    image.write_bytes(b"original bytes")
    args = {
        "content": "A synthetic caption",
        "attachments": [{"path": str(image), "kind": "image", "media_type": "image/png"}],
    }
    try:
        assert json.loads(provider.handle_tool_call("better_hindsight_retain", args)) == {
            "result": "queued_locally"
        }
        assert not transport.calls, "local admission must not perform a version or HTTP request"
        image.unlink()
        config = load_config(tmp_path, environ={})
        box = SQLiteOutbox.open(config)
        try:
            rows = box.read_unconfirmed()
            assert len(rows) == 1
            envelope = decode_envelope(rows[0].content, config)
            assert envelope["blocks"][0]["text"] == "A synthetic caption"
            assert str(image) not in rows[0].content
        finally:
            box.close()
        rejected = json.loads(provider.handle_tool_call("better_hindsight_retain", args))
        assert rejected["reason"] == "local_failure"
        assert str(image) not in json.dumps(rejected) + caplog.text
        transport.reply = {
            "results": [
                {
                    "id": "observation",
                    "type": "observation",
                    "text": "Synthetic caption",
                    "attachments": [descriptor()],
                }
            ],
            "chunks": {"chunk": {"attachments": [descriptor("unrelated-chunk")]}},
        }
        automatic = provider.prefetch("Synthetic caption")
        assert '"attachments"' in automatic
        assert "unrelated-chunk" not in automatic
        recalled = json.loads(
            provider.handle_tool_call("better_hindsight_recall", {"query": "caption"})
        )
        assert recalled["memories"][0]["attachments"] == [descriptor()]
        assert recalled["memories"][0]["type"] == "observation"
        transport.reply = {
            "text": "Synthetic synthesis",
            "based_on": {
                "memories": [{"attachments": [descriptor(), {"url": "https://evil.invalid"}]}]
            },
        }
        reflection = provider.handle_tool_call("better_hindsight_reflect", {"query": "caption"})
        reflected = json.loads(reflection)
        assert reflected["result"] == "ok"
        assert '"attachments"' in reflected["context"]
        assert "evil.invalid" not in reflection
        assert "RECALLED_MEMORY_EVIDENCE_BEGIN" in reflected["context"]
        assert len(reflection.encode()) <= config.reflect.output_max_bytes
    finally:
        provider.shutdown()
        reset_process_runtime_for_tests()


@pytest.mark.parametrize("case", ["disabled", "retain-off", "unauthorized"])
def test_provider_multimodal_gates_prevent_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    reset_process_runtime_for_tests()
    for key in os.environ:
        if key.startswith("HINDSIGHT_"):
            monkeypatch.delenv(key)
    directory = tmp_path / "better_hindsight"
    directory.mkdir()
    directory.joinpath("config.json").write_text(
        json.dumps(
            {
                "single_principal": case != "unauthorized",
                "recall": {"enabled": False},
                "retain": {"enabled": case != "retain-off"},
                "multimodal": {"enabled": case != "disabled", "allowed_roots": [str(tmp_path)]},
            }
        )
    )
    transport = Transport()
    monkeypatch.setattr(
        provider_module,
        "acquire_process_runtime",
        lambda cfg: acquire_process_runtime(
            cfg,
            client_factory=lambda config: HindsightClientAdapter(
                config=config, transport=transport
            ),
            sender_factory=_inert_sender_factory,
        ),
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("disabled or unauthorized attachment opened")

    monkeypatch.setattr("better_hermes_hindsight.multimodal._read_local", forbidden)
    provider = BetterHindsightMemoryProvider()
    provider.initialize(
        "synthetic", hermes_home=str(tmp_path), platform="cli", agent_context="primary"
    )
    try:
        result = json.loads(
            provider.handle_tool_call(
                "better_hindsight_retain",
                {
                    "content": "synthetic",
                    "attachments": [
                        {
                            "path": str(tmp_path / "secret.png"),
                            "kind": "image",
                            "media_type": "image/png",
                        }
                    ],
                },
            )
        )
        assert "error" in result
        assert not transport.calls
        if (directory / "outbox.sqlite3").exists():
            box = SQLiteOutbox.open(load_config(tmp_path, environ={}))
            try:
                assert not box.read_unconfirmed()
            finally:
                box.close()
    finally:
        provider.shutdown()
        reset_process_runtime_for_tests()
