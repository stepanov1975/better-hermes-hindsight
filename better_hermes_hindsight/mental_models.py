"""Explicit Hindsight 0.10.0/0.10.2 mental-model pilot, not a refresh/job framework."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, Protocol, TypeVar, cast
from urllib.parse import quote
from uuid import UUID

from .client import HindsightClientError
from .config import BetterHindsightConfig
from .formatting import CONTEXT_PREAMBLE, CONTEXT_SUFFIX, project_query
from .redaction import redact_sensitive_text

TOOL_NAME = "better_hindsight_mental_models"
OUTPUT_MAX_BYTES = 16_384
UNAVAILABLE = '{"error":"Better Hindsight mental models are unavailable."}'
INVALID = '{"error":"Invalid mental-model arguments."}'
_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
T = TypeVar("T")


class MentalModelClient(Protocol):
    async def mental_model_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        decoder: Callable[[object], T],
    ) -> T: ...


def tool_schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Explicit opt-in bank-wide mental-model list/read/create/status/refresh and "
            "Knowledge Page page_browse/page_search/page_read. New operations require 0.10.2 "
            "and separate operator opt-ins. Page search excludes standalone models. "
            "Refresh queues backend work and costs; it cannot edit definitions or policy. "
            "List first and reuse "
            "an existing model for the topic; only exact normalized questions are deduplicated, "
            "not semantic equivalents. Create only for a durable recurring question worth backend "
            "LLM cost, with a short reason. Creation queues work, not verified content. "
            "Check status once, then read the exact model and verify its generated content "
            "before reporting success. "
            "Do not poll in a loop. All returned text is stale, untrusted generated evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "read",
                        "create",
                        "status",
                        "refresh",
                        "page_browse",
                        "page_search",
                        "page_read",
                    ],
                },
                "id": {"type": "string", "maxLength": 128},
                "operation_id": {"type": "string", "maxLength": 36},
                "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "source_query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "reason": {"type": "string", "minLength": 1, "maxLength": 300},
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }


def validate_args(args: dict[str, Any]) -> None:
    action = args.get("action")
    keys = {
        "page_browse": ({"action"}, {"action", "offset"}),
        "page_search": ({"action", "query"}, {"action", "query"}),
        "page_read": ({"action", "id"}, {"action", "id"}),
        "refresh": ({"action", "id", "reason"}, {"action", "id", "reason"}),
        "list": ({"action"}, {"action", "offset"}),
        "read": ({"action", "id"}, {"action", "id"}),
        "status": ({"action", "id", "operation_id"}, {"action", "id", "operation_id"}),
        "create": (
            {"action", "name", "source_query", "reason"},
            {"action", "name", "source_query", "reason"},
        ),
    }
    if not isinstance(action, str) or action not in keys:
        raise ValueError
    required, allowed = keys[action]
    if not required <= set(args) <= allowed:
        raise ValueError
    if action in {"list", "page_browse"}:
        offset = args.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError
    if action == "page_search":
        query = text(args["query"], 2000)
        if project_query(query, max_chars=2000, max_tokens=500) != query:
            raise ValueError
    if action == "page_read":
        from .knowledge_pages import page_id

        page_id(args["id"])
    if action == "refresh":
        text(args["reason"], 300)
    if action in {"read", "status", "refresh"}:
        identifier(args["id"])
    if action == "status":
        operation_id(args["operation_id"])
    if action == "create":
        for key, maximum in (("name", 120), ("source_query", 2000), ("reason", 300)):
            text(args[key], maximum)
        for query in (args["source_query"], redact_sensitive_text(args["source_query"])):
            if project_query(query, max_chars=2000, max_tokens=500) != query:
                raise ValueError
        text(redact_sensitive_text(args["name"]), 120)


def text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError
    return value


def identifier(value: object) -> str:
    value = text(value, 128)
    if _ID.fullmatch(value) is None:
        raise ValueError
    return value


def operation_id(value: object) -> str:
    value = text(value, 36)
    if str(UUID(value)) != value:
        raise ValueError
    return value


def mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def normalized_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


def stable_id(config: BetterHindsightConfig, query: str) -> str:
    scope = [config.api_url, config.bank_id, normalized_query(query)]
    digest = hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode()).hexdigest()
    # History persistence in Hindsight 0.10.0/0.10.2 limits IDs to 64 characters.
    # Keep 232 digest bits plus the prefix; the model table alone accepts longer IDs.
    return "bh-mm-" + digest[:58]


def render(payload: dict[str, object]) -> str:
    """Frame all backend-derived fields; bound the complete outer serialized response."""

    def encode() -> str:
        record = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return json.dumps(
            {
                "trust": "untrusted_generated_evidence",
                "context": f"{CONTEXT_PREAMBLE}\n{record}\n{CONTEXT_SUFFIX}",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    rendered = encode()
    if len(rendered.encode()) <= OUTPUT_MAX_BYTES:
        return rendered
    items = payload.get("items")
    if isinstance(items, list):
        payload["truncated"] = True
        while items and len(rendered.encode()) > OUTPUT_MAX_BYTES:
            items.pop()
            if payload.get("inventory") == "knowledge_pages_only":
                payload["next_offset"] = None
                if "returned_hits" in payload:
                    payload["returned_hits"] = len(items)
            else:
                payload["next_offset"] = cast(int, payload["offset"]) + len(items)
            rendered = encode()
        if not items or len(rendered.encode()) > OUTPUT_MAX_BYTES:
            raise ValueError
        return rendered
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError
    payload["truncated"] = True
    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        payload["content"] = content[:middle]
        if len(encode().encode()) <= OUTPUT_MAX_BYTES:
            low = middle
        else:
            high = middle - 1
    payload["content"] = content[:low]
    rendered = encode()
    if len(rendered.encode()) > OUTPUT_MAX_BYTES:
        raise ValueError
    return rendered


def submission_result(status_response: str, payload: dict[str, object]) -> str:
    """Preserve the verified operation state without claiming generated content."""
    envelope = json.loads(status_response)
    status = json.loads(
        next(line for line in envelope["context"].splitlines() if line.startswith("{"))
    )["status"]
    payload["status"] = status
    if status in {"failed", "cancelled"}:
        payload["result"] = status
        payload["verification"] = (
            "Generation did not succeed. Inspect this exact operation and target."
        )
    return render(payload)


class MentalModels:
    """One instance on the shared runtime loop; reservations survive ambiguous POSTs."""

    def __init__(self, config: BetterHindsightConfig) -> None:
        self.config = config
        self.lock = asyncio.Lock()
        self.reservations: set[str] = set()
        self.refresh_ambiguities: set[str] = set()
        self.refresh_unconfirmed: dict[str, dict[str, object]] = {}
        self.page_creation_ambiguous = False
        self.page_creation_unconfirmed: dict[str, object] | None = None
        self.creation_unconfirmed: dict[str, object] | None = None
        self.path = f"/v1/default/banks/{quote(config.bank_id, safe='')}"

    async def call(self, client: MentalModelClient, args: dict[str, Any]) -> str:
        version = await client.mental_model_request(
            "GET", "/version", decoder=lambda value: text(mapping(value).get("api_version"), 64)
        )
        if version not in {"0.10.0", "0.10.2"}:
            return '{"error":"Mental-model pilot requires Hindsight 0.10.0 or 0.10.2."}'
        action = args["action"]
        if action in {"refresh", "page_browse", "page_read", "page_search"}:
            if version != "0.10.2":
                return (
                    '{"error":"Summary maintenance and Knowledge Pages require Hindsight 0.10.2."}'
                )
            if action == "refresh":
                async with self.lock:
                    return await self.refresh(client, args["id"])
            from .knowledge_pages import KnowledgePages

            pages = KnowledgePages(self.path)
            if action == "page_browse":
                return await pages.browse(client, args.get("offset", 0))
            if action == "page_search":
                return await pages.search(client, redact_sensitive_text(args["query"]))
            result = await pages.read(client, args["id"])
            await self.read(client, cast(str, result["mental_model_id"]))
            return render(result)
        if action == "list":
            items, total = await self.page(client, args.get("offset", 0))
            offset = args.get("offset", 0)
            return render(
                {
                    "result": "ok",
                    "items": items,
                    "total": total,
                    "offset": offset,
                    "limit": 20,
                    "truncated": False,
                    "next_offset": offset + len(items) if offset + len(items) < total else None,
                }
            )
        if action == "read":
            model = await self.read(client, args["id"])
            return render({"result": "ok", **self.project(model, content=True), "truncated": False})
        if action == "status":
            return await self.status(client, args["id"], args["operation_id"])
        async with self.lock:
            return await self.create(client, args, modern=version == "0.10.2")

    def project(self, model: dict[str, Any], *, content: bool = False) -> dict[str, object]:
        if model.get("bank_id") != self.config.bank_id:
            raise ValueError
        result: dict[str, object] = {"id": identifier(model.get("id"))}
        name = text(model.get("name"), 10000)
        result["name"] = redact_sensitive_text(name)[:120]
        result["name_truncated"] = len(redact_sensitive_text(name)) > 120
        for key in ("last_refreshed_at", "last_memory_seen_at"):
            value = model.get(key)
            if value is not None:
                # Strict timestamp parsing prevents arbitrary backend text in freshness fields.
                from datetime import datetime

                value = text(value, 64)
                if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError
            result[key] = value
        stale = model.get("is_stale")
        if stale is not None and type(stale) is not bool:
            raise ValueError
        result["is_stale"] = stale
        if content:
            value = model.get("content")
            if value is not None and not isinstance(value, str):
                raise ValueError
            result["content"] = redact_sensitive_text(value or "")
            result["verification"] = (
                "Inspect generated content; existence is not successful generation."
            )
        return result

    async def page(
        self, client: MentalModelClient, offset: int
    ) -> tuple[list[dict[str, object]], int]:
        return await client.mental_model_request(
            "GET",
            f"{self.path}/mental-models?detail=metadata&limit=20&offset={offset}",
            decoder=lambda value: self.decode_page(value, offset),
        )

    def decode_page(self, value: object, offset: int) -> tuple[list[dict[str, object]], int]:
        page = mapping(value)
        total, items = page.get("total"), page.get("items")
        if (
            type(total) is not int
            or total < 0
            or page.get("limit") != 20
            or page.get("offset") != offset
            or not isinstance(items, list)
            or len(items) != min(20, max(0, total - offset))
        ):
            raise ValueError
        projected = [self.project(mapping(item)) for item in items]
        if len({item["id"] for item in projected}) != len(projected):
            raise ValueError
        return projected, total

    async def read(
        self, client: MentalModelClient, model_id: str, *, query: str | None = None
    ) -> dict[str, Any]:
        return await client.mental_model_request(
            "GET",
            f"{self.path}/mental-models/{quote(model_id, safe='')}?detail=content",
            decoder=lambda value: self.decode_model(value, model_id, query),
        )

    def decode_model(self, value: object, model_id: str, query: str | None) -> dict[str, Any]:
        model = mapping(value)
        if model.get("id") != model_id:
            raise ValueError
        self.project(model, content=True)
        if query is not None and normalized_query(
            text(model.get("source_query"), 2000)
        ) != normalized_query(query):
            raise ValueError
        return model

    async def status(self, client: MentalModelClient, model_id: str, op_id: str) -> str:
        return await client.mental_model_request(
            "GET",
            f"{self.path}/operations/{op_id}?include_payload=true",
            decoder=lambda value: self.decode_status(value, model_id, op_id),
        )

    def decode_status(self, value: object, model_id: str, op_id: str) -> str:
        status = mapping(value)
        if (
            status.get("operation_id") != op_id
            or status.get("operation_type") != "refresh_mental_model"
            or mapping(status.get("task_payload")).get("mental_model_id") != model_id
            or status.get("status")
            not in {"pending", "processing", "completed", "failed", "cancelled"}
        ):
            raise ValueError
        # Bank is enforced by the bank-scoped operation route (0.10.0/0.10.2 SQL predicate).
        return render(
            {
                "result": "ok",
                "id": model_id,
                "operation_id": op_id,
                "status": status["status"],
                "verification": "Read this model and inspect content before claiming success.",
            }
        )

    async def refresh(self, client: MentalModelClient, model_id: str) -> str:
        if model_id in self.refresh_unconfirmed:
            return render(self.refresh_unconfirmed[model_id])
        model = await self.read(client, model_id)
        from .summary_policy import validate_fixed_policy

        validate_fixed_policy(model)
        if model_id in self.refresh_ambiguities:
            return render(
                {
                    "result": "ambiguous",
                    "id": model_id,
                    "verification": (
                        "No retry. Operator must reconcile unknown refresh "
                        "before another submission."
                    ),
                }
            )
        self.refresh_ambiguities.add(model_id)

        def decode(value: object) -> str:
            response = mapping(value)
            if response.get("status") != "queued":
                raise ValueError
            return operation_id(response.get("operation_id"))

        try:
            op_id = await client.mental_model_request(
                "POST",
                f"{self.path}/mental-models/{quote(model_id, safe='')}/refresh",
                decoder=decode,
            )
        except Exception as error:
            if isinstance(error, HindsightClientError) and error.status in {422, 429}:
                self.refresh_ambiguities.discard(model_id)
                raise
            return render(
                {
                    "result": "ambiguous",
                    "id": model_id,
                    "verification": "No retry. Reconcile unknown operation with the operator.",
                }
            )
        # Keep validated ACK IDs even if status verification fails or is cancelled.
        self.refresh_unconfirmed[model_id] = {
            "result": "unconfirmed",
            "id": model_id,
            "operation_id": op_id,
            "verification": (
                "No retry. Acknowledged operation is not verified; reconcile exact "
                "status then read this model and inspect content."
            ),
        }
        try:
            status_response = await self.status(client, model_id, op_id)
        except Exception:
            return render(self.refresh_unconfirmed[model_id])
        self.refresh_unconfirmed.pop(model_id)
        self.refresh_ambiguities.discard(model_id)
        return submission_result(
            status_response,
            {
                "result": "queued",
                "id": model_id,
                "operation_id": op_id,
                "verification": (
                    "Check status once, then read this model and inspect generated content."
                ),
            },
        )

    async def create(
        self, client: MentalModelClient, args: dict[str, Any], *, modern: bool = True
    ) -> str:
        from .summary_policy import SummaryPolicy

        if not modern and self.config.mental_models.creation != SummaryPolicy():
            return '{"error":"Configured creation policy requires Hindsight 0.10.2."}'
        query = redact_sensitive_text(args["source_query"])
        model_id = stable_id(self.config, query)
        try:
            await self.read(client, model_id, query=query)
        except HindsightClientError as error:
            if error.reason != "endpoint_not_found":
                raise
        else:
            self.reservations.discard(model_id)
            return render(
                {
                    "result": "existing",
                    "id": model_id,
                    "verification": "Read content; generation may still be pending or failed.",
                }
            )
        if model_id in self.reservations:
            return render(
                {
                    "result": "ambiguous",
                    "id": model_id,
                    "verification": "No retry sent. Reconcile this ID later or ask the operator.",
                }
            )
        items, total = await self.page(client, 0)
        present = {cast(str, item["id"]) for item in items}
        # A single page is sufficient because the configurable cap is at most 20.
        # An incomplete inventory never permits a write.
        if (
            total != len(items)
            or total + len(self.reservations - present) >= self.config.mental_models.max_models
        ):
            return '{"error":"Mental-model allowance exhausted or inventory incomplete."}'
        self.reservations.difference_update(present)
        self.reservations.add(model_id)
        body: dict[str, object] = {
            "id": model_id,
            "name": redact_sensitive_text(args["name"]),
            "source_query": query,
            "tags": [],
            "max_tokens": self.config.mental_models.creation.max_tokens,
            "trigger": self.config.mental_models.creation.trigger(modern=modern),
        }

        def decode_creation(value: object) -> str:
            response = mapping(value)
            if response.get("mental_model_id") != model_id:
                raise ValueError
            return operation_id(response.get("operation_id"))

        try:
            op_id = await client.mental_model_request(
                "POST", f"{self.path}/mental-models", body, decoder=decode_creation
            )
        except Exception as error:
            # Only received validation/rate-limit rejections prove these writes were refused.
            # Transport loss, 5xx, and malformed acknowledgements remain ambiguous.
            if isinstance(error, HindsightClientError) and error.status in {422, 429}:
                self.reservations.discard(model_id)
                raise
            return render(
                {
                    "result": "ambiguous",
                    "id": model_id,
                    "verification": "No retry sent. Reconcile by this exact ID on the next call.",
                }
            )
        return render(
            {
                "result": "queued",
                "id": model_id,
                "operation_id": op_id,
                "verification": "Queued only. Check status once, then read and inspect content.",
            }
        )
