"""Contract tests for the narrow public Hindsight 0.8.5 adapter boundary."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError
from importlib import metadata as importlib_metadata
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import pytest
from hindsight_client_api.models.bank_config_response import BankConfigResponse
from hindsight_client_api.models.bank_config_update import BankConfigUpdate
from hindsight_client_api.models.retain_response import RetainResponse as SdkRetainResponse

import better_hermes_hindsight.client as client_module
from better_hermes_hindsight import __version__
from better_hermes_hindsight.client import (
    MISSION_UPDATE_FIELDS,
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


class _SensitiveMissionValue:
    def __str__(self) -> str:  # pragma: no cover - a passing adapter never calls this
        raise AssertionError("raw mission values must not be stringified")

    def __repr__(self) -> str:  # pragma: no cover - a passing adapter never calls this
        raise AssertionError("raw mission values must not be represented")


class _BankConfigResponseSubclass(BankConfigResponse):
    pass


class _DictSubclass(dict[str, object]):
    pass


class _StringSubclass(str):
    pass


class _MissionValueView(Protocol):
    @property
    def present(self) -> bool: ...

    @property
    def value(self) -> str | None: ...


class _MissionSnapshotView(Protocol):
    @property
    def retain_mission(self) -> object: ...

    @property
    def observations_mission(self) -> object: ...


_UNSET = object()


def _sdk_bank_config_response(
    *,
    bank_id: object = "sample-bank",
    config: object = _UNSET,
    overrides: object = _UNSET,
) -> BankConfigResponse:
    resolved = (
        {
            "retain_mission": "retain-old",
            "observations_mission": "observe-old",
        }
        if config is _UNSET
        else config
    )
    resolved_overrides: object = dict(resolved) if isinstance(resolved, dict) else {}
    if overrides is not _UNSET:
        resolved_overrides = overrides
    return BankConfigResponse.model_construct(
        bank_id=bank_id,
        config=resolved,
        overrides=resolved_overrides,
    )


def _expected_mission_snapshot(
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


class _FakeBanksApi:
    def __init__(
        self,
        failures: dict[str, BaseException],
        responses: dict[str, object],
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._failures = failures
        self._responses = responses
        self._mission_config: dict[str, object] = {
            "retain_mission": "retain-old",
            "observations_mission": "observe-old",
        }

    async def get_bank_config(self, **kwargs: object) -> object:
        return self._record("get_bank_config", kwargs)

    async def update_bank_config(self, **kwargs: object) -> object:
        return self._record("update_bank_config", kwargs)

    def _record(self, operation: str, kwargs: Mapping[str, object]) -> object:
        failure = self._failures.get(operation)
        if failure is not None:
            raise failure
        copied = dict(kwargs)
        self.calls.append((operation, copied))
        if operation == "update_bank_config":
            request = copied["bank_config_update"]
            if not isinstance(request, BankConfigUpdate):
                raise AssertionError("contract fake requires the pinned BankConfigUpdate")
            self._mission_config.update(request.updates)
        if operation in self._responses:
            return self._responses[operation]
        if operation in {"get_bank_config", "update_bank_config"}:
            return _sdk_bank_config_response(config=self._mission_config)
        return {"operation": operation}


class _FakeSdkClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures: dict[str, BaseException] = {}
        self.responses: dict[str, object] = {}
        self.banks = _FakeBanksApi(self.failures, self.responses)

    async def arecall(self, **kwargs: object) -> object:
        return self._record("arecall", kwargs)

    async def aretain_batch(self, **kwargs: object) -> object:
        return self._record("aretain_batch", kwargs)

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


def _retain_segment(
    *,
    content: str = "segment",
    document_id: str = "document-id",
) -> RetainSegment:
    return RetainSegment(
        content=content,
        document_id=document_id,
        payload_schema="better-hindsight-turn-v1",
        source_sha256="a" * 64,
        segment_index=0,
        segment_count=1,
    )


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
    segment = _retain_segment(
        content="immutable segment text",
        document_id="stable-document-id",
    )

    result = asyncio.run(adapter.retain_segment(segment))

    expected_item: dict[str, object] = {
        "content": "immutable segment text",
        "document_id": "stable-document-id",
        "metadata": {
            "better_hindsight_payload_schema": "better-hindsight-turn-v1",
            "better_hindsight_segment_count": "1",
            "better_hindsight_segment_index": "0",
            "better_hindsight_source_sha256": "a" * 64,
        },
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

    result = asyncio.run(adapter.retain_segment(_retain_segment()))

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

    result = asyncio.run(adapter.retain_segment(_retain_segment()))

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

    result = asyncio.run(adapter.retain_segment(_retain_segment()))

    assert result == client_module.RetainConfirmation(confirmed=False)


def test_malformed_direct_retain_response_maps_to_fixed_client_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sdk_client = _FakeSdkClient()
    sdk_client.responses["aretain_batch"] = object()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(adapter.retain_segment(_retain_segment()))

    assert caught.value.category == "retain_failed"
    assert str(caught.value) == "Better Hindsight retain failed."
    assert caught.value.__cause__ is None


def test_mission_value_and_snapshot_are_exact_frozen_slotted_project_types() -> None:
    mission_value_type = cast(type[object], vars(client_module)["MissionValue"])
    mission_snapshot_type = cast(type[object], vars(client_module)["MissionSnapshot"])
    mission_value = cast(Callable[..., object], mission_value_type)
    mission_snapshot = cast(Callable[..., object], mission_snapshot_type)

    present = mission_value(present=True, value="synthetic mission")
    absent = mission_value(present=False, value=None)
    snapshot = mission_snapshot(
        retain_mission=present,
        observations_mission=absent,
    )

    assert type(present) is mission_value_type
    assert type(absent) is mission_value_type
    assert type(snapshot) is mission_snapshot_type
    present_view = cast(_MissionValueView, present)
    absent_view = cast(_MissionValueView, absent)
    snapshot_view = cast(_MissionSnapshotView, snapshot)
    assert present_view.present is True
    assert present_view.value == "synthetic mission"
    assert absent_view.present is False
    assert absent_view.value is None
    assert snapshot_view.retain_mission is present
    assert snapshot_view.observations_mission is absent
    assert not hasattr(present, "__dict__")
    assert not hasattr(snapshot, "__dict__")
    with pytest.raises(FrozenInstanceError):
        present.__setattr__("value", "replacement")
    with pytest.raises(FrozenInstanceError):
        snapshot.__setattr__("retain_mission", absent)


def test_bank_config_read_and_allowlisted_mission_update_use_public_banks_api(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    sdk_client = _FakeSdkClient()
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    assert sdk_client.banks.calls == []
    bank_config = asyncio.run(adapter.get_bank_config())
    updated = asyncio.run(
        adapter.update_bank_missions(
            {
                "retain_mission": "Retain stable preferences.",
                "observations_mission": "Consolidate stable observations.",
            }
        )
    )

    assert bank_config == _expected_mission_snapshot(
        retain_present=True,
        retain_value="retain-old",
        observations_present=True,
        observations_value="observe-old",
    )
    assert updated is None
    assert frozenset({"retain_mission", "observations_mission"}) == MISSION_UPDATE_FIELDS
    assert sdk_client.banks.calls[0] == ("get_bank_config", {"bank_id": "sample-bank"})
    operation, kwargs = sdk_client.banks.calls[1]
    assert operation == "update_bank_config"
    assert kwargs["bank_id"] == "sample-bank"
    update = kwargs["bank_config_update"]
    assert isinstance(update, BankConfigUpdate)
    assert update.updates == {
        "retain_mission": "Retain stable preferences.",
        "observations_mission": "Consolidate stable observations.",
    }


@pytest.mark.parametrize(
    (
        "resolved_config",
        "retain_present",
        "retain_value",
        "observations_present",
        "observations_value",
    ),
    [
        ({}, False, None, False, None),
        ({"retain_mission": None}, True, None, False, None),
        (
            {"retain_mission": "", "observations_mission": " \t"},
            True,
            "",
            True,
            " \t",
        ),
        (
            {"retain_mission": "retain-exact", "observations_mission": "observe-exact"},
            True,
            "retain-exact",
            True,
            "observe-exact",
        ),
    ],
)
def test_get_bank_config_preserves_exact_presence_null_and_blank_values_from_resolved_config(
    tmp_path: Path,
    resolved_config: dict[str, object],
    retain_present: bool,
    retain_value: str | None,
    observations_present: bool,
    observations_value: str | None,
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    sdk_client = _FakeSdkClient()
    sdk_client.responses["get_bank_config"] = _sdk_bank_config_response(
        config=resolved_config,
        overrides={"retain_mission": _SensitiveMissionValue()},
    )
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    snapshot = asyncio.run(adapter.get_bank_config())

    assert snapshot == _expected_mission_snapshot(
        retain_present=retain_present,
        retain_value=retain_value,
        observations_present=observations_present,
        observations_value=observations_value,
    )
    assert sdk_client.banks.calls == [("get_bank_config", {"bank_id": "sample-bank"})]


_RAW_MISSION_SENTINEL = "SYNTHETIC_RAW_MISSION_MUST_NOT_LEAK"
_RAW_BANK_SENTINEL = "SYNTHETIC_RAW_BANK_MUST_NOT_LEAK"
_VALID_MISSION_CONFIG = {
    "retain_mission": "retain-old",
    "observations_mission": "observe-old",
}


@pytest.mark.parametrize(
    "response",
    [
        object(),
        _BankConfigResponseSubclass.model_construct(
            bank_id="sample-bank",
            config=_VALID_MISSION_CONFIG,
            overrides={},
        ),
        _sdk_bank_config_response(bank_id=_RAW_BANK_SENTINEL),
        _sdk_bank_config_response(bank_id=_StringSubclass("sample-bank")),
        _sdk_bank_config_response(config=_DictSubclass(_VALID_MISSION_CONFIG)),
        _sdk_bank_config_response(config=[]),
        _sdk_bank_config_response(
            config={
                "retain_mission": 7,
                "observations_mission": "observe-old",
            }
        ),
        _sdk_bank_config_response(
            config={
                "retain_mission": _StringSubclass("retain-old"),
                "observations_mission": "observe-old",
            }
        ),
        _sdk_bank_config_response(
            config={
                "retain_mission": {"raw": _RAW_MISSION_SENTINEL},
                "observations_mission": "observe-old",
            }
        ),
        _sdk_bank_config_response(
            config={
                "retain_mission": _SensitiveMissionValue(),
                "observations_mission": "observe-old",
            }
        ),
    ],
    ids=[
        "wrong-response-type",
        "response-subclass",
        "wrong-bank",
        "bank-string-subclass",
        "config-dict-subclass",
        "wrong-config-type",
        "wrong-mission-type",
        "mission-string-subclass",
        "raw-mission-container",
        "unprintable-mission-value",
    ],
)
def test_get_requires_exact_sdk_response_bank_config_and_mission_types(
    tmp_path: Path,
    response: object,
) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    sdk_client = _FakeSdkClient()
    sdk_client.responses["get_bank_config"] = response
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    with pytest.raises(HindsightClientError) as caught:
        asyncio.run(adapter.get_bank_config())

    assert caught.value.category == "bank_config_failed"
    assert str(caught.value) == "Better Hindsight bank configuration read failed."
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    error_surface = "\n".join(
        (repr(caught.value), "".join(traceback.format_exception(caught.value)))
    )
    assert _RAW_BANK_SENTINEL not in error_surface
    assert _RAW_MISSION_SENTINEL not in error_surface
    assert len(sdk_client.banks.calls) == 1


def test_mission_update_ignores_sparse_noncanonical_sdk_response(tmp_path: Path) -> None:
    config = _config(tmp_path, injected={"bank_id": "sample-bank"})
    sdk_client = _FakeSdkClient()
    sdk_client.responses["update_bank_config"] = {"status": "accepted"}
    adapter = HindsightClientAdapter(config=config, sdk_client=sdk_client)

    result = asyncio.run(adapter.update_bank_missions({"retain_mission": "retain-new"}))

    assert result is None
    assert len(sdk_client.banks.calls) == 1


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


@pytest.mark.parametrize(
    "operation, expected_category, expected_message",
    [
        ("arecall", "recall_failed", "Better Hindsight recall failed."),
        ("aretain_batch", "retain_failed", "Better Hindsight retain failed."),
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
        if operation == "arecall":
            return await adapter.recall("query")
        if operation == "aretain_batch":
            return await adapter.retain_segment(_retain_segment(content="content"))
        if operation == "get_bank_config":
            return await adapter.get_bank_config()
        if operation == "update_bank_config":
            await adapter.update_bank_missions({"retain_mission": "mission"})
            return None
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
