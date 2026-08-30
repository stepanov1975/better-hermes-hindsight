#!/usr/bin/env python3
"""Build an owner-only, unlabelled corpus from real Hermes user queries."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

from better_hermes_hindsight.private_output import PrivateOutputError, write_private_json
from better_hermes_hindsight.redaction import redact_sensitive_text

_TIMESTAMP_PREFIX = re.compile(
    r"^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\]\s*"
)
_MEMORY_CONTEXT_SUFFIX = re.compile(r"\s*<memory-context>.*\Z", flags=re.DOTALL)
_INTERNAL_PREFIXES = (
    "[INTERNAL DELEGATION CLOSEOUT",
    "[OUT-OF-BAND USER MESSAGE",
    "[BACKGROUND PROCESS",
)
_ATTACHMENT_PREFIXES = ("[File:", "[Image:", "[Audio:", "[Video:")
_DEFAULT_SOURCES = ("telegram", "cli", "tui")


class CollectionError(ValueError):
    """Raised when historical queries cannot be collected safely."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    session_id: str
    query: str
    rank: bytes


def _fail(message: str) -> NoReturn:
    raise CollectionError(message)


def clean_historical_query(content: object) -> str:
    """Remove transport timestamp and appended memory context from a persisted user turn."""

    if not isinstance(content, str):
        return ""
    query = _TIMESTAMP_PREFIX.sub("", content, count=1)
    query = _MEMORY_CONTEXT_SUFFIX.sub("", query, count=1)
    return query.strip()


def _normalized_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", query).split()).casefold()


def _exclusion_reason(query: str, *, max_chars: int, max_lines: int) -> str | None:
    if not query:
        return "empty"
    if query.startswith(_INTERNAL_PREFIXES):
        return "internal"
    if query.startswith(_ATTACHMENT_PREFIXES) or "MEDIA:" in query:
        return "attachment"
    if len(query) < 12:
        return "too_short"
    if len(query) > max_chars:
        return "too_long"
    if query.count("\n") + 1 > max_lines:
        return "too_many_lines"
    if redact_sensitive_text(query) != query:
        return "credential_pattern"
    try:
        query.encode("utf-8")
    except UnicodeEncodeError:
        return "invalid_unicode"
    return None


def collect_historical_queries(
    state_db: Path,
    *,
    days: int,
    limit: int,
    max_per_session: int,
    max_chars: int,
    max_lines: int,
    sources: tuple[str, ...],
    seed: str,
    now: float | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Read bounded direct-user turns from Hermes state without retaining source identifiers."""

    if not state_db.is_absolute():
        _fail("state database path must be absolute")
    if days <= 0 or limit <= 0 or max_per_session <= 0 or max_chars <= 0 or max_lines <= 0:
        _fail("collection bounds must be positive integers")
    if not sources or any(not source.strip() for source in sources):
        _fail("at least one non-empty source is required")
    cutoff = (time.time() if now is None else now) - days * 86_400
    placeholders = ",".join("?" for _ in sources)
    uri = f"file:{quote(str(state_db))}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            f"""
            SELECT m.session_id, m.content
            FROM messages AS m
            JOIN sessions AS s ON s.id = m.session_id
            WHERE m.role = 'user'
              AND m.timestamp >= ?
              AND s.source IN ({placeholders})
              AND COALESCE(m._compressed_summary, 0) = 0
              AND m.display_kind IS NULL
              AND m.display_metadata IS NULL
            """,
            (cutoff, *sources),
        ).fetchall()
    except sqlite3.Error as error:
        raise CollectionError("Hermes state database could not be read") from error
    finally:
        if connection is not None:
            connection.close()

    excluded: Counter[str] = Counter()
    candidates: list[_Candidate] = []
    seen: set[str] = set()
    for session_id, content in rows:
        query = clean_historical_query(content)
        reason = _exclusion_reason(query, max_chars=max_chars, max_lines=max_lines)
        if reason is not None:
            excluded[reason] += 1
            continue
        normalized = _normalized_query(query)
        if normalized in seen:
            excluded["duplicate"] += 1
            continue
        seen.add(normalized)
        rank = hashlib.sha256(f"{seed}\0{normalized}".encode()).digest()
        candidates.append(_Candidate(session_id=str(session_id), query=query, rank=rank))

    selected: list[_Candidate] = []
    per_session: Counter[str] = Counter()
    for candidate in sorted(candidates, key=lambda item: item.rank):
        if per_session[candidate.session_id] >= max_per_session:
            excluded["session_cap"] += 1
            continue
        selected.append(candidate)
        per_session[candidate.session_id] += 1
        if len(selected) == limit:
            break

    stats = {
        "eligible": len(candidates),
        "scanned": len(rows),
        "selected": len(selected),
        **{f"excluded_{key}": value for key, value in sorted(excluded.items())},
    }
    return [candidate.query for candidate in selected], stats


def corpus_payload(queries: list[str]) -> dict[str, object]:
    """Create an intentionally unlabelled capture-input corpus."""

    return {
        "schema_version": 1,
        "cases": [
            {
                "id": f"historical-{index:03d}",
                "query": query,
                "expect_recall": False,
                "useful_result_ids": [],
                "redundant_result_ids": [],
                "irrelevant_result_ids": [],
                "labels_complete": False,
            }
            for index, query in enumerate(queries, start=1)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--state-db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--max-per-session", type=int, default=2)
    parser.add_argument("--max-chars", type=int, default=1_200)
    parser.add_argument("--max-lines", type=int, default=12)
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--seed", default="better-hindsight-recall-quality-v1")
    args = parser.parse_args()
    try:
        queries, stats = collect_historical_queries(
            args.state_db,
            days=args.days,
            limit=args.limit,
            max_per_session=args.max_per_session,
            max_chars=args.max_chars,
            max_lines=args.max_lines,
            sources=tuple(args.sources or _DEFAULT_SOURCES),
            seed=args.seed,
        )
        if not queries:
            _fail("no eligible historical queries were found")
        write_private_json(args.output, corpus_payload(queries))
        print(json.dumps({"result": "ok", **stats}, sort_keys=True, separators=(",", ":")))
    except (CollectionError, PrivateOutputError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
