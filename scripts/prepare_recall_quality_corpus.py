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
_MEMORY_CONTEXT_OPEN = "<memory-context>"
_MEMORY_CONTEXT_CLOSE = "</memory-context>"
_MEMORY_CONTEXT_START = re.compile(
    r"(?:\A|\n\n)(?P<envelope>"
    + re.escape(_MEMORY_CONTEXT_OPEN)
    + r"\s*\n\[System note:\s*The following is recalled memory context,[^\]]*\]\s*)",
    flags=re.IGNORECASE,
)
_INTERNAL_PREFIXES = (
    "[INTERNAL DELEGATION CLOSEOUT",
    "[OUT-OF-BAND USER MESSAGE",
    "[BACKGROUND PROCESS",
)
_ATTACHMENT_PREFIXES = ("[File:", "[Image:", "[Audio:", "[Video:")
_DEFAULT_SOURCES = ("telegram", "cli", "tui")
_MAX_CORPUS_LIMIT = 500


class CollectionError(ValueError):
    """Raised when historical queries cannot be collected safely."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    session_id: str
    query: str
    normalized: str
    rank: bytes


def _fail(message: str) -> NoReturn:
    raise CollectionError(message)


def clean_historical_query(content: object) -> str:
    """Remove one unambiguous appended memory envelope from a persisted user turn."""

    if not isinstance(content, str):
        return ""
    query = _TIMESTAMP_PREFIX.sub("", content, count=1)
    without_trailing_space = query.rstrip()
    signed_starts = list(_MEMORY_CONTEXT_START.finditer(without_trailing_space))
    if len(signed_starts) > 1:
        # User literals and recalled evidence can contain arbitrary delimiters.
        # Exclude ambiguous turns rather than silently altering private query text.
        return ""
    if without_trailing_space.endswith(_MEMORY_CONTEXT_CLOSE) and signed_starts:
        query = without_trailing_space[: signed_starts[0].start("envelope")]
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


def _select_candidates(
    candidates: list[_Candidate],
    *,
    limit: int,
    max_per_session: int,
) -> tuple[list[_Candidate], int]:
    by_query: dict[str, dict[str, _Candidate]] = {}
    for candidate in sorted(candidates, key=lambda item: (item.rank, item.session_id, item.query)):
        by_query.setdefault(candidate.normalized, {}).setdefault(candidate.session_id, candidate)
    ordered_queries = sorted(
        by_query,
        key=lambda normalized: (
            min(candidate.rank for candidate in by_query[normalized].values()),
            normalized,
        ),
    )
    assignments: dict[str, str] = {}
    session_queries: dict[str, list[str]] = {}
    selected_queries: list[str] = []
    session_cap_rejections = 0

    def find_destination(
        normalized: str,
        *,
        seen_queries: set[str],
        seen_sessions: set[str],
    ) -> str | None:
        if normalized in seen_queries:
            return None
        seen_queries.add(normalized)
        for session_id in sorted(by_query[normalized]):
            if session_id in seen_sessions:
                continue
            seen_sessions.add(session_id)
            occupants = session_queries.setdefault(session_id, [])
            if len(occupants) < max_per_session:
                return session_id
            for occupant in reversed(tuple(occupants)):
                destination = find_destination(
                    occupant,
                    seen_queries=seen_queries,
                    seen_sessions=seen_sessions,
                )
                if destination is None:
                    continue
                occupants.remove(occupant)
                session_queries.setdefault(destination, []).append(occupant)
                assignments[occupant] = destination
                return session_id
        return None

    for normalized in ordered_queries:
        destination = find_destination(
            normalized,
            seen_queries=set(),
            seen_sessions=set(),
        )
        if destination is None:
            session_cap_rejections += 1
            continue
        assignments[normalized] = destination
        session_queries.setdefault(destination, []).append(normalized)
        selected_queries.append(normalized)
        if len(selected_queries) == limit:
            break

    selected = [by_query[normalized][assignments[normalized]] for normalized in selected_queries]
    return selected, session_cap_rejections


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
    if limit > _MAX_CORPUS_LIMIT:
        _fail(f"collection limit must not exceed {_MAX_CORPUS_LIMIT}")
    if not sources or any(not source.strip() for source in sources):
        _fail("at least one non-empty source is required")
    cutoff = (time.time() if now is None else now) - days * 86_400
    placeholders = ",".join("?" for _ in sources)
    scan_limit = limit * 100
    stored_char_limit = max_chars + 65_536
    uri = f"file:{quote(str(state_db))}?mode=ro"
    connection: sqlite3.Connection | None = None
    excluded: Counter[str] = Counter()
    candidates: list[_Candidate] = []
    scanned = 0
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
              AND typeof(m.content) = 'text'
              AND length(m.content) <= ?
            ORDER BY m.timestamp DESC, m.rowid DESC
            LIMIT ?
            """,
            (cutoff, *sources, stored_char_limit, scan_limit),
        )
        for session_id, content in rows:
            scanned += 1
            query = clean_historical_query(content)
            reason = _exclusion_reason(query, max_chars=max_chars, max_lines=max_lines)
            if reason is not None:
                excluded[reason] += 1
                continue
            normalized = _normalized_query(query)
            rank = hashlib.sha256(f"{seed}\0{normalized}".encode()).digest()
            candidates.append(
                _Candidate(
                    session_id=str(session_id),
                    query=query,
                    normalized=normalized,
                    rank=rank,
                )
            )
    except sqlite3.Error as error:
        raise CollectionError("Hermes state database could not be read") from error
    finally:
        if connection is not None:
            connection.close()

    unique_queries = {candidate.normalized for candidate in candidates}
    excluded["duplicate"] += len(candidates) - len(unique_queries)
    selected, session_cap_rejections = _select_candidates(
        candidates,
        limit=limit,
        max_per_session=max_per_session,
    )
    excluded["session_cap"] += session_cap_rejections

    stats = {
        "eligible": len(unique_queries),
        "scanned": scanned,
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
