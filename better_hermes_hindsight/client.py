"""Narrow asynchronous HTTP client for the supported Hindsight server APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib.util import find_spec
from typing import Protocol, TypeVar, cast
from urllib.parse import quote

from . import __version__
from .config import BetterHindsightConfig, ObservationScopes, RecallConfig, ReflectConfig
from .telemetry import elapsed_milliseconds, emit_event

HINDSIGHT_REQUEST_TIMEOUT_SECONDS = 300.0
HINDSIGHT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
HINDSIGHT_MAX_RECALL_RESPONSE_BYTES = 2 * 1024 * 1024
HINDSIGHT_MAX_RECALL_RESULTS = 4096
HINDSIGHT_MAX_RECALL_NESTED_ITEMS = 4096
HINDSIGHT_MAX_REFLECT_RESPONSE_BYTES = 1024 * 1024
HINDSIGHT_MAX_REFLECT_TEXT_BYTES = 256 * 1024
MISSION_UPDATE_FIELDS = frozenset({"retain_mission", "observations_mission"})
HINDSIGHT_ERROR_REASONS = frozenset(
    {
        "authentication_failed",
        "bank_config_failed",
        "cancelled",
        "client_close_failed",
        "client_initialization_failed",
        "client_status",
        "connection_error",
        "dns_error",
        "endpoint_not_found",
        "malformed_json",
        "mission_update_failed",
        "non_json",
        "rate_limited",
        "recall_failed",
        "reflect_failed",
        "redirect",
        "response_oversized",
        "retain_failed",
        "schema_invalid",
        "server_status",
        "session_closed",
        "timeout",
        "tls_error",
        "transport_error",
        "unexpected_error",
        "unexpected_status",
    }
)

logger = logging.getLogger(__name__)
T = TypeVar("T")

_SAFE_TRACE_PHASE_NAMES = frozenset(
    {
        "backend_acquisition",
        "chunk_fetch",
        "combined_scoring",
        "connection_wait",
        "entity_build",
        "generate_query_embedding",
        "parallel_retrieval",
        "prefer_observations_dedup",
        "reranking",
        "result_serialization",
        "retrieval_bm25",
        "retrieval_graph",
        "retrieval_semantic",
        "retrieval_temporal",
        "rrf_merge",
        "semaphore_wait",
        "source_fact_fetch",
        "token_filtering",
        "trace_finalize",
    }
)
_SAFE_TRACE_DETAIL_NAMES = frozenset(
    {
        "bm25_count",
        "candidate_count",
        "candidates_merged",
        "candidates_reranked",
        "candidates_scored",
        "chunk_tokens",
        "chunks_returned",
        "diagnostic",
        "entities_returned",
        "graph_count",
        "max_tokens",
        "observations_considered",
        "result_count",
        "results_selected",
        "results_serialized",
        "semantic_count",
        "source_facts_returned",
        "temporal_count",
        "tokens_used",
    }
)
_MAX_TRACE_PHASES = 24
_MAX_TRACE_DETAILS = 8
_MAX_TRACE_DURATION_SECONDS = 86_400.0
_MAX_TRACE_DETAIL_NUMBER = 1_000_000_000


@dataclass(frozen=True, slots=True)
class RecallScores:
    """Strictly decoded ranking scores used by recall formatting."""

    final: float | int
    reranker: float | int | None = None
    semantic: float | int | None = None
    keyword: float | int | None = None


@dataclass(frozen=True, slots=True)
class RecallResult:
    """Allowlisted Hindsight recall result fields used by the provider."""

    id: str
    text: str
    type: str | None = None
    entities: list[str] | None = None
    context: str | None = None
    occurred_start: str | None = None
    occurred_end: str | None = None
    mentioned_at: str | None = None
    document_id: str | None = None
    metadata: dict[str, str] | None = None
    chunk_id: str | None = None
    tags: list[str] | None = None
    source_fact_ids: list[str] | None = None
    scores: RecallScores | None = None


@dataclass(frozen=True, slots=True)
class RecallPhaseMetric:
    """One privacy-safe Hindsight trace phase."""

    phase_name: str
    duration_seconds: float
    details: dict[str, float | int | bool]

    def as_dict(self) -> dict[str, object]:
        return {
            "details": dict(self.details),
            "duration_seconds": self.duration_seconds,
            "phase_name": self.phase_name,
        }


@dataclass(frozen=True, slots=True)
class RecallTrace:
    """Bounded trace summary with no query, IDs, candidates, or recalled text."""

    total_duration_seconds: float | None
    phase_metrics: tuple[RecallPhaseMetric, ...]
    collection_counts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "collection_counts": dict(self.collection_counts),
            "phase_metrics": [phase.as_dict() for phase in self.phase_metrics],
        }
        if self.total_duration_seconds is not None:
            payload["total_duration_seconds"] = self.total_duration_seconds
        return payload


@dataclass(frozen=True, slots=True)
class RecallResponse:
    """Narrow decoded recall response consumed by provider and formatter code."""

    results: list[RecallResult]
    source_facts: dict[str, RecallResult] | None = None
    trace: RecallTrace | None = None


@dataclass(frozen=True, slots=True)
class ReflectResponse:
    """Strictly decoded reflection text consumed by the provider formatter."""

    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RetainSegment:
    """One immutable outbox segment ready for Hindsight delivery."""

    content: str
    document_id: str
    payload_schema: str
    source_sha256: str
    segment_index: int
    segment_count: int
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class RetainConfirmation:
    """Typed proof that one synchronous retain response was confirmed."""

    confirmed: bool


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """Bounded JSON response metadata passed across the internal transport seam."""

    payload: object = field(repr=False)
    response_bytes: int
    status: int


@dataclass(frozen=True, slots=True)
class MissionValue:
    """Presence-preserving mission value from the bank config."""

    present: bool
    value: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class MissionSnapshot:
    """Allowlisted bank mission fields used for exact operator readback."""

    retain_mission: MissionValue
    observations_mission: MissionValue


class HindsightClientError(RuntimeError):
    """Sanitized fixed-category client failure safe for logs and operator output."""

    __slots__ = ("category", "reason")

    def __init__(self, category: str, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.category = category
        candidate = category if reason is None else reason
        self.reason = candidate if candidate in HINDSIGHT_ERROR_REASONS else "unexpected_error"


class _JsonTransportError(RuntimeError):
    """Fixed transport outcome with no raw response or exception detail."""

    __slots__ = ("reason", "response_bytes", "status")

    def __init__(
        self,
        reason: str,
        *,
        response_bytes: int = 0,
        status: int | None = None,
    ) -> None:
        super().__init__("Hindsight transport failed.")
        self.reason = reason
        self.response_bytes = response_bytes
        self.status = status


class MissionUpdateError(ValueError):
    """Raised before transport when a mission update violates the narrow policy."""


class HindsightClientProtocol(Protocol):
    """Complete Hindsight surface used by the shared process runtime."""

    async def recall(self, query: str) -> object: ...

    async def retain_segment(self, segment: RetainSegment) -> RetainConfirmation: ...

    async def get_bank_config(self) -> object: ...

    async def update_bank_missions(self, updates: Mapping[str, str]) -> None: ...

    async def close(self) -> None: ...


class DiagnosticRecallClientProtocol(Protocol):
    """Operator-only trace replay surface."""

    async def replay_recall(self, query: str, request: Mapping[str, object]) -> RecallResponse: ...


class ReflectClientProtocol(Protocol):
    """Bounded explicit reflection surface used by the process runtime."""

    async def reflect(self, query: str) -> ReflectResponse: ...


class MissionClientProtocol(Protocol):
    """Exact typed mission surface used by explicit management commands."""

    async def get_bank_config(self) -> MissionSnapshot: ...

    async def update_bank_missions(self, updates: Mapping[str, str]) -> None: ...


class JsonTransportProtocol(Protocol):
    """Internal JSON transport boundary used by the adapter and deterministic tests."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
    ) -> JsonResponse: ...

    async def close(self) -> None: ...


class JsonTransportFactory(Protocol):
    """Constructor shape for the internal JSON transport."""

    def __call__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout: float,
        user_agent: str,
    ) -> JsonTransportProtocol: ...


class _AiohttpJsonTransport:
    """One bounded, no-redirect aiohttp session for JSON requests."""

    __slots__ = ("_base_url", "_session")

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout: float,
        user_agent: str,
    ) -> None:
        import aiohttp

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        }
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        self._base_url = base_url.rstrip("/")
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            trust_env=False,
        )

    def __repr__(self) -> str:
        return "_AiohttpJsonTransport()"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
    ) -> JsonResponse:
        import aiohttp

        if self._session.closed:
            raise _JsonTransportError("session_closed")
        response_limit = (
            HINDSIGHT_MAX_RESPONSE_BYTES if max_response_bytes is None else max_response_bytes
        )
        if (
            type(response_limit) is not int
            or not 0 < response_limit <= HINDSIGHT_MAX_RESPONSE_BYTES
        ):
            raise _JsonTransportError("transport_error")
        try:
            if timeout_seconds is None:
                request = self._session.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json_body,
                    allow_redirects=False,
                )
            else:
                request = self._session.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json_body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                )
            async with request as response:
                status = response.status
                if status != 200:
                    response.close()
                    raise _JsonTransportError(_status_outcome(status), status=status)
                media_type = (
                    response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
                )
                if media_type != "application/json" and not media_type.endswith("+json"):
                    response.close()
                    raise _JsonTransportError("non_json", status=status)
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > response_limit:
                        response.close()
                        raise _JsonTransportError(
                            "response_oversized",
                            response_bytes=len(body),
                            status=status,
                        )
                try:
                    payload = cast(object, json.loads(body))
                except json.JSONDecodeError:
                    raise _JsonTransportError(
                        "malformed_json",
                        response_bytes=len(body),
                        status=status,
                    ) from None
                return JsonResponse(
                    payload=payload,
                    response_bytes=len(body),
                    status=status,
                )
        except asyncio.CancelledError:
            raise
        except _JsonTransportError:
            raise
        except TimeoutError:
            raise _JsonTransportError("timeout") from None
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
            raise _JsonTransportError("tls_error") from None
        except aiohttp.ClientConnectorDNSError:
            raise _JsonTransportError("dns_error") from None
        except aiohttp.ClientConnectionError:
            raise _JsonTransportError("connection_error") from None
        except aiohttp.ClientError:
            raise _JsonTransportError("transport_error") from None
        except Exception:
            raise _JsonTransportError("transport_error") from None

    async def close(self) -> None:
        await self._session.close()


def _status_outcome(status: int) -> str:
    if 300 <= status < 400:
        return "redirect"
    if status in (401, 403):
        return "authentication_failed"
    if status == 404:
        return "endpoint_not_found"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "client_status"
    if 500 <= status < 600:
        return "server_status"
    return "unexpected_status"


def is_available() -> bool:
    """Return whether both installed runtime dependencies are importable."""

    try:
        return all(find_spec(module) is not None for module in ("aiohttp", "tiktoken"))
    except (ImportError, ValueError):
        return False


def create_hindsight_client(
    config: BetterHindsightConfig,
    *,
    transport_factory: JsonTransportFactory | None = None,
) -> HindsightClientAdapter:
    """Construct the process runtime's narrow Hindsight HTTP client on its owning event loop."""

    factory = transport_factory or _AiohttpJsonTransport
    try:
        transport = factory(
            base_url=config.api_url,
            api_key=config.api_key,
            timeout=HINDSIGHT_REQUEST_TIMEOUT_SECONDS,
            user_agent=f"better-hermes-hindsight/{__version__}",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        emit_event(
            logger,
            "better_hindsight.client_lifecycle",
            outcome="initialization_failed",
        )
        raise HindsightClientError(
            "client_initialization_failed",
            "Better Hindsight client initialization failed.",
        ) from None
    emit_event(logger, "better_hindsight.client_lifecycle", outcome="initialized")
    return HindsightClientAdapter(config=config, transport=transport)


class HindsightClientAdapter:
    """Narrow provider adapter over one internal JSON transport."""

    __slots__ = (
        "_bank_id",
        "_bank_path",
        "_recall_config",
        "_reflect_config",
        "_retain_scopes",
        "_retain_tags",
        "_transport",
    )

    def __init__(self, *, config: BetterHindsightConfig, transport: JsonTransportProtocol) -> None:
        self._bank_id = config.bank_id
        self._bank_path = f"/v1/default/banks/{quote(config.bank_id, safe='')}"
        self._recall_config = config.recall
        self._reflect_config = config.reflect
        self._retain_scopes = config.retain.observation_scopes
        self._retain_tags = config.retain.tags
        self._transport = transport

    def __repr__(self) -> str:
        return "HindsightClientAdapter()"

    async def recall(self, query: str) -> RecallResponse:
        """Recall current-query memories with the exact supported request defaults."""

        payload = _recall_body(query, self._recall_config)
        return await self._perform_recall(payload, timeout_seconds=None)

    async def recall_with_timeout(self, query: str, *, timeout_seconds: float) -> RecallResponse:
        """Recall with a smaller caller-owned native transport deadline."""

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise HindsightClientError(
                "recall_failed",
                "Better Hindsight recall failed.",
                reason="timeout",
            )
        payload = _recall_body(query, self._recall_config)
        return await self._perform_recall(payload, timeout_seconds=timeout_seconds)

    async def replay_recall(self, query: str, request: Mapping[str, object]) -> RecallResponse:
        """Replay one plugin-recorded request with Hindsight trace collection enabled."""

        if not _diagnostic_request_matches_current(request, self._recall_config):
            raise HindsightClientError(
                "recall_failed",
                "Better Hindsight recall failed.",
                reason="schema_invalid",
            )
        payload = _diagnostic_recall_body(query, request)
        return await self._perform_recall(payload, timeout_seconds=None)

    async def _perform_recall(
        self,
        payload: Mapping[str, object],
        *,
        timeout_seconds: float | None,
    ) -> RecallResponse:
        return await _observed_http_call(
            operation="recall",
            category="recall_failed",
            message="Better Hindsight recall failed.",
            call=lambda: self._transport.request(
                "POST",
                f"{self._bank_path}/memories/recall",
                json_body=payload,
                timeout_seconds=timeout_seconds,
                max_response_bytes=HINDSIGHT_MAX_RECALL_RESPONSE_BYTES,
            ),
            decoder=_decode_recall_response,
        )

    async def reflect(self, query: str) -> ReflectResponse:
        """Generate a bounded read-only reflection using the configured bank policy."""

        payload = _reflect_body(query, self._reflect_config)
        return await self._perform_reflect(payload, timeout_seconds=None)

    async def reflect_with_timeout(self, query: str, *, timeout_seconds: float) -> ReflectResponse:
        """Reflect with a smaller caller-owned native transport deadline."""

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise HindsightClientError(
                "reflect_failed",
                "Better Hindsight reflection failed.",
                reason="timeout",
            )
        payload = _reflect_body(query, self._reflect_config)
        return await self._perform_reflect(payload, timeout_seconds=timeout_seconds)

    async def _perform_reflect(
        self,
        payload: Mapping[str, object],
        *,
        timeout_seconds: float | None,
    ) -> ReflectResponse:
        return await _observed_http_call(
            operation="reflect",
            category="reflect_failed",
            message="Better Hindsight reflection failed.",
            call=lambda: self._transport.request(
                "POST",
                f"{self._bank_path}/reflect",
                json_body=payload,
                timeout_seconds=timeout_seconds,
                max_response_bytes=HINDSIGHT_MAX_REFLECT_RESPONSE_BYTES,
            ),
            decoder=_decode_reflect_response,
        )

    async def retain_segment(self, segment: RetainSegment) -> RetainConfirmation:
        """Synchronously retain one stable replace-mode segment and validate confirmation."""

        item: dict[str, object] = {
            "content": segment.content,
            "timestamp": segment.timestamp,
            "context": None,
            "metadata": {
                "better_hindsight_payload_schema": segment.payload_schema,
                "better_hindsight_segment_count": str(segment.segment_count),
                "better_hindsight_segment_index": str(segment.segment_index),
                "better_hindsight_source_sha256": segment.source_sha256,
            },
            "document_id": segment.document_id,
            "entities": None,
            "tags": list(self._retain_tags),
            "observation_scopes": _observation_scopes_wire(self._retain_scopes),
            "strategy": None,
            "update_mode": "replace",
        }
        return await _observed_http_call(
            operation="retain",
            category="retain_failed",
            message="Better Hindsight retain failed.",
            call=lambda: self._transport.request(
                "POST",
                f"{self._bank_path}/memories",
                json_body={"items": [item], "async": False, "document_tags": None},
            ),
            decoder=lambda response: _decode_retain_confirmation(
                response,
                bank_id=self._bank_id,
            ),
        )

    async def get_bank_config(self) -> MissionSnapshot:
        """Read and exactly validate allowlisted mission values."""

        return await _observed_http_call(
            operation="bank_config_get",
            category="bank_config_failed",
            message="Better Hindsight bank configuration read failed.",
            call=lambda: self._transport.request("GET", f"{self._bank_path}/config"),
            decoder=lambda response: _decode_bank_config(response, bank_id=self._bank_id),
        )

    async def update_bank_missions(self, updates: Mapping[str, str]) -> None:
        """Patch only non-empty allowlisted mission fields; exact readback remains caller-owned."""

        copied = _validate_mission_updates(updates)
        await _observed_http_call(
            operation="bank_config_patch",
            category="mission_update_failed",
            message="Better Hindsight mission update failed.",
            call=lambda: self._transport.request(
                "PATCH",
                f"{self._bank_path}/config",
                json_body={"updates": copied},
            ),
            decoder=_decode_mission_update,
        )

    async def close(self) -> None:
        """Close the owned transport on the caller's event loop."""

        started_at = time.monotonic()
        try:
            await _mapped_call("client_close", self._transport.close)
        except asyncio.CancelledError:
            emit_event(
                logger,
                "better_hindsight.client_lifecycle",
                elapsed_ms=elapsed_milliseconds(started_at, time.monotonic()),
                outcome="close_cancelled",
            )
            raise
        except HindsightClientError:
            emit_event(
                logger,
                "better_hindsight.client_lifecycle",
                elapsed_ms=elapsed_milliseconds(started_at, time.monotonic()),
                outcome="close_failed",
            )
            raise
        emit_event(
            logger,
            "better_hindsight.client_lifecycle",
            elapsed_ms=elapsed_milliseconds(started_at, time.monotonic()),
            outcome="closed",
        )


def _recall_body(query: str, config: RecallConfig) -> dict[str, object]:
    source_facts: dict[str, object] | None = None
    if config.include_source_facts:
        source_facts = {
            "max_tokens": config.max_source_facts_tokens or 4096,
            "max_tokens_per_observation": -1,
        }
    min_scores: dict[str, object] | None = None
    if config.min_scores is not None:
        scores = config.min_scores.as_dict()
        min_scores = {
            "semantic": scores.get("semantic"),
            "keyword": scores.get("keyword"),
            "reranker": scores.get("reranker"),
            "final": scores.get("final"),
        }
    return {
        "query": query,
        "types": list(config.types) if config.types is not None else None,
        "prefer_observations": config.prefer_observations or False,
        "budget": config.budget or "mid",
        "max_tokens": config.max_tokens or 4096,
        "trace": False,
        "query_timestamp": None,
        "include": {"entities": None, "chunks": None, "source_facts": source_facts},
        "tags": list(config.tags) if config.tags is not None else None,
        "tags_match": config.tag_mode or "any",
        "tag_groups": None,
        "min_scores": min_scores,
    }


def _reflect_body(query: str, config: ReflectConfig) -> dict[str, object]:
    return {
        "query": query,
        "budget": config.budget,
        "max_tokens": config.max_tokens,
        "tags": list(config.tags) if config.tags is not None else None,
        "tags_match": config.tag_mode or "any",
    }


def recall_request_parameters(config: RecallConfig) -> dict[str, object]:
    """Return the exact credential-free request parameters used for replay capture."""

    payload = _recall_body("", config)
    del payload["query"]
    return payload


def _diagnostic_request_matches_current(
    request: Mapping[str, object], config: RecallConfig
) -> bool:
    if type(request) is not dict:
        return False
    try:
        captured = json.dumps(
            request,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        current = json.dumps(
            recall_request_parameters(config),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return False
    return captured == current


def _diagnostic_recall_body(query: str, request: Mapping[str, object]) -> dict[str, object]:
    expected = set(recall_request_parameters(RecallConfig()))
    if set(request) != expected or "query" in request:
        raise HindsightClientError(
            "recall_failed", "Better Hindsight recall failed.", reason="schema_invalid"
        )
    payload = dict(request)
    payload["query"] = query
    payload["trace"] = True
    return payload


def _decode_retain_confirmation(value: object, *, bank_id: str) -> RetainConfirmation:
    payload = _exact_dict(value)
    success = _required_exact(payload, "success", bool)
    response_bank_id = _required_exact(payload, "bank_id", str)
    items_count = _required_exact(payload, "items_count", int)
    asynchronous = _required_exact(payload, "async", bool)
    return RetainConfirmation(
        confirmed=(
            success is True
            and response_bank_id == bank_id
            and items_count == 1
            and asynchronous is False
        )
    )


def _decode_bank_config(value: object, *, bank_id: str) -> MissionSnapshot:
    _, response_bank_id, config = _validate_bank_config_response_shape(value)
    if response_bank_id != bank_id:
        raise TypeError
    return MissionSnapshot(
        retain_mission=_mission_value(config, "retain_mission"),
        observations_mission=_mission_value(config, "observations_mission"),
    )


def _decode_mission_update(value: object) -> None:
    _validate_bank_config_response_shape(value)


def _decode_recall_response(value: object) -> RecallResponse:
    payload = _exact_dict(value)
    raw_results = _required_exact(payload, "results", list)
    if len(raw_results) > HINDSIGHT_MAX_RECALL_RESULTS:
        raise TypeError
    results = [_decode_recall_result(item) for item in raw_results]
    raw_source_facts = payload.get("source_facts")
    source_facts: dict[str, RecallResult] | None
    if raw_source_facts is None:
        source_facts = None
    else:
        facts = _exact_dict(raw_source_facts)
        if len(facts) > HINDSIGHT_MAX_RECALL_RESULTS:
            raise TypeError
        source_facts = {}
        for key, item in facts.items():
            if type(key) is not str:
                raise TypeError
            source_facts[key] = _decode_recall_result(item)
    return RecallResponse(
        results=results,
        source_facts=source_facts,
        trace=_decode_recall_trace(payload.get("trace")),
    )


def _decode_reflect_response(value: object) -> ReflectResponse:
    payload = _exact_dict(value)
    text = _required_exact(payload, "text", str)
    if not text.strip() or len(text.encode("utf-8")) > HINDSIGHT_MAX_REFLECT_TEXT_BYTES:
        raise TypeError
    return ReflectResponse(text=text)


def _decode_recall_trace(value: object) -> RecallTrace | None:
    """Best-effort safe projection; trace drift must never invalidate recall results."""

    if type(value) is not dict:
        return None
    trace = value
    total: float | None = None
    phases: list[RecallPhaseMetric] = []
    summary = trace.get("summary")
    if type(summary) is dict:
        candidate_total = _optional_finite_number(summary.get("total_duration_seconds"))
        if candidate_total is not None and 0 <= candidate_total <= _MAX_TRACE_DURATION_SECONDS:
            total = candidate_total
        phases = _decode_phase_metrics(summary.get("phase_metrics"))

    collections: dict[str, int] = {}
    for name in (
        "entry_points",
        "rrf_merged",
        "reranked",
        "pruned",
        "final_results",
        "visits",
    ):
        collection = trace.get(name)
        if isinstance(collection, (list, dict)):
            collections[name] = min(len(collection), 1_000_000)
    retrieval = trace.get("retrieval_results")
    if isinstance(retrieval, (list, dict)):
        collections["retrieval_methods"] = min(len(retrieval), 1_000_000)
        collections["retrieval_candidates"] = _retrieval_candidate_count(retrieval)
    if total is None and not phases and not collections:
        return None
    return RecallTrace(
        total_duration_seconds=total,
        phase_metrics=tuple(phases),
        collection_counts=collections,
    )


def _retrieval_candidate_count(value: list[object] | dict[object, object]) -> int:
    collections: Iterable[object] = value.values() if isinstance(value, dict) else value
    total = 0
    for index, item in enumerate(collections):
        if index >= HINDSIGHT_MAX_RECALL_NESTED_ITEMS:
            break
        if not isinstance(item, dict):
            continue
        results = item.get("results")
        if isinstance(results, (list, dict)):
            total = min(1_000_000, total + len(results))
    return total


def _decode_phase_metrics(value: object) -> list[RecallPhaseMetric]:
    raw_items: list[object]
    if type(value) is list:
        raw_items = list(value[:_MAX_TRACE_PHASES])
    elif type(value) is dict:
        raw_items = []
        for phase_name in _SAFE_TRACE_PHASE_NAMES:
            phase_value = value.get(phase_name)
            if type(phase_value) is dict:
                item = dict(phase_value)
                item["phase_name"] = phase_name
                raw_items.append(item)
            elif type(phase_value) in (int, float):
                raw_items.append(
                    {"phase_name": phase_name, "duration_seconds": phase_value, "details": {}}
                )
            if len(raw_items) >= _MAX_TRACE_PHASES:
                break
    else:
        return []

    phases: list[RecallPhaseMetric] = []
    for raw_item in raw_items:
        if type(raw_item) is not dict:
            continue
        name = raw_item.get("phase_name")
        duration = _optional_finite_number(raw_item.get("duration_seconds"))
        if (
            type(name) is not str
            or name not in _SAFE_TRACE_PHASE_NAMES
            or duration is None
            or not 0 <= duration <= _MAX_TRACE_DURATION_SECONDS
        ):
            continue
        details: dict[str, float | int | bool] = {}
        raw_details = raw_item.get("details")
        if type(raw_details) is dict:
            for detail_name in sorted(_SAFE_TRACE_DETAIL_NAMES):
                detail_value = _safe_trace_detail(raw_details.get(detail_name))
                if detail_value is not None:
                    details[detail_name] = detail_value
                if len(details) >= _MAX_TRACE_DETAILS:
                    break
        phases.append(
            RecallPhaseMetric(
                phase_name=name,
                duration_seconds=duration,
                details=details,
            )
        )
    return phases


def _safe_trace_detail(value: object) -> float | int | bool | None:
    if type(value) is bool:
        return value
    if type(value) is int:
        return value if abs(value) <= _MAX_TRACE_DETAIL_NUMBER else None
    if type(value) is float:
        return value if math.isfinite(value) and abs(value) <= _MAX_TRACE_DETAIL_NUMBER else None
    return None


def _optional_finite_number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(cast(float | int, value))
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _decode_recall_result(value: object) -> RecallResult:
    payload = _exact_dict(value)
    return RecallResult(
        id=_required_exact(payload, "id", str),
        text=_required_exact(payload, "text", str),
        type=_optional_exact(payload, "type", str),
        entities=_optional_string_list(payload, "entities"),
        context=_optional_exact(payload, "context", str),
        occurred_start=_optional_exact(payload, "occurred_start", str),
        occurred_end=_optional_exact(payload, "occurred_end", str),
        mentioned_at=_optional_exact(payload, "mentioned_at", str),
        document_id=_optional_exact(payload, "document_id", str),
        metadata=_optional_string_dict(payload, "metadata"),
        chunk_id=_optional_exact(payload, "chunk_id", str),
        tags=_optional_string_list(payload, "tags"),
        source_fact_ids=_optional_string_list(payload, "source_fact_ids"),
        scores=_optional_scores(payload.get("scores")),
    )


def _optional_scores(value: object) -> RecallScores | None:
    if value is None:
        return None
    payload = _exact_dict(value)
    return RecallScores(
        final=_required_number(payload, "final"),
        reranker=_optional_number(payload, "reranker"),
        semantic=_optional_number(payload, "semantic"),
        keyword=_optional_number(payload, "keyword"),
    )


def _exact_dict(value: object) -> dict[object, object]:
    if type(value) is not dict:
        raise TypeError
    return cast(dict[object, object], value)


def _required_exact(payload: Mapping[object, object], key: str, kind: type[T]) -> T:
    value = payload[key]
    if type(value) is not kind:
        raise TypeError
    return value


def _optional_exact(payload: Mapping[object, object], key: str, kind: type[T]) -> T | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not kind:
        raise TypeError
    return value


def _required_number(payload: Mapping[object, object], key: str) -> float | int:
    value = payload[key]
    if type(value) not in (float, int):
        raise TypeError
    return cast(float | int, value)


def _optional_number(payload: Mapping[object, object], key: str) -> float | int | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) not in (float, int):
        raise TypeError
    return cast(float | int, value)


def _optional_string_list(payload: Mapping[object, object], key: str) -> list[str] | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not list or len(value) > HINDSIGHT_MAX_RECALL_NESTED_ITEMS:
        raise TypeError
    if any(type(item) is not str for item in value):
        raise TypeError
    return cast(list[str], value)


def _optional_string_dict(payload: Mapping[object, object], key: str) -> dict[str, str] | None:
    value = payload.get(key)
    if value is None:
        return None
    if (
        type(value) is not dict
        or len(value) > HINDSIGHT_MAX_RECALL_NESTED_ITEMS
        or any(
            type(item_key) is not str or type(item_value) is not str
            for item_key, item_value in value.items()
        )
    ):
        raise TypeError
    return cast(dict[str, str], value)


def _mission_value(config: Mapping[object, object], field: str) -> MissionValue:
    if field not in config:
        return MissionValue(present=False, value=None)
    value = config[field]
    if value is not None and type(value) is not str:
        raise TypeError
    return MissionValue(present=True, value=value)


def _validate_bank_config_response_shape(
    payload: object,
) -> tuple[dict[object, object], str, dict[object, object]]:
    """Validate required BankConfigResponse fields formerly checked by the SDK."""

    mapping = _exact_dict(payload)
    bank_id = _required_exact(mapping, "bank_id", str)
    config = _required_exact(mapping, "config", dict)
    _required_exact(mapping, "overrides", dict)
    return mapping, bank_id, config


def _validate_mission_updates(updates: Mapping[str, str]) -> dict[str, str]:
    if type(updates) is not dict or not updates or not set(updates).issubset(MISSION_UPDATE_FIELDS):
        raise MissionUpdateError("Mission updates must contain only allowlisted changed fields.")
    copied: dict[str, str] = {}
    for mission_field, value in updates.items():
        if type(mission_field) is not str or type(value) is not str or not value.strip():
            raise MissionUpdateError("Mission updates require non-empty string values.")
        copied[mission_field] = value
    return copied


def _observation_scopes_wire(scopes: ObservationScopes) -> str | list[list[str]] | None:
    if scopes is None or isinstance(scopes, str):
        return scopes
    return [list(scope) for scope in scopes]


async def _mapped_call(operation: str, call: Callable[[], Awaitable[T]]) -> T:
    category, message = {
        "recall": ("recall_failed", "Better Hindsight recall failed."),
        "retain": ("retain_failed", "Better Hindsight retain failed."),
        "mission_read": (
            "bank_config_failed",
            "Better Hindsight bank configuration read failed.",
        ),
        "mission_update": ("mission_update_failed", "Better Hindsight mission update failed."),
        "client_close": ("client_close_failed", "Better Hindsight client close failed."),
    }[operation]
    try:
        return await call()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HindsightClientError(category, message) from None


async def _observed_http_call(
    *,
    operation: str,
    category: str,
    message: str,
    call: Callable[[], Awaitable[JsonResponse]],
    decoder: Callable[[object], T],
) -> T:
    started_at = time.monotonic()
    try:
        response = await call()
    except asyncio.CancelledError:
        _emit_http_event(started_at=started_at, operation=operation, outcome="cancelled")
        raise
    except _JsonTransportError as error:
        _emit_http_event(
            started_at=started_at,
            operation=operation,
            outcome=error.reason,
            response_bytes=error.response_bytes,
            status=error.status,
        )
        raise HindsightClientError(category, message, reason=error.reason) from None
    except Exception:
        _emit_http_event(started_at=started_at, operation=operation, outcome="transport_error")
        raise HindsightClientError(category, message, reason="transport_error") from None
    try:
        result = decoder(response.payload)
    except asyncio.CancelledError:
        _emit_http_event(
            started_at=started_at,
            operation=operation,
            outcome="cancelled",
            response_bytes=response.response_bytes,
            status=response.status,
        )
        raise
    except Exception:
        _emit_http_event(
            started_at=started_at,
            operation=operation,
            outcome="schema_invalid",
            response_bytes=response.response_bytes,
            status=response.status,
        )
        raise HindsightClientError(category, message, reason="schema_invalid") from None
    _emit_http_event(
        started_at=started_at,
        operation=operation,
        outcome="success",
        response_bytes=response.response_bytes,
        status=response.status,
    )
    return result


def _emit_http_event(
    *,
    started_at: float,
    operation: str,
    outcome: str,
    response_bytes: int = 0,
    status: int | None = None,
) -> None:
    fields: dict[str, object] = {
        "elapsed_ms": elapsed_milliseconds(started_at, time.monotonic()),
        "operation": operation,
        "outcome": outcome,
        "response_bytes": max(0, response_bytes),
    }
    if status is not None:
        fields["status"] = status
    emit_event(logger, "better_hindsight.http_request", **fields)


__all__ = [
    "DiagnosticRecallClientProtocol",
    "HINDSIGHT_MAX_REFLECT_RESPONSE_BYTES",
    "HINDSIGHT_MAX_REFLECT_TEXT_BYTES",
    "HINDSIGHT_MAX_RECALL_NESTED_ITEMS",
    "HINDSIGHT_MAX_RECALL_RESPONSE_BYTES",
    "HINDSIGHT_MAX_RECALL_RESULTS",
    "HINDSIGHT_REQUEST_TIMEOUT_SECONDS",
    "HindsightClientAdapter",
    "HindsightClientError",
    "HindsightClientProtocol",
    "JsonResponse",
    "JsonTransportFactory",
    "JsonTransportProtocol",
    "MissionClientProtocol",
    "MissionSnapshot",
    "MissionUpdateError",
    "MissionValue",
    "RecallPhaseMetric",
    "RecallResponse",
    "RecallResult",
    "RecallScores",
    "RecallTrace",
    "ReflectClientProtocol",
    "ReflectResponse",
    "RetainConfirmation",
    "RetainSegment",
    "create_hindsight_client",
    "is_available",
    "recall_request_parameters",
]
