"""Explicit operator summary/page lifecycle, with exact-target verification."""

from __future__ import annotations

import asyncio
import json
from argparse import ArgumentParser, Namespace
from dataclasses import asdict
from typing import Any, cast
from urllib.parse import quote

from .client import HindsightClientError, HindsightClientProtocol
from .config import BetterHindsightConfig
from .knowledge_pages import KnowledgePages, page_id
from .management import ManagementResult
from .mental_models import (
    MentalModelClient,
    MentalModels,
    identifier,
    mapping,
    operation_id,
    render,
    submission_result,
    text,
    validate_args,
)
from .redaction import redact_sensitive_text
from .runtime import create_operator_runtime
from .summary_policy import SummaryPolicy, parse_policy, validate_fixed_policy

POLICY_FLAGS = {
    "mode": "mode",
    "budget": "budget",
    "refresh_cron": "refresh_cron",
    "refresh_after_consolidation": "refresh_after_consolidation",
    "min_refresh_interval_seconds": "min_refresh_interval_seconds",
    "max_tokens": "max_tokens",
    "recall_max_tokens": "recall_max_tokens",
    "observations_max_tokens": "reflect_search_observations_max_tokens",
}


def register_commands(commands: Any) -> None:
    for family, actions in (
        ("summaries", ("list", "inspect", "create", "edit", "refresh", "status", "delete")),
        ("pages", ("browse", "search", "read", "create")),
    ):
        group = commands.add_parser(
            family, help="Explicit bounded summary/page operations", allow_abbrev=False
        )
        subs = group.add_subparsers(dest="summary_action", required=True)
        for action in actions:
            parser: ArgumentParser = subs.add_parser(action, allow_abbrev=False)
            if action in {"inspect", "read", "edit", "refresh", "status", "delete"}:
                parser.add_argument("id")
            if action == "status":
                parser.add_argument("operation_id")
            if action in {"list", "browse"}:
                parser.add_argument("--offset", type=int, default=0)
            if action == "search":
                parser.add_argument("query")
            if action in {"create", "edit"}:
                parser.add_argument("--name", required=action == "create")
                parser.add_argument("--source-query", required=action == "create")
                parser.add_argument("--mode", choices=("full", "delta"))
                parser.add_argument("--budget", choices=("low", "mid", "high"))
                trigger = parser.add_mutually_exclusive_group()
                trigger.add_argument(
                    "--refresh-cron", help="Five-field UTC cron; use 'none' to disable"
                )
                trigger.add_argument(
                    "--after-consolidation",
                    dest="refresh_after_consolidation",
                    choices=("true", "false"),
                )
                for flag in (
                    "min-refresh-interval-seconds",
                    "max-tokens",
                    "recall-max-tokens",
                    "observations-max-tokens",
                ):
                    parser.add_argument("--" + flag, type=int)
            if action == "create":
                parser.add_argument("--confirm", action="store_true", required=True)
                if family == "pages":
                    parser.add_argument("--parent-id")
            if action in {"edit", "refresh", "delete"}:
                parser.add_argument(
                    "--confirm",
                    required=True,
                    help=(
                        "Repeat the exact mental-model ID. Delete cascades to "
                        "its page; no folder deletion."
                    ),
                )


def namespace_arguments(args: Namespace) -> dict[str, Any]:
    family = args.better_hindsight_action
    action = args.summary_action
    result: dict[str, Any] = {
        "action": {
            "inspect": "inspect",
            "browse": "page_browse",
            "search": "page_search",
            "read": "page_read",
        }.get(action, action)
    }
    if family == "pages" and action == "create":
        result["action"] = "page_create"
    for key in (
        "id",
        "operation_id",
        "offset",
        "query",
        "name",
        "source_query",
        "confirm",
        "parent_id",
    ):
        value = getattr(args, key, None)
        if value is not None:
            result[key] = value
    changes: dict[str, object] = {}
    for key in POLICY_FLAGS:
        value = getattr(args, key, None)
        if value is not None:
            if key == "refresh_cron" and value == "none":
                value = None
            if key == "refresh_after_consolidation":
                value = value == "true"
            changes[key] = value
    # Explicitly clear the other automatic trigger when switching modes.
    if (
        "refresh_cron" in changes
        and changes["refresh_cron"] is not None
        and "refresh_after_consolidation" not in changes
    ):
        changes["refresh_after_consolidation"] = False
    if changes.get("refresh_after_consolidation") is True and "refresh_cron" not in changes:
        changes["refresh_cron"] = None
    if changes:
        result["policy"] = changes
    return result


def run_operator(config: BetterHindsightConfig, args: dict[str, Any]) -> ManagementResult:
    """One finite command, no sender, automatic retry, or persistent policy sync."""
    command = "summary_" + str(args.get("action"))
    if not config.authorize_cli().identity_authorized or not config.mental_models.enabled:
        return ManagementResult(
            {"command": command, "result": "error", "error": "summary_unavailable"}, 3
        )
    try:
        validate_operator(config, args)
    except Exception:
        return ManagementResult(
            {"command": command, "result": "error", "error": "arguments_invalid"}, 2
        )
    runtime = create_operator_runtime(config)
    models = MentalModels(config)
    result: ManagementResult | None = None
    try:

        async def operation(client: HindsightClientProtocol) -> str:
            return await operator_call(models, cast(MentalModelClient, client), args)

        raw = runtime.call(operation, timeout=config.mental_models.timeout_seconds)
        payload = json.loads(raw)
        # The generated evidence envelope is kept intact for the operator, too.
        inner = payload
        if "context" in payload:
            inner = json.loads(
                next(line for line in payload["context"].splitlines() if line.startswith("{"))
            )
        failed = (
            "error" in inner
            or inner.get("result") in {"ambiguous", "unconfirmed", "failed", "cancelled"}
            or inner.get("status") in {"failed", "cancelled"}
        )
        result = ManagementResult(payload, 3 if failed else 0)
        return result
    except (Exception, asyncio.CancelledError):
        # Runtime deadlines may cancel verification after a valid remote acknowledgement.
        known = None
        if args["action"] == "page_create":
            known = models.page_creation_unconfirmed
        elif args["action"] == "refresh":
            known = models.refresh_unconfirmed.get(args.get("id", ""))
        elif args["action"] == "create":
            known = models.creation_unconfirmed
        if known is not None:
            result = ManagementResult(json.loads(render(known)), 3)
            return result
        mutation = args["action"] in {"create", "page_create", "edit", "refresh", "delete"}
        result = ManagementResult(
            {
                "command": command,
                "result": "unconfirmed" if mutation else "error",
                "error": "summary_unavailable",
                "verification": (
                    "No automatic retry. Reconcile exact target and operation before retrying."
                ),
            },
            3,
        )
        return result
    finally:
        try:
            finalized = runtime.finalize()
        except (Exception, asyncio.CancelledError):
            finalized = False
        if not finalized and result is not None:
            result.payload["cleanup_warning"] = (
                "Runtime cleanup incomplete; the reported outcome and identities are unchanged. "
                "Do not retry a mutation because of this warning."
            )


def validate_operator(config: BetterHindsightConfig, args: dict[str, Any]) -> None:
    action = args.get("action")
    if action in {"edit", "delete", "refresh", "inspect"}:
        key = identifier(args.get("id"))
        if action != "inspect" and args.get("confirm") != key:
            raise ValueError
    elif action in {"create", "page_create"}:
        if args.get("confirm") is not True or not config.mental_models.create_enabled:
            raise ValueError
        validate_args(
            {
                "action": "create",
                "name": args.get("name"),
                "source_query": args.get("source_query"),
                "reason": "Explicit operator creation",
            }
        )
        if args.get("parent_id") is not None:
            page_id(args["parent_id"])
    else:
        validate_args(args)
    if action in {"edit", "create", "page_create"}:
        if "name" in args:
            text(redact_sensitive_text(args["name"]), 120)
        if "source_query" in args:
            validate_args(
                {
                    "action": "create",
                    "name": "Definition",
                    "source_query": args["source_query"],
                    "reason": "Definition edit",
                }
            )
        parse_policy(args.get("policy", {}), base=config.mental_models.creation)
    if action == "edit" and not any(key in args for key in ("name", "source_query", "policy")):
        raise ValueError
    allowed = {
        "inspect": {"action", "id"},
        "edit": {"action", "id", "confirm", "name", "source_query", "policy"},
        "delete": {"action", "id", "confirm"},
        "refresh": {"action", "id", "confirm"},
        "create": {"action", "name", "source_query", "confirm", "policy"},
        "page_create": {"action", "name", "source_query", "confirm", "policy", "parent_id"},
    }
    if action in allowed and set(args) - allowed[action]:
        raise ValueError
    if str(action).startswith("page_") and not config.mental_models.pages_enabled:
        raise ValueError


async def operator_call(
    models: MentalModels, client: MentalModelClient, args: dict[str, Any]
) -> str:
    action = args["action"]
    if action in {"list", "status", "page_browse", "page_read", "page_search"}:
        return await models.call(client, args)
    version = await client.mental_model_request(
        "GET", "/version", decoder=lambda value: text(mapping(value).get("api_version"), 64)
    )
    if version != "0.10.2":
        return '{"error":"Operator summary maintenance requires Hindsight 0.10.2."}'
    pages = KnowledgePages(models.path)
    if action in {"create", "page_create"}:
        policy = parse_policy(args.get("policy", {}), base=models.config.mental_models.creation)
        if action == "create":
            # Reuse the same quota/deterministic-ID path, with operator-local defaults only.
            from dataclasses import replace

            models.config = replace(
                models.config, mental_models=replace(models.config.mental_models, creation=policy)
            )
            raw = await models.create(client, args)
            envelope = json.loads(raw)
            if "context" not in envelope:
                return raw
            outcome = json.loads(
                next(line for line in envelope["context"].splitlines() if line.startswith("{"))
            )
            if outcome.get("result") not in {"queued", "existing"}:
                return raw
            if outcome["result"] == "queued":
                models.creation_unconfirmed = {
                    "result": "unconfirmed",
                    "id": outcome["id"],
                    "operation_id": outcome["operation_id"],
                    "verification": (
                        "Acknowledged creation; readback unavailable. No automatic retry. "
                        "Check this exact operation, then read and inspect the model."
                    ),
                }
            record = await models.read(
                client, outcome["id"], query=redact_sensitive_text(args["source_query"])
            )
            if outcome["result"] == "queued":
                verify_creation(record, policy, redact_sensitive_text(args["name"]))
                status_response = await models.status(
                    client, outcome["id"], outcome["operation_id"]
                )
                raw = submission_result(status_response, outcome)
            models.creation_unconfirmed = None
            return raw
        return await create_page(models, pages, client, args, policy)
    key = args["id"]
    before = await models.read(client, key)
    if action == "inspect":
        return render(
            {
                "result": "ok",
                **models.project(before, content=True),
                "source_query": redact_sensitive_text(text(before.get("source_query"), 2000)),
                "definition": definition(before),
                "truncated": False,
            }
        )
    if action == "refresh":
        return await models.refresh(client, key, model=before)
    if action == "edit":
        if "name" in args and redact_sensitive_text(args["name"]) != before.get("name"):
            nodes = await pages.tree(client)
            if any(node["mental_model_id"] == key for node in nodes):
                return render(
                    {
                        "error": (
                            "Page-backed summary renames require the Hindsight page interface; "
                            "no mutation sent. Other definition edits remain supported."
                        )
                    }
                )
        return await edit_definition(models, client, args, before)
    if action != "delete":
        raise ValueError
    nodes = await pages.tree(client)
    affected = [node["page_id"] for node in nodes if node["mental_model_id"] == key]

    def decode_delete(value: object) -> None:
        if mapping(value).get("status") != "deleted":
            raise ValueError

    await client.mental_model_request(
        "DELETE", f"{models.path}/mental-models/{quote(key, safe='')}", decoder=decode_delete
    )
    try:
        await models.read(client, key)
    except HindsightClientError as error:
        if error.reason != "endpoint_not_found":
            raise
    else:
        raise ValueError("Deleted model remains present.")
    remaining = await pages.tree(client)
    if any(node["page_id"] in affected or node["mental_model_id"] == key for node in remaining):
        raise ValueError("Associated page remains present.")
    # Verify the exact page HTTP resources too, not only their tree membership.
    for page in affected:
        try:
            await client.mental_model_request("GET", pages.path + f"/pages/{page}", decoder=mapping)
        except HindsightClientError as error:
            if error.reason != "endpoint_not_found":
                raise
        else:
            raise ValueError
    return render(
        {
            "result": "deleted_verified",
            "id": key,
            "deleted_page_ids": affected,
            "verification": (
                "Model absence and associated page cascade verified. No folders deleted."
            ),
        }
    )


def verify_creation(record: dict[str, Any], policy: SummaryPolicy, name: str) -> None:
    if (
        record.get("name") != name
        or record.get("tags") != []
        or record.get("max_tokens") != policy.max_tokens
    ):
        raise ValueError
    stored_trigger = mapping(record.get("trigger"))
    if any(stored_trigger.get(key) != value for key, value in policy.trigger().items()):
        raise ValueError


def definition(model: dict[str, Any]) -> dict[str, object]:
    policy = mapping(model.get("trigger"))

    def clean(value: object) -> object:
        if isinstance(value, str):
            return redact_sensitive_text(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {redact_sensitive_text(str(key)): clean(item) for key, item in value.items()}
        return value

    return {
        "tags": clean(model.get("tags")),
        "max_tokens": model.get("max_tokens"),
        "trigger": {key: clean(policy.get(key)) for key in SummaryPolicy().trigger()},
    }


async def edit_definition(
    models: MentalModels, client: MentalModelClient, args: dict[str, Any], before: dict[str, Any]
) -> str:
    validate_fixed_policy(before)
    body: dict[str, object] = {}
    for key in ("name", "source_query"):
        if key in args:
            body[key] = redact_sensitive_text(args[key])
    changes = args.get("policy", {})
    current = mapping(before.get("trigger"))
    defaults = asdict(SummaryPolicy())
    for key, wire in POLICY_FLAGS.items():
        value = before.get("max_tokens") if key == "max_tokens" else current.get(wire)
        if value is not None:
            defaults[key] = value
    parse_policy(changes, base=SummaryPolicy(**defaults))
    trigger = {POLICY_FLAGS[key]: value for key, value in changes.items() if key != "max_tokens"}
    if trigger:
        body["trigger"] = trigger
    if "max_tokens" in changes:
        body["max_tokens"] = changes["max_tokens"]
    key = args["id"]
    await client.mental_model_request(
        "PATCH",
        f"{models.path}/mental-models/{quote(key, safe='')}",
        body,
        decoder=lambda value: models.decode_model(value, key, None),
    )
    after = await models.read(client, key)
    expected_trigger = {**current, **trigger}
    if mapping(after.get("trigger")) != expected_trigger or after.get("tags") != before.get("tags"):
        raise ValueError("Definition policy readback mismatch.")
    for field in ("name", "source_query", "max_tokens"):
        if after.get(field) != body.get(field, before.get(field)):
            raise ValueError("Definition readback mismatch.")
    return render(
        {
            "result": "definition_verified",
            "id": key,
            "definition": definition(after),
            "verification": (
                "Definition only; no regeneration requested. Explicit refresh/status/read required."
            ),
        }
    )


async def create_page(
    models: MentalModels,
    pages: KnowledgePages,
    client: MentalModelClient,
    args: dict[str, Any],
    policy: SummaryPolicy,
) -> str:
    if models.page_creation_unconfirmed is not None:
        return render(models.page_creation_unconfirmed)
    if models.page_creation_ambiguous:
        return render(
            {
                "result": "ambiguous",
                "verification": "No retry; reconcile page inventory with the operator.",
            }
        )
    items, total = await models.page(client, 0)
    nodes = await pages.tree(client)
    parent = args.get("parent_id")
    if parent is not None and not any(
        node["page_id"] == parent and node["kind"] == "folder" for node in nodes
    ):
        raise ValueError
    if (
        total != len(items)
        or total + len(models.reservations) >= models.config.mental_models.max_models
    ):
        return '{"error":"Mental-model allowance exhausted or inventory incomplete."}'
    body: dict[str, object] = {
        "name": redact_sensitive_text(args["name"]),
        "source_query": redact_sensitive_text(args["source_query"]),
        "parent_id": parent,
        "tags": [],
        "max_tokens": policy.max_tokens,
        "trigger": policy.trigger(),
    }

    def decode(value: object) -> tuple[str, str, str]:
        response = mapping(value)
        return (
            page_id(response.get("page_id")),
            identifier(response.get("mental_model_id")),
            operation_id(response.get("operation_id")),
        )

    models.page_creation_ambiguous = True
    try:
        page, backing, op_id = await client.mental_model_request(
            "POST", pages.path + "/pages", body, decoder=decode
        )
    except Exception as error:
        if isinstance(error, HindsightClientError) and error.status in {409, 422, 429}:
            models.page_creation_ambiguous = False
            if error.status == 409:
                return render(
                    {
                        "result": "rejected",
                        "error": "page_name_conflict",
                        "verification": (
                            "No page created. Browse the folder and reuse the "
                            "existing page or choose a new name."
                        ),
                    }
                )
            return render(
                {
                    "result": "rejected",
                    "error": "summary_write_rejected",
                    "verification": "No page created. A later explicit attempt is permitted.",
                }
            )
        return render(
            {
                "result": "ambiguous",
                "verification": (
                    "No retry. Server-generated IDs may be unknown; browse "
                    "pages and reconcile before another create."
                ),
            }
        )
    models.page_creation_unconfirmed = {
        "result": "unconfirmed",
        "page_id": page,
        "mental_model_id": backing,
        "operation_id": op_id,
        "verification": (
            "No retry. Acknowledged creation is not verified; reconcile exact "
            "page, model definition and operation before another create."
        ),
    }
    try:
        record = await models.read(client, backing, query=cast(str, body["source_query"]))
        verify_creation(record, policy, cast(str, body["name"]))
        read = await pages.read(client, page)
        if read["mental_model_id"] != backing:
            raise ValueError
        status_response = await models.status(client, backing, op_id)
    except Exception:
        return render(models.page_creation_unconfirmed)
    models.page_creation_unconfirmed = None
    models.page_creation_ambiguous = False
    return submission_result(
        status_response,
        {
            "result": "queued",
            "page_id": page,
            "mental_model_id": backing,
            "operation_id": op_id,
            "verification": (
                "Creation definition and page binding verified. Check "
                "status then read body; queued is not generated."
            ),
        },
    )
