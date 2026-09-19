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
from better_hermes_hindsight.evaluation import (
    EvaluationCapture,
    drain_evaluation_for_tests,
    model_metadata,
)


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
    assert drain_evaluation_for_tests()
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
    assert drain_evaluation_for_tests()
    assert not files[0].exists()
    for _ in range(5):
        capture.stage("retrieval", outcome="skip")
    assert drain_evaluation_for_tests()
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
    assert drain_evaluation_for_tests()
    assert len([p for p in directory.glob("*.json") if p != unrelated and not p.is_symlink()]) == 2


def test_default_disabled_and_symlink_store_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    capture = EvaluationCapture(config, uuid.uuid4().hex)
    capture.stage("input", capsule="not persisted")
    assert drain_evaluation_for_tests()
    directory = tmp_path / "better_hindsight/planner_evaluation"
    assert not directory.exists()
    outside = tmp_path / "other"
    outside.mkdir()
    directory.symlink_to(outside, target_is_directory=True)
    EvaluationCapture(
        replace(config, evaluation=EvaluationConfig(enabled=True)), uuid.uuid4().hex
    ).stage("input", capsule="not persisted")
    assert drain_evaluation_for_tests()
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


def test_rejected_stage_preserves_full_store(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, max_records=1)
    capture = EvaluationCapture(config, uuid.uuid4().hex)
    capture.stage("plan", action="skip")
    assert drain_evaluation_for_tests()
    directory = tmp_path / "better_hindsight/planner_evaluation"
    before = {p.name: p.read_bytes() for p in directory.glob("*.json")}
    assert len(before) == 1
    limited = replace(config, evaluation=replace(config.evaluation, max_record_bytes=1))
    for _ in range(3):
        EvaluationCapture(limited, uuid.uuid4().hex).stage("input", capsule="oversized")
    capture.stage("plan", cost=float("nan"))
    assert drain_evaluation_for_tests()
    assert {p.name: p.read_bytes() for p in directory.glob("*.json")} == before


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf"), True])
def test_metadata_omits_nonfinite_numbers(number: object) -> None:
    assert model_metadata({"cost": number, "confidence": number, "usage": {"cost": number}}) == {}


@pytest.mark.parametrize(
    "result", [None, {}, {"usage": None}, {"usage": {}}, {"usage": {"text": "x"}}]
)
def test_unavailable_usage_is_omitted(result: object) -> None:
    assert model_metadata(result) == {}


@pytest.mark.parametrize("padding", ["", " ", "\t\n"])
@pytest.mark.parametrize("short", ["test-key", "key-tail"])
def test_overlapping_secrets_redacted_longest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short: str, padding: str
) -> None:
    config = replace(_config(tmp_path, enabled=True), api_key=short)
    monkeypatch.setenv("OPENROUTER_API_KEY", f"{padding}test-key-tail{padding}")
    EvaluationCapture(config, uuid.uuid4().hex).stage("input", capsule="test-key-tail")
    assert drain_evaluation_for_tests()
    rows = [
        json.loads(p.read_text())
        for p in (tmp_path / "better_hindsight/planner_evaluation").glob("*.json")
    ]
    assert rows[0]["capsule"] == "[REDACTED]"


def test_failed_write_preserves_full_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import better_hermes_hindsight.evaluation as evaluation

    config = _config(tmp_path, enabled=True, max_records=1)
    capture = EvaluationCapture(config, uuid.uuid4().hex)
    capture.stage("input", capsule="existing evidence")
    assert drain_evaluation_for_tests()
    directory = tmp_path / "better_hindsight/planner_evaluation"
    before = {p.name: p.read_bytes() for p in directory.glob("*.json")}
    assert len(before) == 1

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(evaluation, "_write_record", fail)
    capture.stage("plan", action="recall")
    assert drain_evaluation_for_tests()
    assert {p.name: p.read_bytes() for p in directory.glob("*.json")} == before


def test_writer_queue_is_bounded_and_snapshots_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Mapping
    from threading import Event

    import better_hermes_hindsight.evaluation as evaluation
    from better_hermes_hindsight.diagnostics import _write_record

    assert drain_evaluation_for_tests()
    entered, release = Event(), Event()

    def slow(directory: Path, record_id: str, payload: Mapping[str, object]) -> None:
        entered.set()
        assert release.wait(5)
        _write_record(directory, record_id, payload)

    monkeypatch.setattr(evaluation, "_write_record", slow)
    config = _config(tmp_path, enabled=True)
    capture = EvaluationCapture(config, uuid.uuid4().hex)
    capsule = {"text": "original"}
    capture.stage("input", capsule=capsule)
    try:
        assert entered.wait(2)
        capsule["text"] = "mutated"
        for index in range(48):
            capture.stage("plan", index=index)
        assert not drain_evaluation_for_tests(timeout=0.01)
    finally:
        release.set()
    assert drain_evaluation_for_tests()
    rows = [
        json.loads(p.read_text())
        for p in (tmp_path / "better_hindsight/planner_evaluation").glob("*.json")
    ]
    assert len(rows) == 17  # One in flight and 16 queued; newer stages drop on full.
    assert sorted(r["index"] for r in rows if r["stage"] == "plan") == list(range(16))
    assert next(r for r in rows if r["stage"] == "input")["capsule"] == {"text": "original"}


def test_concurrent_captures_respect_store_cap(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    config = _config(tmp_path, enabled=True, max_records=2)
    capture = EvaluationCapture(config, uuid.uuid4().hex)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: capture.stage("plan", index=index), range(40)))
    assert drain_evaluation_for_tests()
    rows = list((tmp_path / "better_hindsight/planner_evaluation").glob("*.json"))
    assert len(rows) == 2
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in rows)


def test_import_aliases_share_one_bounded_writer() -> None:
    import importlib.util
    import sys

    import better_hermes_hindsight.evaluation as evaluation

    name = "better_hermes_hindsight._evaluation_test_alias"
    spec = importlib.util.spec_from_file_location(name, evaluation.__file__)
    assert spec is not None and spec.loader is not None
    alias = importlib.util.module_from_spec(spec)
    sys.modules[name] = alias
    try:
        spec.loader.exec_module(alias)
        assert alias._writer is evaluation._writer
    finally:
        sys.modules.pop(name)


def test_nonfinite_metadata_is_omitted_from_strict_json(tmp_path: Path) -> None:
    metadata = model_metadata(
        {
            "confidence": float("nan"),
            "cost": float("inf"),
            "usage": {"cost_usd": -float("inf"), "input_tokens": 0, "output_tokens": 2},
        }
    )
    capture = EvaluationCapture(_config(tmp_path, enabled=True), uuid.uuid4().hex)
    capture.stage("decision", metadata=metadata)
    assert drain_evaluation_for_tests()

    def reject_constant(value: str) -> None:
        raise AssertionError(value)

    rows = [
        json.loads(p.read_text(), parse_constant=reject_constant)
        for p in (tmp_path / "better_hindsight/planner_evaluation").glob("*.json")
    ]
    assert rows[0]["metadata"] == {"usage": {"input_tokens": 0, "output_tokens": 2}}


def test_process_exit_does_not_wait_for_stalled_writer(tmp_path: Path) -> None:
    import subprocess
    import sys

    _config(tmp_path, enabled=True)
    script = """
import sys
from pathlib import Path
from threading import Event
from better_hermes_hindsight.config import load_config
import better_hermes_hindsight.evaluation as evaluation
entered = Event()
def stalled(*args):
    entered.set()
    Event().wait()
evaluation._persist_stage = stalled
evaluation.EvaluationCapture(load_config(Path(sys.argv[1])), 'a' * 32).stage('input')
assert entered.wait(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        timeout=5,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
