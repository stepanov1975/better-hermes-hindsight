"""Deterministic, privacy-safe construction of retained turn segments."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NoReturn

from .config import (
    PAYLOAD_SCHEMA_VERSION,
    RETAINED_EVENT_RECORD_SCHEMA,
    RETAINED_MODEL_RECORD_SCHEMA,
    canonicalize_retain_tags,
)
from .redaction import redact_sensitive_text

DOCUMENT_ID_PREFIX = f"{PAYLOAD_SCHEMA_VERSION}:"
RETENTION_REJECTED_MESSAGE = "Better Hindsight retention input was rejected."

_EVENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_PARAGRAPH_SEPARATOR_PATTERN = re.compile(r"(?:\r\n|[\r\n])(?:[ \t]*(?:\r\n|[\r\n]))+")


class RetentionConstructionError(ValueError):
    """A fixed, sanitized retained-turn construction failure."""


class RetentionCapacityError(RetentionConstructionError):
    """A fixed construction-time segment-cap rejection."""


class _RetentionCapacityExceeded(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _RetainedRole:
    name: str
    content: str
    prefix: str | None = None


@dataclass(frozen=True, slots=True)
class RetainedSegment:
    """One immutable, independently decodable record for a retained occurrence."""

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
    assistant_context: object = None,
    model_selected: object = False,
) -> tuple[RetainedSegment, ...]:
    """Build canonical redacted segments for one completed Hermes callback.

    The raw session identifier is used only as SHA-256 input. Role content and configured tags pass
    through the shared high-confidence redactor before semantic records are encoded and hashed.
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
            assistant_context=assistant_context,
            model_selected=model_selected,
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
    assistant_context: object,
    model_selected: object,
) -> tuple[RetainedSegment, ...]:
    if not isinstance(session_id, str):
        _reject()
    if not isinstance(user_content, str) or not user_content.strip():
        _reject()
    if not isinstance(assistant_content, str) or not assistant_content.strip():
        _reject()
    if type(segment_max_bytes) is not int or segment_max_bytes <= 0:
        _reject()
    if type(model_selected) is not bool:
        _reject()
    if assistant_context is not None and (
        not isinstance(assistant_context, str) or not assistant_context.strip()
    ):
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
    redacted_context = (
        None if assistant_context is None else redact_sensitive_text(assistant_context)
    )
    if (
        not isinstance(redacted_user, str)
        or not isinstance(redacted_assistant, str)
        or (redacted_context is not None and not isinstance(redacted_context, str))
    ):
        _reject()

    session_sha256 = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    common: dict[str, object] = {
        "payload_schema": PAYLOAD_SCHEMA_VERSION,
        "session_sha256": session_sha256,
        "tags": list(canonical_tags),
    }
    roles = (
        _RetainedRole(name="user", content=redacted_user),
        _RetainedRole(
            name="assistant",
            content=redacted_assistant,
            prefix=None if redacted_context is None else f"Context: {redacted_context}",
        ),
    )
    if model_selected:
        identity_source = _canonical_json(
            {
                **common,
                "record_schema": RETAINED_MODEL_RECORD_SCHEMA,
                "roles": [_role_payload(role) for role in roles],
            }
        )
        common.update(
            {
                "memory_id": hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:32],
                "record_schema": RETAINED_MODEL_RECORD_SCHEMA,
            }
        )
    else:
        event_id = _new_event_id()
        occurred_at = _capture_occurrence_time()
        if _EVENT_ID_PATTERN.fullmatch(event_id) is None or not occurred_at:
            _reject()
        common.update(
            {
                "event_id": event_id,
                "occurred_at": occurred_at,
                "record_schema": RETAINED_EVENT_RECORD_SCHEMA,
            }
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
    roles: Sequence[_RetainedRole],
    max_bytes: int,
    segment_count_limit: int | None,
) -> tuple[str, ...]:
    complete = _event_content(
        common=common,
        roles=tuple(_role_payload(role) for role in roles),
    )
    if len(complete.encode("utf-8")) <= max_bytes:
        if segment_count_limit is not None and segment_count_limit < 1:
            raise _RetentionCapacityExceeded from None
        return (complete,)

    contents: list[str] = []
    for role in roles:
        complete_role = _event_content(common=common, roles=(_role_payload(role),))
        if len(complete_role.encode("utf-8")) <= max_bytes:
            contents.append(complete_role)
            if segment_count_limit is not None and len(contents) > segment_count_limit:
                raise _RetentionCapacityExceeded from None
            continue

        for paragraph in _semantic_paragraphs(role.content):
            content = _event_content(
                common=common,
                roles=(_role_payload(role, content=paragraph),),
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
    paragraphs = tuple(part for part in _PARAGRAPH_SEPARATOR_PATTERN.split(text) if part.strip())
    if not paragraphs:
        _reject()
    return paragraphs


def _event_content(
    *,
    common: dict[str, object],
    roles: Sequence[dict[str, str]],
) -> str:
    return _canonical_json({**common, "roles": list(roles)})


def _role_payload(role: _RetainedRole, *, content: str | None = None) -> dict[str, str]:
    rendered = role.content if content is None else content
    if role.prefix is not None:
        rendered = f"{role.prefix}\n\n{rendered}"
    return {"role": role.name, "content": rendered}


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
    return datetime.now(UTC).isoformat(timespec="microseconds")


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
