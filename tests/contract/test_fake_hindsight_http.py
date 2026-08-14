"""Loopback HTTP contracts for the SDK-free Hindsight 0.8.5 adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import socket
import traceback
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from aiohttp import ClientError, ClientSession

import better_hermes_hindsight.client as client_module
from better_hermes_hindsight import __version__
from better_hermes_hindsight.client import (
    HindsightClientAdapter,
    HindsightClientError,
    RecallResponse,
    RetainConfirmation,
    RetainSegment,
    create_hindsight_client,
)
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.redaction import REDACTION_MARKER
from better_hermes_hindsight.runtime import AsyncCallTimeoutError, ProcessRuntime
from tests.fakes.hindsight_server import (
    MAX_REQUEST_BYTES,
    MAX_REQUEST_RECORDS,
    FakeHindsightServer,
    MissionPatchFault,
    MissionReadbackFault,
    MissionReadFault,
    RecallFault,
    RequestRecord,
    RetainFault,
)

FIXTURE_BANK_ID = "fixture-bank"
FIXTURE_API_KEY = "fixture-api-key"
FIXTURE_USER_AGENT = f"better-hermes-hindsight/{__version__}"


@pytest.fixture(autouse=True)
def _force_direct_loopback_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep SDK traffic on loopback even when the host exports proxy variables."""

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture(scope="module")
def runtime_sentinel() -> str:
    """Return one unpredictable marker that must stay inside raw fake response channels."""

    return secrets.token_urlsafe(24)


def _config(
    tmp_path: Path,
    *,
    base_url: str,
    api_key: str | None,
    recall: dict[str, object] | None = None,
    retain: dict[str, object] | None = None,
) -> BetterHindsightConfig:
    injected: dict[str, object] = {
        "api_url": base_url,
        "bank_id": FIXTURE_BANK_ID,
        "single_principal": True,
    }
    if recall is not None:
        injected["recall"] = recall
    if retain is not None:
        injected["retain"] = retain
    environ = {} if api_key is None else {"HINDSIGHT_API_KEY": api_key}
    return load_config(hermes_home=tmp_path, environ=environ, injected=injected)


def _segment(
    *,
    content: str = "stable segment",
    document_id: str = "stable-document-id",
) -> RetainSegment:
    return RetainSegment(
        content=content,
        document_id=document_id,
        payload_schema="better-hindsight-turn-v1",
        source_sha256="b" * 64,
        segment_index=0,
        segment_count=1,
    )


def _mission_snapshot(
    *,
    retain_present: bool,
    retain_value: str | None,
    observations_present: bool,
    observations_value: str | None,
) -> object:
    mission_value = cast(Callable[..., object], vars(client_module)["MissionValue"])
    mission_snapshot = cast(Callable[..., object], vars(client_module)["MissionSnapshot"])
    return mission_snapshot(
        retain_mission=mission_value(present=retain_present, value=retain_value),
        observations_mission=mission_value(
            present=observations_present,
            value=observations_value,
        ),
    )


async def _capture_failure(operation: Callable[[], Awaitable[object]]) -> BaseException:
    try:
        await operation()
    except BaseException as error:
        return error
    raise AssertionError("operation unexpectedly succeeded")


def _event_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for record in caplog.records:
        if not record.getMessage().startswith("{"):
            continue
        value = json.loads(record.getMessage())
        if type(value) is dict:
            payloads.append(cast(dict[str, object], value))
    return payloads


@contextlib.contextmanager
def _reserved_refusing_loopback() -> Iterator[tuple[socket.socket, str]]:
    """Reserve a loopback port without listening so refusal cannot race port reuse."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        yield reserved, f"http://127.0.0.1:{port}"


async def _release_delay(server: FakeHindsightServer) -> None:
    server.release_delay()
    await server.wait_for_delay_finished()


async def _release_retain_delay(server: FakeHindsightServer) -> None:
    server.release_retain_delay()
    await server.wait_for_retain_delay_finished()


def test_real_adapter_serializes_and_decodes_complete_public_contract(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    runtime_sentinel: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> FakeHindsightServer:
        disposable_bank_id = f"disposable-{secrets.token_hex(12)}"
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=disposable_bank_id,
            error_sentinel=runtime_sentinel,
            expected_api_key=FIXTURE_API_KEY,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            config = _config(
                tmp_path,
                base_url=server.base_url,
                api_key=FIXTURE_API_KEY,
                recall={
                    "budget": "mid",
                    "max_tokens": 321,
                    "types": ["world", "observation"],
                    "tags": ["scope-a", "scope-b"],
                    "tag_mode": "all_strict",
                    "prefer_observations": True,
                    "min_scores": {"semantic": 0.2, "final": 0.5},
                    "include_source_facts": True,
                    "max_source_facts_tokens": 123,
                },
                retain={
                    "tags": ["source:fixture", "kind:turn"],
                    "observation_scopes": "shared",
                },
            )
            adapter = create_hindsight_client(config)
            initial_records: tuple[RequestRecord, ...] = server.records
            assert len(initial_records) == 0

            recalled = await adapter.recall("current query")
            segment = _segment(
                content="immutable segment",
                document_id="stable-document-id",
            )
            retained = await adapter.retain_segment(segment)
            replayed = await adapter.retain_segment(segment)
            bank_config = await adapter.get_bank_config()
            await adapter.update_bank_missions({"retain_mission": "retain-new"})

            assert isinstance(recalled, RecallResponse)
            assert retained == RetainConfirmation(confirmed=True)
            assert replayed == RetainConfirmation(confirmed=True)
            assert bank_config == _mission_snapshot(
                retain_present=True,
                retain_value="retain-old",
                observations_present=True,
                observations_value="observe-old",
            )

            assert len(recalled.results) == 1
            result = recalled.results[0]
            assert result.id == "observation-1"
            assert result.text == "fixture observation"
            assert result.type == "observation"
            assert result.tags == ["scope-a"]
            assert result.source_fact_ids == ["source-fact-1"]
            assert result.scores is not None
            assert result.scores.final == 0.9
            assert result.scores.semantic == 0.8
            assert recalled.source_facts is not None
            assert recalled.source_facts["source-fact-1"].text == "fixture source fact"

            records: tuple[RequestRecord, ...] = server.records
            assert len(records) == 5
            assert len(records) <= MAX_REQUEST_RECORDS
            assert all(record.query == "" for record in records)

            for record in records:
                assert record.accept == "application/json"
                assert record.user_agent == FIXTURE_USER_AGENT
                assert record.content_type == "application/json"
                assert record.authorization == "valid_bearer"

            assert [(record.method, record.path) for record in records] == [
                ("POST", f"/v1/default/banks/{FIXTURE_BANK_ID}/memories/recall"),
                ("POST", f"/v1/default/banks/{FIXTURE_BANK_ID}/memories"),
                ("POST", f"/v1/default/banks/{FIXTURE_BANK_ID}/memories"),
                ("GET", f"/v1/default/banks/{FIXTURE_BANK_ID}/config"),
                ("PATCH", f"/v1/default/banks/{FIXTURE_BANK_ID}/config"),
            ]

            assert records[0].json_body == {
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
            }
            expected_retain = {
                "items": [
                    {
                        "content": "immutable segment",
                        "timestamp": None,
                        "context": None,
                        "metadata": {
                            "better_hindsight_payload_schema": "better-hindsight-turn-v1",
                            "better_hindsight_segment_count": "1",
                            "better_hindsight_segment_index": "0",
                            "better_hindsight_source_sha256": "b" * 64,
                        },
                        "document_id": "stable-document-id",
                        "entities": None,
                        "tags": ["kind:turn", "source:fixture"],
                        "observation_scopes": [[]],
                        "strategy": None,
                        "update_mode": "replace",
                    }
                ],
                "async": False,
                "document_tags": None,
            }
            assert records[1].json_body == expected_retain
            assert records[2].json_body == expected_retain
            assert records[1].json_body == records[2].json_body
            assert records[3].json_body is None
            assert records[4].json_body == {"updates": {"retain_mission": "retain-new"}}

            report = server.safe_report()
            assert report.request_count == 5
            assert report.recorded_count == 5
            assert len(report.routes) <= MAX_REQUEST_RECORDS
            assert runtime_sentinel not in repr(report)
            assert FIXTURE_API_KEY not in repr(report)
            assert runtime_sentinel not in repr(records)
            assert FIXTURE_API_KEY not in repr(records)
            return server
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    server = asyncio.run(scenario())
    captured = capsys.readouterr()
    assert server.closed is True

    assert FIXTURE_API_KEY not in caplog.text
    assert FIXTURE_API_KEY not in captured.out
    assert FIXTURE_API_KEY not in captured.err
    http_events = [
        event
        for event in _event_payloads(caplog)
        if event.get("event") == "better_hindsight.http_request"
    ]
    assert [(event["operation"], event["outcome"]) for event in http_events] == [
        ("recall", "success"),
        ("retain", "success"),
        ("retain", "success"),
        ("bank_config_get", "success"),
        ("bank_config_patch", "success"),
    ]
    assert all(type(event["elapsed_ms"]) is int for event in http_events)
    assert all(type(event["response_bytes"]) is int for event in http_events)
    assert all(event["status"] == 200 for event in http_events)
    lifecycle = [
        event["outcome"]
        for event in _event_payloads(caplog)
        if event.get("event") == "better_hindsight.client_lifecycle"
    ]
    assert lifecycle == ["initialized", "closed"]


def test_real_adapter_mission_wire_is_stateful_typed_and_changed_field_only(
    tmp_path: Path,
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )

            before = await adapter.get_bank_config()
            await adapter.update_bank_missions({"retain_mission": "retain-new"})
            readback = await adapter.get_bank_config()

            records = server.records
            assert [(record.method, record.path) for record in records] == [
                ("GET", f"/v1/default/banks/{FIXTURE_BANK_ID}/config"),
                ("PATCH", f"/v1/default/banks/{FIXTURE_BANK_ID}/config"),
                ("GET", f"/v1/default/banks/{FIXTURE_BANK_ID}/config"),
            ]
            assert records[0].json_body is None
            assert records[1].json_body == {"updates": {"retain_mission": "retain-new"}}
            assert records[2].json_body is None
            patch_body = cast(dict[str, object], records[1].json_body)
            patch_updates = patch_body["updates"]
            assert isinstance(patch_updates, dict)
            assert "observations_mission" not in patch_updates

            initial_snapshot = _mission_snapshot(
                retain_present=True,
                retain_value="retain-old",
                observations_present=True,
                observations_value="observe-old",
            )
            updated_snapshot = _mission_snapshot(
                retain_present=True,
                retain_value="retain-new",
                observations_present=True,
                observations_value="observe-old",
            )
            assert before == initial_snapshot
            assert readback == updated_snapshot
            assert runtime_sentinel not in repr(server.safe_report())
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_bank",
        "wrong_type",
        "missing",
        "null",
        "blank",
        "malformed_json",
        "http_503",
    ],
)
def test_real_adapter_mission_get_validates_wire_and_preserves_missing_null_blank(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    runtime_sentinel: str,
    fault: MissionReadFault,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> str:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            server.arm_mission_read_fault(fault)
            if fault in {"wrong_bank", "wrong_type", "malformed_json", "http_503"}:
                failure = await _capture_failure(adapter.get_bank_config)
                assert type(failure) is HindsightClientError
                assert isinstance(failure, HindsightClientError)
                assert failure.category == "bank_config_failed"
                expected_reason = {
                    "wrong_bank": "schema_invalid",
                    "wrong_type": "schema_invalid",
                    "malformed_json": "malformed_json",
                    "http_503": "server_status",
                }[fault]
                assert failure.reason == expected_reason
                assert str(failure) == "Better Hindsight bank configuration read failed."
                assert failure.__cause__ is None
                assert failure.__suppress_context__ is True
                error_surface = "\n".join(
                    (repr(failure), "".join(traceback.format_exception(failure)))
                )
            else:
                snapshot = await adapter.get_bank_config()
                if fault == "missing":
                    expected = _mission_snapshot(
                        retain_present=False,
                        retain_value=None,
                        observations_present=False,
                        observations_value=None,
                    )
                elif fault == "null":
                    expected = _mission_snapshot(
                        retain_present=True,
                        retain_value=None,
                        observations_present=True,
                        observations_value=None,
                    )
                else:
                    expected = _mission_snapshot(
                        retain_present=True,
                        retain_value="",
                        observations_present=True,
                        observations_value=" \t",
                    )
                assert snapshot == expected
                error_surface = ""

            recovered = await adapter.get_bank_config()
            assert recovered == _mission_snapshot(
                retain_present=True,
                retain_value="retain-old",
                observations_present=True,
                observations_value="observe-old",
            )
            assert len(server.records) == 2
            return error_surface
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    error_surface = asyncio.run(scenario())
    assert runtime_sentinel not in error_surface
    assert runtime_sentinel not in caplog.text


@pytest.mark.parametrize(
    "fault",
    ["malformed_json", "http_503"],
)
def test_real_adapter_mission_patch_maps_sdk_or_http_failure_without_raw_error_leakage(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    runtime_sentinel: str,
    fault: MissionPatchFault,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> str:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            server.arm_mission_patch_fault(fault)
            failure = await _capture_failure(
                lambda: adapter.update_bank_missions({"retain_mission": "retain-new"})
            )
            assert type(failure) is HindsightClientError
            assert isinstance(failure, HindsightClientError)
            assert failure.category == "mission_update_failed"
            assert str(failure) == "Better Hindsight mission update failed."
            assert failure.__cause__ is None
            assert failure.__suppress_context__ is True
            assert len(server.records) == 1
            assert server.records[0].json_body == {"updates": {"retain_mission": "retain-new"}}
            return "\n".join((repr(failure), "".join(traceback.format_exception(failure))))
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    error_surface = asyncio.run(scenario())
    assert runtime_sentinel not in error_surface
    assert runtime_sentinel not in caplog.text


@pytest.mark.parametrize("fault", ["wrong_type", "wrong_bank", "response_mismatch"])
def test_real_adapter_ignores_noncanonical_patch_response_and_allows_exact_readback(
    tmp_path: Path,
    runtime_sentinel: str,
    fault: MissionPatchFault,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            server.arm_mission_patch_fault(fault)

            await adapter.update_bank_missions({"retain_mission": "retain-new"})
            readback = await adapter.get_bank_config()

            assert readback == _mission_snapshot(
                retain_present=True,
                retain_value="retain-new",
                observations_present=True,
                observations_value="observe-old",
            )
            assert [record.method for record in server.records] == ["PATCH", "GET"]
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    asyncio.run(scenario())


def test_real_adapter_maps_commit_then_patch_response_loss_but_fake_retains_state(
    tmp_path: Path,
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            server.arm_mission_patch_fault("response_loss_after_commit")
            failure = await _capture_failure(
                lambda: adapter.update_bank_missions({"retain_mission": "retain-new"})
            )
            assert type(failure) is HindsightClientError
            assert isinstance(failure, HindsightClientError)
            assert failure.category == "mission_update_failed"
            assert str(failure) == "Better Hindsight mission update failed."
            assert failure.__cause__ is None

            readback = await adapter.get_bank_config()
            assert readback == _mission_snapshot(
                retain_present=True,
                retain_value="retain-new",
                observations_present=True,
                observations_value="observe-old",
            )
            assert [record.method for record in server.records] == ["PATCH", "GET"]
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    asyncio.run(scenario())


def test_real_adapter_retain_wire_uses_only_canonical_redacted_tags(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    runtime_sentinel: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> tuple[str, str]:
        raw_tag_secret = secrets.token_urlsafe(24)
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=FIXTURE_API_KEY,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(
                    tmp_path,
                    base_url=server.base_url,
                    api_key=FIXTURE_API_KEY,
                    retain={
                        "tags": ["zeta", f"api_key={raw_tag_secret}", "alpha"],
                    },
                )
            )
            segment = _segment(
                content="privacy-safe synthetic segment",
                document_id="stable-private-wire-id",
            )

            confirmed = await adapter.retain_segment(segment)
            assert confirmed == RetainConfirmation(confirmed=True)

            server.arm_retain_fault("http_503")
            failure = await _capture_failure(lambda: adapter.retain_segment(segment))
            assert type(failure) is HindsightClientError
            assert isinstance(failure, HindsightClientError)
            assert failure.category == "retain_failed"
            assert str(failure) == "Better Hindsight retain failed."
            assert failure.__cause__ is None
            assert failure.__suppress_context__ is True

            records = server.records
            assert len(records) == 2
            assert records[0].json_body == records[1].json_body
            for record in records:
                assert record.authorization == "valid_bearer"
                assert isinstance(record.json_body, dict)
                assert record.json_body["async"] is False
                items = record.json_body["items"]
                assert isinstance(items, list) and len(items) == 1
                item = items[0]
                assert isinstance(item, dict)
                assert item["tags"] == [
                    "alpha",
                    f"api_key={REDACTION_MARKER}",
                    "zeta",
                ]
                assert item["update_mode"] == "replace"

            surfaces = "\n".join(
                (
                    repr(records),
                    repr(server.safe_report()),
                    repr(failure),
                    "".join(traceback.format_exception(failure)),
                )
            )
            assert raw_tag_secret not in surfaces
            assert runtime_sentinel not in surfaces
            assert FIXTURE_API_KEY not in surfaces
            return raw_tag_secret, surfaces
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    raw_tag_secret, surfaces = asyncio.run(scenario())
    captured = capsys.readouterr()
    for forbidden in (raw_tag_secret, runtime_sentinel, FIXTURE_API_KEY):
        assert forbidden not in surfaces
        assert forbidden not in caplog.text
        assert forbidden not in captured.out
        assert forbidden not in captured.err


@pytest.mark.parametrize(
    "fault",
    [
        "false_success",
        "wrong_bank",
        "wrong_count",
        "asynchronous",
    ],
)
def test_real_adapter_returns_typed_false_for_valid_unconfirmed_retain_responses(
    tmp_path: Path,
    runtime_sentinel: str,
    fault: RetainFault,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            segment = _segment()

            server.arm_retain_fault(fault)
            unconfirmed = await adapter.retain_segment(segment)
            recovered = await adapter.retain_segment(segment)

            assert unconfirmed == RetainConfirmation(confirmed=False)
            assert recovered == RetainConfirmation(confirmed=True)
            assert len(server.records) == 2
            assert server.records[0].json_body == server.records[1].json_body
            assert runtime_sentinel not in repr(server.records)
            assert runtime_sentinel not in repr(server.safe_report())
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "malformed_json",
        "malformed_schema",
        "http_503",
        "success_integer",
        "items_count_boolean",
        "async_integer",
    ],
)
def test_real_adapter_maps_invalid_retain_wire_responses_to_fixed_error_then_recovers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    runtime_sentinel: str,
    fault: RetainFault,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> tuple[str, str, str]:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            segment = _segment()

            server.arm_retain_fault(fault)
            failure = await _capture_failure(lambda: adapter.retain_segment(segment))
            assert type(failure) is HindsightClientError
            assert isinstance(failure, HindsightClientError)
            assert failure.category == "retain_failed"
            assert str(failure) == "Better Hindsight retain failed."
            assert failure.__cause__ is None
            assert failure.__suppress_context__ is True

            recovered = await adapter.retain_segment(segment)
            assert recovered == RetainConfirmation(confirmed=True)
            assert len(server.records) == 2
            assert server.records[0].json_body == server.records[1].json_body
            return (
                repr(failure),
                "".join(traceback.format_exception(failure)),
                repr(server.safe_report()),
            )
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    error_repr, error_traceback, safe_report = asyncio.run(scenario())
    for surface in (error_repr, error_traceback, safe_report, caplog.text):
        assert runtime_sentinel not in surface


def test_query_only_recall_freezes_sdk_defaults_nulls_and_absent_auth(
    tmp_path: Path,
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            response = await adapter.recall("query-only recall")
            assert isinstance(response, RecallResponse)
            assert response.results[0].text == "fixture observation"

            assert len(server.records) == 1
            record = server.records[0]
            assert record.authorization == "absent"
            assert record.query == ""
            assert record.json_body == {
                "query": "query-only recall",
                "types": None,
                "prefer_observations": False,
                "budget": "mid",
                "max_tokens": 4096,
                "trace": False,
                "query_timestamp": None,
                "include": {
                    "entities": None,
                    "chunks": None,
                    "source_facts": None,
                },
                "tags": None,
                "tags_match": "any",
                "tag_groups": None,
                "min_scores": None,
            }
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    asyncio.run(scenario())


def test_fake_mission_config_is_stateful_and_preserves_unrequested_fields(
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        config_url = f"{server.base_url}/v1/default/banks/{FIXTURE_BANK_ID}/config"
        try:
            async with ClientSession() as session:
                before_response = await session.get(config_url)
                before = await before_response.json()
                patch_response = await session.patch(
                    config_url,
                    json={"updates": {"retain_mission": "retain-new"}},
                )
                patched = await patch_response.json()
                after_response = await session.get(config_url)
                after = await after_response.json()

            assert before_response.status == 200
            assert patch_response.status == 200
            assert after_response.status == 200
            assert before == {
                "bank_id": FIXTURE_BANK_ID,
                "config": {
                    "retain_mission": "retain-old",
                    "observations_mission": "observe-old",
                },
                "overrides": {
                    "retain_mission": "retain-old",
                    "observations_mission": "observe-old",
                },
            }
            expected_after = {
                "bank_id": FIXTURE_BANK_ID,
                "config": {
                    "retain_mission": "retain-new",
                    "observations_mission": "observe-old",
                },
                "overrides": {
                    "retain_mission": "retain-new",
                    "observations_mission": "observe-old",
                },
            }
            assert patched == expected_after
            assert after == expected_after
            assert [(record.method, record.json_body) for record in server.records] == [
                ("GET", None),
                ("PATCH", {"updates": {"retain_mission": "retain-new"}}),
                ("GET", None),
            ]
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_bank",
        "wrong_type",
        "missing",
        "null",
        "blank",
        "malformed_json",
        "http_503",
    ],
)
def test_fake_mission_read_faults_are_one_shot_and_do_not_mutate_state(
    runtime_sentinel: str,
    fault: MissionReadFault,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        config_url = f"{server.base_url}/v1/default/banks/{FIXTURE_BANK_ID}/config"
        try:
            server.arm_mission_read_fault(fault)
            with pytest.raises(RuntimeError, match="mission read fault is already armed"):
                server.arm_mission_read_fault("wrong_bank")

            async with ClientSession() as session:
                fault_response = await session.get(config_url)
                if fault == "malformed_json":
                    assert fault_response.status == 200
                    assert runtime_sentinel in await fault_response.text()
                elif fault == "http_503":
                    assert fault_response.status == 503
                    assert runtime_sentinel in await fault_response.text()
                else:
                    payload = await fault_response.json()
                    assert fault_response.status == 200
                    if fault == "wrong_bank":
                        assert payload["bank_id"] == "different-fixture-bank"
                    elif fault == "wrong_type":
                        assert payload["config"]["retain_mission"] == {
                            "raw_error": runtime_sentinel
                        }
                    elif fault == "missing":
                        assert payload["config"] == {}
                    elif fault == "null":
                        assert payload["config"] == {
                            "retain_mission": None,
                            "observations_mission": None,
                        }
                    else:
                        assert payload["config"] == {
                            "retain_mission": "",
                            "observations_mission": " \t",
                        }

                recovered_response = await session.get(config_url)
                recovered = await recovered_response.json()

            assert recovered_response.status == 200
            assert recovered["bank_id"] == FIXTURE_BANK_ID
            assert recovered["config"] == {
                "retain_mission": "retain-old",
                "observations_mission": "observe-old",
            }
            assert len(server.records) == 2
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_bank",
        "wrong_type",
        "response_mismatch",
        "malformed_json",
        "http_503",
    ],
)
def test_fake_mission_patch_faults_are_one_shot_with_explicit_commit_behavior(
    runtime_sentinel: str,
    fault: MissionPatchFault,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        config_url = f"{server.base_url}/v1/default/banks/{FIXTURE_BANK_ID}/config"
        try:
            server.arm_mission_patch_fault(fault)
            with pytest.raises(RuntimeError, match="mission patch fault is already armed"):
                server.arm_mission_patch_fault("wrong_bank")

            async with ClientSession() as session:
                fault_response = await session.patch(
                    config_url,
                    json={"updates": {"retain_mission": "retain-new"}},
                )
                if fault == "malformed_json":
                    assert fault_response.status == 200
                    assert runtime_sentinel in await fault_response.text()
                elif fault == "http_503":
                    assert fault_response.status == 503
                    assert runtime_sentinel in await fault_response.text()
                else:
                    payload = await fault_response.json()
                    assert fault_response.status == 200
                    if fault == "wrong_bank":
                        assert payload["bank_id"] == "different-fixture-bank"
                    elif fault == "wrong_type":
                        assert payload["config"]["retain_mission"] == {
                            "raw_error": runtime_sentinel
                        }
                    else:
                        assert payload["config"]["retain_mission"] == "patch-response-mismatch"

                recovered_response = await session.get(config_url)
                recovered = await recovered_response.json()

            expected_retain = "retain-old" if fault == "http_503" else "retain-new"
            assert recovered_response.status == 200
            assert recovered["config"] == {
                "retain_mission": expected_retain,
                "observations_mission": "observe-old",
            }
            assert len(server.records) == 2
        finally:
            await server.close()

    asyncio.run(scenario())


def test_fake_mission_patch_can_commit_then_lose_its_response(runtime_sentinel: str) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        config_url = f"{server.base_url}/v1/default/banks/{FIXTURE_BANK_ID}/config"
        try:
            server.arm_mission_patch_fault("response_loss_after_commit")
            async with ClientSession() as session:
                with pytest.raises(ClientError):
                    await session.patch(
                        config_url,
                        json={"updates": {"retain_mission": "retain-new"}},
                    )
                recovered_response = await session.get(config_url)
                recovered = await recovered_response.json()

            assert recovered_response.status == 200
            assert recovered["config"] == {
                "retain_mission": "retain-new",
                "observations_mission": "observe-old",
            }
            assert [record.method for record in server.records] == ["PATCH", "GET"]
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["mismatch", "malformed_json", "http_503"])
def test_fake_mission_readback_faults_wait_for_patch_and_are_one_shot(
    runtime_sentinel: str,
    fault: MissionReadbackFault,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        config_url = f"{server.base_url}/v1/default/banks/{FIXTURE_BANK_ID}/config"
        try:
            server.arm_mission_readback_fault(fault)
            async with ClientSession() as session:
                before_response = await session.get(config_url)
                before = await before_response.json()
                patch_response = await session.patch(
                    config_url,
                    json={"updates": {"retain_mission": "retain-new"}},
                )
                patched = await patch_response.json()
                fault_response = await session.get(config_url)
                if fault == "mismatch":
                    fault_payload = await fault_response.json()
                    assert fault_response.status == 200
                    assert fault_payload["config"]["retain_mission"] == "readback-mismatch"
                elif fault == "malformed_json":
                    assert fault_response.status == 200
                    assert runtime_sentinel in await fault_response.text()
                else:
                    assert fault_response.status == 503
                    assert runtime_sentinel in await fault_response.text()
                recovered_response = await session.get(config_url)
                recovered = await recovered_response.json()

            assert before["config"]["retain_mission"] == "retain-old"
            assert patched["config"]["retain_mission"] == "retain-new"
            assert recovered_response.status == 200
            assert recovered["config"] == {
                "retain_mission": "retain-new",
                "observations_mission": "observe-old",
            }
            assert [record.method for record in server.records] == ["GET", "PATCH", "GET", "GET"]
        finally:
            await server.close()

    asyncio.run(scenario())


def test_fake_mission_patch_can_mutate_an_untouched_field_deterministically(
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        config_url = f"{server.base_url}/v1/default/banks/{FIXTURE_BANK_ID}/config"
        try:
            server.arm_mission_patch_fault("unintended_untouched_mutation")
            async with ClientSession() as session:
                patch_response = await session.patch(
                    config_url,
                    json={"updates": {"retain_mission": "retain-new"}},
                )
                patched = await patch_response.json()
                read_response = await session.get(config_url)
                readback = await read_response.json()

            expected = {
                "retain_mission": "retain-new",
                "observations_mission": "unexpected-untouched-mutation",
            }
            assert patch_response.status == 200
            assert read_response.status == 200
            assert patched["config"] == expected
            assert readback["config"] == expected
            assert server.records[0].json_body == {"updates": {"retain_mission": "retain-new"}}
        finally:
            await server.close()

    asyncio.run(scenario())


def test_fake_rejects_unregistered_methods_paths_and_query_parameters(
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        try:
            async with ClientSession() as session:
                profile_path = f"/v1/default/banks/{FIXTURE_BANK_ID}/profile"
                config_path = f"/v1/default/banks/{FIXTURE_BANK_ID}/config"
                responses = [
                    await session.head(f"{server.base_url}/version"),
                    await session.head(f"{server.base_url}{profile_path}"),
                    await session.head(f"{server.base_url}{config_path}"),
                    await session.post(f"{server.base_url}/version"),
                    await session.get(f"{server.base_url}/not-a-task-3-route"),
                    await session.get(f"{server.base_url}/v1/default/banks/unregistered/profile"),
                    await session.get(f"{server.base_url}/version?unexpected=1"),
                ]
                assert [response.status for response in responses] == [
                    405,
                    405,
                    405,
                    405,
                    404,
                    404,
                    400,
                ]
                await asyncio.gather(*(response.read() for response in responses))
        finally:
            await server.close()

    asyncio.run(scenario())


def test_fake_enforces_body_record_and_safe_report_bounds(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    runtime_sentinel: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> tuple[str, str, str]:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=FIXTURE_API_KEY,
        )
        await server.start()
        try:
            headers = {"Authorization": f"Bearer {FIXTURE_API_KEY}"}
            async with ClientSession(headers=headers) as session:
                oversized = await session.post(
                    f"{server.base_url}/v1/default/banks/{FIXTURE_BANK_ID}/memories/recall",
                    data=b"x" * (MAX_REQUEST_BYTES + 1),
                    headers={"Content-Type": "application/json"},
                )
                assert oversized.status == 413
                await oversized.read()
                assert len(server.records) == 0

                for _ in range(MAX_REQUEST_RECORDS):
                    accepted = await session.get(f"{server.base_url}/version")
                    assert accepted.status == 200
                    await accepted.read()

                overflow = await session.get(f"{server.base_url}/version")
                assert overflow.status == 429
                await overflow.read()

            records: tuple[RequestRecord, ...] = server.records
            report = server.safe_report()
            assert len(records) == MAX_REQUEST_RECORDS
            assert report.request_count == MAX_REQUEST_RECORDS + 1
            assert report.recorded_count == MAX_REQUEST_RECORDS
            assert len(report.routes) == MAX_REQUEST_RECORDS
            assert all(record.json_body is None for record in records)
            assert all(record.authorization == "valid_bearer" for record in records)
            assert not hasattr(report, "json_body")
            assert not hasattr(report, "authorization")
            return repr(server), repr(records), repr(report)
        finally:
            await server.close()

    server_repr, records_repr, report_repr = asyncio.run(scenario())
    captured = capsys.readouterr()
    all_output = "\n".join(
        (caplog.text, captured.out, captured.err, server_repr, records_repr, report_repr)
    )
    assert FIXTURE_API_KEY not in all_output
    assert runtime_sentinel not in all_output


def test_adapter_rejects_oversized_response_and_next_request_succeeds(
    tmp_path: Path,
    runtime_sentinel: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            monkeypatch.setattr(client_module, "HINDSIGHT_MAX_RESPONSE_BYTES", 64)
            failure = await _capture_failure(lambda: adapter.recall("oversized response"))
            assert type(failure) is HindsightClientError
            assert isinstance(failure, HindsightClientError)
            assert failure.category == "recall_failed"
            assert str(failure) == "Better Hindsight recall failed."
            assert failure.__cause__ is None

            monkeypatch.setattr(client_module, "HINDSIGHT_MAX_RESPONSE_BYTES", 16 * 1024 * 1024)
            response = await adapter.recall("after oversized response")
            assert response.results[0].text == "fixture observation"
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    asyncio.run(scenario())


def test_adapter_cancellation_is_cooperative_and_next_request_succeeds(
    tmp_path: Path,
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        task: asyncio.Task[object] | None = None
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            try:
                server.arm_recall_fault("delay")
                task = asyncio.create_task(adapter.recall("cancelled query"))
                await server.wait_for_delay_entered()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert task.cancelled()
            finally:
                await _release_delay(server)
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            response = await adapter.recall("after cancellation")
            assert isinstance(response, RecallResponse)
            assert response.results[0].text == "fixture observation"
            queries: list[object] = []
            for record in server.records:
                assert isinstance(record.json_body, dict)
                queries.append(record.json_body["query"])
            assert queries == ["cancelled query", "after cancellation"]
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    asyncio.run(scenario())


def test_runtime_total_timeout_uses_real_adapter_and_next_request_succeeds(
    tmp_path: Path,
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        runtime: ProcessRuntime | None = None
        call: asyncio.Task[object] | None = None
        try:
            config = _config(tmp_path, base_url=server.base_url, api_key=None)
            runtime = await asyncio.to_thread(ProcessRuntime, config)
            try:
                server.arm_recall_fault("delay")
                started = asyncio.get_running_loop().time()
                call = asyncio.create_task(
                    asyncio.to_thread(runtime.recall, "runtime timeout", timeout=0.03)
                )
                await server.wait_for_delay_entered()
                with pytest.raises(
                    AsyncCallTimeoutError,
                    match="Better Hindsight operation exceeded its total deadline",
                ):
                    await call
                assert asyncio.get_running_loop().time() - started < 1.0
            finally:
                await _release_delay(server)
                if call is not None and not call.done():
                    call.cancel()
                    await asyncio.gather(call, return_exceptions=True)

            response = await asyncio.to_thread(
                runtime.recall,
                "after runtime timeout",
                timeout=1.0,
            )
            assert isinstance(response, RecallResponse)
            assert response.results[0].text == "fixture observation"
        finally:
            if runtime is not None:
                await asyncio.to_thread(runtime.finalize)
            await server.close()

    asyncio.run(scenario())


def test_adapter_maps_raw_failures_to_fixed_nonleaking_errors_and_reports(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    runtime_sentinel: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> tuple[list[dict[str, str]], str, str]:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        adapter: HindsightClientAdapter | None = None
        refused_adapter: HindsightClientAdapter | None = None
        safe_errors: list[dict[str, str]] = []
        try:
            adapter = create_hindsight_client(
                _config(tmp_path, base_url=server.base_url, api_key=None)
            )
            expected_reasons: dict[RecallFault, str] = {
                "malformed_json": "malformed_json",
                "malformed_schema": "schema_invalid",
                "http_503": "server_status",
            }
            for fault, expected_reason in expected_reasons.items():
                server.arm_recall_fault(fault)
                failure = await _capture_failure(lambda: adapter.recall("sanitized query"))
                assert type(failure) is HindsightClientError
                assert isinstance(failure, HindsightClientError)
                assert failure.category == "recall_failed"
                assert failure.reason == expected_reason
                assert str(failure) == "Better Hindsight recall failed."
                assert failure.__cause__ is None
                assert failure.__suppress_context__ is True
                assert runtime_sentinel not in "".join(traceback.format_exception(failure))
                safe_errors.append({"category": failure.category, "message": str(failure)})

            with _reserved_refusing_loopback() as (_reserved_socket, refused_url):
                active_refused_adapter = create_hindsight_client(
                    _config(tmp_path, base_url=refused_url, api_key=None)
                )
                refused_adapter = active_refused_adapter
                refused = await _capture_failure(
                    lambda: active_refused_adapter.recall("sanitized refusal")
                )
                assert type(refused) is HindsightClientError
                assert isinstance(refused, HindsightClientError)
                assert refused.category == "recall_failed"
                assert refused.reason == "connection_error"
                assert str(refused) == "Better Hindsight recall failed."
                assert refused.__cause__ is None
                assert refused.__suppress_context__ is True
                assert runtime_sentinel not in "".join(traceback.format_exception(refused))
                safe_errors.append({"category": refused.category, "message": str(refused)})
                await active_refused_adapter.close()
                refused_adapter = None

            return safe_errors, repr(server.safe_report()), repr(server.records)
        finally:
            if refused_adapter is not None:
                await refused_adapter.close()
            if adapter is not None:
                await adapter.close()
            await server.close()

    safe_errors, safe_report, request_records = asyncio.run(scenario())
    serialized_errors = json.dumps(safe_errors, sort_keys=True)
    assert len(safe_errors) == 4
    assert all(len(item["message"]) <= 100 for item in safe_errors)
    assert runtime_sentinel not in caplog.text
    assert runtime_sentinel not in serialized_errors
    assert runtime_sentinel not in safe_report
    assert runtime_sentinel not in request_records
