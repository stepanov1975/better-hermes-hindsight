"""Best-effort private stage evidence; never a routing input or automatic ground truth."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .config import BetterHindsightConfig
from .diagnostics import _new_record_id, _record_paths, _write_record
from .redaction import REDACTION_MARKER, redact_sensitive_text

logger = logging.getLogger(__name__)


def _clean(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, REDACTION_MARKER)
        return redact_sensitive_text(value)
    if isinstance(value, Mapping):
        return {str(key): _clean(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item, secrets) for item in value]
    if value is None or type(value) in {bool, int, float}:
        return value
    return None


def model_metadata(result: object) -> dict[str, object]:
    """Whitelist returned metadata, never completion text, headers, or raw responses."""

    def get(value: object, key: str) -> object:
        try:
            return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        except Exception:
            return None

    metadata: dict[str, object] = {}
    for field in ("model", "model_id", "provider"):
        value = get(result, field)
        if isinstance(value, str) and len(value) <= 256:
            metadata[field] = value
    for field in ("confidence", "cost"):
        value = get(result, field)
        if type(value) in {int, float}:
            metadata[field] = value
    usage = get(result, "usage")
    metadata["usage"] = {
        key: get(usage, key)
        for key in (
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost",
            "cost_usd",
        )
        if type(get(usage, key)) in {int, float}
    }
    return metadata


@dataclass(frozen=True)
class EvaluationCapture:
    config: BetterHindsightConfig
    correlation_id: str

    def stage(self, stage: str, **fields: object) -> None:
        policy = self.config.evaluation
        if not policy.enabled:
            return
        try:
            if not re.fullmatch(r"[0-9a-f]{32}", self.correlation_id):
                return
            home = self.config.hermes_home.resolve()
            directory = home / "better_hindsight" / "planner_evaluation"
            # Refuse redirected stores, including profile-local symlinks.
            for path in (directory.parent, directory):
                if path.is_symlink():
                    return
                path.mkdir(mode=0o700, exist_ok=True)
            directory.resolve().relative_to(home)
            os.chmod(directory, 0o700)
            descriptor = os.open(
                directory / ".records.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            try:
                os.fchmod(descriptor, 0o600)
                # Contention drops evidence rather than blocking the planner/provider.
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                now = time.time()
                paths = _record_paths(directory, regular_only=True)
                for index, path in enumerate(paths):
                    if (
                        index >= policy.max_records - 1
                        or now - path.stat().st_mtime > policy.max_age_seconds
                    ):
                        path.unlink()
                payload = _clean(
                    {
                        "schema": 1,
                        "correlation_id": self.correlation_id,
                        "stage": stage,
                        "recorded_at": now,
                        **fields,
                    },
                    (self.config.api_key or "", os.environ.get("OPENROUTER_API_KEY", "")),
                )
                assert isinstance(payload, dict)
                encoded = json.dumps(
                    payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
                )
                if len(encoded.encode()) > policy.max_record_bytes:
                    return  # Never silently replace the exact capsule with clipped evidence.
                _write_record(directory, _new_record_id(), payload)
            finally:
                os.close(descriptor)
        except Exception:
            logger.debug("Better Hindsight private evaluation capture unavailable")
