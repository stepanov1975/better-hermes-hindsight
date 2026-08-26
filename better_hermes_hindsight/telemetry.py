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


def _installed_commit(hermes_home: Path) -> str | None:
    path = hermes_home / "plugins/.install-metadata.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return None
        plugin = document.get("better_hindsight")
        if not isinstance(plugin, dict):
            return None
        return _valid_commit(plugin.get("revision"))
    except (OSError, TypeError, ValueError):
        return None


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _checkout_commit(plugin_root: Path) -> str | None:
    git_dir = plugin_root / ".git"
    if not git_dir.is_dir():
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    candidate = _valid_commit(head)
    if candidate is not None:
        return candidate
    prefix = "ref: "
    if not head.startswith(prefix):
        return None
    ref_name = head[len(prefix) :]
    ref_path = Path(ref_name)
    if not ref_name.startswith("refs/") or ref_path.is_absolute() or ".." in ref_path.parts:
        return None
    try:
        candidate = _valid_commit((git_dir / ref_path).read_text(encoding="ascii").strip())
    except (OSError, UnicodeError):
        candidate = None
    if candidate is not None:
        return candidate
    try:
        packed_refs = (git_dir / "packed-refs").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in packed_refs:
        commit, separator, packed_ref = line.partition(" ")
        if separator and packed_ref == ref_name:
            return _valid_commit(commit)
    return None


def deployed_identity(hermes_home: Path | None = None) -> dict[str, str]:
    """Return bounded plugin identity without exposing installation paths."""

    candidate = _valid_commit(os.environ.get("BETTER_HINDSIGHT_COMMIT"))
    if candidate is None and hermes_home is not None:
        candidate = _installed_commit(hermes_home)
    if candidate is None:
        candidate = _checkout_commit(_plugin_root())
    return {"commit": candidate or "unknown", "version": __version__[:64]}


__all__ = ["deployed_identity", "elapsed_milliseconds", "emit_event", "error_counts"]
