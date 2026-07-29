"""Contract tests for the narrow public Hindsight 0.8.5 adapter boundary."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from importlib import metadata as importlib_metadata
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest
from hindsight_client_api.models.bank_config_update import BankConfigUpdate
from hindsight_client_api.models.retain_response import RetainResponse as SdkRetainResponse

import better_hermes_hindsight.client as client_module
from better_hermes_hindsight import __version__
from better_hermes_hindsight.client import (
    MISSION_UPDATE_FIELDS,
    DisposableBankGuardError,
    HindsightClientAdapter,
    HindsightClientError,
    MissionUpdateError,
    RetainSegment,
    create_hindsight_client,
    is_available,
)
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.redaction import REDACTION_MARKER


class _UnprintableFailure(RuntimeError):
    def __str__(self) -> str:  # pragma: no cover - a passing adapter never calls this
        raise AssertionError("raw SDK failures must not be stringified")


class _FakeBanksApi:
    def __init__(self, failures: dict[str, BaseException]) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._failures = failures

    async def get_bank_profile(self, **kwargs: object) -> object:
        return self._record("get_bank_profile", kwargs)

    async def get_bank_config(self, **kwargs: object) -> object:
        return self._record("get_bank_config", kwargs)

    async def update_bank_config(self, **kwargs: object) -> object:
        return self._record("update_bank_config", kwargs)

    async def delete_bank(self, **kwargs: object) -> object:
        return self._record("delete_bank", kwargs)

    def _record(self, operation: str, kwargs: Mapping[str, object]) -> object:
        failure = self._failures.get(operation)
        if failure is not None:
            raise failure
        copied = dict(kwargs)
        self.calls.append((operation, copied))
        return {"operation": operation}


class _FakeSdkClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures: dict[str, BaseException] = {}
        self.responses: dict[str, object] = {}
        self.banks = _FakeBanksApi(self.failures)

    async def arecall(self, **kwargs: object) -> object:
        return self._record("arecall", kwargs)

    async def aget_version(self) -> object:
        return self._record("aget_version", {})

    async def aretain_batch(self, **kwargs: object) -> object:
        return self._record("aretain_batch", kwargs)

    async def acreate_bank(self, **kwargs: object) -> object:
        return self._record("acreate_bank", kwargs)

    async def aclose(self) -> None:
        self._record("aclose", {})

    def _record(self, operation: str, kwargs: Mapping[str, object]) -> object:
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure
        copied = dict(kwargs)
        self.calls.append((operation, copied))
        if operation in self.responses:
            return self.responses[operation]
        if operation == "aretain_batch":
            return _sdk_retain_response(bank_id=str(kwargs["bank_id"]))
        return {"operation": operation}


class _RecordingSdkFactory:
    def __init__(self, client: _FakeSdkClient) -> None:
        self.client = client
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 300.0,
        user_agent: str | None = None,
    ) -> _FakeSdkClient:
        self.calls.append(
            {
                "base_url": base_url,
                "api_key": api_key,
                "timeout": timeout,
                "user_agent": user_agent,
            }
        )
        return self.client


def _sdk_retain_response(
    *,
    success: bool = True,
    bank_id: str = "sample-bank",
    items_count: int = 1,
    var_async: bool = False,
) -> SdkRetainResponse:
    return SdkRetainResponse.model_validate(
        {
            "success": success,
            "bank_id": bank_id,
            "items_count": items_count,
            "async": var_async,
        }
    )


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


def test_package_import_is_sdk_network_and_install_side_effect_free() -> None:
    script = """
import socket
import subprocess
import sys

def forbidden(*args, **kwargs):
    raise AssertionError("import attempted an external side effect")

socket.socket.connect = forbidden
subprocess.Popen = forbidden
import better_hermes_hindsight
assert "hindsight_client" not in sys.modules
from better_hermes_hindsight.client import is_available
assert is_available()
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_is_available_checks_exact_sdk_without_constructing_a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_module = importlib.import_module("hindsight_client")
    constructor_calls = 0

    def forbidden_constructor(*args: object, **kwargs: object) -> object:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("availability must not construct a client")

    monkeypatch.setattr(sdk_module, "Hindsight", forbidden_constructor)

    assert is_available() is True
    assert constructor_calls == 0


def test_is_available_fails_cleanly_for_missing_or_wrong_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib_metadata, "version", lambda _name: "0.8.4")
    assert is_available() is False

    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(importlib_metadata, "version", missing)
    assert is_available() is False


def test_client_construction_uses_exact_public_constructor_and_no_remote_calls(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        injected={"api_url": "https://service.example.test", "bank_id": "sample-bank"},
        api_key="sample-api-key",
    )
    sdk_client = _FakeSdkClient()
    factory = _RecordingSdkFactory(sdk_client)

    adapter = create_hindsight_client(config, sdk_factory=factory)

    assert isinstance(adapter, HindsightClientAdapter)
    assert factory.calls == [
        {
            "base_url": "https://service.example.test",
            "api_key": "sample-api-key",
            "timeout": 300.0,
            "user_agent": f"better-hermes-hindsight/{__version__}",
        }
    ]
    assert sdk_client.calls == []
    assert sdk_client.banks.calls == []


async def _recall(adapter: HindsightClientAdapter, query: str = "current query") -> object:
    return await adapter.recall(query)


def test_recall_forwards_only_explicit_controls_including_false_and_empty_tags(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        injected={
            "bank_id": "sample-bank",
            "recall": {
                "budget": "low",
                "max_tokens": 2048,
                "types": ["observation", "world"],
                "tags": [],
                "tag_mode": "exact",
                "prefer_observations": False,
                "min_scores": {"semantic": 0.0, "final": 1.25},
                "include_source_facts": False,
                "max_source_facts_tokens": 321,
            },
        },
    )
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    result = asyncio.run(_recall(adapter))

    assert result == {"operation": "arecall"}
    assert sdk_client.calls == [
        (
            "arecall",
            {
                "bank_id": "sample-bank",
                "query": "current query",
                "budget": "low",
                "max_tokens": 2048,
                "types": ["observation", "world"],
                "tags": [],
                "tags_match": "exact",
                "prefer_observations": False,
                "min_scores": {"semantic": 0.0, "final": 1.25},
                "include_source_facts": False,
                "max_source_facts_tokens": 321,
            },
        )
    ]


def test_recall_omits_unconfigured_kwargs_at_adapter_boundary(tmp_path: Path) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    asyncio.run(_recall(adapter))

    # This assertion is deliberately about the adapter call boundary. Hindsight 0.8.5 itself
    # serializes several SDK defaults even when these keyword arguments are omitted.
    assert sdk_client.calls == [("arecall", {"bank_id": "sample-bank", "query": "current query"})]


@pytest.mark.parametrize(
    "configured_scope, expected_scope",
    [
        (None, None),
        ("combined", "combined"),
        ("shared", [[]]),
    ],
)
def test_retain_sends_one_replace_item_with_stable_item_id_scopes_and_tags(
    tmp_path: Path,
    configured_scope: str | None,
    expected_scope: object,
) -> None:
    retain: dict[str, object] = {"tags": ["source:sample", "kind:turn"]}
    if configured_scope is not None:
        retain["observation_scopes"] = configured_scope
    config = _config(
        tmp_path,
        injected={
            "bank_id": "sample-bank",
            "single_principal": True,
            "retain": retain,
        },
    )
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)
    segment = RetainSegment(content="immutable segment text", document_id="stable-document-id")

    result = asyncio.run(adapter.retain_segment(segment))

    expected_item: dict[str, object] = {
        "content": "immutable segment text",
        "document_id": "stable-document-id",
        "update_mode": "replace",
        "tags": ["kind:turn", "source:sample"],
    }
    if expected_scope is not None:
        expected_item["observation_scopes"] = expected_scope
    assert result == client_module.RetainConfirmation(confirmed=True)
    assert sdk_client.calls == [
        (
            "aretain_batch",
            {
                "bank_id": "sample-bank",
                "items": [expected_item],
                "retain_async": False,
            },
        )
    ]
    with pytest.raises(FrozenInstanceError):
        segment.__setattr__("content", "replacement")
    with pytest.raises(FrozenInstanceError):
        result.__setattr__("confirmed", False)


def test_retain_sends_only_canonical_redacted_tags_to_the_sdk(tmp_path: Path) -> None:
    raw_tag_secret = "SYNTHETIC_WIRE_TAG_SECRET"
    config = _config(
        tmp_path,
        injected={
            "bank_id": "sample-bank",
            "retain": {
                "tags": ["zeta", f"api_key={raw_tag_secret}", "alpha"],
            },
        },
    )
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    result = asyncio.run(
        adapter.retain_segment(RetainSegment(content="segment", document_id="document-id"))
    )

    assert result == client_module.RetainConfirmation(confirmed=True)
    _operation, kwargs = sdk_client.calls[0]
    items = kwargs["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    assert item["tags"] == ["alpha", f"api_key={REDACTION_MARKER}", "zeta"]
    assert raw_tag_secret not in repr(sdk_client.calls)


@pytest.mark.parametrize(
    "response",
    [
        _sdk_retain_response(success=False),
        _sdk_retain_response(bank_id="other-bank"),
        _sdk_retain_response(items_count=2),
        _sdk_retain_response(var_async=True),
    ],
)
def test_well_formed_nonconfirming_retain_responses_return_typed_false(
    tmp_path: Path,
    response: SdkRetainResponse,
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    sdk_client = _FakeSdkClient()
    sdk_client.responses["aretain_batch"] = response
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    result = asyncio.run(
        adapter.retain_segment(RetainSegment(content="segment", document_id="document-id"))
    )

    assert result == client_module.RetainConfirmation(confirmed=False)


@pytest.mark.parametrize(
    "overrides",
    [
        {"success": 1},
        {"bank_id": SimpleNamespace(__eq__=lambda _self, _other: True)},
        {"items_count": True},
        {"var_async": 0},
    ],
)
def test_direct_response_type_lookalikes_do_not_confirm(
    tmp_path: Path,
    overrides: Mapping[str, object],
) -> None:
    fields: dict[str, object] = {
        "success": True,
        "bank_id": "sample-bank",
        "items_count": 1,
        "var_async": False,
    }
    fields.update(overrides)
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    sdk_client = _FakeSdkClient()
    sdk_client.responses["aretain_batch"] = SimpleNamespace(**fields)
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    result = asyncio.run(
        adapter.retain_segment(RetainSegment(content="segment", document_id="document-id"))
    )

    assert result == client_module.RetainConfirmation(confirmed=False)


def test_malformed_direct_retain_response_maps_to_fixed_client_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sdk_client = _FakeSdkClient()
    sdk_client.responses["aretain_batch"] = object()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(
            adapter.retain_segment(RetainSegment(content="segment", document_id="document-id"))
        )

    assert caught.value.category == "retain_failed"
    assert str(caught.value) == "Better Hindsight retain failed."
    assert caught.value.__cause__ is None


def test_bank_reads_and_allowlisted_mission_update_use_public_banks_api(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    assert sdk_client.banks.calls == []
    version = asyncio.run(adapter.get_server_version())
    profile = asyncio.run(adapter.get_bank_profile())
    bank_config = asyncio.run(adapter.get_bank_config())
    updated = asyncio.run(
        adapter.update_bank_missions(
            {
                "retain_mission": "Retain stable preferences.",
                "observations_mission": "Consolidate stable observations.",
            }
        )
    )

    assert version == {"operation": "aget_version"}
    assert profile == {"operation": "get_bank_profile"}
    assert bank_config == {"operation": "get_bank_config"}
    assert updated == {"operation": "update_bank_config"}
    assert frozenset({"retain_mission", "observations_mission"}) == MISSION_UPDATE_FIELDS
    assert sdk_client.banks.calls[:2] == [
        ("get_bank_profile", {"bank_id": "sample-bank"}),
        ("get_bank_config", {"bank_id": "sample-bank"}),
    ]
    operation, kwargs = sdk_client.banks.calls[2]
    assert operation == "update_bank_config"
    assert kwargs["bank_id"] == "sample-bank"
    update = kwargs["bank_config_update"]
    assert isinstance(update, BankConfigUpdate)
    assert update.updates == {
        "retain_mission": "Retain stable preferences.",
        "observations_mission": "Consolidate stable observations.",
    }


def test_non_allowlisted_bank_config_update_is_rejected_before_sdk_call(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    with pytest.raises(
        MissionUpdateError,
        match="only retain and observations mission updates are supported",
    ):
        asyncio.run(adapter.update_bank_missions({"unsupported_mission": "not allowed"}))

    assert sdk_client.banks.calls == []


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"retain_mission": None},
        {"observations_mission": ""},
    ],
)
def test_empty_or_clearing_mission_update_is_rejected_before_sdk_call(
    tmp_path: Path,
    updates: Mapping[str, str | None],
) -> None:
    config = _config(tmp_path)
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    with pytest.raises(MissionUpdateError, match="configured non-empty mission"):
        asyncio.run(adapter.update_bank_missions(updates))  # type: ignore[arg-type]

    assert sdk_client.banks.calls == []


def test_disposable_bank_create_and_delete_require_explicit_guard(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    with pytest.raises(DisposableBankGuardError, match="disposable-bank confirmation required"):
        asyncio.run(adapter.create_disposable_bank("disposable-bank", confirm_disposable=False))
    with pytest.raises(DisposableBankGuardError, match="disposable-bank confirmation required"):
        asyncio.run(adapter.delete_disposable_bank("disposable-bank", confirm_disposable=False))
    assert sdk_client.calls == []
    assert sdk_client.banks.calls == []

    created = asyncio.run(
        adapter.create_disposable_bank("disposable-bank", confirm_disposable=True)
    )
    deleted = asyncio.run(
        adapter.delete_disposable_bank("disposable-bank", confirm_disposable=True)
    )

    assert created == {"operation": "acreate_bank"}
    assert deleted == {"operation": "delete_bank"}
    assert sdk_client.calls == [("acreate_bank", {"bank_id": "disposable-bank"})]
    assert sdk_client.banks.calls == [("delete_bank", {"bank_id": "disposable-bank"})]


@pytest.mark.parametrize(
    "operation, expected_category, expected_message",
    [
        ("aget_version", "version_failed", "Better Hindsight version check failed."),
        ("arecall", "recall_failed", "Better Hindsight recall failed."),
        ("aretain_batch", "retain_failed", "Better Hindsight retain failed."),
        (
            "get_bank_profile",
            "bank_profile_failed",
            "Better Hindsight bank profile read failed.",
        ),
        (
            "get_bank_config",
            "bank_config_failed",
            "Better Hindsight bank configuration read failed.",
        ),
        (
            "update_bank_config",
            "mission_update_failed",
            "Better Hindsight mission update failed.",
        ),
        (
            "acreate_bank",
            "disposable_bank_create_failed",
            "Better Hindsight disposable bank creation failed.",
        ),
        (
            "delete_bank",
            "disposable_bank_delete_failed",
            "Better Hindsight disposable bank deletion failed.",
        ),
        ("aclose", "client_close_failed", "Better Hindsight client close failed."),
    ],
)
def test_raw_sdk_failures_map_to_fixed_sanitized_errors(
    tmp_path: Path,
    operation: str,
    expected_category: str,
    expected_message: str,
) -> None:
    config = _config(tmp_path)
    sdk_client = _FakeSdkClient()
    sdk_client.failures[operation] = _UnprintableFailure()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    async def invoke() -> object | None:
        if operation == "aget_version":
            return await adapter.get_server_version()
        if operation == "arecall":
            return await adapter.recall("query")
        if operation == "aretain_batch":
            return await adapter.retain_segment(RetainSegment("content", "document-id"))
        if operation == "get_bank_profile":
            return await adapter.get_bank_profile()
        if operation == "get_bank_config":
            return await adapter.get_bank_config()
        if operation == "update_bank_config":
            return await adapter.update_bank_missions({"retain_mission": "mission"})
        if operation == "acreate_bank":
            return await adapter.create_disposable_bank("disposable-bank", confirm_disposable=True)
        if operation == "delete_bank":
            return await adapter.delete_disposable_bank("disposable-bank", confirm_disposable=True)
        await adapter.close()
        return None

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(invoke())

    assert caught.value.category == expected_category
    assert str(caught.value) == expected_message
    assert caught.value.__cause__ is None


def test_raw_constructor_failure_is_sanitized_without_exception_text(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def failing_factory(**_kwargs: object) -> _FakeSdkClient:
        raise _UnprintableFailure

    with pytest.raises(HindsightClientError) as caught:
        create_hindsight_client(config, sdk_factory=failing_factory)

    assert caught.value.category == "client_initialization_failed"
    assert str(caught.value) == "Better Hindsight client initialization failed."
    assert caught.value.__cause__ is None


def test_asyncio_cancellation_propagates_unchanged(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sdk_client = _FakeSdkClient()
    sdk_client.failures["arecall"] = asyncio.CancelledError()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.recall("query"))
