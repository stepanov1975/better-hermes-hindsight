"""Best-effort private stage evidence; never a routing input or automatic ground truth."""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from .config import BetterHindsightConfig
from .diagnostics import _new_record_id, _record_paths, _write_record
from .redaction import REDACTION_MARKER, redact_sensitive_text

logger = logging.getLogger(__name__)
_QUEUE_MAX = 16
_SHARED_WRITER_MODULE = "_better_hermes_hindsight_evaluation_writer_v1"


@dataclass
class _Writer:
    jobs: queue.Queue[tuple[BetterHindsightConfig, str]] = field(
        default_factory=lambda: queue.Queue(maxsize=_QUEUE_MAX)
    )
    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: threading.Thread | None = None


def _shared_writer() -> _Writer:
    # The companion and provider have different import names in Hermes, like the mailbox.
    candidate = types.ModuleType(_SHARED_WRITER_MODULE)
    candidate.__dict__["state"] = _Writer()
    shared = sys.modules.setdefault(_SHARED_WRITER_MODULE, candidate)
    return cast(_Writer, shared.__dict__["state"])


_writer = _shared_writer()


def _enqueue(config: BetterHindsightConfig, encoded: str) -> None:
    # Follow diagnostics' bounded daemon pattern, but do not share its queue or the
    # remote-client event loop: a stalled evaluation disk must not stall either.
    if not _writer.lock.acquire(blocking=False):
        return
    try:
        if _writer.thread is None or not _writer.thread.is_alive():
            _writer.thread = threading.Thread(
                target=_write_jobs, name="better-hindsight-evaluation", daemon=True
            )
            _writer.thread.start()
        _writer.jobs.put_nowait((config, encoded))
    finally:
        _writer.lock.release()


def _write_jobs() -> None:
    while True:
        job = _writer.jobs.get()
        try:
            _persist_stage(*job)
        except Exception:
            logger.debug("Better Hindsight private evaluation capture unavailable")
        finally:
            del job
            _writer.jobs.task_done()


def drain_evaluation_for_tests(*, timeout: float = 5.0) -> bool:
    """Wait at most timeout for queued writes; never called on shutdown or routing."""
    deadline = time.monotonic() + timeout
    with _writer.jobs.all_tasks_done:
        while _writer.jobs.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _writer.jobs.all_tasks_done.wait(remaining)
    return True


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


def _finite_number(value: object) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def model_metadata(result: object) -> dict[str, object]:
    """Whitelist returned metadata, never completion text, headers, or raw responses."""

    def get(value: object, key: str) -> object:
        try:
            return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        except Exception:
            return None

    metadata: dict[str, object] = {}
    for name in ("model", "model_id", "provider"):
        value = get(result, name)
        if isinstance(value, str) and len(value) <= 256:
            metadata[name] = value
    for name in ("confidence", "cost"):
        value = get(result, name)
        if _finite_number(value):
            metadata[name] = value
    usage = get(result, "usage")
    usage_fields = {}
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
    ):
        value = get(usage, key)
        if _finite_number(value):
            usage_fields[key] = value
    if usage_fields:
        metadata["usage"] = usage_fields
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
            secrets = tuple(
                sorted(
                    (self.config.api_key or "", os.environ.get("OPENROUTER_API_KEY", "").strip()),
                    key=len,
                    reverse=True,
                )
            )
            payload = _clean(
                {
                    "schema": 1,
                    "correlation_id": self.correlation_id,
                    "stage": stage,
                    "recorded_at": time.time(),
                    **fields,
                },
                secrets,
            )
            # Validate before touching the store, and queue only an immutable, redacted,
            # byte-bounded snapshot. No filesystem work runs on the calling thread.
            encoded = json.dumps(
                payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False
            )
            if len(encoded) > policy.max_record_bytes:  # ensure_ascii makes bytes == characters.
                return  # Never silently replace the exact capsule with clipped evidence.
            _enqueue(self.config, encoded)
        except Exception:
            logger.debug("Better Hindsight private evaluation capture unavailable")


def _persist_stage(config: BetterHindsightConfig, encoded: str) -> None:
    policy = config.evaluation
    payload = json.loads(encoded)
    home = config.hermes_home.resolve()
    directory = home / "better_hindsight" / "planner_evaluation"
    # Refuse redirected stores, including profile-local symlinks.
    for path in (directory.parent, directory):
        if path.is_symlink():
            return
        path.mkdir(mode=0o700, exist_ok=True)
    directory.resolve().relative_to(home)
    os.chmod(directory, 0o700)
    descriptor = os.open(directory / ".records.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        # One shared writer serializes in-process work; the lock also fences other processes.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = time.time()
        paths = _record_paths(directory, regular_only=True)
        for index, path in enumerate(paths):
            if (
                index >= policy.max_records - 1
                or now - path.stat().st_mtime > policy.max_age_seconds
            ):
                path.unlink()
        _write_record(directory, _new_record_id(), payload)
    finally:
        os.close(descriptor)
