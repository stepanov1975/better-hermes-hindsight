from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import scripts.evaluate_recall_quality as evaluation_module
from better_hermes_hindsight.client import RecallResponse, RecallResult
from better_hermes_hindsight.config import load_config
from better_hermes_hindsight.formatting import QUERY_OMISSION_MARKER
from scripts.evaluate_recall_quality import (
    EvaluationInputError,
    QualityCase,
    _offline_responses,
    collect_live_responses,
    comparison_configs,
    evaluate,
    load_corpus,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "recall_quality_synthetic.json"
SCRIPT = Path(__file__).parents[2] / "scripts" / "evaluate_recall_quality.py"


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def recall(self, query: str) -> RecallResponse:
        self.calls.append(query)
        return RecallResponse(
            results=[
                RecallResult(id="rank-1", text="Stable Ａ memory"),
                RecallResult(id="rank-2", text="Stable A   memory"),
            ]
        )

    async def close(self) -> None:
        self.closed = True


def test_synthetic_corpus_reports_deterministic_baseline_and_observation_metrics() -> None:
    cases = load_corpus(FIXTURE)
    responses = _offline_responses(cases, ("baseline", "prefer_observations"))

    report = evaluate(cases, responses)

    assert report == {
        "result": "ok",
        "schema_version": 1,
        "variants": {
            "baseline": {
                "case_count": 3,
                "elapsed_ms_total": 30.5,
                "expected_memory_count": 2,
                "expected_memory_coverage": 1.0,
                "expected_memory_hits": 2,
                "expected_recall_cases": 2,
                "irrelevant_returns": 2,
                "negative_cases": 1,
                "no_context_correct": 0,
                "no_context_rate": 0.0,
                "redundant_returns": 1,
                "returned_records": 5,
                "returned_text_bytes": 291,
                "unlabeled_returns": 0,
                "useful_at_3_hits": 2,
                "useful_at_3_precision": 0.4,
            },
            "prefer_observations": {
                "case_count": 3,
                "elapsed_ms_total": 28.5,
                "expected_memory_count": 2,
                "expected_memory_coverage": 1.0,
                "expected_memory_hits": 2,
                "expected_recall_cases": 2,
                "irrelevant_returns": 1,
                "negative_cases": 1,
                "no_context_correct": 1,
                "no_context_rate": 1.0,
                "redundant_returns": 0,
                "returned_records": 3,
                "returned_text_bytes": 174,
                "unlabeled_returns": 0,
                "useful_at_3_hits": 2,
                "useful_at_3_precision": 0.666667,
            },
        },
    }


def test_cli_ab_report_contains_metrics_but_not_queries_or_memory_text() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(FIXTURE),
            "--compare-prefer-observations",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert set(report["variants"]) == {"baseline", "prefer_observations"}
    assert "limerick" not in completed.stdout
    assert "lab-media-01" not in completed.stdout


def test_comparison_config_changes_only_prefer_observations(tmp_path: Path) -> None:
    config = load_config(
        tmp_path,
        environ={},
        injected={
            "single_principal": True,
            "recall": {
                "budget": "low",
                "max_tokens": 777,
                "types": ["world", "observation"],
                "tags": ["synthetic"],
                "tag_mode": "all_strict",
                "prefer_observations": False,
                "min_scores": {"final": 0.25},
            },
        },
    )

    baseline, preferred = comparison_configs(config)

    assert baseline is config
    assert preferred.recall.prefer_observations is True
    assert replace(preferred.recall, prefer_observations=False) == config.recall
    assert replace(preferred, recall=config.recall) == config


def test_live_comparison_projects_queries_with_the_production_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(
        tmp_path,
        environ={},
        injected={
            "single_principal": True,
            "recall": {"enabled": True, "input_max_chars": 96, "input_max_tokens": 500},
        },
    )
    query = "HEAD-" + ("private-history " * 30) + "-TAIL"
    cases = (
        QualityCase(
            case_id="bounded-query",
            query=query,
            expect_recall=True,
            useful_result_ids=frozenset(),
            redundant_result_ids=frozenset(),
            irrelevant_result_ids=frozenset(),
            responses={},
        ),
    )
    clients: list[_FakeClient] = []

    def create_client(_config: object) -> _FakeClient:
        client = _FakeClient()
        clients.append(client)
        return client

    monkeypatch.setattr(evaluation_module, "create_hindsight_client", create_client)

    responses = asyncio.run(collect_live_responses(cases, config, compare_prefer_observations=True))

    assert set(responses) == {"baseline", "prefer_observations"}
    assert len(clients) == 2
    assert clients[0].calls == clients[1].calls
    assert len(clients[0].calls) == 1
    projected = clients[0].calls[0]
    assert projected != query
    assert len(projected) <= 96
    assert projected.startswith("HEAD-")
    assert projected.endswith("-TAIL")
    assert QUERY_OMISSION_MARKER in projected
    assert all(client.closed for client in clients)
    assert [result.result_id for result in responses["baseline"]["bounded-query"].results] == [
        "rank-1"
    ]


def test_comparison_rejects_a_baseline_that_already_prefers_observations(tmp_path: Path) -> None:
    config = load_config(
        tmp_path,
        environ={},
        injected={
            "single_principal": True,
            "recall": {"prefer_observations": True},
        },
    )

    with pytest.raises(EvaluationInputError, match="already sets"):
        comparison_configs(config)


def test_corpus_rejects_unknown_fields_and_duplicate_json_keys(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.json"
    unknown.write_text(
        json.dumps({"schema_version": 1, "cases": [], "unexpected": True}),
        encoding="utf-8",
    )
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1,"cases":[]}',
        encoding="utf-8",
    )

    with pytest.raises(EvaluationInputError, match="exactly schema_version and cases"):
        load_corpus(unknown)
    with pytest.raises(EvaluationInputError, match="duplicate JSON key"):
        load_corpus(duplicate)
