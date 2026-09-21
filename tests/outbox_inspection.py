"""Test-only polling of an outbox that may be changing under a sender."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from better_hermes_hindsight.outbox import OutboxOpenError, OutboxReadError, OutboxRow


class OutboxWaitError(AssertionError):
    """Fixed, content-free diagnostics for a failed inspection wait."""

    def __init__(
        self,
        category: Literal["sqlite_contention", "local_failure", "predicate_not_met"],
        stage: Literal["open", "read", "predicate"],
    ) -> None:
        self.category = category
        self.stage = stage
        super().__init__(f"Outbox inspection failed: category={category} stage={stage}")


def outbox_failure_details(error: BaseException) -> dict[str, str]:
    """Only allowlisted labels, never exception text, paths, SQL, or row content."""
    if isinstance(error, OutboxWaitError):
        return {"outbox_category": error.category, "outbox_stage": error.stage}
    if isinstance(error, (OutboxOpenError, OutboxReadError)):
        return {
            "outbox_category": (
                "sqlite_contention" if error.category == "sqlite_contention" else "local_failure"
            ),
            "outbox_stage": "open" if isinstance(error, OutboxOpenError) else "read",
        }
    return {}


def wait_for_rows(
    read_rows: Callable[[], tuple[OutboxRow, ...]],
    predicate: Callable[[tuple[OutboxRow, ...]], bool],
    *,
    timeout: float,
    poll_interval: float,
) -> tuple[OutboxRow, ...]:
    """Retry only SQLite contention, never mistake a failed read for an empty queue.

    One absolute deadline fences attempts and successful observations. An in-flight
    SQLite call retains its existing busy timeout; no production policy is changed.
    """
    deadline = time.monotonic() + timeout
    category: Literal["sqlite_contention", "local_failure", "predicate_not_met"] = (
        "predicate_not_met"
    )
    stage: Literal["open", "read", "predicate"] = "predicate"
    while time.monotonic() < deadline:
        try:
            rows = read_rows()
        except (OutboxOpenError, OutboxReadError) as error:
            stage = "open" if isinstance(error, OutboxOpenError) else "read"
            if error.category != "sqlite_contention":
                raise OutboxWaitError("local_failure", stage) from None
            category = "sqlite_contention"
        else:
            category, stage = "predicate_not_met", "predicate"
            if time.monotonic() >= deadline:
                break
            if predicate(rows) and time.monotonic() < deadline:
                return rows
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    raise OutboxWaitError(category, stage) from None
