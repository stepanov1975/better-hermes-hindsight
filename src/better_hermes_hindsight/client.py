"""Narrow public adapter for ``hindsight-client==0.8.5``.

The SDK is imported lazily when a concrete client is created. Importing this module and checking
availability do not construct a client, contact a service, or install anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from typing import Protocol, TypeVar, cast

from better_hermes_hindsight import __version__
from better_hermes_hindsight.config import BetterHindsightConfig, ObservationScopes, RecallConfig

HINDSIGHT_DISTRIBUTION = "hindsight-client"
HINDSIGHT_SDK_VERSION = "0.8.5"
HINDSIGHT_REQUEST_TIMEOUT_SECONDS = 300.0
MISSION_UPDATE_FIELDS = frozenset({"retain_mission", "observations_mission"})


class HindsightClientError(RuntimeError):
    """A fixed, sanitized failure at the Hindsight adapter boundary."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


class MissionUpdateError(ValueError):
    """A mission update attempted to use unsupported bank configuration surface."""


@dataclass(frozen=True, slots=True)
class RetainSegment:
    """One immutable retained source segment with its stable document identity."""

    content: str
    document_id: str
    payload_schema: str
    source_sha256: str
    segment_index: int
    segment_count: int


@dataclass(frozen=True, slots=True)
class RetainConfirmation:
    """Sanitized typed result of exact synchronous retain-response validation."""

    confirmed: bool


@dataclass(frozen=True, slots=True)
class MissionValue:
    """One exact remote mission value without value-bearing representation."""

    present: bool
    value: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class MissionSnapshot:
    """Validated allowlisted mission values from resolved bank configuration."""

    retain_mission: MissionValue
    observations_mission: MissionValue


class HindsightClientProtocol(Protocol):
    """The complete Hindsight surface used by the shared process runtime."""

    async def recall(self, query: str) -> object:
        """Recall against the configured bank."""

    async def retain_segment(self, segment: RetainSegment) -> RetainConfirmation:
        """Synchronously confirm one replace-mode retained segment."""
        ...

    async def get_bank_config(self) -> object:
        """Read the configured bank configuration."""

    async def update_bank_missions(self, updates: Mapping[str, str]) -> None:
        """Update only allowlisted retain and observations mission fields."""

    async def close(self) -> None:
        """Close the client on its owning event loop."""


class MissionClientProtocol(Protocol):
    """The exact typed mission surface used only by explicit management commands."""

    async def get_bank_config(self) -> MissionSnapshot: ...

    async def update_bank_missions(self, updates: Mapping[str, str]) -> None: ...


class HindsightSdkFactory(Protocol):
    """Constructor shape of the pinned public Hindsight SDK client."""

    def __call__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = HINDSIGHT_REQUEST_TIMEOUT_SECONDS,
        user_agent: str | None = None,
    ) -> object: ...


class _BanksApiProtocol(Protocol):
    async def get_bank_config(self, **kwargs: object) -> object: ...

    async def update_bank_config(self, **kwargs: object) -> object: ...


class _HindsightSdkProtocol(Protocol):
    banks: _BanksApiProtocol

    async def arecall(self, **kwargs: object) -> object: ...

    async def aretain_batch(self, **kwargs: object) -> object: ...

    async def aclose(self) -> None: ...


_T = TypeVar("_T")

_CLIENT_FAILURES: dict[str, tuple[str, str]] = {
    "recall": ("recall_failed", "Better Hindsight recall failed."),
    "retain": ("retain_failed", "Better Hindsight retain failed."),
    "bank_config": (
        "bank_config_failed",
        "Better Hindsight bank configuration read failed.",
    ),
    "mission_update": (
        "mission_update_failed",
        "Better Hindsight mission update failed.",
    ),
    "client_close": (
        "client_close_failed",
        "Better Hindsight client close failed.",
    ),
}


def is_available() -> bool:
    """Return whether the exact pinned SDK is importable without creating a client."""

    try:
        if metadata.version(HINDSIGHT_DISTRIBUTION) != HINDSIGHT_SDK_VERSION:
            return False
        from hindsight_client import Hindsight
        from hindsight_client_api.models.bank_config_response import BankConfigResponse
        from hindsight_client_api.models.bank_config_update import BankConfigUpdate
    except Exception:
        return False
    return Hindsight is not None and BankConfigResponse is not None and BankConfigUpdate is not None


def create_hindsight_client(
    config: BetterHindsightConfig,
    *,
    sdk_factory: HindsightSdkFactory | None = None,
) -> HindsightClientAdapter:
    """Construct the one concrete SDK client used by a process runtime.

    The caller must invoke this function on the runtime's owning event loop. The SDK constructor is
    local-only; bank reads remain explicit adapter operations.
    """

    factory = sdk_factory
    if factory is None:
        if not is_available():
            raise HindsightClientError(
                "sdk_unavailable",
                "Better Hindsight requires hindsight-client==0.8.5.",
            )
        try:
            from hindsight_client import Hindsight
        except Exception:
            raise HindsightClientError(
                "sdk_unavailable",
                "Better Hindsight requires hindsight-client==0.8.5.",
            ) from None
        factory = cast(HindsightSdkFactory, Hindsight)

    try:
        sdk_client = factory(
            base_url=config.api_url,
            api_key=config.api_key,
            timeout=HINDSIGHT_REQUEST_TIMEOUT_SECONDS,
            user_agent=f"better-hermes-hindsight/{__version__}",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HindsightClientError(
            "client_initialization_failed",
            "Better Hindsight client initialization failed.",
        ) from None

    return HindsightClientAdapter(config=config, sdk_client=sdk_client)


class HindsightClientAdapter:
    """Adapter exposing only the audited public Hindsight 0.8.5 operations."""

    _retain_scopes: ObservationScopes

    __slots__ = ("_bank_id", "_recall_config", "_retain_scopes", "_retain_tags", "_sdk")

    def __init__(self, *, config: BetterHindsightConfig, sdk_client: object) -> None:
        self._bank_id = config.bank_id
        self._recall_config = config.recall
        self._retain_scopes = config.retain.observation_scopes
        self._retain_tags = config.retain.tags
        self._sdk = cast(_HindsightSdkProtocol, sdk_client)

    def __repr__(self) -> str:
        return "HindsightClientAdapter()"

    async def recall(self, query: str) -> object:
        """Call public ``arecall`` with only explicitly configured optional controls.

        Omission is guaranteed at this adapter boundary. Hindsight 0.8.5's public method applies
        and serializes some SDK defaults after this call; the fake HTTP contract test owns that
        wire behavior.
        """

        kwargs = _build_recall_kwargs(self._recall_config)
        return await _mapped_call(
            "recall",
            lambda: self._sdk.arecall(bank_id=self._bank_id, query=query, **kwargs),
        )

    async def retain_segment(self, segment: RetainSegment) -> RetainConfirmation:
        """Retain exactly one stable, replace-mode segment with synchronous confirmation."""

        item: dict[str, object] = {
            "content": segment.content,
            "document_id": segment.document_id,
            "metadata": {
                "better_hindsight_payload_schema": segment.payload_schema,
                "better_hindsight_segment_count": str(segment.segment_count),
                "better_hindsight_segment_index": str(segment.segment_index),
                "better_hindsight_source_sha256": segment.source_sha256,
            },
            "update_mode": "replace",
            "tags": list(self._retain_tags),
        }
        encoded_scopes = _encode_observation_scopes(self._retain_scopes)
        if encoded_scopes is not None:
            item["observation_scopes"] = encoded_scopes

        async def retain() -> RetainConfirmation:
            response = await self._sdk.aretain_batch(
                bank_id=self._bank_id,
                items=[item],
                retain_async=False,
            )
            return _retain_confirmation(response, expected_bank_id=self._bank_id)

        return await _mapped_call(
            "retain",
            retain,
        )

    async def get_bank_config(self) -> MissionSnapshot:
        """Read and exactly validate missions through the SDK's public async banks API."""

        async def get() -> MissionSnapshot:
            response = await self._sdk.banks.get_bank_config(bank_id=self._bank_id)
            return _mission_snapshot(response, expected_bank_id=self._bank_id)

        return await _mapped_call(
            "bank_config",
            get,
        )

    async def update_bank_missions(self, updates: Mapping[str, str]) -> None:
        """Apply only configured retain and observations mission changes.

        The PATCH response is intentionally ignored. Operator success depends on a separate,
        exact GET readback of the configured missions.
        """

        copied_updates = dict(updates)
        if (
            not copied_updates
            or not set(copied_updates).issubset(MISSION_UPDATE_FIELDS)
            or any(
                not isinstance(value, str) or not value.strip() for value in copied_updates.values()
            )
        ):
            raise MissionUpdateError(
                "Better Hindsight only retain and observations mission updates are supported; "
                "each update must be a configured non-empty mission."
            )

        async def update() -> None:
            from hindsight_client_api.models.bank_config_update import BankConfigUpdate

            request = BankConfigUpdate(updates=copied_updates)
            await self._sdk.banks.update_bank_config(
                bank_id=self._bank_id,
                bank_config_update=request,
            )

        await _mapped_call("mission_update", update)

    async def close(self) -> None:
        """Close the public SDK client on the caller's event loop."""

        await _mapped_call("client_close", self._sdk.aclose)


def _build_recall_kwargs(config: RecallConfig) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if config.budget is not None:
        kwargs["budget"] = config.budget
    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.types is not None:
        kwargs["types"] = list(config.types)
    if config.tags is not None:
        kwargs["tags"] = list(config.tags)
    if config.tag_mode is not None:
        kwargs["tags_match"] = config.tag_mode
    if config.prefer_observations is not None:
        kwargs["prefer_observations"] = config.prefer_observations
    if config.min_scores is not None:
        kwargs["min_scores"] = config.min_scores.as_dict()
    if config.include_source_facts is not None:
        kwargs["include_source_facts"] = config.include_source_facts
    if config.max_source_facts_tokens is not None:
        kwargs["max_source_facts_tokens"] = config.max_source_facts_tokens
    return kwargs


def _encode_observation_scopes(scopes: ObservationScopes) -> object | None:
    if scopes is None or scopes == "combined":
        return scopes
    return [list(scope) for scope in scopes]


def _retain_confirmation(response: object, *, expected_bank_id: str) -> RetainConfirmation:
    missing = object()
    success = getattr(response, "success", missing)
    bank_id = getattr(response, "bank_id", missing)
    items_count = getattr(response, "items_count", missing)
    var_async = getattr(response, "var_async", missing)
    if any(value is missing for value in (success, bank_id, items_count, var_async)):
        raise ValueError("malformed retain response")
    return RetainConfirmation(
        confirmed=(
            type(success) is bool
            and success is True
            and type(bank_id) is str
            and bank_id == expected_bank_id
            and type(items_count) is int
            and items_count == 1
            and type(var_async) is bool
            and var_async is False
        )
    )


def _mission_snapshot(
    response: object,
    *,
    expected_bank_id: str,
) -> MissionSnapshot:
    from hindsight_client_api.models.bank_config_response import BankConfigResponse

    if type(response) is not BankConfigResponse:
        raise ValueError("malformed bank configuration response")
    bank_id = response.bank_id
    config = response.config
    if type(bank_id) is not str or bank_id != expected_bank_id or type(config) is not dict:
        raise ValueError("malformed bank configuration response")
    mission_config = cast(dict[str, object], config)
    snapshot = MissionSnapshot(
        retain_mission=_mission_value(mission_config, "retain_mission"),
        observations_mission=_mission_value(mission_config, "observations_mission"),
    )
    return snapshot


def _mission_value(config: dict[str, object], name: str) -> MissionValue:
    if name not in config:
        return MissionValue(present=False, value=None)
    value = config[name]
    if value is None:
        return MissionValue(present=True, value=None)
    if type(value) is not str:
        raise ValueError("malformed bank configuration response")
    return MissionValue(present=True, value=value)


async def _mapped_call(operation: str, call: Callable[[], Awaitable[_T]]) -> _T:
    category, message = _CLIENT_FAILURES[operation]
    try:
        return await call()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HindsightClientError(category, message) from None


__all__ = [
    "HINDSIGHT_DISTRIBUTION",
    "HINDSIGHT_SDK_VERSION",
    "HindsightClientAdapter",
    "HindsightClientError",
    "HindsightClientProtocol",
    "MISSION_UPDATE_FIELDS",
    "MissionClientProtocol",
    "MissionSnapshot",
    "MissionUpdateError",
    "MissionValue",
    "RetainConfirmation",
    "RetainSegment",
    "create_hindsight_client",
    "is_available",
]
