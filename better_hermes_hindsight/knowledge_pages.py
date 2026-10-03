"""Bounded Knowledge Page projections, separate from standalone summaries."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import quote

from .mental_models import MentalModelClient, identifier, mapping, render, text
from .redaction import redact_sensitive_text

TREE_MAX_NODES = 200
TREE_MAX_DEPTH = 12
SEARCH_LIMIT = 10


def page_id(value: object) -> str:
    # Exact 0.10.2 engine uses kp-/kf- plus uuid4().hex, not UUID strings.
    if not isinstance(value, str) or not re.fullmatch(r"k[pf]-[0-9a-f]{32}", value):
        raise ValueError
    return value


def safe_text(value: object, maximum: int) -> str:
    return redact_sensitive_text(text(value, 100_000))[:maximum]


class KnowledgePages:
    def __init__(self, path: str) -> None:
        self.path = path + "/knowledge-base"

    async def tree(self, client: MentalModelClient) -> list[dict[str, Any]]:
        return await client.mental_model_request(
            "GET", self.path + "/tree", decoder=self.decode_tree
        )

    @staticmethod
    def decode_tree(value: object) -> list[dict[str, Any]]:
        roots = mapping(value).get("roots")
        if not isinstance(roots, list):
            raise ValueError
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        if len(roots) > TREE_MAX_NODES:
            raise ValueError
        stack: list[tuple[object, str | None, int]] = [(node, None, 1) for node in reversed(roots)]
        while stack:
            raw, parent, depth = stack.pop()
            if len(result) >= TREE_MAX_NODES or depth > TREE_MAX_DEPTH:
                raise ValueError("Knowledge tree exceeds bounded inventory.")
            node = mapping(raw)
            key = page_id(node.get("id"))
            if key in seen or node.get("parent_id") != parent:
                raise ValueError
            seen.add(key)
            kind = node.get("kind")
            children = node.get("children")
            if kind not in {"folder", "page"} or not isinstance(children, list):
                raise ValueError
            backing = node.get("mental_model_id")
            if kind == "page":
                backing = identifier(backing)
                if children:
                    raise ValueError
            elif backing is not None:
                raise ValueError
            stale = node.get("is_stale")
            if stale is not None and type(stale) is not bool:
                raise ValueError
            result.append(
                {
                    "page_id": key,
                    "kind": kind,
                    "name": safe_text(node.get("name"), 120),
                    "parent_id": parent,
                    "mental_model_id": backing,
                    "is_stale": stale,
                }
            )
            if len(stack) + len(children) + len(result) > TREE_MAX_NODES:
                raise ValueError
            stack.extend((child, key, depth + 1) for child in reversed(children))
        return result

    async def browse(self, client: MentalModelClient) -> str:
        nodes = await self.tree(client)
        return render(
            {
                "result": "ok",
                "inventory": "knowledge_pages_and_folders",
                "items": nodes,
                "offset": 0,
                "truncated": False,
                "verification": (
                    "Unpaginated tree; oversized inventories are refused. "
                    "Standalone models are not pages."
                ),
            }
        )

    async def search(self, client: MentalModelClient, query: str) -> str:
        def decode(value: object) -> str:
            response = mapping(value)
            hits, total = response.get("results"), response.get("total")
            if (
                not isinstance(hits, list)
                or len(hits) > SEARCH_LIMIT
                or type(total) is not int
                or total != len(hits)
            ):
                raise ValueError
            items: list[dict[str, object]] = []
            seen: set[str] = set()
            for raw in hits:
                hit = mapping(raw)
                key = page_id(hit.get("id"))
                if key in seen:
                    raise ValueError
                seen.add(key)
                score: Any = hit.get("score")
                if (
                    type(score) not in {int, float}
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise ValueError
                backing = hit.get("mental_model_id")
                items.append(
                    {
                        "page_id": key,
                        "mental_model_id": identifier(backing),
                        "name": safe_text(hit.get("name"), 120),
                        "snippet": safe_text(hit.get("snippet"), 500),
                        "score": score,
                    }
                )
            return render(
                {
                    "result": "ok",
                    "inventory": "knowledge_pages_only",
                    "items": items,
                    "offset": 0,
                    "returned_hits": total,
                    "limit": SEARCH_LIMIT,
                    "truncated": False,
                    "verification": (
                        "Semantic page search excludes standalone mental "
                        "models; read body to verify generation."
                    ),
                }
            )

        return await client.mental_model_request(
            "GET",
            self.path + f"/search?q={quote(query, safe='')}&limit={SEARCH_LIMIT}",
            decoder=decode,
        )

    async def read(self, client: MentalModelClient, key: str) -> dict[str, object]:
        nodes = await self.tree(client)
        matches = [node for node in nodes if node["page_id"] == key and node["kind"] == "page"]
        if len(matches) != 1:
            raise ValueError
        node = matches[0]

        def decode(value: object) -> dict[str, object]:
            page = mapping(value)
            if page.get("id") != key:
                raise ValueError
            body = page.get("body")
            if body is not None and not isinstance(body, str):
                raise ValueError
            return {
                "result": "ok",
                "page_id": key,
                "mental_model_id": node["mental_model_id"],
                "name": safe_text(page.get("name"), 120),
                "content": redact_sensitive_text(body or ""),
                "generated_content_present": bool(body and body.strip()),
                "truncated": False,
                "verification": (
                    "Inspect the stored body; rendered placeholders are not generated content."
                ),
            }

        return await client.mental_model_request("GET", self.path + f"/pages/{key}", decoder=decode)
