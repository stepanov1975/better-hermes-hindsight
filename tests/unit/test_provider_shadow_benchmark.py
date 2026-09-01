from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import scripts.benchmark_provider_shadow as benchmark
from scripts.benchmark_provider_shadow import (
    BankControl,
    BenchmarkInputError,
    LiveInputs,
    OwnedBank,
    _child_timeout_seconds,
    _outbox_is_drained,
    _write_public_report,
    build_identity_payload,
    build_report,
    clean_child_environment,
    collect_live_runs,
    evaluate_provider,
    load_corpus,
    provider_config,
    require_clean_tree,
    revalidate_source_identity,
    selected_executable,
    validate_corpus_digest,
    validate_endpoint,
    validate_fail_open_probe,
    validate_owned_profile,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "provider_shadow_benchmark.json"
REQUIRED_FEATURES = {
    "dated_event",
    "pronoun_cross_turn",
    "prompt_injection_like_text",
    "relative_time_phrase",
    "repeated_identical_event",
    "stale_fact",
    "timeless_fact",
    "updated_fact",
}


def _live_inputs(tmp_path: Path) -> LiveInputs:
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o700)
    return LiveInputs(
        api_url="http://127.0.0.1:18888",
        api_key="synthetic-test-key",
        expected_hindsight_version="0.9.2",
        hermes_python=python,
        hindsight_build_id="synthetic-hindsight-build",
        model_provider="mock",
        model_id="mock-model",
        model_build_id="synthetic-model-build",
        allowed_endpoints=(),
        samples_per_case=1,
    )


def _successful_samples(corpus: Any) -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    for index, case in enumerate(corpus.cases, start=1):
        markers = sorted(case.expected_markers)
        samples.append(
            {
                "case_id": case.case_id,
                "context_bytes": 0 if case.kind == "negative" else 100 + index,
                "elapsed_ms": float(index),
                "markers": markers,
            }
        )
    return samples


def _probe(provider: str) -> dict[str, object]:
    return {
        "deadline_ms": 250.0,
        "first": {"context_empty": True, "elapsed_ms": 260.0, "requests": 1},
        "mode": "fail_open",
        "provider": provider,
        "retry": {"context_empty": True, "elapsed_ms": 1.0, "requests": 0},
        "status": "ok",
    }


def test_fixture_covers_required_memory_shapes_and_exact_duplicate() -> None:
    corpus = load_corpus(FIXTURE)

    features = {feature for turn in corpus.turns for feature in turn.features}
    duplicate_pairs = [
        (left.turn_id, right.turn_id)
        for index, left in enumerate(corpus.turns)
        for right in corpus.turns[index + 1 :]
        if (left.user, left.assistant) == (right.user, right.assistant)
    ]

    assert features == REQUIRED_FEATURES
    assert duplicate_pairs == [("repeated-event-first", "repeated-event-second")]
    assert {case.kind for case in corpus.cases} == {"factual", "negative", "temporal"}
    assert len(corpus.all_markers) == 9
    assert corpus.readiness.expected_marker in corpus.all_markers


def test_fixture_validation_rejects_missing_feature_and_marker_in_query(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["turns"] = [
        turn for turn in payload["turns"] if "prompt_injection_like_text" not in turn["features"]
    ]
    missing_feature = tmp_path / "missing-feature.json"
    missing_feature.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkInputError, match="required synthetic feature"):
        load_corpus(missing_feature)

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["queries"][0]["query"] += " BH27-TIMELESS-QUARTZ-FERN"
    marker_in_query = tmp_path / "marker-in-query.json"
    marker_in_query.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkInputError, match="queries must not contain audit markers"):
        load_corpus(marker_in_query)


def test_fixture_validation_rejects_unknown_fields_and_duplicate_json_keys(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["private_endpoint"] = "https://private.invalid"
    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps(payload), encoding="utf-8")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")

    with pytest.raises(BenchmarkInputError, match="fixture root has unknown fields") as error:
        load_corpus(unknown)
    assert "private_endpoint" not in str(error.value)
    with pytest.raises(BenchmarkInputError, match="duplicate JSON key"):
        load_corpus(duplicate)


def test_metrics_include_correctness_noise_and_nearest_rank_percentiles() -> None:
    corpus = load_corpus(FIXTURE)
    samples = _successful_samples(corpus)
    old_marker = "BH27-STATUS-OLD-ORANGE"
    unrelated_marker = "BH27-DATED-LAUNCH-2042-04-05"
    first_markers = cast(list[str], samples[0]["markers"])
    samples[0]["markers"] = [*first_markers, unrelated_marker]
    latest_index = next(
        index for index, case in enumerate(corpus.cases) if case.case_id == "latest-beacon"
    )
    latest_markers = cast(list[str], samples[latest_index]["markers"])
    samples[latest_index]["markers"] = [*latest_markers, old_marker]

    metrics = evaluate_provider(corpus, samples, _probe("better"), provider="better")

    assert metrics["correctness"] == {
        "factual": {"accuracy": 1.0, "correct": 4, "sample_checks": 4},
        "negative": {"accuracy": 1.0, "correct": 1, "sample_checks": 1},
        "temporal": {"accuracy": 0.666667, "correct": 2, "sample_checks": 3},
    }
    assert metrics["latency_ms"] == {"p50": 4.0, "p95": 8.0, "samples": 8}
    assert metrics["noise"] == {
        "marker_free_output_samples": 0,
        "returned_context_bytes": 728,
        "samples_with_noise": 2,
        "unexpected_marker_hits": 2,
    }
    fail_open = cast(dict[str, object], metrics["fail_open"])
    assert fail_open["status"] == "pass"
    assert metrics["usage_cost"] == {
        "cost": None,
        "input_tokens": None,
        "output_tokens": None,
        "reason": "provider_lifecycle_exposes_no_usage_or_cost",
        "status": "unavailable",
    }


def test_negative_case_rejects_marker_free_recalled_output() -> None:
    corpus = load_corpus(FIXTURE)
    samples = _successful_samples(corpus)
    negative = next(sample for sample in samples if sample["case_id"] == "negative-lighthouse")
    negative["markers"] = []
    negative["context_bytes"] = 42

    metrics = evaluate_provider(corpus, samples, _probe("better"), provider="better")

    correctness = cast(dict[str, object], metrics["correctness"])
    negative_correctness = cast(dict[str, object], correctness["negative"])
    noise = cast(dict[str, object], metrics["noise"])
    assert negative_correctness["correct"] == 0
    assert negative_correctness["accuracy"] == 0.0
    assert noise["marker_free_output_samples"] == 1


def test_fail_open_probe_contract_is_exact_and_bounded() -> None:
    validated = validate_fail_open_probe(_probe("bundled"), provider="bundled")
    assert validated["status"] == "pass"
    assert validated["returned_empty"] is True
    assert validated["retry_returned_empty"] is True

    late = _probe("bundled")
    late["first"] = {"context_empty": True, "elapsed_ms": 751.0, "requests": 1}
    with pytest.raises(BenchmarkInputError, match="fail-open probe exceeded its bound"):
        validate_fail_open_probe(late, provider="bundled")

    extra = _probe("bundled")
    extra["endpoint"] = "https://private.invalid"
    with pytest.raises(BenchmarkInputError, match="fail-open probe shape is invalid"):
        validate_fail_open_probe(extra, provider="bundled")

    repeated_request = _probe("bundled")
    repeated_request["retry"] = {"context_empty": True, "elapsed_ms": 1.0, "requests": 1}
    with pytest.raises(BenchmarkInputError, match="fail-open probe did not return empty"):
        validate_fail_open_probe(repeated_request, provider="bundled")


def test_report_shape_is_public_safe_and_contains_human_summary(tmp_path: Path) -> None:
    corpus = load_corpus(FIXTURE)
    inputs = _live_inputs(tmp_path)
    provider_reports = {
        provider: evaluate_provider(
            corpus,
            _successful_samples(corpus),
            _probe(provider),
            provider=provider,
        )
        for provider in ("better", "bundled")
    }
    identities = build_identity_payload(
        corpus,
        inputs,
        better={"git_commit": "a" * 40, "package_version": "0.4.0", "tree_state": "dirty"},
        hermes={"git_commit": "b" * 40, "package_version": "0.20.6", "tree_state": "clean"},
        hindsight_version="0.9.2",
    )

    report = build_report(corpus, identities, provider_reports)
    serialized = json.dumps(report, allow_nan=False, sort_keys=True)

    assert set(report) == {
        "evidence",
        "human_summary",
        "identities",
        "providers",
        "result",
        "schema_version",
    }
    assert report["evidence"] == {
        "corpus": "checked_in_synthetic",
        "execution_order": "counterbalanced_pair",
        "retrieval_quality": "live_provider_lifecycle",
    }
    assert "better factual=" in cast(str, report["human_summary"])
    assert inputs.api_url not in serialized
    assert inputs.api_key not in serialized
    assert "generated-bank-id" not in serialized
    assert "query" not in serialized
    assert "markers" not in serialized
    assert "context" not in serialized
    assert "samples" in serialized


def test_identity_capture_pins_all_required_surfaces(tmp_path: Path) -> None:
    corpus = load_corpus(FIXTURE)
    identities = build_identity_payload(
        corpus,
        _live_inputs(tmp_path),
        better={"git_commit": "a" * 40, "package_version": "0.4.0", "tree_state": "clean"},
        hermes={"git_commit": "b" * 40, "package_version": "0.20.6", "tree_state": "clean"},
        hindsight_version="0.9.2",
    )

    better_identity = cast(dict[str, object], identities["better"])
    hermes_identity = cast(dict[str, object], identities["hermes"])
    mission_identity = cast(dict[str, object], identities["missions"])
    corpus_identity = cast(dict[str, object], identities["corpus"])
    assert better_identity["git_commit"] == "a" * 40
    assert hermes_identity["git_commit"] == "b" * 40
    assert identities["hindsight"] == {
        "api_version": "0.9.2",
        "build_id": "synthetic-hindsight-build",
        "build_id_source": "operator_declared",
    }
    assert identities["model"] == {
        "build_id": "synthetic-model-build",
        "build_id_source": "operator_declared",
        "model_id": "mock-model",
        "provider": "mock",
    }
    assert set(mission_identity) == {
        "observations_mission_sha256",
        "retain_mission_sha256",
    }
    assert corpus_identity["id"] == "better-hindsight-provider-shadow-v1"
    assert len(cast(str, corpus_identity["sha256"])) == 64


def test_provider_configs_pin_equivalent_recall_and_retention_policies() -> None:
    corpus = load_corpus(FIXTURE)
    better = provider_config(
        "better",
        api_url="http://127.0.0.1:18888",
        bank_id="generated-better-bank",
        missions=corpus.missions,
        retention_enabled=True,
        recall_timeout_seconds=5.0,
    )
    bundled = provider_config(
        "bundled",
        api_url="http://127.0.0.1:18888",
        bank_id="generated-bundled-bank",
        missions=corpus.missions,
        retention_enabled=True,
        recall_timeout_seconds=5.0,
    )

    assert better["bank_id"] == "generated-better-bank"
    better_recall = cast(dict[str, object], better["recall"])
    better_retain = cast(dict[str, object], better["retain"])
    better_missions = cast(dict[str, object], better["missions"])
    assert better_recall["types"] == ["world", "experience", "observation"]
    assert better_retain["enabled"] is True
    assert better_missions["retain_mission"] == corpus.missions.retain_mission
    assert bundled["bank_id"] == "generated-bundled-bank"
    assert bundled["recall_types"] == ["world", "experience", "observation"]
    assert bundled["auto_retain"] is True
    assert bundled["retain_async"] is False
    assert bundled["recall_sync"] is True
    assert bundled["bank_retain_mission"] == corpus.missions.retain_mission
    assert "apiKey" not in bundled
    assert "api_key" not in better


class _FakeBankControl:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.created: list[OwnedBank] = []
        self.deleted: list[OwnedBank] = []
        self.fail_create = fail_create

    def create(self, bank: OwnedBank, _missions: object) -> None:
        self.created.append(bank)
        if self.fail_create:
            raise RuntimeError("uncertain create")

    def delete(self, bank: OwnedBank) -> None:
        self.deleted.append(bank)


def test_live_collection_cleans_every_owned_bank_when_child_fails(tmp_path: Path) -> None:
    corpus = load_corpus(FIXTURE)
    control = _FakeBankControl()

    def failing_child(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("child failed")

    with pytest.raises(RuntimeError, match="child failed"):
        collect_live_runs(
            corpus, _live_inputs(tmp_path), control, failing_child, tmp_path / "homes"
        )

    assert len(control.created) == 4
    assert control.deleted == list(reversed(control.created))
    assert {bank.provider for bank in control.created} == {"better", "bundled"}


def test_live_collection_counterbalances_provider_order(tmp_path: Path) -> None:
    corpus = load_corpus(FIXTURE)
    control = _FakeBankControl()
    observed: list[str] = []

    def child(
        _corpus: object,
        _inputs: object,
        bank: OwnedBank,
        _home: object,
        _config: object,
    ) -> dict[str, object]:
        observed.append(bank.provider)
        return {}

    results = collect_live_runs(corpus, _live_inputs(tmp_path), control, child, tmp_path / "homes")

    assert observed == ["better", "bundled", "bundled", "better"]
    assert len(results["better"]) == 2
    assert len(results["bundled"]) == 2
    assert len(control.created) == 4
    assert control.deleted == list(reversed(control.created))


def test_live_collection_attempts_cleanup_when_create_result_is_uncertain(tmp_path: Path) -> None:
    corpus = load_corpus(FIXTURE)
    control = _FakeBankControl(fail_create=True)

    with pytest.raises(RuntimeError, match="uncertain create"):
        collect_live_runs(
            corpus,
            _live_inputs(tmp_path),
            control,
            lambda *_args, **_kwargs: {},
            tmp_path / "homes",
        )

    assert control.deleted == control.created


def test_endpoint_requires_exact_allowlist_only_for_non_loopback() -> None:
    assert validate_endpoint("http://127.0.0.1:18888", ()) == "http://127.0.0.1:18888"
    assert validate_endpoint("http://localhost:18888/", ()) == "http://localhost:18888"
    endpoint = "https://synthetic-benchmark.invalid:8443"
    assert validate_endpoint(endpoint, (endpoint,)) == endpoint

    with pytest.raises(BenchmarkInputError, match="explicitly allowlisted"):
        validate_endpoint(endpoint, ("https://synthetic-benchmark.invalid",))
    with pytest.raises(BenchmarkInputError, match="absolute HTTP"):
        validate_endpoint("http://user:secret@127.0.0.1:18888", ())


def test_child_environment_drops_unrelated_credentials(tmp_path: Path) -> None:
    inputs = _live_inputs(tmp_path)
    inherited = {
        "AWS_SECRET_ACCESS_KEY": "unrelated-secret",
        "LANG": "C.UTF-8",
        "PATH": "/synthetic/bin",
    }
    bank = OwnedBank(
        provider="better",
        bank_id="generated-bank-id",
        ownership_name="exact generated ownership name",
    )

    environment = clean_child_environment(
        inputs,
        bank,
        tmp_path / "home",
        corpus_path=FIXTURE,
        corpus_sha256="f" * 64,
        inherited=inherited,
    )

    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["HINDSIGHT_API_KEY"] == inputs.api_key
    assert environment["PATH"] == "/synthetic/bin"
    assert environment["HERMES_HOME"] == str(tmp_path / "home")


def test_cleanup_polls_for_delayed_owned_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bank = OwnedBank(
        provider="better",
        bank_id="generated-bank-id",
        ownership_name="exact generated ownership name",
    )
    control = object.__new__(BankControl)
    control._inputs = _live_inputs(tmp_path)
    calls = 0

    def get_profile(_bank: OwnedBank) -> object:
        nonlocal calls
        calls += 1
        if calls < 3:
            return None
        if calls == 3:
            return {"bank_id": bank.bank_id, "name": bank.ownership_name}
        return None

    requests: list[str] = []
    monkeypatch.setattr(control, "get_profile", get_profile)
    monkeypatch.setattr(control, "_request", lambda method, *_args: requests.append(method))
    ticks = iter((0.0, 0.01, 0.02, 0.03))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(benchmark, "_PROFILE_SETTLE_SECONDS", 0.05)

    control.delete(bank)

    assert requests == ["DELETE"]
    assert calls == 4


def test_owned_bank_readback_requires_exact_random_identity() -> None:
    bank = OwnedBank(
        provider="better",
        bank_id="generated-bank-id",
        ownership_name="exact generated ownership name",
    )
    validate_owned_profile(
        {"bank_id": bank.bank_id, "name": bank.ownership_name, "extra": "allowed"},
        bank,
    )

    with pytest.raises(BenchmarkInputError, match="ownership readback failed"):
        validate_owned_profile(
            {"bank_id": bank.bank_id, "name": "different ownership name"},
            bank,
        )


def test_live_inputs_do_not_expose_secret_in_repr(tmp_path: Path) -> None:
    inputs = _live_inputs(tmp_path)
    assert inputs.api_key not in repr(inputs)
    assert "synthetic-test-key" not in str(replace(inputs, samples_per_case=2))


def test_selected_executable_preserves_virtualenv_symlink(tmp_path: Path) -> None:
    target = tmp_path / "base-python"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o700)
    selected = tmp_path / "venv-python"
    selected.symlink_to(target)

    absolute = selected_executable(selected)

    assert absolute == selected.absolute()
    assert absolute != target.resolve()


def test_public_report_does_not_repermission_existing_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    output = parent / "report.json"

    _write_public_report(output, {"schema_version": 1, "result": "pass"})

    assert parent.stat().st_mode & 0o777 == 0o755
    assert output.stat().st_mode & 0o777 == 0o600


def test_child_validates_parent_corpus_digest(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_bytes(FIXTURE.read_bytes())
    digest = hashlib.sha256(corpus.read_bytes()).hexdigest()

    validate_corpus_digest(corpus, digest)
    corpus.write_text("{}", encoding="utf-8")

    with pytest.raises(BenchmarkInputError, match="corpus changed"):
        validate_corpus_digest(corpus, digest)


def test_source_identity_requires_clean_trees() -> None:
    require_clean_tree("clean", "Better")
    with pytest.raises(BenchmarkInputError, match="must be clean"):
        require_clean_tree("dirty", "Better")


def test_source_identity_is_revalidated_after_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark, "_git_identity", lambda _path: "expected-commit")
    monkeypatch.setattr(benchmark, "_tree_identity", lambda _path: "clean")
    revalidate_source_identity(
        tmp_path,
        expected_commit="expected-commit",
        expected_tree_state="clean",
        source_name="Better",
    )

    monkeypatch.setattr(benchmark, "_tree_identity", lambda _path: "dirty")
    with pytest.raises(BenchmarkInputError, match="changed during benchmark"):
        revalidate_source_identity(
            tmp_path,
            expected_commit="expected-commit",
            expected_tree_state="clean",
            source_name="Better",
        )


def test_child_deadline_scales_to_the_permitted_sample_maximum() -> None:
    corpus = load_corpus(FIXTURE)
    assert _child_timeout_seconds(corpus, 20) > 1_600.0
    assert _child_timeout_seconds(corpus, 20) > _child_timeout_seconds(corpus, 1)


def test_better_outbox_drain_requires_every_state_to_be_empty() -> None:
    drained = SimpleNamespace(
        outbox="ready",
        mismatch_count=0,
        pending_count=0,
        retry_count=0,
        sending_count=0,
    )
    sending = SimpleNamespace(
        outbox="ready",
        mismatch_count=0,
        pending_count=0,
        retry_count=0,
        sending_count=1,
    )

    assert _outbox_is_drained(drained) is True
    assert _outbox_is_drained(sending) is False
