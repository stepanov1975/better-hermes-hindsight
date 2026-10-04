"""Public admission and evidence contracts for explicit multimodal memory."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from better_hermes_hindsight.client import RecallResponse, RecallResult
from better_hermes_hindsight.config import BetterHindsightConfig, ConfigError, load_config
from better_hermes_hindsight.formatting import format_recall_context, format_reflection_context
from better_hermes_hindsight.multimodal import (
    attachment_descriptors,
    build_multimodal_segment,
    canonical,
    decode_envelope,
)
from better_hermes_hindsight.outbox import AdmissionStatus, SQLiteOutbox, inspect_outbox
from better_hermes_hindsight.retention import RetentionConstructionError


def configuration(home: Path, **policy: object) -> BetterHindsightConfig:
    return load_config(
        home,
        injected={
            "single_principal": True,
            "retain": {"enabled": True},
            "multimodal": {"enabled": True, "allowed_roots": [str(home)], **policy},
        },
        environ={},
    )


def file_args(path: Path, kind: str = "image", media: str = "image/png") -> list[dict[str, str]]:
    return [{"path": str(path), "kind": kind, "media_type": media}]


def descriptor(identifier: str = "att_test") -> dict[str, object]:
    return {
        "id": identifier,
        "hash": "a" * 64,
        "kind": "image",
        "media_type": "image/png",
        "byte_size": 24,
        "url": "/v1/default/banks/test/attachments/" + identifier,
    }


def test_snapshot_survives_deletion_and_duplicate_admission(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    image = tmp_path / "sample.png"
    image.write_bytes(b"synthetic-image")
    segment = build_multimodal_segment(
        config, content="Caption", context=None, attachments=file_args(image)
    )
    image.unlink()
    envelope = decode_envelope(segment.content, config)
    assert base64.b64decode(envelope["blocks"][1]["source"]["data"]) == b"synthetic-image"
    assert str(image) not in segment.content
    box = SQLiteOutbox.open(config)
    try:
        assert box.admit([segment]).status is AdmissionStatus.ADMITTED
        assert box.admit([segment]).status is AdmissionStatus.DUPLICATE
    finally:
        box.close()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "empty",
        "directory",
        "outside",
        "symlink",
        "fifo",
        "blocked",
        "limit",
        "url",
        "mime",
        "shape",
    ],
)
def test_invalid_input_is_atomic_and_redacted(tmp_path: Path, case: str) -> None:
    config = configuration(tmp_path / "root", max_decoded_bytes=20)
    home = config.hermes_home
    home.mkdir()
    good = home / "good.png"
    good.write_bytes(b"good")
    bad = home / "invalid.png"
    args: list[dict[str, Any]] = file_args(bad)
    if case == "empty":
        bad.touch()
    elif case == "directory":
        bad.mkdir()
    elif case == "outside":
        bad = tmp_path / "outside.png"
        bad.write_bytes(b"outside")
        args = file_args(bad)
    elif case == "symlink":
        bad.symlink_to(good)
    elif case == "fifo":
        os.mkfifo(bad)
    elif case == "blocked":
        bad = home / ".env"
        bad.write_bytes(b"SECRET")
        args = file_args(bad)
    elif case == "limit":
        bad.write_bytes(b"x" * 21)
    elif case == "url":
        args[0]["path"] = "https://example.invalid/private.png"
    elif case == "mime":
        bad.write_bytes(b"data")
        args[0]["media_type"] = "image/svg+xml"
    elif case == "shape":
        args[0]["extra"] = "private"
    with pytest.raises(RetentionConstructionError) as failure:
        build_multimodal_segment(
            config, content="caption", context=None, attachments=[*file_args(good), *args]
        )
    assert str(failure.value) == "Better Hindsight multimodal input was rejected."
    assert not config.outbox.path.exists()


def test_policy_change_quarantines_binary_not_text_fingerprint(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    image = tmp_path / "x.png"
    image.write_bytes(b"data")
    segment = build_multimodal_segment(
        config, content="Caption", context=None, attachments=file_args(image)
    )
    box = SQLiteOutbox.open(config)
    assert box.admit([segment]).accepted
    box.close()
    changed = replace(config, multimodal=replace(config.multimodal, max_decoded_bytes=64))
    assert changed.destination_fingerprint == config.destination_fingerprint
    assert changed.multimodal_destination_fingerprint != config.multimodal_destination_fingerprint
    assert inspect_outbox(changed).mismatch_count == 1
    disabled = replace(config, multimodal=replace(config.multimodal, enabled=False))
    assert inspect_outbox(disabled).mismatch_count == 1
    box = SQLiteOutbox.open(changed)
    try:
        acquisition = box.try_acquire_profile_lock()
        assert acquisition.owner is not None
        try:
            assert box.claim_due(acquisition.owner, now=10**12).row is None
        finally:
            acquisition.owner.release()
    finally:
        box.close()


def test_tampered_binary_and_encoded_limit_rejected(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    image = tmp_path / "x.png"
    image.write_bytes(b"original")
    segment = build_multimodal_segment(
        config, content="Caption", context=None, attachments=file_args(image)
    )
    envelope = json.loads(segment.content)
    envelope["blocks"][1]["source"]["data"] = base64.b64encode(b"tampered").decode()
    with pytest.raises(ValueError):
        decode_envelope(canonical(envelope), config)
    tiny = replace(config, multimodal=replace(config.multimodal, max_encoded_bytes=32))
    with pytest.raises(RetentionConstructionError):
        build_multimodal_segment(
            tiny, content="caption", context=None, attachments=file_args(image)
        )


def test_descriptors_fail_independently_and_are_bounded() -> None:
    valid = descriptor()
    malformed = [
        {**valid, "kind": []},
        {**valid, "byte_size": True},
        {**valid, "hash": "bad"},
        {**valid, "url": "https://example.invalid/a"},
        {**valid, "url": "/v1/default/banks/other/attachments/att_test"},
    ]
    assert attachment_descriptors([*malformed, valid], bank_id="test") == (valid,)
    assert len(attachment_descriptors([valid] * 100, bank_id="test")) == 8
    assert attachment_descriptors({}, bank_id="test") == ()


def test_descriptor_cap_counts_valid_handles_after_malformed_entries() -> None:
    valid = [descriptor(f"att_{index}") for index in range(12)]
    malformed = [None, {}, {**descriptor(), "hash": "invalid"}] * 4
    assert attachment_descriptors([*malformed, *valid], bank_id="test") == tuple(valid[:8])


def test_descriptor_scan_respects_existing_nested_input_bound() -> None:
    from better_hermes_hindsight.client import HINDSIGHT_MAX_RECALL_NESTED_ITEMS

    padding = [None] * (HINDSIGHT_MAX_RECALL_NESTED_ITEMS - 1)
    assert attachment_descriptors([*padding, descriptor()], bank_id="test") == (descriptor(),)
    assert attachment_descriptors([*padding, None, descriptor()], bank_id="test") == ()


def test_dropped_handles_deduplicate_text_and_leave_room_for_next_memory() -> None:
    text_only = RecallResponse([RecallResult("a", "caption"), RecallResult("c", "other memory")])
    budget = len(format_recall_context(text_only, max_bytes=4096).encode())
    attached = RecallResponse(
        [
            RecallResult("a", "caption", attachments=(descriptor("one"),)),
            RecallResult("b", "caption", attachments=(descriptor("two"),)),
            RecallResult("c", "other memory"),
        ]
    )
    output = format_recall_context(attached, max_bytes=budget)
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert [record["memory"] for record in records] == ["caption", "other memory"]
    assert all("attachments" not in record for record in records)
    assert len(output.encode()) <= budget
    assert output == format_recall_context(text_only, max_bytes=budget)


def test_evidence_preserves_distinct_attachment_records_and_frame() -> None:
    records = [
        RecallResult("a", "caption", attachments=(descriptor("one"),)),
        RecallResult("b", "caption", attachments=(descriptor("two"),)),
    ]
    context = format_recall_context(RecallResponse(records), max_bytes=4096)
    assert context.count('"attachments"') == 2
    assert "RECALLED_MEMORY_EVIDENCE_BEGIN" in context
    assert len(context.encode()) <= 4096
    reflected = format_reflection_context(
        "Generated answer", max_bytes=4096, attachments=(descriptor(),)
    )
    assert '"attachments"' in reflected
    assert "RECALLED_MEMORY_EVIDENCE_END" in reflected


def test_new_config_is_default_off_and_strict(tmp_path: Path) -> None:
    config = load_config(tmp_path, environ={})
    assert not config.multimodal.enabled
    assert not config.recall.include_attachments
    assert not config.reflect.include_attachments
    for invalid in (
        {"enabled": True},
        {"enabled": True, "allowed_roots": ["relative"]},
        {"unknown": 1},
        {"max_attachments": 17},
        {"max_decoded_bytes": True},
    ):
        with pytest.raises(ConfigError):
            load_config(tmp_path, injected={"multimodal": invalid}, environ={})


@pytest.mark.parametrize("case", ["count", "aggregate", "traversal", "parent-symlink"])
def test_additional_input_bounds_and_components(tmp_path: Path, case: str) -> None:
    config = configuration(tmp_path, max_attachments=1, max_decoded_bytes=4)
    image = tmp_path / "image.png"
    image.write_bytes(b"1234")
    args = file_args(image)
    if case == "count":
        args *= 2
    elif case == "aggregate":
        config = replace(config, multimodal=replace(config.multimodal, max_attachments=2))
        args *= 2
    elif case == "traversal":
        args[0]["path"] = str(tmp_path / "sub" / ".." / "image.png")
    else:
        link = tmp_path / "alias"
        link.symlink_to(tmp_path, target_is_directory=True)
        args[0]["path"] = str(link / "image.png")
    with pytest.raises(RetentionConstructionError):
        build_multimodal_segment(config, content="synthetic", context=None, attachments=args)


@pytest.mark.parametrize("field", ["base64", "mime", "timestamp", "unknown", "schema"])
def test_persisted_envelope_rejects_malformed_fields(tmp_path: Path, field: str) -> None:
    config = configuration(tmp_path)
    image = tmp_path / "image.png"
    image.write_bytes(b"fixture")
    segment = build_multimodal_segment(
        config, content="synthetic", context=None, attachments=file_args(image)
    )
    value = json.loads(segment.content)
    if field == "base64":
        value["blocks"][1]["source"]["data"] = "bad!"
    elif field == "mime":
        value["blocks"][1]["source"]["media_type"] = "application/x-executable"
    elif field == "timestamp":
        value["timestamp"] = "2026-10-03T00:00:00"
    elif field == "unknown":
        value["path"] = str(image)
    else:
        value["schema"] = "unknown"
    with pytest.raises(ValueError):
        decode_envelope(canonical(value), config)


def test_queue_capacity_accounts_encoded_binary_and_is_atomic(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    config = replace(config, outbox=replace(config.outbox, max_pending_bytes=1024))
    image = tmp_path / "image.png"
    image.write_bytes(b"x" * 1024)
    segment = build_multimodal_segment(
        config, content="caption", context=None, attachments=file_args(image)
    )
    box = SQLiteOutbox.open(config)
    try:
        result = box.admit([segment])
        assert not result.accepted
        assert not box.read_unconfirmed()
        malformed = replace(segment, source_sha256="0" * 64)
        assert box.admit([malformed]).status is AdmissionStatus.INVALID
    finally:
        box.close()


def test_policy_fingerprint_covers_destination_and_retain_options(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    for altered in (
        replace(config, api_url="https://other.example.invalid"),
        replace(config, bank_id="other-bank"),
        replace(config, retain=replace(config.retain, tags=("different",))),
        replace(config, retain=replace(config.retain, enabled=False)),
        replace(config, multimodal=replace(config.multimodal, allowed_roots=(tmp_path / "other",))),
    ):
        assert (
            altered.multimodal_destination_fingerprint != config.multimodal_destination_fingerprint
        )


def test_unicode_attachment_filenames_redact_and_output_caps_are_complete() -> None:
    attached = {**descriptor(), "filename": "雪\napi_key=synthetic-credential-value.png"}
    projected = attachment_descriptors([attached], bank_id="test")
    assert projected
    assert "synthetic-credential" not in str(projected)
    assert "\n" not in str(projected[0]["filename"])
    response = RecallResponse([RecallResult("fact", "雪" * 1000, attachments=projected)])
    for limit in (100, 500, 1024, 4096):
        output = format_recall_context(response, max_bytes=limit)
        assert len(output.encode()) <= limit
        if limit == 100:
            assert not output
            continue
        assert output.endswith("[RECALLED_MEMORY_EVIDENCE_END]")
        records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        assert len(records) == 1
        assert records[0]["memory"].startswith("雪")
        if limit == 4096:
            assert records[0]["attachments"] == list(projected)
        else:
            assert "attachments" not in records[0]
