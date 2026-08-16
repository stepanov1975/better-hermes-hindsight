"""Tests for bounded local slow-recall capture."""

from __future__ import annotations

import json
import stat
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

import better_hermes_hindsight.diagnostics as diagnostics_module
from better_hermes_hindsight.client import RecallPhaseMetric, RecallTrace
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.diagnostics import (
    DiagnosticRecordError,
    capture_recall,
    enqueue_recall_capture,
    initialize_recall_capture,
    list_recall_records,
    load_recall_record,
    safe_trace_payload,
    save_replay_result,
)


def _config(tmp_path: Path, *, max_records: int = 3) -> BetterHindsightConfig:
    return load_config(
        tmp_path,
        environ={},
        injected={
            "single_principal": True,
            "diagnostics": {
                "enabled": True,
                "max_records": max_records,
                "slow_threshold_seconds": 5,
            },
        },
    )


def _request() -> dict[str, object]:
    return {
        "budget": "mid",
        "include": {"chunks": None, "entities": None, "source_facts": None},
        "max_tokens": 2048,
        "min_scores": None,
        "prefer_observations": False,
        "query_timestamp": None,
        "tag_groups": None,
        "tags": None,
        "tags_match": "any",
        "trace": False,
        "types": None,
    }


def test_unreadable_diagnostic_store_is_not_reported_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.diagnostics.path.mkdir(mode=0o700, parents=True)

    def denied_glob(path: Path, pattern: str) -> Iterator[Path]:
        assert path == config.diagnostics.path
        assert pattern == "*.json"
        raise PermissionError("synthetic denied")

    monkeypatch.setattr(Path, "glob", denied_glob)

    with pytest.raises(DiagnosticRecordError, match="diagnostic_store_unavailable"):
        list_recall_records(config)


def test_non_directory_diagnostic_store_is_not_reported_as_empty(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.diagnostics.path.parent.mkdir(mode=0o700, parents=True)
    config.diagnostics.path.write_text("corrupt store", encoding="utf-8")

    with pytest.raises(DiagnosticRecordError, match="diagnostic_store_unavailable"):
        list_recall_records(config)


def test_invalid_newer_entry_does_not_consume_prune_allowance(tmp_path: Path) -> None:
    config = _config(tmp_path, max_records=1)
    original_record = capture_recall(
        config,
        query="original slow query",
        request=_request(),
        elapsed_ms=6_000,
        outcome="success",
    )
    assert original_record is not None
    invalid = config.diagnostics.path / "9999999999999999999-deadbeefcafe.json"
    invalid.symlink_to(f"{original_record}.json")

    replacement_record = capture_recall(
        config,
        query="replacement slow query",
        request=_request(),
        elapsed_ms=7_000,
        outcome="success",
    )

    assert replacement_record is not None
    regular_records = [
        path
        for path in config.diagnostics.path.glob("*.json")
        if not path.is_symlink() and path.is_file()
    ]
    assert [path.stem for path in regular_records] == [replacement_record]
    assert invalid.is_symlink()


def test_enqueue_does_not_wait_for_diagnostic_filesystem_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    initialize_recall_capture(config)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_capture(*args: object, **kwargs: object) -> str:
        started.set()
        release.wait(timeout=2)
        finished.set()
        return str(kwargs["_record_id"])

    monkeypatch.setattr(diagnostics_module, "capture_recall", blocking_capture)
    record_id = enqueue_recall_capture(
        config,
        query="slow private query",
        request=_request(),
        elapsed_ms=6_000,
        outcome="success",
    )

    assert record_id is not None
    assert started.wait(timeout=1)
    assert not finished.is_set()
    release.set()
    assert finished.wait(timeout=1)


def test_replay_update_and_capture_pruning_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, max_records=1)
    original_record = capture_recall(
        config,
        query="original slow query",
        request=_request(),
        elapsed_ms=6_000,
        outcome="success",
    )
    assert original_record is not None

    replay_write_started = threading.Event()
    release_replay_write = threading.Event()
    capture_finished = threading.Event()
    errors: list[BaseException] = []
    real_write = diagnostics_module._write_record

    def blocking_write(directory: Path, record_id: str, payload: Mapping[str, object]) -> None:
        if isinstance(payload, dict) and "last_replay" in payload:
            replay_write_started.set()
            release_replay_write.wait(timeout=2)
        real_write(directory, record_id, payload)

    monkeypatch.setattr(diagnostics_module, "_write_record", blocking_write)

    def update_replay() -> None:
        try:
            save_replay_result(
                config,
                original_record,
                elapsed_ms=7_000,
                outcome="success",
            )
        except BaseException as exc:
            errors.append(exc)

    def capture_new() -> None:
        try:
            capture_recall(
                config,
                query="new slow query",
                request=_request(),
                elapsed_ms=8_000,
                outcome="success",
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            capture_finished.set()

    replay_thread = threading.Thread(target=update_replay)
    replay_thread.start()
    assert replay_write_started.wait(timeout=1)
    capture_thread = threading.Thread(target=capture_new)
    capture_thread.start()
    assert not capture_finished.wait(timeout=0.1)
    release_replay_write.set()
    replay_thread.join(timeout=2)
    capture_thread.join(timeout=2)

    assert errors == []
    assert capture_finished.is_set()
    summaries = list_recall_records(config)
    assert len(summaries) == 1
    assert summaries[0]["record_id"] != original_record


def test_capture_keeps_only_slow_or_failed_recalls_and_never_lists_query(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert (
        capture_recall(
            config,
            query="fast private query",
            request=_request(),
            elapsed_ms=1_000,
            outcome="success",
            result_count=2,
            formatted_bytes=100,
        )
        is None
    )
    assert not config.diagnostics.path.exists()

    record_id = capture_recall(
        config,
        query="exact private replay query",
        request=_request(),
        elapsed_ms=5_001,
        outcome="success",
        result_count=3,
        formatted_bytes=200,
    )

    assert record_id is not None
    record = load_recall_record(config, record_id)
    assert record["query"] == "exact private replay query"
    assert record["request"] == _request()
    assert stat.S_IMODE(config.diagnostics.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((config.diagnostics.path / f"{record_id}.json").stat().st_mode) == 0o600
    listed = list_recall_records(config)
    assert listed[0]["record_id"] == record_id
    assert "query" not in listed[0]
    assert "exact private replay query" not in json.dumps(listed)


def test_capture_prunes_oldest_records_and_persists_safe_replay_summary(tmp_path: Path) -> None:
    config = _config(tmp_path, max_records=2)
    record_ids = [
        capture_recall(
            config,
            query=f"query {index}",
            request=_request(),
            elapsed_ms=10_000,
            outcome="timeout",
        )
        for index in range(3)
    ]

    assert all(record_ids)
    assert len(list(config.diagnostics.path.glob("*.json"))) == 2
    assert record_ids[0] not in {item["record_id"] for item in list_recall_records(config)}

    latest = record_ids[-1]
    assert latest is not None
    trace = {
        "collection_counts": {"reranked": 100},
        "phase_metrics": [{"details": {}, "duration_seconds": 2.5, "phase_name": "reranking"}],
        "total_duration_seconds": 3.0,
    }
    save_replay_result(
        config,
        latest,
        elapsed_ms=3_100,
        outcome="success",
        result_count=8,
        trace=trace,
    )

    summary = list_recall_records(config)[0]
    assert isinstance(summary["recorded_at"], str)
    assert summary["recorded_at"].endswith("+00:00")
    assert summary["replay_outcome"] == "success"
    assert summary["replay_elapsed_ms"] == 3_100
    assert isinstance(summary["replayed_at"], str)
    assert summary["replayed_at"].endswith("+00:00")
    record = load_recall_record(config, latest)
    replay = record["last_replay"]
    assert isinstance(replay, dict)
    assert replay["elapsed_ms"] == 3_100
    assert replay["outcome"] == "success"
    assert replay["result_count"] == 8
    assert replay["trace"] == trace


def test_safe_trace_payload_projects_only_typed_summary() -> None:
    trace = RecallTrace(
        total_duration_seconds=4.5,
        phase_metrics=(
            RecallPhaseMetric(
                phase_name="parallel_retrieval",
                duration_seconds=3.0,
                details={"candidate_count": 450},
            ),
        ),
        collection_counts={"rrf_merged": 450},
    )

    assert safe_trace_payload(trace) == {
        "collection_counts": {"rrf_merged": 450},
        "phase_metrics": [
            {
                "details": {"candidate_count": 450},
                "duration_seconds": 3.0,
                "phase_name": "parallel_retrieval",
            }
        ],
        "total_duration_seconds": 4.5,
    }
