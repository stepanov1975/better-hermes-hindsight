"""Local immutable binary snapshots and bounded attachment provenance.

No URL downloads or path-bearing diagnostics. Binary bytes are NOT text-redacted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import MULTIMODAL_PAYLOAD_SCHEMA, BetterHindsightConfig
from .redaction import redact_sensitive_text
from .retention import RetainedSegment, RetentionConstructionError, derive_segment_payload_hash

INVALID = "Better Hindsight multimodal input was rejected."
_MEDIA = {
    "image": {"image/png", "image/jpeg", "image/webp", "image/gif"},
    "file": {"application/pdf", "text/plain"},
}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def build_multimodal_segment(
    config: BetterHindsightConfig, *, content: str, context: str | None, attachments: object
) -> RetainedSegment:
    """Read bounded files once, atomically construct one independently replayable row."""
    try:
        policy = config.multimodal
        if not policy.enabled or not config.retain.enabled:
            raise ValueError
        if type(attachments) is not list or not 1 <= len(attachments) <= policy.max_attachments:
            raise ValueError
        if not content.strip() or len(content) > 16_384:
            raise ValueError
        blocks: list[dict[str, Any]] = [{"type": "text", "text": redact_sensitive_text(content)}]
        hashes: list[str] = []
        remaining = policy.max_decoded_bytes
        for item in attachments:
            if type(item) is not dict or set(item) != {"path", "kind", "media_type"}:
                raise ValueError
            kind, media = item["kind"], item["media_type"]
            if type(kind) is not str or kind not in _MEDIA or media not in _MEDIA[kind]:
                raise ValueError
            path = item["path"]
            if type(path) is not str or len(path) > 4096:
                raise ValueError
            raw = _read_local(Path(path), policy.allowed_roots, remaining)
            remaining -= len(raw)
            block: dict[str, Any] = {
                "type": kind,
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            }
            if kind == "file":
                block["filename"] = _safe_filename(Path(path).name)
            blocks.append(block)
            hashes.append(hashlib.sha256(raw).hexdigest())
        envelope = canonical(
            {
                "schema": MULTIMODAL_PAYLOAD_SCHEMA,
                "destination": config.multimodal_destination_fingerprint,
                "timestamp": datetime.now(UTC).isoformat(),
                "context": None if context is None else redact_sensitive_text(context),
                "blocks": blocks,
                "sha256": hashes,
            }
        )
        decode_envelope(envelope, config)
        source = hashlib.sha256(envelope.encode()).hexdigest()
        digest = derive_segment_payload_hash(
            payload_schema=MULTIMODAL_PAYLOAD_SCHEMA,
            source_sha256=source,
            segment_index=0,
            segment_count=1,
            content=envelope,
        )
        return RetainedSegment(
            document_id=MULTIMODAL_PAYLOAD_SCHEMA + ":" + digest,
            payload_hash=digest,
            payload_schema=MULTIMODAL_PAYLOAD_SCHEMA,
            source_sha256=source,
            segment_index=0,
            segment_count=1,
            content=envelope,
        )
    except Exception:
        raise RetentionConstructionError(INVALID) from None


def _read_local(path: Path, roots: tuple[Path, ...], maximum: int) -> bytes:
    from agent.file_safety import raise_if_read_blocked

    if not path.is_absolute() or ".." in path.parts or maximum <= 0:
        raise ValueError
    # Reject every symlink component, not just the final filename. Walk open directory
    # descriptors with NOFOLLOW so a concurrent directory replacement cannot escape roots.
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError
    raise_if_read_blocked(str(path))
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open("/", flags | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(path.name, flags, dir_fd=descriptor)
        try:
            status = os.fstat(file_descriptor)
            if not stat.S_ISREG(status.st_mode) or not 0 < status.st_size <= maximum:
                raise ValueError
            # Every directory and the file were opened with NOFOLLOW: the authorized
            # absolute spelling cannot resolve through a symlink to a blocked target.
            # Refuse replacement between the policy check and the descriptor open.
            current = path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (status.st_dev, status.st_ino):
                raise ValueError
            with os.fdopen(file_descriptor, "rb", closefd=False) as stream:
                data = stream.read(maximum + 1)
            after = os.fstat(file_descriptor)
            if (
                len(data) != status.st_size
                or len(data) > maximum
                or (status.st_size, status.st_mtime_ns, status.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ValueError
            return data
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _safe_filename(value: str) -> str:
    value = redact_sensitive_text(value)
    return "".join(c for c in value if c.isprintable() and c not in "/\\")[:200] or "attachment"


def decode_envelope(content: str, config: BetterHindsightConfig) -> dict[str, Any]:
    """Strict bounded persisted-row validation; never read original source paths."""
    if len(content.encode()) > config.multimodal.max_encoded_bytes:
        raise ValueError
    value = json.loads(content)
    if type(value) is not dict or set(value) != {
        "schema",
        "destination",
        "timestamp",
        "context",
        "blocks",
        "sha256",
    }:
        raise ValueError
    if (
        value["schema"] != MULTIMODAL_PAYLOAD_SCHEMA
        or value["destination"] != config.multimodal_destination_fingerprint
    ):
        raise ValueError
    if type(value["timestamp"]) is not str or len(value["timestamp"]) > 64:
        raise ValueError
    if datetime.fromisoformat(value["timestamp"]).tzinfo is None:
        raise ValueError
    context = value["context"]
    if context is not None and (type(context) is not str or len(context) > 1024):
        raise ValueError
    blocks, hashes = value["blocks"], value["sha256"]
    if type(blocks) is not list or not 2 <= len(blocks) <= config.multimodal.max_attachments + 1:
        raise ValueError
    if type(hashes) is not list or len(hashes) != len(blocks) - 1:
        raise ValueError
    first = blocks[0]
    if (
        type(first) is not dict
        or set(first) != {"type", "text"}
        or first["type"] != "text"
        or type(first["text"]) is not str
        or not first["text"].strip()
        or len(first["text"]) > 16384
    ):
        raise ValueError
    total = 0
    for block, digest in zip(blocks[1:], hashes, strict=True):
        if type(block) is not dict or block.get("type") not in _MEDIA:
            raise ValueError
        kind = block["type"]
        if set(block) != ({"type", "source", "filename"} if kind == "file" else {"type", "source"}):
            raise ValueError
        if kind == "file" and (
            type(block["filename"]) is not str
            or _safe_filename(block["filename"]) != block["filename"]
        ):
            raise ValueError
        source = block["source"]
        if (
            type(source) is not dict
            or set(source) != {"type", "media_type", "data"}
            or source["type"] != "base64"
            or source["media_type"] not in _MEDIA[kind]
            or type(source["data"]) is not str
        ):
            raise ValueError
        raw = base64.b64decode(source["data"], validate=True)
        total += len(raw)
        if (
            not raw
            or total > config.multimodal.max_decoded_bytes
            or base64.b64encode(raw).decode() != source["data"]
            or type(digest) is not str
            or _HASH.fullmatch(digest) is None
            or hashlib.sha256(raw).hexdigest() != digest
        ):
            raise ValueError
    if canonical(value) != content:
        raise ValueError
    return value


def attachment_descriptors(value: object, *, bank_id: str) -> tuple[dict[str, object], ...]:
    """Best-effort independent optional descriptors, no fetching or arbitrary metadata."""
    if type(value) is not list:
        return ()
    result: list[dict[str, object]] = []
    prefix = f"/v1/default/banks/{quote(bank_id, safe='')}/attachments/"
    from .client import HINDSIGHT_MAX_RECALL_NESTED_ITEMS

    for item in value[:HINDSIGHT_MAX_RECALL_NESTED_ITEMS]:
        if type(item) is not dict:
            continue
        identifier, digest, kind = item.get("id"), item.get("hash"), item.get("kind")
        media, size, url = item.get("media_type"), item.get("byte_size"), item.get("url")
        if (
            type(identifier) is not str
            or _ID.fullmatch(identifier) is None
            or type(digest) is not str
            or _HASH.fullmatch(digest) is None
            or type(kind) is not str
            or kind not in {"image", "file"}
            or type(media) is not str
            or len(media) > 100
            or re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", media) is None
            or type(size) is not int
            or not 0 <= size <= 1_073_741_824
        ):
            continue
        if url != prefix + identifier:
            continue
        descriptor: dict[str, object] = {
            "id": identifier,
            "hash": digest,
            "kind": kind,
            "media_type": media,
            "byte_size": size,
            "url": url,
        }
        filename = item.get("filename")
        if type(filename) is str and len(filename) <= 200:
            descriptor["filename"] = _safe_filename(filename)
        result.append(descriptor)
        if len(result) == 8:
            break
    return tuple(result)
