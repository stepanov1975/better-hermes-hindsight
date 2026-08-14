"""Bounded privacy-safe structured operational events."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from . import __version__


def emit_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Emit one canonical JSON event containing only caller-supplied safe fields."""

    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def elapsed_milliseconds(start: float, end: float) -> int:
    """Return a non-negative integer duration for bounded operational output."""

    return max(0, min(2_147_483_647, round((end - start) * 1_000)))


def error_counts(categories: Mapping[str, int]) -> dict[str, int]:
    """Return the fixed schema-v1 sender error category shape."""

    return {
        "retain_failed": int(categories.get("retain_failed", 0)),
        "retain_timeout": int(categories.get("retain_timeout", 0)),
        "retain_unconfirmed": int(categories.get("retain_unconfirmed", 0)),
    }


def _valid_commit(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.lower()
    valid = 7 <= len(candidate) <= 40 and candidate.isascii()
    if not valid or any(character not in "0123456789abcdef" for character in candidate):
        return None
    return candidate


def deployed_identity(hermes_home: Path | None = None) -> dict[str, str]:
    """Return bounded plugin identity without exposing installation paths."""

    del hermes_home
    candidate = _valid_commit(os.environ.get("BETTER_HINDSIGHT_COMMIT"))
    return {"commit": candidate or "unknown", "version": __version__[:64]}


__all__ = ["deployed_identity", "elapsed_milliseconds", "emit_event", "error_counts"]
