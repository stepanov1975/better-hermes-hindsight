"""Contracts for the SDK-free narrow supported-Hindsight adapter boundary."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

import better_hermes_hindsight.client as client_module
from better_hermes_hindsight import __version__
from better_hermes_hindsight.client import (
    HINDSIGHT_REQUEST_TIMEOUT_SECONDS,
    HindsightClientAdapter,
    HindsightClientError,
    JsonResponse,
    JsonTransportProtocol,
    MissionSnapshot,
    MissionUpdateError,
    MissionValue,
    RecallResponse,
    RecallResult,
    RecallScores,
    RetainConfirmation,
    RetainSegment,
    create_hindsight_client,
    is_available,
    recall_request_parameters,
)
from better_hermes_hindsight.config import BetterHindsightConfig, load_config


class _UnprintableFailure(RuntimeError):
    def __str__(self) -> str:  # pragma: no cover - passing code never calls this
        raise AssertionError("raw transport failures must not be stringified")


class _DictSubclass(dict[str, object]):
    pass


class _StringSubclass(str):
    pass


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object] | None]] = []
        self.responses: dict[tuple[str, str], object] = {}
        self.failures: dict[tuple[str, str], BaseException] = {}
        self.close_failure: BaseException | None = None
        self.closed = False

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> JsonResponse:
        self.calls.append((method, path, json_body))
        key = (method, path)
        failure = self.failures.get(key)
        if failure is not None:
            raise failure
        return JsonResponse(
            payload=self.responses.get(key, {}),
            response_bytes=128,
            status=200,
        )

    async def close(self) -> None:
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True


class _RecordingFactory:
    def __init__(self, transport: JsonTransportProtocol) -> None:
        self.transport = transport
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout: float,
        user_agent: str,
    ) -> JsonTransportProtocol:
        self.calls.append(
            {
                "base_url": base_url,
                "api_key": api_key,
                "timeout": timeout,
                "user_agent": user_agent,
            }
        )
        return self.transport


def _config(
    tmp_path: Path,
    *,
    injected: Mapping[str, object] | None = None,
    api_key: str | None = None,
) -> BetterHindsightConfig:
    environ = {} if api_key is None else {"HINDSIGHT_API_KEY": api_key}
    return load_config(
        hermes_home=tmp_path,
        environ=environ,
        injected={} if injected is None else injected,
    )


def _segment() -> RetainSegment:
    return RetainSegment(
        content="immutable segment",
        document_id="stable-document-id",
        payload_schema="better-hindsight-turn-v1",
        source_sha256="a" * 64,
        segment_index=0,
        segment_count=1,
    )


def _recall_result_payload() -> dict[str, object]:
    return {
        "id": "result-1",
        "text": "fixture observation",
        "type": "observation",
        "entities": ["entity-a"],
        "context": "fixture context",
        "occurred_start": "2026-01-01",
        "occurred_end": None,
        "mentioned_at": "2026-01-02",
        "document_id": "document-1",
        "metadata": {"scope": "fixture"},
        "chunk_id": None,
        "tags": ["fixture"],
        "source_fact_ids": ["fact-1"],
        "scores": {"final": 0.9, "reranker": 1, "semantic": None, "keyword": 0.2},
    }


def test_package_import_is_network_install_and_sdk_side_effect_free() -> None:
    script = """
import socket
import subprocess
import sys

def forbidden(*args, **kwargs):
    raise AssertionError("import attempted an external side effect")

socket.socket.connect = forbidden
subprocess.Popen = forbidden
import better_hermes_hindsight
from better_hermes_hindsight.client import is_available
assert is_available()
assert "hindsight_client" not in sys.modules
assert "hindsight_client_api" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert is_available() is True


@pytest.mark.parametrize("missing_module", ["aiohttp", "tiktoken"])
def test_availability_requires_each_runtime_dependency(
    monkeypatch: pytest.MonkeyPatch, missing_module: str
) -> None:
    monkeypatch.setattr(
        client_module,
        "find_spec",
        lambda module: None if module == missing_module else object(),
    )

    assert is_available() is False


def test_client_construction_uses_internal_transport_without_remote_calls(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        injected={"api_url": "https://hindsight.invalid/base", "bank_id": "sample-bank"},
        api_key="synthetic-secret",
    )
    transport = _RecordingTransport()
    factory = _RecordingFactory(transport)

    adapter = create_hindsight_client(config, transport_factory=factory)

    assert isinstance(adapter, HindsightClientAdapter)
    assert repr(adapter) == "HindsightClientAdapter()"
    assert factory.calls == [
        {
            "base_url": "https://hindsight.invalid/base",
            "api_key": "synthetic-secret",
            "timeout": HINDSIGHT_REQUEST_TIMEOUT_SECONDS,
            "user_agent": f"better-hermes-hindsight/{__version__}",
        }
    ]
    assert "synthetic-secret" not in repr(adapter)
    assert transport.calls == []


def test_recall_serializes_full_contract_and_decodes_internal_models(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        injected={
            "bank_id": "sample-bank",
            "recall": {
                "types": ["world", "observation"],
                "max_tokens": 321,
                "tags": ["scope-a", "scope-b"],
                "tag_mode": "all_strict",
                "prefer_observations": True,
                "min_scores": {"semantic": 0.2, "final": 0.5},
                "include_source_facts": True,
                "max_source_facts_tokens": 123,
            },
        },
    )
    transport = _RecordingTransport()
    path = "/v1/default/banks/sample-bank/memories/recall"
    transport.responses[("POST", path)] = {
        "results": [_recall_result_payload()],
        "source_facts": {"fact-1": {"id": "fact-1", "text": "source fact"}},
        "source_facts_truncated": True,
        "trace": None,
        "entities": None,
        "chunks": None,
    }
    adapter = HindsightClientAdapter(config=config, transport=transport)

    response = asyncio.run(adapter.recall("current query"))

    assert response == RecallResponse(
        results=[
            RecallResult(
                id="result-1",
                text="fixture observation",
                type="observation",
                entities=["entity-a"],
                context="fixture context",
                occurred_start="2026-01-01",
                mentioned_at="2026-01-02",
                document_id="document-1",
                metadata={"scope": "fixture"},
                tags=["fixture"],
                source_fact_ids=["fact-1"],
                scores=RecallScores(final=0.9, reranker=1, keyword=0.2),
            )
        ],
        source_facts={"fact-1": RecallResult(id="fact-1", text="source fact")},
    )
    assert transport.calls == [
        (
            "POST",
            path,
            {
                "query": "current query",
                "types": ["world", "observation"],
                "prefer_observations": True,
                "budget": "mid",
                "max_tokens": 321,
                "trace": False,
                "query_timestamp": None,
                "include": {
                    "entities": None,
                    "chunks": None,
                    "source_facts": {
                        "max_tokens": 123,
                        "max_tokens_per_observation": -1,
                    },
                },
                "tags": ["scope-a", "scope-b"],
                "tags_match": "all_strict",
                "tag_groups": None,
                "min_scores": {
                    "semantic": 0.2,
                    "keyword": None,
                    "reranker": None,
                    "final": 0.5,
                },
            },
        )
    ]


def test_query_only_recall_preserves_wire_defaults_and_quotes_bank(tmp_path: Path) -> None:
    config = _config(tmp_path, injected={"bank_id": "bank/with spaces"})
    transport = _RecordingTransport()
    path = "/v1/default/banks/bank%2Fwith%20spaces/memories/recall"
    transport.responses[("POST", path)] = {"results": []}
    adapter = HindsightClientAdapter(config=config, transport=transport)

    assert asyncio.run(adapter.recall("query-only recall")) == RecallResponse(results=[])
    assert transport.calls == [
        (
            "POST",
            path,
            {
                "query": "query-only recall",
                "types": None,
                "prefer_observations": False,
                "budget": "mid",
                "max_tokens": 4096,
                "trace": False,
                "query_timestamp": None,
                "include": {"entities": None, "chunks": None, "source_facts": None},
                "tags": None,
                "tags_match": "any",
                "tag_groups": None,
                "min_scores": None,
            },
        )
    ]


@pytest.mark.parametrize(
    "response",
    [
        [],
        _DictSubclass(results=[]),
        {},
        {"results": ()},
        {"results": [{"id": "id", "text": _StringSubclass("text")}]},
        {"results": [{"id": "id", "text": "text", "scores": {"final": True}}]},
        {"results": [], "source_facts": []},
    ],
)
def test_recall_requires_exact_builtin_response_types(tmp_path: Path, response: object) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    path = "/v1/default/banks/sample-bank/memories/recall"
    transport.responses[("POST", path)] = response
    adapter = HindsightClientAdapter(config=config, transport=transport)

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(adapter.recall("query"))
    assert caught.value.category == "recall_failed"
    assert str(caught.value) == "Better Hindsight recall failed."


@pytest.mark.parametrize(
    "configured_scope, expected_scope",
    [(None, None), ("combined", "combined"), ("shared", [[]])],
)
def test_retain_uses_exact_replace_batch_and_typed_confirmation(
    tmp_path: Path,
    configured_scope: str | None,
    expected_scope: object,
) -> None:
    retain: dict[str, object] = {"tags": ["source:sample", "kind:turn"]}
    if configured_scope is not None:
        retain["observation_scopes"] = configured_scope
    config = _config(
        tmp_path,
        injected={"bank_id": "sample-bank", "single_principal": True, "retain": retain},
    )
    transport = _RecordingTransport()
    path = "/v1/default/banks/sample-bank/memories"
    transport.responses[("POST", path)] = {
        "success": True,
        "bank_id": "sample-bank",
        "items_count": 1,
        "async": False,
    }
    adapter = HindsightClientAdapter(config=config, transport=transport)

    confirmation = asyncio.run(adapter.retain_segment(_segment()))

    assert confirmation == RetainConfirmation(confirmed=True)
    assert transport.calls == [
        (
            "POST",
            path,
            {
                "items": [
                    {
                        "content": "immutable segment",
                        "timestamp": None,
                        "context": None,
                        "metadata": {
                            "better_hindsight_payload_schema": "better-hindsight-turn-v1",
                            "better_hindsight_segment_count": "1",
                            "better_hindsight_segment_index": "0",
                            "better_hindsight_source_sha256": "a" * 64,
                        },
                        "document_id": "stable-document-id",
                        "entities": None,
                        "tags": ["kind:turn", "source:sample"],
                        "observation_scopes": expected_scope,
                        "strategy": None,
                        "update_mode": "replace",
                    }
                ],
                "async": False,
                "document_tags": None,
            },
        )
    ]
    request_body = transport.calls[0][2]
    assert request_body is not None
    assert "operation_id" not in request_body
    items = request_body["items"]
    assert isinstance(items, list)
    assert "resolve_entities" not in items[0]


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "bank_id": "sample-bank", "items_count": 1, "async": False},
        {"success": True, "bank_id": "wrong", "items_count": 1, "async": False},
        {"success": True, "bank_id": "sample-bank", "items_count": 0, "async": False},
        {"success": True, "bank_id": "sample-bank", "items_count": 1, "async": True},
    ],
)
def test_well_formed_nonconfirming_retain_response_returns_false(
    tmp_path: Path, response: object
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    transport.responses[("POST", "/v1/default/banks/sample-bank/memories")] = response
    adapter = HindsightClientAdapter(config=config, transport=transport)

    assert asyncio.run(adapter.retain_segment(_segment())) == RetainConfirmation(confirmed=False)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"success": 1, "bank_id": "sample-bank", "items_count": 1, "async": False},
        {"success": True, "bank_id": "sample-bank", "items_count": True, "async": False},
        _DictSubclass(success=True, bank_id="sample-bank", items_count=1, async_=False),
    ],
)
def test_malformed_retain_response_maps_to_fixed_error(tmp_path: Path, response: object) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    transport.responses[("POST", "/v1/default/banks/sample-bank/memories")] = response
    adapter = HindsightClientAdapter(config=config, transport=transport)

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(adapter.retain_segment(_segment()))
    assert caught.value.category == "retain_failed"
    assert str(caught.value) == "Better Hindsight retain failed."


def test_mission_read_preserves_exact_presence_null_blank_and_patch_body(tmp_path: Path) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    config_path = "/v1/default/banks/sample-bank/config"
    transport.responses[("GET", config_path)] = {
        "bank_id": "sample-bank",
        "config": {"retain_mission": None, "observations_mission": ""},
        "overrides": {},
    }
    transport.responses[("PATCH", config_path)] = {
        "bank_id": "sample-bank",
        "config": {"retain_mission": "new"},
        "overrides": {},
    }
    adapter = HindsightClientAdapter(config=config, transport=transport)

    snapshot = asyncio.run(adapter.get_bank_config())
    asyncio.run(adapter.update_bank_missions({"retain_mission": "new"}))

    assert snapshot == MissionSnapshot(
        retain_mission=MissionValue(present=True, value=None),
        observations_mission=MissionValue(present=True, value=""),
    )
    assert transport.calls[-1] == (
        "PATCH",
        config_path,
        {"updates": {"retain_mission": "new"}},
    )


@pytest.mark.parametrize(
    "response",
    [
        [],
        {},
        {"bank_id": 1, "config": {}, "overrides": {}},
        {"bank_id": "sample-bank", "config": [], "overrides": {}},
        {"bank_id": "sample-bank", "config": {}, "overrides": []},
    ],
)
def test_mission_update_requires_bank_config_response_schema(
    tmp_path: Path, response: object
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    transport.responses[("PATCH", "/v1/default/banks/sample-bank/config")] = response
    adapter = HindsightClientAdapter(config=config, transport=transport)

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(adapter.update_bank_missions({"retain_mission": "new"}))
    assert caught.value.category == "mission_update_failed"
    assert str(caught.value) == "Better Hindsight mission update failed."


@pytest.mark.parametrize(
    "response",
    [
        [],
        _DictSubclass(bank_id="sample-bank", config={}),
        {"bank_id": "wrong", "config": {}},
        {"bank_id": "sample-bank", "config": []},
        {"bank_id": "sample-bank", "config": {"retain_mission": 1}},
        {"bank_id": "sample-bank", "config": {"retain_mission": _StringSubclass("x")}},
    ],
)
def test_mission_read_requires_exact_builtin_types(tmp_path: Path, response: object) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    transport.responses[("GET", "/v1/default/banks/sample-bank/config")] = response
    adapter = HindsightClientAdapter(config=config, transport=transport)

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(adapter.get_bank_config())
    assert caught.value.category == "bank_config_failed"
    assert str(caught.value) == "Better Hindsight bank configuration read failed."


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"other": "value"},
        {"retain_mission": ""},
        {"retain_mission": "   "},
        {"retain_mission": cast(str, 3)},
        cast(dict[str, str], _DictSubclass(retain_mission="value")),
    ],
)
def test_invalid_mission_updates_are_rejected_before_transport(
    tmp_path: Path, updates: Mapping[str, str]
) -> None:
    transport = _RecordingTransport()
    adapter = HindsightClientAdapter(
        config=_config(tmp_path, injected={"bank_id": "sample-bank"}),
        transport=transport,
    )
    with pytest.raises(MissionUpdateError):
        asyncio.run(adapter.update_bank_missions(updates))
    assert transport.calls == []


@pytest.mark.parametrize(
    "operation, expected_category, expected_message",
    [
        ("recall", "recall_failed", "Better Hindsight recall failed."),
        ("retain", "retain_failed", "Better Hindsight retain failed."),
        (
            "config",
            "bank_config_failed",
            "Better Hindsight bank configuration read failed.",
        ),
        (
            "patch",
            "mission_update_failed",
            "Better Hindsight mission update failed.",
        ),
        ("close", "client_close_failed", "Better Hindsight client close failed."),
    ],
)
def test_raw_transport_failures_map_to_fixed_sanitized_errors(
    tmp_path: Path,
    operation: str,
    expected_category: str,
    expected_message: str,
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    bank_path = "/v1/default/banks/sample-bank"
    if operation == "recall":
        transport.failures[("POST", f"{bank_path}/memories/recall")] = _UnprintableFailure()
    elif operation == "retain":
        transport.failures[("POST", f"{bank_path}/memories")] = _UnprintableFailure()
    elif operation == "config":
        transport.failures[("GET", f"{bank_path}/config")] = _UnprintableFailure()
    elif operation == "patch":
        transport.failures[("PATCH", f"{bank_path}/config")] = _UnprintableFailure()
    else:
        transport.close_failure = _UnprintableFailure()
    adapter = HindsightClientAdapter(config=config, transport=transport)

    async def invoke() -> None:
        if operation == "recall":
            await adapter.recall("query")
        elif operation == "retain":
            await adapter.retain_segment(_segment())
        elif operation == "config":
            await adapter.get_bank_config()
        elif operation == "patch":
            await adapter.update_bank_missions({"retain_mission": "new"})
        else:
            await adapter.close()

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(invoke())
    assert caught.value.category == expected_category
    assert str(caught.value) == expected_message
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_constructor_failure_is_sanitized_without_exception_text(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def failing_factory(**_kwargs: object) -> JsonTransportProtocol:
        raise _UnprintableFailure

    with pytest.raises(HindsightClientError) as caught:
        create_hindsight_client(config, transport_factory=failing_factory)
    assert caught.value.category == "client_initialization_failed"
    assert str(caught.value) == "Better Hindsight client initialization failed."


def test_diagnostic_replay_forces_trace_and_projects_only_safe_phase_data(tmp_path: Path) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    path = "/v1/default/banks/sample-bank/memories/recall"
    transport.responses[("POST", path)] = {
        "results": [_recall_result_payload()],
        "source_facts": None,
        "trace": {
            "summary": {
                "total_duration_seconds": 3.5,
                "phase_metrics": [
                    {
                        "phase_name": "reranking",
                        "duration_seconds": 2.25,
                        "details": {
                            "candidate_count": 100,
                            "private_query_looks_safe": 7,
                            "private_text": "must-not-survive",
                            "tokens_used": 10**100,
                        },
                    },
                    {
                        "phase_name": "private_query_looks_safe",
                        "duration_seconds": 0.4,
                        "details": {"candidate_count": 99},
                    },
                ],
            },
            "retrieval_results": [
                {"method": "semantic", "results": [{"text": "private"}] * 3},
                {"method": "bm25", "results": [{"text": "private"}] * 2},
            ],
            "rrf_merged": [{"text": "private candidate"}, {"text": "private candidate"}],
            "final_results": [{"text": "private result"}],
        },
    }
    adapter = HindsightClientAdapter(config=config, transport=transport)

    response = asyncio.run(
        adapter.replay_recall("exact replay query", recall_request_parameters(config.recall))
    )

    request = transport.calls[0][2]
    assert request is not None
    assert request["query"] == "exact replay query"
    assert request["trace"] is True
    assert response.trace is not None
    assert response.trace.collection_counts == {
        "retrieval_methods": 2,
        "retrieval_candidates": 5,
        "rrf_merged": 2,
        "final_results": 1,
    }
    assert response.trace.phase_metrics[0].as_dict() == {
        "details": {"candidate_count": 100},
        "duration_seconds": 2.25,
        "phase_name": "reranking",
    }
    assert "private" not in str(response.trace.as_dict())


def test_diagnostic_replay_rejects_request_drift_before_transport(tmp_path: Path) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    adapter = HindsightClientAdapter(config=config, transport=transport)
    request = recall_request_parameters(config.recall)
    request["max_tokens"] = float(cast(int, request["max_tokens"]))

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(adapter.replay_recall("exact replay query", request))

    assert caught.value.reason == "schema_invalid"
    assert transport.calls == []


def test_trace_collection_counts_survive_missing_summary(tmp_path: Path) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    path = "/v1/default/banks/sample-bank/memories/recall"
    transport.responses[("POST", path)] = {
        "results": [_recall_result_payload()],
        "source_facts": None,
        "trace": {"summary": None, "final_results": [{"text": "private"}]},
    }
    adapter = HindsightClientAdapter(config=config, transport=transport)

    response = asyncio.run(
        adapter.replay_recall("exact replay query", recall_request_parameters(config.recall))
    )

    assert response.trace is not None
    assert response.trace.total_duration_seconds is None
    assert response.trace.phase_metrics == ()
    assert response.trace.collection_counts == {"final_results": 1}


def test_asyncio_cancellation_propagates_unchanged(tmp_path: Path) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    transport = _RecordingTransport()
    transport.failures[("POST", "/v1/default/banks/sample-bank/memories/recall")] = (
        asyncio.CancelledError()
    )
    adapter = HindsightClientAdapter(config=config, transport=transport)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.recall("query"))


def test_internal_project_models_are_exact_frozen_slotted_and_secret_safe() -> None:
    scores = RecallScores(final=1)
    result = RecallResult(id="id", text="text", scores=scores)
    response = RecallResponse(results=[result])
    segment = _segment()
    mission = MissionValue(present=True, value="synthetic-secret")

    assert not hasattr(response, "__dict__")
    assert not hasattr(result, "__dict__")
    assert not hasattr(scores, "__dict__")
    assert not hasattr(segment, "__dict__")
    assert "synthetic-secret" not in repr(mission)
    with pytest.raises(FrozenInstanceError):
        segment.__setattr__("content", "replacement")
    with pytest.raises(FrozenInstanceError):
        response.__setattr__("results", [])


def test_client_error_rejects_unbounded_reason_text() -> None:
    private = "private-reason-sentinel"
    error = HindsightClientError("recall_failed", "fixed message", reason=private)

    assert error.category == "recall_failed"
    assert error.reason == "unexpected_error"
    assert private not in repr(error)


def test_close_is_idempotent_with_the_real_aiohttp_transport(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = create_hindsight_client(_config(tmp_path))
        await adapter.close()
        await adapter.close()

    asyncio.run(scenario())
