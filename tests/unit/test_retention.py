"""Contract tests for deterministic, privacy-safe retained turn construction."""

from __future__ import annotations

import hashlib
import json
import re

import pytest

import better_hermes_hindsight.retention as retention_module
from better_hermes_hindsight.config import PAYLOAD_SCHEMA_VERSION
from better_hermes_hindsight.redaction import REDACTION_MARKER
from better_hermes_hindsight.retention import (
    DOCUMENT_ID_PREFIX,
    RETENTION_REJECTED_MESSAGE,
    RetainedSegment,
    RetentionConstructionError,
    build_retained_segments,
)


def _build(
    *,
    session_id: object = "synthetic-session",
    user_content: object = "Synthetic user text.",
    assistant_content: object = "Synthetic assistant text.",
    tags: object = ("project:sample",),
    segment_max_bytes: object = 4096,
) -> tuple[RetainedSegment, ...]:
    return build_retained_segments(
        session_id=session_id,
        user_content=user_content,
        assistant_content=assistant_content,
        tags=tags,
        segment_max_bytes=segment_max_bytes,
    )


def _source(segments: tuple[RetainedSegment, ...]) -> str:
    return "".join(segment.content for segment in segments)


def _segment_digest(segment: RetainedSegment) -> str:
    record = json.dumps(
        {
            "content": segment.content,
            "payload_schema": segment.payload_schema,
            "segment_count": segment.segment_count,
            "segment_index": segment.segment_index,
            "source_sha256": segment.source_sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(record).hexdigest()


def test_literal_golden_document_identity_vector() -> None:
    """Freeze v1 identity without deriving the expected values through production code."""

    expected_source = (
        '{"payload_schema":"better-hindsight-turn-v1","roles":['
        '{"content":"Hello, 世界","role":"user"},'
        '{"content":"Acknowledged.","role":"assistant"}],'
        '"session_sha256":"0c36857851849f43582b86eaf6b2185d3538b471e3364dc3651feeca5df4a5c1",'
        '"tags":["project:sample","source:golden"]}'
    )
    expected_source_sha256 = "f0e8471dba822cf02c9781d78c03bc5837e791bc7194fbe1a59e8693d3794349"
    expected_payload_hash = "b3cd7d7f889a7413ce954507ee5fee9e5a073e11ab656c9a063c9618be2c551d"
    expected_document_id = (
        "better-hindsight-turn-v1:b3cd7d7f889a7413ce954507ee5fee9e5a073e11ab656c9a063c9618be2c551d"
    )

    segments = _build(
        session_id="session-golden",
        user_content="Hello, 世界",
        assistant_content="Acknowledged.",
        tags=("source:golden", "project:sample"),
        segment_max_bytes=4096,
    )

    assert len(segments) == 1
    segment = segments[0]
    assert segment.content == expected_source
    assert segment.source_sha256 == expected_source_sha256
    assert segment.payload_hash == expected_payload_hash
    assert segment.document_id == expected_document_id
    assert segment.payload_schema == "better-hindsight-turn-v1"
    assert segment.segment_index == 0
    assert segment.segment_count == 1


def test_canonical_source_has_explicit_roles_hashed_session_and_sorted_tags() -> None:
    raw_session = "SYNTHETIC_RAW_SESSION_CANARY"

    first = _build(
        session_id=raw_session,
        user_content="User role text",
        assistant_content="Assistant role text",
        tags=("zeta", "alpha"),
    )
    second = _build(
        session_id=raw_session,
        user_content="User role text",
        assistant_content="Assistant role text",
        tags=("alpha", "zeta"),
    )

    assert first == second
    source = _source(first)
    decoded = json.loads(source)
    assert decoded == {
        "payload_schema": PAYLOAD_SCHEMA_VERSION,
        "roles": [
            {"content": "User role text", "role": "user"},
            {"content": "Assistant role text", "role": "assistant"},
        ],
        "session_sha256": hashlib.sha256(raw_session.encode("utf-8")).hexdigest(),
        "tags": ["alpha", "zeta"],
    }
    assert raw_session not in source
    assert raw_session not in repr(first)


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_content", None),
        ("user_content", b"not-text"),
        ("user_content", ""),
        ("user_content", " \t\n"),
        ("assistant_content", None),
        ("assistant_content", b"not-text"),
        ("assistant_content", ""),
        ("assistant_content", " \t\n"),
    ],
)
def test_non_text_or_blank_role_content_is_rejected_with_one_fixed_error(
    field: str, value: object
) -> None:
    arguments: dict[str, object] = {
        "user_content": "Synthetic user text.",
        "assistant_content": "Synthetic assistant text.",
    }
    arguments[field] = value

    with pytest.raises(RetentionConstructionError) as caught:
        _build(**arguments)

    assert str(caught.value) == RETENTION_REJECTED_MESSAGE
    assert caught.value.__cause__ is None
    if value:
        assert str(value) not in str(caught.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": None},
        {"tags": "not-a-tag-list"},
        {"tags": ("valid", 7)},
        {"segment_max_bytes": 0},
        {"segment_max_bytes": True},
    ],
)
def test_other_malformed_construction_inputs_are_sanitized(overrides: dict[str, object]) -> None:
    with pytest.raises(RetentionConstructionError) as caught:
        _build(**overrides)

    assert str(caught.value) == RETENTION_REJECTED_MESSAGE
    assert caught.value.__cause__ is None


def test_shared_redactor_runs_before_hashing_segmentation_or_future_storage() -> None:
    private_label = "PRIVATE" + " KEY"
    private_header = "-----" + "BEGIN " + private_label + "-----"
    private_footer = "-----" + "END " + private_label + "-----"
    synthetic_private = f"{private_header}\nSYNTHETIC_PRIVATE_MATERIAL\n{private_footer}"
    authorization_value = "SYNTHETIC_" + "AUTH_" + "VALUE"
    synthetic_values = (
        "api_key=SYNTHETIC_API_VALUE",
        "Bearer SYNTHETIC_BEARER_VALUE",
        "Authorization: Basic " + authorization_value,
        "https://synthetic-user:synthetic-pass@example.test/path",
        synthetic_private,
    )
    raw_text = "\n".join(synthetic_values)

    segments = _build(user_content=raw_text, assistant_content="Synthetic safe response.")

    source = _source(segments)
    assert REDACTION_MARKER in source
    for value in (
        "SYNTHETIC_API_VALUE",
        "SYNTHETIC_BEARER_VALUE",
        authorization_value,
        "synthetic-pass",
        "SYNTHETIC_PRIVATE_MATERIAL",
    ):
        assert value not in source
        assert value not in repr(segments)
    assert segments[0].source_sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()


def test_retain_tags_use_one_sorted_redacted_canonical_tuple() -> None:
    raw_tag_value = "SYNTHETIC_RETAIN_TAG_VALUE"

    segments = _build(
        tags=("zeta", f"api_key={raw_tag_value}", "alpha"),
    )

    source = _source(segments)
    assert json.loads(source)["tags"] == ["alpha", f"api_key={REDACTION_MARKER}", "zeta"]
    assert raw_tag_value not in source
    assert raw_tag_value not in repr(segments)


def test_retain_tag_redaction_collision_rejects_the_complete_turn() -> None:
    with pytest.raises(RetentionConstructionError) as caught:
        _build(
            tags=(
                "api_key=SYNTHETIC_COLLISION_ONE",
                "api_key=SYNTHETIC_COLLISION_TWO",
            )
        )

    assert str(caught.value) == RETENTION_REJECTED_MESSAGE
    assert caught.value.__cause__ is None


def test_redaction_failure_rejects_the_complete_turn_with_no_sensitive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "SYNTHETIC_REDACTION_FAILURE_CANARY"

    def fail_redaction(text: str) -> str:
        raise RuntimeError(text)

    monkeypatch.setattr(retention_module, "redact_sensitive_text", fail_redaction)

    with pytest.raises(RetentionConstructionError) as caught:
        _build(user_content=canary)

    assert str(caught.value) == RETENTION_REJECTED_MESSAGE
    assert canary not in str(caught.value)
    assert caught.value.__cause__ is None


def test_input_bearing_retention_error_is_recanonicalized_at_the_public_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "SYNTHETIC_NESTED_RETENTION_ERROR_CANARY"

    def fail_redaction(_text: str) -> str:
        raise RetentionConstructionError(canary)

    monkeypatch.setattr(retention_module, "redact_sensitive_text", fail_redaction)

    with pytest.raises(RetentionConstructionError) as caught:
        _build()

    assert str(caught.value) == RETENTION_REJECTED_MESSAGE
    assert canary not in str(caught.value)
    assert caught.value.__cause__ is None


def test_utf8_segmentation_round_trips_at_code_point_boundaries() -> None:
    segments = _build(
        user_content="🙂漢字é" * 80,
        assistant_content="終わり🌍" * 60,
        tags=("unicode",),
        segment_max_bytes=37,
    )

    source = _source(segments)
    decoded = json.loads(source)
    assert decoded["roles"][0]["content"] == "🙂漢字é" * 80
    assert decoded["roles"][1]["content"] == "終わり🌍" * 60
    assert all(len(segment.content.encode("utf-8")) <= 37 for segment in segments)
    assert [segment.segment_index for segment in segments] == list(range(len(segments)))
    assert {segment.segment_count for segment in segments} == {len(segments)}
    assert {segment.source_sha256 for segment in segments} == {
        hashlib.sha256(source.encode("utf-8")).hexdigest()
    }


def test_a_single_code_point_that_cannot_fit_rejects_the_whole_turn() -> None:
    with pytest.raises(RetentionConstructionError) as caught:
        _build(user_content="🙂", segment_max_bytes=3)

    assert str(caught.value) == RETENTION_REJECTED_MESSAGE


def test_each_segment_hash_and_document_id_derive_from_the_exact_canonical_record() -> None:
    segments = _build(
        user_content="u" * 500,
        assistant_content="a" * 500,
        segment_max_bytes=73,
    )

    assert len(segments) > 1
    for segment in segments:
        expected_hash = _segment_digest(segment)
        assert segment.payload_hash == expected_hash
        assert segment.document_id == DOCUMENT_ID_PREFIX + expected_hash
        assert re.fullmatch(r"better-hindsight-turn-v1:[0-9a-f]{64}", segment.document_id)


def test_segment_dataclass_repr_never_exposes_content() -> None:
    canary = "SYNTHETIC_REPR_CONTENT_CANARY"
    segment = _build(user_content=canary)[0]

    rendered = repr(segment)
    assert canary not in rendered
    assert segment.content not in rendered
    assert "document_id=" in rendered


def test_construction_is_deterministic_across_repeated_calls() -> None:
    def build() -> tuple[RetainedSegment, ...]:
        return _build(
            session_id="stable-session",
            user_content="stable user",
            assistant_content="stable assistant",
            tags=("beta", "alpha"),
            segment_max_bytes=41,
        )

    assert build() == build()
