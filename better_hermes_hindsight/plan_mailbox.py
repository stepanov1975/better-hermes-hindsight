"""Short-lived one-shot SQLite handoff for contextual recall plans."""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .config import PlannerMode

PlanAction = Literal["skip", "reuse", "recall"]
PlanState = Literal["pending", "ready"]
_SCHEMA_VERSION = 2
_ALLOWED_MODES = frozenset({"shadow", "active"})
_ALLOWED_ACTIONS = frozenset({"skip", "reuse", "recall"})
_PROCESS_NONCE_ENV = "BETTER_HINDSIGHT_RUNTIME_NONCE"
_SCHEMA_TABLES = frozenset({"active_session", "recall_plan"})

_SCHEMA_SQL = """
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


class PlanMailboxError(RuntimeError):
    """A sanitized local recall-plan mailbox failure."""


@dataclass(frozen=True, slots=True)
class RecallPlan:
    """One validated plan atomically removed from the mailbox."""

    mode: PlannerMode
    action: PlanAction
    rewritten_query: str | None
    turn_id: str


def _query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8", errors="surrogatepass")).hexdigest()


def _process_identity() -> str:
    """Return a same-process, restart-distinct identity without exposing a PID."""

    nonce = os.environ.get(_PROCESS_NONCE_ENV, "")
    if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
        nonce = uuid.uuid4().hex
        os.environ[_PROCESS_NONCE_ENV] = nonce
    material = f"{os.getpid()}:{nonce}".encode()
    return hashlib.sha256(material).hexdigest()


class SQLitePlanMailbox:
    """A profile-local, process-bound, consume-once plan rendezvous."""

    __slots__ = (
        "_busy_timeout_seconds",
        "_clock",
        "_monotonic",
        "_path",
        "_process_identity",
    )

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_seconds: float,
        process_identity: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._busy_timeout_seconds = busy_timeout_seconds
        self._process_identity = process_identity or _process_identity()
        self._clock = clock
        self._monotonic = monotonic

    @property
    def process_identity(self) -> str:
        """Return the opaque identity used to reject prior-process rows."""

        return self._process_identity

    def activate(self, *, session_id: str) -> None:
        """Mark one authorized provider session as active in this process."""

        _validate_identifier(session_id, "session_id")
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                now = self._clock()
                self._delete_stale(connection, now)
                connection.execute(
                    """
                    INSERT INTO active_session (
                        process_identity, session_id, activated_at, ref_count
                    ) VALUES (?, ?, ?, 1)
                    ON CONFLICT(process_identity, session_id) DO UPDATE SET
                        activated_at = excluded.activated_at,
                        ref_count = active_session.ref_count + 1
                    """,
                    (self._process_identity, session_id, now),
                )
                connection.commit()
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError(
                "Better Hindsight recall-plan mailbox activation failed."
            ) from exc

    def deactivate(self, *, session_id: str) -> None:
        """Release one provider owner's activation reference."""

        _validate_identifier(session_id, "session_id")
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT ref_count FROM active_session
                    WHERE process_identity = ? AND session_id = ?
                    """,
                    (self._process_identity, session_id),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return
                if int(row[0]) <= 1:
                    connection.execute(
                        """
                        DELETE FROM active_session
                        WHERE process_identity = ? AND session_id = ?
                        """,
                        (self._process_identity, session_id),
                    )
                    connection.execute(
                        """
                        DELETE FROM recall_plan
                        WHERE process_identity = ? AND session_id = ?
                        """,
                        (self._process_identity, session_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE active_session
                        SET ref_count = ref_count - 1
                        WHERE process_identity = ? AND session_id = ?
                        """,
                        (self._process_identity, session_id),
                    )
                connection.commit()
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError(
                "Better Hindsight recall-plan mailbox deactivation failed."
            ) from exc

    def rebind(self, *, old_session_id: str, new_session_id: str) -> None:
        """Move one provider activation reference to its new Hermes session."""

        _validate_identifier(old_session_id, "old_session_id")
        _validate_identifier(new_session_id, "new_session_id")
        if old_session_id == new_session_id:
            return
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT ref_count FROM active_session
                    WHERE process_identity = ? AND session_id = ?
                    """,
                    (self._process_identity, old_session_id),
                ).fetchone()
                if row is not None and int(row[0]) > 1:
                    connection.execute(
                        """
                        UPDATE active_session
                        SET ref_count = ref_count - 1
                        WHERE process_identity = ? AND session_id = ?
                        """,
                        (self._process_identity, old_session_id),
                    )
                elif row is not None:
                    connection.execute(
                        """
                        DELETE FROM active_session
                        WHERE process_identity = ? AND session_id = ?
                        """,
                        (self._process_identity, old_session_id),
                    )
                    connection.execute(
                        """
                        DELETE FROM recall_plan
                        WHERE process_identity = ? AND session_id = ?
                        """,
                        (self._process_identity, old_session_id),
                    )
                connection.execute(
                    """
                    INSERT INTO active_session (
                        process_identity, session_id, activated_at, ref_count
                    ) VALUES (?, ?, ?, 1)
                    ON CONFLICT(process_identity, session_id) DO UPDATE SET
                        activated_at = excluded.activated_at,
                        ref_count = active_session.ref_count + 1
                    """,
                    (self._process_identity, new_session_id, self._clock()),
                )
                connection.commit()
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError(
                "Better Hindsight recall-plan mailbox session rebind failed."
            ) from exc

    def clear_session_plans(self, *, session_id: str) -> None:
        """Invalidate pending and ready plans tied to one rewound session."""

        _validate_identifier(session_id, "session_id")
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    DELETE FROM recall_plan
                    WHERE process_identity = ? AND session_id = ?
                    """,
                    (self._process_identity, session_id),
                )
                connection.commit()
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError(
                "Better Hindsight recall-plan mailbox session cleanup failed."
            ) from exc

    def is_active(self, *, session_id: str) -> bool:
        """Return whether this exact provider session is authorized."""

        if not session_id:
            return False
        _validate_identifier(session_id, "session_id")
        try:
            with contextlib.closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT 1 FROM active_session
                    WHERE process_identity = ? AND session_id = ?
                    LIMIT 1
                    """,
                    (self._process_identity, session_id),
                ).fetchone()
                return row is not None
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError("Better Hindsight recall-plan mailbox read failed.") from exc

    def reserve(
        self,
        *,
        source_query: str,
        session_id: str,
        parent_session_id: str,
        turn_id: str,
        mode: str,
        ttl_seconds: float,
    ) -> bool:
        """Reserve this turn before model work so provider consumption fences late writers."""

        _validate_reservation(
            source_query=source_query,
            session_id=session_id,
            parent_session_id=parent_session_id,
            turn_id=turn_id,
            mode=mode,
            ttl_seconds=ttl_seconds,
        )
        now = self._clock()
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._delete_stale(connection, now)
                active = connection.execute(
                    """
                    SELECT 1 FROM active_session
                    WHERE process_identity = ? AND session_id = ?
                    LIMIT 1
                    """,
                    (self._process_identity, session_id),
                ).fetchone()
                if active is None:
                    connection.commit()
                    return False
                cursor = connection.execute(
                    """
                    INSERT INTO recall_plan (
                        process_identity, session_id, parent_session_id, turn_id,
                        query_digest, mode, state, action, rewritten_query,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)
                    ON CONFLICT(process_identity, turn_id) DO NOTHING
                    """,
                    (
                        self._process_identity,
                        session_id,
                        parent_session_id,
                        turn_id,
                        _query_digest(source_query),
                        mode,
                        now,
                        now + ttl_seconds,
                    ),
                )
                connection.commit()
                return cursor.rowcount == 1
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError(
                "Better Hindsight recall-plan mailbox reservation failed."
            ) from exc

    def finalize(
        self,
        *,
        turn_id: str,
        mode: str,
        action: str,
        rewritten_query: str | None,
        publish_deadline: float | None = None,
    ) -> bool:
        """Atomically publish a ready plan only while its reservation still exists."""

        _validate_ready_plan(
            turn_id=turn_id,
            mode=mode,
            action=action,
            rewritten_query=rewritten_query,
        )
        now = self._clock()
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._delete_stale(connection, now)
                if publish_deadline is not None and self._monotonic() >= publish_deadline:
                    connection.execute(
                        "DELETE FROM recall_plan WHERE process_identity = ? AND turn_id = ?",
                        (self._process_identity, turn_id),
                    )
                    connection.commit()
                    return False
                cursor = connection.execute(
                    """
                    UPDATE recall_plan
                    SET state = 'ready', action = ?, rewritten_query = ?
                    WHERE process_identity = ? AND turn_id = ?
                      AND state = 'pending' AND mode = ? AND expires_at > ?
                    """,
                    (
                        action,
                        rewritten_query,
                        self._process_identity,
                        turn_id,
                        mode,
                        now,
                    ),
                )
                connection.commit()
                return cursor.rowcount == 1
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError(
                "Better Hindsight recall-plan mailbox publication failed."
            ) from exc

    def cancel(self, *, turn_id: str) -> None:
        """Delete this turn's reservation after a shadow-mode planner failure."""

        _validate_identifier(turn_id, "turn_id")
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM recall_plan WHERE process_identity = ? AND turn_id = ?",
                    (self._process_identity, turn_id),
                )
                connection.commit()
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError(
                "Better Hindsight recall-plan mailbox cancellation failed."
            ) from exc

    def publish(
        self,
        *,
        source_query: str,
        session_id: str,
        parent_session_id: str,
        turn_id: str,
        mode: str,
        action: str,
        rewritten_query: str | None,
        ttl_seconds: float,
        publish_deadline: float | None = None,
    ) -> bool:
        """Reserve then publish a ready plan; useful for non-hook producers and tests."""

        _validate_ready_plan(
            turn_id=turn_id,
            mode=mode,
            action=action,
            rewritten_query=rewritten_query,
        )
        if not self.reserve(
            source_query=source_query,
            session_id=session_id,
            parent_session_id=parent_session_id,
            turn_id=turn_id,
            mode=mode,
            ttl_seconds=ttl_seconds,
        ):
            return False
        return self.finalize(
            turn_id=turn_id,
            mode=mode,
            action=action,
            rewritten_query=rewritten_query,
            publish_deadline=publish_deadline,
        )

    def consume(self, *, source_query: str, session_id: str = "") -> RecallPlan | None:
        """Atomically remove the newest exact-session plan, ready or pending."""

        if not session_id:
            return None
        now = self._clock()
        try:
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._delete_stale(connection, now)
                rows = connection.execute(
                    """
                    SELECT id, mode, state, action, rewritten_query, turn_id
                    FROM recall_plan
                    WHERE process_identity = ? AND query_digest = ? AND expires_at > ?
                      AND session_id = ?
                    ORDER BY created_at DESC, id DESC
                    """,
                    (
                        self._process_identity,
                        _query_digest(source_query),
                        now,
                        session_id,
                    ),
                ).fetchall()
                if not rows:
                    connection.commit()
                    return None
                selected = rows[0]
                connection.execute(
                    """
                    DELETE FROM recall_plan
                    WHERE process_identity = ? AND query_digest = ? AND session_id = ?
                    """,
                    (
                        self._process_identity,
                        _query_digest(source_query),
                        session_id,
                    ),
                )
                connection.commit()
                return self._decode_plan(selected)
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            raise PlanMailboxError("Better Hindsight recall-plan mailbox consume failed.") from exc

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        connection = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            busy_timeout_ms = max(1, round(self._busy_timeout_seconds * 1000))
            connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA secure_delete = ON")
            self._prepare_schema(connection)
            os.chmod(self._path, 0o600)
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _prepare_schema(connection: sqlite3.Connection) -> None:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        }
        if current == 0 and not tables:
            for statement in _SCHEMA_SQL.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return
        if current != _SCHEMA_VERSION or tables != _SCHEMA_TABLES:
            raise PlanMailboxError("Better Hindsight recall-plan mailbox schema is unsupported.")

    def _delete_stale(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM recall_plan WHERE expires_at <= ?", (now,))

    @staticmethod
    def _decode_plan(row: sqlite3.Row) -> RecallPlan | None:
        if row["state"] != "ready":
            return None
        mode = row["mode"]
        action = row["action"]
        rewritten_query = row["rewritten_query"]
        turn_id = row["turn_id"]
        if mode not in _ALLOWED_MODES or action not in _ALLOWED_ACTIONS:
            return None
        if action == "recall":
            if not isinstance(rewritten_query, str) or not rewritten_query.strip():
                return None
        elif rewritten_query is not None:
            return None
        if not isinstance(turn_id, str) or not turn_id:
            return None
        return RecallPlan(
            mode=cast(PlannerMode, mode),
            action=cast(PlanAction, action),
            rewritten_query=rewritten_query,
            turn_id=turn_id,
        )


def _session_scope(session_id: str, parent_session_id: str) -> tuple[str, ...]:
    _validate_identifier(session_id, "session_id")
    if parent_session_id:
        _validate_identifier(parent_session_id, "parent_session_id")
        if parent_session_id != session_id:
            return session_id, parent_session_id
    return (session_id,)


def _validate_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc


def _validate_reservation(
    *,
    source_query: str,
    session_id: str,
    parent_session_id: str,
    turn_id: str,
    mode: str,
    ttl_seconds: float,
) -> None:
    if not isinstance(source_query, str) or not source_query:
        raise ValueError("source_query must be a non-empty string")
    _session_scope(session_id, parent_session_id)
    _validate_identifier(turn_id, "turn_id")
    if mode not in _ALLOWED_MODES:
        raise ValueError("mode must be shadow or active")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not math.isfinite(ttl_seconds)
        or ttl_seconds <= 0
    ):
        raise ValueError("ttl_seconds must be positive and finite")


def _validate_ready_plan(
    *,
    turn_id: str,
    mode: str,
    action: str,
    rewritten_query: str | None,
) -> None:
    _validate_identifier(turn_id, "turn_id")
    if mode not in _ALLOWED_MODES:
        raise ValueError("mode must be shadow or active")
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("action must be skip, reuse, or recall")
    if action == "recall":
        if not isinstance(rewritten_query, str) or not rewritten_query.strip():
            raise ValueError("recall plans require a rewritten query")
        try:
            rewritten_query.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("rewritten_query must be valid UTF-8") from exc
    elif rewritten_query is not None:
        raise ValueError("skip and reuse plans must not carry a rewritten query")


__all__ = [
    "PlanAction",
    "PlanMailboxError",
    "RecallPlan",
    "SQLitePlanMailbox",
]
