"""Deterministic, privacy-safe construction of retained turn segments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NoReturn

from better_hermes_hindsight.config import PAYLOAD_SCHEMA_VERSION, canonicalize_retain_tags
from better_hermes_hindsight.redaction import redact_sensitive_text

DOCUMENT_ID_PREFIX = "better-hindsight-turn-v1:"
RETENTION_REJECTED_MESSAGE = "Better Hindsight retention input was rejected."


class RetentionConstructionError(ValueError):
    """A fixed, sanitized retained-turn construction failure."""


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
        )
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
) -> tuple[RetainedSegment, ...]:
    if not isinstance(session_id, str):
        _reject()
    if not isinstance(user_content, str) or not user_content.strip():
        _reject()
    if not isinstance(assistant_content, str) or not assistant_content.strip():
        _reject()
    if type(segment_max_bytes) is not int or segment_max_bytes <= 0:
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
    source = _canonical_json(
        {
            "payload_schema": PAYLOAD_SCHEMA_VERSION,
            "roles": [
                {"role": "user", "content": redacted_user},
                {"role": "assistant", "content": redacted_assistant},
            ],
            "session_sha256": session_sha256,
            "tags": list(canonical_tags),
        }
    )
    source_bytes = source.encode("utf-8")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    contents = _segment_utf8(source, segment_max_bytes)
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


def _segment_utf8(text: str, max_bytes: int) -> tuple[str, ...]:
    segments: list[str] = []
    characters: list[str] = []
    used_bytes = 0
    for character in text:
        character_bytes = len(character.encode("utf-8"))
        if character_bytes > max_bytes:
            _reject()
        if characters and used_bytes + character_bytes > max_bytes:
            segments.append("".join(characters))
            characters = []
            used_bytes = 0
        characters.append(character)
        used_bytes += character_bytes
    if characters:
        segments.append("".join(characters))
    if not segments:
        _reject()
    return tuple(segments)


def _reject() -> NoReturn:
    raise RetentionConstructionError(RETENTION_REJECTED_MESSAGE) from None


__all__ = [
    "DOCUMENT_ID_PREFIX",
    "RETENTION_REJECTED_MESSAGE",
    "RetainedSegment",
    "RetentionConstructionError",
    "build_retained_segments",
    "derive_segment_payload_hash",
]
