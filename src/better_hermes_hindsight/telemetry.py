"""Bounded privacy-safe structured operational events."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from better_hermes_hindsight import __version__


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


def _installed_commit(hermes_home: Path) -> str | None:
    candidates = (
        (hermes_home / "better_hindsight/install.json", "commit"),
        (hermes_home / "plugins/.install-metadata.json", "better_hindsight"),
    )
    for path, key in candidates:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            value = document.get(key)
            if key == "better_hindsight" and isinstance(value, dict):
                value = value.get("revision")
            candidate = _valid_commit(value)
            if candidate is not None:
                return candidate
        except (OSError, TypeError, ValueError):
            continue
    return None


def deployed_identity(hermes_home: Path | None = None) -> dict[str, str]:
    """Return bounded package identity without exposing installation paths."""

    candidate = _valid_commit(os.environ.get("BETTER_HINDSIGHT_COMMIT"))
    if candidate is None and hermes_home is not None:
        candidate = _installed_commit(hermes_home)
    return {"commit": candidate or "unknown", "version": __version__[:64]}


__all__ = ["deployed_identity", "elapsed_milliseconds", "emit_event", "error_counts"]
