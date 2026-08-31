"""Deterministic, privacy-safe construction of retained turn segments."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn

from .config import PAYLOAD_SCHEMA_VERSION, canonicalize_retain_tags
from .redaction import redact_sensitive_text

DOCUMENT_ID_PREFIX = "better-hindsight-turn-v1:"
RETENTION_REJECTED_MESSAGE = "Better Hindsight retention input was rejected."
RETAINED_EVENT_RECORD_SCHEMA = "better-hindsight-retained-event-v2"
_EVENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class RetentionConstructionError(ValueError):
    """A fixed, sanitized retained-turn construction failure."""


class RetentionCapacityError(RetentionConstructionError):
    """A fixed construction-time segment-cap rejection."""


class _RetentionCapacityExceeded(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RetainedSegment:
    """One immutable segment of a complete canonical redacted turn."""

    document_id: str
    payload_hash: str
    payload_schema: str
    source_sha256: str
    segment_index: int
    segment_count: int
    content: str = field(repr=False)


def build_retained_segments(
    *,
    session_id: object,
    user_content: object,
    assistant_content: object,
    tags: object,
    segment_max_bytes: object,
    segment_count_limit: object = None,
) -> tuple[RetainedSegment, ...]:
    """Build canonical redacted segments for one completed Hermes callback.

    The raw session identifier is used only as SHA-256 input. Role content and configured tags pass
    through the shared high-confidence redactor before the canonical source is hashed or segmented.
    Every failure crosses this boundary as one fixed message with no chained input-bearing
    exception.
    """

    try:
        return _build_retained_segments(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
            tags=tags,
            segment_max_bytes=segment_max_bytes,
            segment_count_limit=segment_count_limit,
        )
    except _RetentionCapacityExceeded:
        raise RetentionCapacityError(RETENTION_REJECTED_MESSAGE) from None
    except Exception:
        raise RetentionConstructionError(RETENTION_REJECTED_MESSAGE) from None


def derive_segment_payload_hash(
    *,
    payload_schema: str,
    source_sha256: str,
    segment_index: int,
    segment_count: int,
    content: str,
) -> str:
    """Return the v1 digest of the exact canonical segment record without ``document_id``."""

    try:
        record = _canonical_json(
            {
                "content": content,
                "payload_schema": payload_schema,
                "segment_count": segment_count,
                "segment_index": segment_index,
                "source_sha256": source_sha256,
            }
        ).encode("utf-8")
        return hashlib.sha256(record).hexdigest()
    except Exception:
        raise RetentionConstructionError(RETENTION_REJECTED_MESSAGE) from None


def _build_retained_segments(
    *,
    session_id: object,
    user_content: object,
    assistant_content: object,
    tags: object,
    segment_max_bytes: object,
    segment_count_limit: object,
) -> tuple[RetainedSegment, ...]:
    if not isinstance(session_id, str):
        _reject()
    if not isinstance(user_content, str) or not user_content.strip():
        _reject()
    if not isinstance(assistant_content, str) or not assistant_content.strip():
        _reject()
    if type(segment_max_bytes) is not int or segment_max_bytes <= 0:
        _reject()
    if segment_count_limit is None:
        validated_segment_count_limit = None
    elif (
        isinstance(segment_count_limit, int)
        and not isinstance(segment_count_limit, bool)
        and segment_count_limit > 0
    ):
        validated_segment_count_limit = segment_count_limit
    else:
        _reject()
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes, bytearray)):
        _reject()
    if not all(isinstance(tag, str) and tag and tag.strip() == tag for tag in tags):
        _reject()
    if len(set(tags)) != len(tags):
        _reject()

    canonical_tags = canonicalize_retain_tags(tags)
    redacted_user = redact_sensitive_text(user_content)
    redacted_assistant = redact_sensitive_text(assistant_content)
    if not isinstance(redacted_user, str) or not isinstance(redacted_assistant, str):
        _reject()

    session_sha256 = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    event_id = _new_event_id()
    occurred_at = _capture_occurrence_time()
    if _EVENT_ID_PATTERN.fullmatch(event_id) is None or not occurred_at:
        _reject()
    common: dict[str, object] = {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "payload_schema": PAYLOAD_SCHEMA_VERSION,
        "record_schema": RETAINED_EVENT_RECORD_SCHEMA,
        "session_sha256": session_sha256,
        "tags": list(canonical_tags),
    }
    roles = (
        {"role": "user", "content": redacted_user},
        {"role": "assistant", "content": redacted_assistant},
    )
    contents = _semantic_contents(
        common=common,
        roles=roles,
        max_bytes=segment_max_bytes,
        segment_count_limit=validated_segment_count_limit,
    )
    source_sha256 = hashlib.sha256("".join(contents).encode("utf-8")).hexdigest()
    segment_count = len(contents)

    segments: list[RetainedSegment] = []
    for segment_index, content in enumerate(contents):
        payload_hash = derive_segment_payload_hash(
            payload_schema=PAYLOAD_SCHEMA_VERSION,
            source_sha256=source_sha256,
            segment_index=segment_index,
            segment_count=segment_count,
            content=content,
        )
        segments.append(
            RetainedSegment(
                document_id=DOCUMENT_ID_PREFIX + payload_hash,
                payload_hash=payload_hash,
                payload_schema=PAYLOAD_SCHEMA_VERSION,
                source_sha256=source_sha256,
                segment_index=segment_index,
                segment_count=segment_count,
                content=content,
            )
        )
    return tuple(segments)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _semantic_contents(
    *,
    common: dict[str, object],
    roles: Sequence[dict[str, str]],
    max_bytes: int,
    segment_count_limit: int | None,
) -> tuple[str, ...]:
    complete = _event_content(common=common, roles=roles)
    if len(complete.encode("utf-8")) <= max_bytes:
        if segment_count_limit is not None and segment_count_limit < 1:
            raise _RetentionCapacityExceeded from None
        return (complete,)

    contents: list[str] = []
    for role in roles:
        role_name = role["role"]
        complete_role = _event_content(common=common, roles=(role,))
        if len(complete_role.encode("utf-8")) <= max_bytes:
            contents.append(complete_role)
            if segment_count_limit is not None and len(contents) > segment_count_limit:
                raise _RetentionCapacityExceeded from None
            continue

        for paragraph in _semantic_paragraphs(role["content"]):
            content = _event_content(
                common=common,
                roles=({"role": role_name, "content": paragraph},),
            )
            if len(content.encode("utf-8")) > max_bytes:
                raise _RetentionCapacityExceeded from None
            contents.append(content)
            if segment_count_limit is not None and len(contents) > segment_count_limit:
                raise _RetentionCapacityExceeded from None
    if not contents:
        _reject()
    return tuple(contents)


def _semantic_paragraphs(text: str) -> tuple[str, ...]:
    paragraphs = tuple(part for part in text.split("\n\n") if part.strip())
    if not paragraphs:
        _reject()
    return paragraphs


def _event_content(
    *,
    common: dict[str, object],
    roles: Sequence[dict[str, str]],
) -> str:
    return _canonical_json({**common, "roles": list(roles)})


def retained_event_timestamp(content: str) -> str | None:
    """Read a v2 occurrence timestamp from persisted content; legacy v1 content has none."""

    try:
        value = json.loads(content)
        if type(value) is not dict or value.get("record_schema") != RETAINED_EVENT_RECORD_SCHEMA:
            return None
        timestamp = value.get("occurred_at")
        if not isinstance(timestamp, str) or not timestamp:
            return None
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return timestamp
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _new_event_id() -> str:
    return uuid.uuid4().hex


def _capture_occurrence_time() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _reject() -> NoReturn:
    raise RetentionConstructionError(RETENTION_REJECTED_MESSAGE) from None


__all__ = [
    "DOCUMENT_ID_PREFIX",
    "RETENTION_REJECTED_MESSAGE",
    "RetainedSegment",
    "RetentionCapacityError",
    "RetentionConstructionError",
    "build_retained_segments",
    "derive_segment_payload_hash",
    "retained_event_timestamp",
]
