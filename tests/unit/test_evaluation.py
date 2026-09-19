"""Private evaluation storage: synthetic data only, no remote service."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from better_hermes_hindsight.config import BetterHindsightConfig, EvaluationConfig, load_config
from better_hermes_hindsight.evaluation import EvaluationCapture, model_metadata


def _config(home: Path, **policy: object) -> BetterHindsightConfig:
    directory = home / "better_hindsight"
    directory.mkdir(exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"evaluation": policy}))
    return load_config(home)


def test_capture_private_redacted_bounded_and_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enabled=True, max_records=2, max_age_seconds=60)
    config = replace(config, api_key="test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "opaque-router-secret")
    capture = EvaluationCapture(config, uuid.uuid4().hex)
    capture.stage(
        "input",
        capsule={
            "current_user_message": (
                "Authorization: Bearer fixture-secret\napi_key=abcdefghi "
                "test-key opaque-router-secret"
            )
        },
    )
    directory = tmp_path / "better_hindsight/planner_evaluation"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    files = list(directory.glob("*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    for secret in ("fixture-secret", "abcdefghi", "test-key", "opaque-router-secret"):
        assert secret not in text
    assert "[REDACTED]" in text
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    unrelated = directory / "notes.json"
    unrelated.write_text("operator notes")
    outside = tmp_path / "outside.json"
    outside.write_text("not ours")
    (directory / "0000000000000000-deadbeefcafe.json").symlink_to(outside)
    os.utime(files[0], (0, 0))
    capture.stage("plan", action="skip")
    assert not files[0].exists()
    for _ in range(5):
        capture.stage("retrieval", outcome="skip")
    records = [p for p in directory.glob("*.json") if p != unrelated and not p.is_symlink()]
    assert len(records) == 2
    assert all(p.stat().st_size <= config.evaluation.max_record_bytes for p in records)
    assert unrelated.read_text() == "operator notes"
    assert outside.read_text() == "not ours"
    bounded = EvaluationCapture(
        replace(
            config, evaluation=EvaluationConfig(enabled=True, max_record_bytes=100, max_records=10)
        ),
        uuid.uuid4().hex,
    )
    bounded.stage("input", capsule="x" * 1000)
    assert len([p for p in directory.glob("*.json") if p != unrelated and not p.is_symlink()]) == 2


def test_default_disabled_and_symlink_store_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    capture = EvaluationCapture(config, uuid.uuid4().hex)
    capture.stage("input", capsule="not persisted")
    directory = tmp_path / "better_hindsight/planner_evaluation"
    assert not directory.exists()
    outside = tmp_path / "other"
    outside.mkdir()
    directory.symlink_to(outside, target_is_directory=True)
    EvaluationCapture(
        replace(config, evaluation=EvaluationConfig(enabled=True)), uuid.uuid4().hex
    ).stage("input", capsule="not persisted")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("fence", ["cancel", "mismatch", "expired", "consumed"])
def test_correlation_obeys_mailbox_fences(tmp_path: Path, fence: str) -> None:
    from better_hermes_hindsight.plan_mailbox import InMemoryPlanMailbox

    now = [100.0]
    mailbox = InMemoryPlanMailbox(tmp_path, monotonic=lambda: now[0])
    activation = mailbox.activate(session_id="session")
    owner = mailbox.begin_turn(session_id="session", turn_id="turn")
    assert owner is not None
    assert mailbox.reserve(
        source_query="same query",
        session_id="session",
        parent_session_id="",
        turn_id="turn",
        mode="active",
        owner_token=owner,
        evaluation_id=owner,
    )
    assert mailbox.finalize(
        turn_id="turn", mode="active", action="skip", rewritten_query=None, owner_token=owner
    )
    observed: list[str] = []
    if fence == "cancel":
        mailbox.cancel(turn_id="turn", owner_token=owner)
    elif fence == "mismatch":
        assert (
            mailbox.consume(
                source_query="other query", session_id="session", on_evaluation=observed.append
            )
            is None
        )
    elif fence == "expired":
        now[0] += 1000
    else:
        assert (
            mailbox.consume(
                source_query="same query", session_id="session", on_evaluation=observed.append
            )
            is not None
        )
        assert observed == [owner]
        observed.clear()
    assert (
        mailbox.consume(
            source_query="same query", session_id="session", on_evaluation=observed.append
        )
        is None
    )
    assert observed == ([owner] if fence == "cancel" else [])
    mailbox.deactivate(token=activation)


def test_model_metadata_accepts_real_host_usage_shape_not_response_content() -> None:
    result = SimpleNamespace(
        model="resolved-model",
        provider="fixture",
        text="private response",
        audit={"secret": "x"},
        usage=SimpleNamespace(input_tokens=12, output_tokens=4, cost_usd=0.001),
    )
    assert model_metadata(result) == {
        "model": "resolved-model",
        "provider": "fixture",
        "usage": {"input_tokens": 12, "output_tokens": 4, "cost_usd": 0.001},
    }
    assert model_metadata({"usage": {"text": "private", "cost": 0.01}, "confidence": 0.9}) == {
        "usage": {"cost": 0.01},
        "confidence": 0.9,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "true"),
        ("max_records", 0),
        ("max_records", 2001),
        ("max_record_bytes", 524289),
        ("max_age_seconds", 2592001),
    ],
)
def test_invalid_evaluation_policy(tmp_path: Path, field: str, value: object) -> None:
    from better_hermes_hindsight.config import ConfigError

    with pytest.raises(ConfigError):
        _config(tmp_path, **{field: value})
