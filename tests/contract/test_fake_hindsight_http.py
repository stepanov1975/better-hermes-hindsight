"""Loopback HTTP contract tests against the real pinned Hindsight 0.8.5 client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import traceback
import zipfile
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Protocol, cast

import pytest
from aiohttp import ClientConnectorError, ClientSession
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import ServiceException
from hindsight_client_api.models.bank_config_response import BankConfigResponse
from hindsight_client_api.models.bank_profile_response import BankProfileResponse
from hindsight_client_api.models.delete_response import DeleteResponse
from hindsight_client_api.models.recall_response import RecallResponse
from hindsight_client_api.models.version_response import VersionResponse
from pydantic import ValidationError

from better_hermes_hindsight import __version__
from better_hermes_hindsight.client import (
    DisposableBankGuardError,
    HindsightClientAdapter,
    HindsightClientError,
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
    RecallFault,
    RequestRecord,
    RetainFault,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BANK_ID = "fixture-bank"
FIXTURE_API_KEY = "fixture-api-key"
FIXTURE_USER_AGENT = f"better-hermes-hindsight/{__version__}"


class _AsyncClosable(Protocol):
    async def aclose(self) -> None:
        """Close one real generated SDK client."""


async def _close_sdk(sdk: object) -> None:
    await cast(_AsyncClosable, sdk).aclose()


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


async def _capture_failure(operation: Callable[[], Awaitable[object]]) -> BaseException:
    try:
        await operation()
    except BaseException as error:
        return error
    raise AssertionError("operation unexpectedly succeeded")


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

    async def scenario() -> tuple[FakeHindsightServer, str]:
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

            version = await adapter.get_server_version()
            with pytest.warns(DeprecationWarning):
                profile = await adapter.get_bank_profile()
            recalled = await adapter.recall("current query")
            segment = RetainSegment(
                content="immutable segment",
                document_id="stable-document-id",
            )
            retained = await adapter.retain_segment(segment)
            replayed = await adapter.retain_segment(segment)
            bank_config = await adapter.get_bank_config()
            updated_config = await adapter.update_bank_missions({"retain_mission": "retain-new"})
            before_guarded_mutations: tuple[RequestRecord, ...] = server.records
            with pytest.raises(
                DisposableBankGuardError,
                match="disposable-bank confirmation required",
            ):
                await adapter.create_disposable_bank(disposable_bank_id)
            with pytest.raises(
                DisposableBankGuardError,
                match="disposable-bank confirmation required",
            ):
                await adapter.delete_disposable_bank(disposable_bank_id)
            assert server.records == before_guarded_mutations

            created = await adapter.create_disposable_bank(
                disposable_bank_id,
                confirm_disposable=True,
            )
            deleted = await adapter.delete_disposable_bank(
                disposable_bank_id,
                confirm_disposable=True,
            )

            assert isinstance(version, VersionResponse)
            assert isinstance(profile, BankProfileResponse)
            assert isinstance(recalled, RecallResponse)
            assert retained == RetainConfirmation(confirmed=True)
            assert replayed == RetainConfirmation(confirmed=True)
            assert isinstance(bank_config, BankConfigResponse)
            assert isinstance(updated_config, BankConfigResponse)
            assert isinstance(created, BankProfileResponse)
            assert isinstance(deleted, DeleteResponse)
            assert version.api_version == "0.8.5"
            assert version.features.observations is True
            assert version.features.store_document_text is True
            assert profile.bank_id == FIXTURE_BANK_ID
            assert profile.name == "Fixture bank"
            assert profile.disposition.skepticism == 3
            assert profile.mission == "Fixture mission"

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

            assert bank_config.bank_id == FIXTURE_BANK_ID
            assert bank_config.config["retain_mission"] == "retain-old"
            assert bank_config.config["observations_mission"] == "observe-old"
            assert updated_config.config["retain_mission"] == "retain-new"
            assert updated_config.config["observations_mission"] == "observe-old"
            assert created.bank_id == disposable_bank_id
            assert created.name == "Disposable fixture bank"
            assert deleted.success is True

            records: tuple[RequestRecord, ...] = server.records
            assert len(records) == 9
            assert len(records) <= MAX_REQUEST_RECORDS
            assert all(record.query == "" for record in records)

            for record in records:
                if record.method == "PUT":
                    assert record.accept == "*/*"
                    assert record.user_agent is not None
                    assert record.user_agent.startswith("Python/")
                    assert "aiohttp/" in record.user_agent
                    assert record.user_agent != FIXTURE_USER_AGENT
                else:
                    assert record.accept == "application/json"
                    assert record.user_agent == FIXTURE_USER_AGENT
                assert record.content_type == "application/json"
                assert record.authorization == "valid_bearer"

            assert [(record.method, record.path) for record in records] == [
                ("GET", "/version"),
                ("GET", f"/v1/default/banks/{FIXTURE_BANK_ID}/profile"),
                ("POST", f"/v1/default/banks/{FIXTURE_BANK_ID}/memories/recall"),
                ("POST", f"/v1/default/banks/{FIXTURE_BANK_ID}/memories"),
                ("POST", f"/v1/default/banks/{FIXTURE_BANK_ID}/memories"),
                ("GET", f"/v1/default/banks/{FIXTURE_BANK_ID}/config"),
                ("PATCH", f"/v1/default/banks/{FIXTURE_BANK_ID}/config"),
                ("PUT", f"/v1/default/banks/{disposable_bank_id}"),
                ("DELETE", f"/v1/default/banks/{disposable_bank_id}"),
            ]

            assert records[0].json_body is None
            assert records[1].json_body is None
            assert records[2].json_body == {
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
                        "metadata": None,
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
            assert records[3].json_body == expected_retain
            assert records[4].json_body == expected_retain
            assert records[3].json_body == records[4].json_body
            assert records[5].json_body is None
            assert records[6].json_body == {"updates": {"retain_mission": "retain-new"}}
            assert records[7].json_body == {}
            assert records[8].json_body is None

            report = server.safe_report()
            assert report.request_count == 9
            assert report.recorded_count == 9
            assert len(report.routes) <= MAX_REQUEST_RECORDS
            assert runtime_sentinel not in repr(report)
            assert FIXTURE_API_KEY not in repr(report)
            assert runtime_sentinel not in repr(records)
            assert FIXTURE_API_KEY not in repr(records)
            return server, disposable_bank_id
        finally:
            if adapter is not None:
                await adapter.close()
            await server.close()

    server, disposable_bank_id = asyncio.run(scenario())
    captured = capsys.readouterr()
    assert server.closed is True
    assert disposable_bank_id.startswith("disposable-")
    assert FIXTURE_API_KEY not in caplog.text
    assert FIXTURE_API_KEY not in captured.out
    assert FIXTURE_API_KEY not in captured.err


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
            segment = RetainSegment(
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
            segment = RetainSegment(content="stable segment", document_id="stable-document-id")

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
            segment = RetainSegment(content="stable segment", document_id="stable-document-id")

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


def test_real_sdk_adapter_retain_delay_is_one_shot_recorded_and_deterministic(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    runtime_sentinel: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> tuple[str, str]:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=None,
        )
        await server.start()
        config = _config(tmp_path, base_url=server.base_url, api_key=None)
        sdk = Hindsight(
            base_url=server.base_url,
            timeout=0.05,
            user_agent=FIXTURE_USER_AGENT,
        )
        adapter = HindsightClientAdapter(config=config, sdk_client=sdk)
        call: asyncio.Task[RetainConfirmation] | None = None
        try:
            segment = RetainSegment(content="stable segment", document_id="stable-document-id")
            server.arm_retain_fault("delay")
            with pytest.raises(RuntimeError, match="retain fault is already armed"):
                server.arm_retain_fault("http_503")

            try:
                call = asyncio.create_task(adapter.retain_segment(segment))
                await server.wait_for_retain_delay_entered()
                with pytest.raises(RuntimeError, match="delayed retain handler is still active"):
                    server.arm_retain_fault("http_503")
                failure = await call
                raise AssertionError(f"retain unexpectedly succeeded: {failure!r}")
            except HindsightClientError as failure:
                assert failure.category == "retain_failed"
                assert str(failure) == "Better Hindsight retain failed."
                assert failure.__cause__ is None
                assert failure.__suppress_context__ is True
                error_surface = "\n".join(
                    (repr(failure), "".join(traceback.format_exception(failure)))
                )
            finally:
                await _release_retain_delay(server)
                if call is not None and not call.done():
                    call.cancel()
                    await asyncio.gather(call, return_exceptions=True)

            recovered = await adapter.retain_segment(segment)
            assert recovered == RetainConfirmation(confirmed=True)
            assert len(server.records) == 2
            assert server.records[0].json_body == server.records[1].json_body
            report_surface = repr(server.safe_report())
            return error_surface, report_surface
        finally:
            await adapter.close()
            await server.close()

    error_surface, report_surface = asyncio.run(scenario())
    for surface in (error_surface, report_surface, caplog.text):
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


@pytest.mark.parametrize(
    ("fault", "expected_type"),
    [
        ("malformed_json", json.JSONDecodeError),
        ("malformed_schema", ValidationError),
        ("http_503", ServiceException),
    ],
)
def test_real_sdk_exposes_source_grounded_raw_response_failures(
    fault: RecallFault,
    expected_type: type[BaseException],
    caplog: pytest.LogCaptureFixture,
    runtime_sentinel: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        server = FakeHindsightServer(
            bank_id=FIXTURE_BANK_ID,
            disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
            error_sentinel=runtime_sentinel,
            expected_api_key=FIXTURE_API_KEY,
        )
        await server.start()
        sdk = Hindsight(
            base_url=server.base_url,
            api_key=FIXTURE_API_KEY,
            timeout=1.0,
            user_agent=FIXTURE_USER_AGENT,
        )
        try:
            server.arm_recall_fault(fault)
            failure = await _capture_failure(
                lambda: sdk.arecall(bank_id=FIXTURE_BANK_ID, query="fault query")
            )
            assert type(failure) is expected_type
            if isinstance(failure, json.JSONDecodeError):
                assert runtime_sentinel in failure.doc
            elif isinstance(failure, ValidationError):
                assert runtime_sentinel in str(failure)
            elif isinstance(failure, ServiceException):
                body = failure.body
                if isinstance(body, bytes):
                    assert runtime_sentinel.encode() in body
                else:
                    assert isinstance(body, str)
                    assert runtime_sentinel in body

            recovered = await sdk.arecall(
                bank_id=FIXTURE_BANK_ID,
                query=f"after {fault}",
            )
            assert recovered.results[0].text == "fixture observation"
            assert len(server.records) == 2
        finally:
            await _close_sdk(sdk)
            await server.close()

    asyncio.run(scenario())
    assert runtime_sentinel not in caplog.text


def test_real_sdk_connection_refusal_recovers_with_same_client(
    runtime_sentinel: str,
) -> None:
    async def scenario() -> None:
        with _reserved_refusing_loopback() as (reserved_socket, base_url):
            server = FakeHindsightServer(
                bank_id=FIXTURE_BANK_ID,
                disposable_bank_id=f"disposable-{secrets.token_hex(12)}",
                error_sentinel=runtime_sentinel,
                expected_api_key=None,
            )
            sdk = Hindsight(base_url=base_url, timeout=1.0)
            try:
                failure = await _capture_failure(
                    lambda: sdk.arecall(bank_id=FIXTURE_BANK_ID, query="refused query")
                )
                assert type(failure) is ClientConnectorError

                await server.start(bound_socket=reserved_socket)
                recovered = await sdk.arecall(
                    bank_id=FIXTURE_BANK_ID,
                    query="after connection refusal",
                )
                assert recovered.results[0].text == "fixture observation"
                assert len(server.records) == 1
            finally:
                await _close_sdk(sdk)
                await server.close()

    asyncio.run(scenario())


def test_real_sdk_transport_timeout_releases_handler_and_next_request_succeeds(
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
        sdk = Hindsight(base_url=server.base_url, timeout=0.25)
        capture: asyncio.Task[BaseException] | None = None
        try:
            try:
                server.arm_recall_fault("delay")
                capture = asyncio.create_task(
                    _capture_failure(
                        lambda: sdk.arecall(
                            bank_id=FIXTURE_BANK_ID,
                            query="transport timeout",
                        )
                    )
                )
                await server.wait_for_delay_entered()
                failure = await capture
                assert type(failure) is TimeoutError
            finally:
                await _release_delay(server)
                if capture is not None and not capture.done():
                    capture.cancel()
                    await asyncio.gather(capture, return_exceptions=True)

            response = await sdk.arecall(bank_id=FIXTURE_BANK_ID, query="after timeout")
            assert response.results[0].text == "fixture observation"
        finally:
            await _close_sdk(sdk)
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
            for fault in ("malformed_json", "malformed_schema", "http_503"):
                server.arm_recall_fault(fault)
                failure = await _capture_failure(lambda: adapter.recall("sanitized query"))
                assert type(failure) is HindsightClientError
                assert isinstance(failure, HindsightClientError)
                assert failure.category == "recall_failed"
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


def _copy_build_project(destination: Path) -> Path:
    project = destination / "project"
    shutil.copytree(
        ROOT,
        project,
        ignore=shutil.ignore_patterns(
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "*.egg-info",
            "*.pyc",
            "__pycache__",
            "build",
            "dist",
        ),
    )
    return project


def _archive_payloads(path: Path) -> list[bytes]:
    payloads: list[bytes] = [path.read_bytes()]
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            payloads.extend(archive.read(name) for name in archive.namelist())
        return payloads
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is not None:
                    payloads.append(extracted.read())
        return payloads
    raise AssertionError(f"unexpected build artifact: {path.name}")


def test_runtime_sentinel_is_absent_from_wheel_and_sdist_bytes(
    tmp_path: Path,
    runtime_sentinel: str,
) -> None:
    project = _copy_build_project(tmp_path)
    output_dir = tmp_path / "build-artifacts"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(output_dir),
            str(project),
        ],
        cwd=project,
        env={
            **os.environ,
            "NO_PROXY": "*",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "no_proxy": "*",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]

    artifacts = sorted(output_dir.iterdir())
    assert any(path.suffix == ".whl" for path in artifacts)
    assert any(path.name.endswith(".tar.gz") for path in artifacts)
    marker = runtime_sentinel.encode()
    assert all(marker not in payload for path in artifacts for payload in _archive_payloads(path))
