"""Fixtures for the two obsolete branch-preview planner mailbox schemas."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_LEGACY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS process_owner (
    process_identity TEXT PRIMARY KEY,
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    boot_token TEXT NOT NULL,
    start_token TEXT NOT NULL,
    observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS active_session (
    process_identity TEXT NOT NULL,
    session_id TEXT NOT NULL,
    activated_at REAL NOT NULL,
    ref_count INTEGER NOT NULL CHECK (ref_count > 0),
    PRIMARY KEY (process_identity, session_id)
);
CREATE TABLE IF NOT EXISTS recall_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_identity TEXT NOT NULL,
    session_id TEXT NOT NULL,
    parent_session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    query_digest TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('shadow', 'active')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'ready')),
    action TEXT CHECK (action IN ('skip', 'reuse', 'recall')),
    rewritten_query TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    UNIQUE (process_identity, turn_id),
    CHECK (
        (state = 'pending' AND action IS NULL AND rewritten_query IS NULL)
        OR
        (
            state = 'ready'
            AND action IS NOT NULL
            AND (
                (action = 'recall' AND rewritten_query IS NOT NULL)
                OR (action IN ('skip', 'reuse') AND rewritten_query IS NULL)
            )
        )
    )
);
CREATE INDEX IF NOT EXISTS recall_plan_lookup
ON recall_plan (process_identity, query_digest, session_id, expires_at);
"""


def create_legacy_plan_mailbox(path: Path, *, version: int = 3) -> None:
    """Create the exact obsolete version-two or version-three schema."""

    if version not in {2, 3}:
        raise ValueError("legacy schema version must be 2 or 3")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, isolation_level=None) as connection:
        for statement in _LEGACY_SCHEMA_SQL.split(";"):
            if not statement.strip():
                continue
            if version == 2 and "CREATE TABLE IF NOT EXISTS process_owner" in statement:
                continue
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {version}")
