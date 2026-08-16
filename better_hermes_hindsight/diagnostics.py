"""Bounded local capture and replay metadata for slow Better Hindsight recalls."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import BetterHindsightConfig
from .telemetry import emit_event

_RECORD_SCHEMA = 1
_RECORD_ID = re.compile(r"[0-9]{19}-[0-9a-f]{12}")
_MAX_LIST_RECORDS = 20
_MAX_RECORD_BYTES = 512 * 1024
_CAPTURE_QUEUE_MAX = 16
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CaptureJob:
    config: BetterHindsightConfig
    record_id: str
    query: str
    request: Mapping[str, object]
    elapsed_ms: int
    outcome: str
    result_count: int | None
    formatted_bytes: int | None
    reason: str | None


_capture_queue: queue.Queue[_CaptureJob] = queue.Queue(maxsize=_CAPTURE_QUEUE_MAX)
_capture_thread: threading.Thread | None = None
_capture_thread_lock = threading.Lock()


class DiagnosticRecordError(RuntimeError):
    """Fixed local diagnostic-record failure without private path or content."""


def recall_target_fingerprint(config: BetterHindsightConfig) -> str:
    """Return a credential-free identity for the exact recall destination."""

    payload = json.dumps(
        {"api_url": config.api_url, "bank_id": config.bank_id},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def initialize_recall_capture(config: BetterHindsightConfig) -> None:
    """Start the bounded writer outside the recall response deadline."""

    if config.diagnostics.enabled:
        _ensure_capture_thread()


def enqueue_recall_capture(
    config: BetterHindsightConfig,
    *,
    query: str,
    request: Mapping[str, object],
    elapsed_ms: int,
    outcome: str,
    result_count: int | None = None,
    formatted_bytes: int | None = None,
    reason: str | None = None,
) -> str | None:
    """Queue one diagnostic write without extending the recall response deadline."""

    if not _capture_required(config, elapsed_ms=elapsed_ms, outcome=outcome):
        return None
    record_id = _new_record_id()
    writer = _capture_thread
    if writer is None or not writer.is_alive():
        raise DiagnosticRecordError("diagnostic_writer_unavailable")
    try:
        _capture_queue.put_nowait(
            _CaptureJob(
                config=config,
                record_id=record_id,
                query=query,
                request=dict(request),
                elapsed_ms=elapsed_ms,
                outcome=outcome,
                result_count=result_count,
                formatted_bytes=formatted_bytes,
                reason=reason,
            )
        )
    except queue.Full:
        raise DiagnosticRecordError("diagnostic_queue_full") from None
    return record_id


def capture_recall(
    config: BetterHindsightConfig,
    *,
    query: str,
    request: Mapping[str, object],
    elapsed_ms: int,
    outcome: str,
    result_count: int | None = None,
    formatted_bytes: int | None = None,
    reason: str | None = None,
    _record_id: str | None = None,
) -> str | None:
    """Persist one slow or failed recall; direct callers may use this synchronously."""

    if not _capture_required(config, elapsed_ms=elapsed_ms, outcome=outcome):
        return None
    diagnostics = config.diagnostics
    record_id = _record_id or _new_record_id()
    payload: dict[str, object] = {
        "elapsed_ms": max(0, elapsed_ms),
        "outcome": outcome,
        "query": query,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "record_id": record_id,
        "recorded_at": datetime.now(UTC).isoformat(),
        "request": dict(request),
        "schema": _RECORD_SCHEMA,
        "target_fingerprint": recall_target_fingerprint(config),
    }
    if result_count is not None:
        payload["result_count"] = max(0, result_count)
    if formatted_bytes is not None:
        payload["formatted_bytes"] = max(0, formatted_bytes)
    if reason is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
        payload["reason"] = reason
    with _exclusive_directory_lock(diagnostics.path):
        _write_record(diagnostics.path, record_id, payload)
        _prune_records(diagnostics.path, diagnostics.max_records)
    return record_id


def _capture_required(config: BetterHindsightConfig, *, elapsed_ms: int, outcome: str) -> bool:
    diagnostics = config.diagnostics
    return diagnostics.enabled and not (
        outcome in {"success", "empty"} and elapsed_ms < diagnostics.slow_threshold_ms
    )


def _new_record_id() -> str:
    return f"{time.time_ns():019d}-{uuid.uuid4().hex[:12]}"


def _ensure_capture_thread() -> None:
    global _capture_thread
    with _capture_thread_lock:
        if _capture_thread is not None and _capture_thread.is_alive():
            return
        _capture_thread = threading.Thread(
            target=_capture_worker,
            name="better-hindsight-diagnostics",
            daemon=True,
        )
        _capture_thread.start()


def _capture_worker() -> None:
    while True:
        job = _capture_queue.get()
        try:
            capture_recall(
                job.config,
                query=job.query,
                request=job.request,
                elapsed_ms=job.elapsed_ms,
                outcome=job.outcome,
                result_count=job.result_count,
                formatted_bytes=job.formatted_bytes,
                reason=job.reason,
                _record_id=job.record_id,
            )
        except Exception:
            with contextlib.suppress(Exception):
                emit_event(
                    _logger,
                    "better_hindsight.recall_diagnostic",
                    outcome="write_failed",
                )
        finally:
            _capture_queue.task_done()


def list_recall_records(config: BetterHindsightConfig) -> list[dict[str, object]]:
    """Return bounded, query-free summaries for the newest diagnostic records."""

    records: list[dict[str, object]] = []
    for path in _record_paths(config.diagnostics.path)[:_MAX_LIST_RECORDS]:
        payload = _read_record(path)
        summary: dict[str, object] = {
            "elapsed_ms": _required_nonnegative_int(payload, "elapsed_ms"),
            "outcome": _required_string(payload, "outcome", 64),
            "query_sha256": _required_sha256(payload, "query_sha256"),
            "record_id": _required_record_id(payload, path.stem),
            "recorded_at": _required_string(payload, "recorded_at", 64),
        }
        reason = payload.get("reason")
        if type(reason) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
            summary["reason"] = reason
        replay = payload.get("last_replay")
        if type(replay) is dict:
            replay_mapping = replay
            replay_outcome = replay_mapping.get("outcome")
            replay_elapsed = replay_mapping.get("elapsed_ms")
            replayed_at = replay_mapping.get("replayed_at")
            if type(replay_outcome) is str:
                summary["replay_outcome"] = replay_outcome[:64]
            if type(replay_elapsed) is int and replay_elapsed >= 0:
                summary["replay_elapsed_ms"] = replay_elapsed
            if type(replayed_at) is str:
                summary["replayed_at"] = replayed_at[:64]
        records.append(summary)
    return records


def load_recall_record(config: BetterHindsightConfig, record_id: str) -> dict[str, object]:
    """Load one exact local record after ID, schema, and destination validation."""

    if not _RECORD_ID.fullmatch(record_id):
        raise DiagnosticRecordError("diagnostic_record_invalid")
    path = config.diagnostics.path / f"{record_id}.json"
    try:
        payload = _read_record(path)
        _required_record_id(payload, record_id)
        if payload.get("schema") != _RECORD_SCHEMA:
            raise DiagnosticRecordError("diagnostic_record_invalid")
        query = _required_string(payload, "query", 65_536)
        if hashlib.sha256(query.encode("utf-8")).hexdigest() != _required_sha256(
            payload, "query_sha256"
        ):
            raise DiagnosticRecordError("diagnostic_record_invalid")
        if payload.get("target_fingerprint") != recall_target_fingerprint(config):
            raise DiagnosticRecordError("diagnostic_destination_mismatch")
        request = payload.get("request")
        if type(request) is not dict:
            raise DiagnosticRecordError("diagnostic_record_invalid")
    except DiagnosticRecordError:
        raise
    except Exception:
        raise DiagnosticRecordError("diagnostic_record_unavailable") from None
    return payload


def save_replay_result(
    config: BetterHindsightConfig,
    record_id: str,
    *,
    elapsed_ms: int,
    outcome: str,
    result_count: int | None = None,
    trace: Mapping[str, object] | None = None,
) -> None:
    """Atomically attach one safe replay result to an existing private record."""

    with _exclusive_directory_lock(config.diagnostics.path):
        payload = load_recall_record(config, record_id)
        replay: dict[str, object] = {
            "elapsed_ms": max(0, elapsed_ms),
            "outcome": outcome,
            "replayed_at": datetime.now(UTC).isoformat(),
        }
        if result_count is not None:
            replay["result_count"] = max(0, result_count)
        if trace is not None:
            replay["trace"] = dict(trace)
        payload["last_replay"] = replay
        _write_record(config.diagnostics.path, record_id, payload)


def safe_trace_payload(trace: object) -> dict[str, object] | None:
    """Project the typed client trace into JSON-safe operator output."""

    if trace is None:
        return None
    as_payload = getattr(trace, "as_dict", None)
    if not callable(as_payload):
        return None
    value = as_payload()
    return value if type(value) is dict else None


@contextmanager
def _exclusive_directory_lock(directory: Path) -> Iterator[None]:
    """Serialize record replacement and pruning across local processes."""

    descriptor: int | None = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        descriptor = os.open(
            directory / ".records.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise DiagnosticRecordError("diagnostic_record_lock_failed") from None
    assert descriptor is not None
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_record(directory: Path, record_id: str, payload: Mapping[str, object]) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            raise DiagnosticRecordError("diagnostic_record_too_large")
        temporary = directory / f".{record_id}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, directory / f"{record_id}.json")
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    except DiagnosticRecordError:
        raise
    except Exception:
        raise DiagnosticRecordError("diagnostic_record_write_failed") from None


def _read_record(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_RECORD_BYTES:
        raise DiagnosticRecordError("diagnostic_record_unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise DiagnosticRecordError("diagnostic_record_invalid")
    return value


def _record_paths(
    directory: Path, *, unavailable_ok: bool = False, regular_only: bool = False
) -> list[Path]:
    try:
        mode = os.lstat(directory).st_mode
        if not stat.S_ISDIR(mode):
            raise DiagnosticRecordError("diagnostic_store_unavailable")
        paths = [
            path
            for path in directory.glob("*.json")
            if _RECORD_ID.fullmatch(path.stem)
            and (not regular_only or (not path.is_symlink() and path.is_file()))
        ]
    except FileNotFoundError:
        return []
    except DiagnosticRecordError:
        if unavailable_ok:
            return []
        raise
    except OSError:
        if unavailable_ok:
            return []
        raise DiagnosticRecordError("diagnostic_store_unavailable") from None
    return sorted(paths, key=lambda item: item.name, reverse=True)


def _prune_records(directory: Path, max_records: int) -> None:
    for path in _record_paths(directory, unavailable_ok=True, regular_only=True)[max_records:]:
        try:
            path.unlink()
        except OSError:
            continue


def _required_record_id(payload: Mapping[str, object], expected: str) -> str:
    value = payload.get("record_id")
    if type(value) is not str or value != expected or not _RECORD_ID.fullmatch(value):
        raise DiagnosticRecordError("diagnostic_record_invalid")
    return value


def _required_string(payload: Mapping[str, object], key: str, maximum: int) -> str:
    value = payload.get(key)
    if type(value) is not str or not value or len(value) > maximum:
        raise DiagnosticRecordError("diagnostic_record_invalid")
    return value


def _required_nonnegative_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise DiagnosticRecordError("diagnostic_record_invalid")
    return value


def _required_sha256(payload: Mapping[str, object], key: str) -> str:
    value = _required_string(payload, key, 64)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise DiagnosticRecordError("diagnostic_record_invalid")
    return value


__all__ = [
    "DiagnosticRecordError",
    "capture_recall",
    "enqueue_recall_capture",
    "initialize_recall_capture",
    "list_recall_records",
    "load_recall_record",
    "recall_target_fingerprint",
    "safe_trace_payload",
    "save_replay_result",
]
