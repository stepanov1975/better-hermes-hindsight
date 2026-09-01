#!/usr/bin/env python3
"""Public-safe, live shadow benchmark for bundled and Better Hindsight.

The parent process owns two disposable Hindsight banks and invokes this file in
isolated child processes. Each child activates exactly one real Hermes memory
provider, retains the same synthetic corpus, and recalls the same prompts. Raw
provider context remains inside mode-0700 temporary directories; the persisted
report contains only aggregate scores, safe case IDs, latencies, and source
identities.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import http.server
import importlib
import importlib.metadata
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "tests" / "fixtures" / "provider_shadow_benchmark.json"
_PROVIDER_NAMES = ("better", "bundled")
_REQUIRED_FEATURES = frozenset(
    {
        "timeless_fact",
        "dated_event",
        "relative_time_phrase",
        "repeated_identical_event",
        "pronoun_cross_turn",
        "stale_fact",
        "updated_fact",
        "prompt_injection_like_text",
    }
)
_MARKER_RE = re.compile(r"BH27-[A-Z0-9-]+")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_MAX_HTTP_BYTES = 1_048_576
_DEFAULT_TAGS = ("better-hindsight-provider-shadow", "synthetic-only")
_DEFAULT_TYPES = ("world", "experience", "observation")
_DEFAULT_BUDGET = "high"
_DEFAULT_MAX_TOKENS = 4096
_RECALL_TIMEOUT_SECONDS = 5.0
_RETAIN_TIMEOUT_SECONDS = 60.0
_CHILD_MARGIN_SECONDS = 30.0
_READINESS_TIMEOUT_SECONDS = 120.0
_REPORT_SCHEMA_VERSION = 1


class BenchmarkInputError(ValueError):
    """Raised for unsafe, malformed, or incomplete benchmark inputs."""


@dataclasses.dataclass(frozen=True)
class MissionSet:
    retain_mission: str
    observations_mission: str


@dataclasses.dataclass(frozen=True)
class ReadinessCase:
    prompt: str
    expected_marker: str


@dataclasses.dataclass(frozen=True)
class CorpusTurn:
    turn_id: str
    session_id: str
    features: tuple[str, ...]
    user: str
    assistant: str


@dataclasses.dataclass(frozen=True)
class CorpusCase:
    case_id: str
    kind: str
    prompt: str
    expected_markers: tuple[str, ...]
    forbidden_markers: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class BenchmarkCorpus:
    schema_version: int
    corpus_id: str
    missions: MissionSet
    readiness: ReadinessCase
    turns: tuple[CorpusTurn, ...]
    cases: tuple[CorpusCase, ...]
    all_markers: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class LiveInputs:
    api_url: str
    api_key: str = dataclasses.field(repr=False)
    expected_hindsight_version: str = ""
    hermes_python: Path = Path(sys.executable)
    hermes_source: Path | None = None
    hindsight_build_id: str = ""
    model_provider: str = ""
    model_id: str = ""
    model_build_id: str = ""
    corpus_sha256: str = ""
    allowed_endpoints: tuple[str, ...] = ()
    samples_per_case: int = 1


@dataclasses.dataclass(frozen=True)
class OwnedBank:
    provider: str
    bank_id: str = dataclasses.field(repr=False)
    ownership_name: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class SourceIdentity:
    better_commit: str
    better_patch_sha256: str
    hermes_commit: str
    hindsight_version: str
    hindsight_build: str
    model_id: str


def _require_nonempty_string(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise BenchmarkInputError(f"{field} must be a non-empty string")
    return value.strip()


def _require_exact_keys(value: object, expected: set[str], field: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise BenchmarkInputError(f"{field} has an invalid shape")
    return dict(value)


def _string_tuple(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not list or (not value and not allow_empty):
        raise BenchmarkInputError(f"{field} must be a list of strings")
    copied: list[str] = []
    for item in value:
        copied.append(_require_nonempty_string(item, field))
    if len(set(copied)) != len(copied):
        raise BenchmarkInputError(f"{field} contains duplicates")
    return tuple(copied)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkInputError("fixture contains a duplicate JSON key")
        result[key] = value
    return result


def validate_corpus_digest(path: Path, expected_sha256: str) -> None:
    """Bind a child process to the exact corpus bytes selected by its parent."""

    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise BenchmarkInputError("parent corpus digest is invalid")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise BenchmarkInputError("corpus changed after the parent validated it")


def load_corpus(path: Path) -> BenchmarkCorpus:
    """Load and strictly validate the synthetic benchmark corpus."""

    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except BenchmarkInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkInputError("benchmark corpus could not be read") from exc
    expected_root = {
        "schema_version",
        "corpus_id",
        "missions",
        "readiness",
        "turns",
        "queries",
    }
    if type(payload) is not dict:
        raise BenchmarkInputError("fixture root is invalid")
    if set(payload) - expected_root:
        raise BenchmarkInputError("fixture root has unknown fields")
    if set(payload) != expected_root:
        raise BenchmarkInputError("fixture root is missing required fields")
    root = dict(payload)
    if root["schema_version"] != 1:
        raise BenchmarkInputError("unsupported benchmark corpus schema")
    corpus_id = _require_nonempty_string(root["corpus_id"], "corpus_id")

    mission_payload = _require_exact_keys(
        root["missions"], {"retain_mission", "observations_mission"}, "missions"
    )
    missions = MissionSet(
        retain_mission=_require_nonempty_string(
            mission_payload["retain_mission"], "retain_mission"
        ),
        observations_mission=_require_nonempty_string(
            mission_payload["observations_mission"], "observations_mission"
        ),
    )

    readiness_payload = _require_exact_keys(
        root["readiness"], {"query", "expected_marker"}, "readiness"
    )
    readiness = ReadinessCase(
        prompt=_require_nonempty_string(readiness_payload["query"], "readiness.query"),
        expected_marker=_require_nonempty_string(
            readiness_payload["expected_marker"], "readiness.expected_marker"
        ),
    )
    if _MARKER_RE.fullmatch(readiness.expected_marker) is None:
        raise BenchmarkInputError("readiness marker is invalid")

    if type(root["turns"]) is not list or not root["turns"]:
        raise BenchmarkInputError("turns must be a non-empty list")
    turns: list[CorpusTurn] = []
    turn_ids: set[str] = set()
    features_seen: set[str] = set()
    markers_seen: set[str] = set()
    for index, item in enumerate(root["turns"]):
        turn_payload = _require_exact_keys(
            item, {"id", "session_id", "features", "user", "assistant"}, f"turns[{index}]"
        )
        turn_id = _require_nonempty_string(turn_payload["id"], "turn.id")
        if turn_id in turn_ids:
            raise BenchmarkInputError("turn IDs must be unique")
        turn_ids.add(turn_id)
        features = _string_tuple(turn_payload["features"], "turn.features")
        features_seen.update(features)
        user = _require_nonempty_string(turn_payload["user"], "turn.user")
        assistant = _require_nonempty_string(turn_payload["assistant"], "turn.assistant")
        markers_seen.update(_MARKER_RE.findall(f"{user}\n{assistant}"))
        turns.append(
            CorpusTurn(
                turn_id=turn_id,
                session_id=_require_nonempty_string(turn_payload["session_id"], "turn.session_id"),
                features=features,
                user=user,
                assistant=assistant,
            )
        )
    missing_features = _REQUIRED_FEATURES - features_seen
    if missing_features:
        raise BenchmarkInputError("fixture is missing a required synthetic feature")

    if type(root["queries"]) is not list or not root["queries"]:
        raise BenchmarkInputError("queries must be a non-empty list")
    cases: list[CorpusCase] = []
    case_ids: set[str] = set()
    for index, item in enumerate(root["queries"]):
        case_payload = _require_exact_keys(
            item,
            {"id", "kind", "query", "expected_markers", "forbidden_markers"},
            f"queries[{index}]",
        )
        case_id = _require_nonempty_string(case_payload["id"], "case.id")
        if case_id in case_ids:
            raise BenchmarkInputError("case IDs must be unique")
        case_ids.add(case_id)
        kind = _require_nonempty_string(case_payload["kind"], "case.kind")
        if kind not in {"factual", "temporal", "negative"}:
            raise BenchmarkInputError("case kind is invalid")
        expected = _string_tuple(
            case_payload["expected_markers"], "case.expected_markers", allow_empty=True
        )
        forbidden = _string_tuple(
            case_payload["forbidden_markers"], "case.forbidden_markers", allow_empty=True
        )
        if set(expected) & set(forbidden):
            raise BenchmarkInputError("expected and forbidden labels must not overlap")
        if kind == "negative" and expected:
            raise BenchmarkInputError("negative cases must not have expected labels")
        if kind != "negative" and not expected:
            raise BenchmarkInputError("positive cases require expected labels")
        for marker in (*expected, *forbidden):
            if _MARKER_RE.fullmatch(marker) is None or marker not in markers_seen:
                raise BenchmarkInputError("case references an unknown label")
        prompt = _require_nonempty_string(case_payload["query"], "case.prompt")
        if _MARKER_RE.search(prompt) is not None:
            raise BenchmarkInputError("queries must not contain audit markers")
        cases.append(
            CorpusCase(
                case_id=case_id,
                kind=kind,
                prompt=prompt,
                expected_markers=expected,
                forbidden_markers=forbidden,
            )
        )
    if readiness.expected_marker not in markers_seen:
        raise BenchmarkInputError("readiness label is not present in the corpus")
    return BenchmarkCorpus(
        schema_version=1,
        corpus_id=corpus_id,
        missions=missions,
        readiness=readiness,
        turns=tuple(turns),
        cases=tuple(cases),
        all_markers=tuple(sorted(markers_seen)),
    )


def _canonical_endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BenchmarkInputError("benchmark endpoint must be an absolute HTTP origin")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port is not None else ""
    bracketed = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{bracketed}{port}"


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_endpoint(api_url: str, allowed_endpoints: Sequence[str]) -> str:
    """Allow loopback automatically; require an exact opt-in for any other origin."""

    canonical = _canonical_endpoint(api_url)
    hostname = urllib.parse.urlsplit(canonical).hostname
    assert hostname is not None
    if _is_loopback_host(hostname):
        return canonical
    allowlisted = {_canonical_endpoint(item) for item in allowed_endpoints}
    if canonical not in allowlisted:
        raise BenchmarkInputError("non-loopback benchmark endpoint is not explicitly allowlisted")
    return canonical


def provider_config(
    provider: str,
    api_url: str,
    bank_id: str,
    missions: MissionSet,
    *,
    retention_enabled: bool = True,
    recall_timeout_seconds: float = 20.0,
) -> dict[str, object]:
    """Return intentionally aligned provider policies without embedding credentials."""

    if type(retention_enabled) is not bool or recall_timeout_seconds <= 0:
        raise BenchmarkInputError("provider policy inputs are invalid")
    if provider == "better":
        return {
            "api_url": api_url,
            "bank_id": bank_id,
            "single_principal": True,
            "recall": {
                "enabled": True,
                "timeout_seconds": recall_timeout_seconds,
                "input_max_chars": 4096,
                "context_max_bytes": 32768,
                "budget": _DEFAULT_BUDGET,
                "max_tokens": _DEFAULT_MAX_TOKENS,
                "types": list(_DEFAULT_TYPES),
                "tags": list(_DEFAULT_TAGS),
                "tag_mode": "all_strict",
                "include_source_facts": True,
                "max_source_facts_tokens": _DEFAULT_MAX_TOKENS,
            },
            "retain": {
                "enabled": retention_enabled,
                "timeout_seconds": _RETAIN_TIMEOUT_SECONDS,
                "segment_max_bytes": 65536,
                "tags": list(_DEFAULT_TAGS),
            },
            "outbox": {
                "max_pending_rows": 500,
                "max_pending_bytes": 8_000_000,
                "poll_interval_seconds": 0.1,
                "retry_initial_seconds": 0.2,
                "retry_max_seconds": 1.0,
            },
            "missions": {
                "retain_mission": missions.retain_mission,
                "observations_mission": missions.observations_mission,
            },
        }
    if provider == "bundled":
        return {
            "mode": "local_external",
            "api_url": api_url,
            "bank_id": bank_id,
            "budget": _DEFAULT_BUDGET,
            "memory_mode": "context",
            "auto_retain": retention_enabled,
            "retain_every_n_turns": 1,
            "retain_async": False,
            "retain_tags": list(_DEFAULT_TAGS),
            "retain_source": "hermes-provider-shadow",
            "auto_recall": True,
            "recall_sync": True,
            "recall_types": list(_DEFAULT_TYPES),
            "recall_tags": list(_DEFAULT_TAGS),
            "recall_tags_match": "all",
            "recall_max_tokens": _DEFAULT_MAX_TOKENS,
            "recall_max_input_chars": 4096,
            "recall_indicator": False,
            "retain_indicator": False,
            "timeout": max(1, math.ceil(recall_timeout_seconds)),
            "bank_mission": missions.observations_mission,
            "bank_retain_mission": missions.retain_mission,
        }
    raise BenchmarkInputError("unknown benchmark provider")


def clean_child_environment(
    inputs: LiveInputs,
    bank: OwnedBank,
    hermes_home: Path,
    *,
    corpus_path: Path,
    corpus_sha256: str,
    inherited: Mapping[str, str],
) -> dict[str, str]:
    """Build a narrow child environment without inheriting unrelated credentials."""

    python_paths = [os.fspath(ROOT)]
    if inputs.hermes_source is not None:
        python_paths.append(os.fspath(inputs.hermes_source))
    result = {
        "PATH": inherited.get("PATH", os.defpath),
        "HOME": os.fspath(hermes_home.parent),
        "HERMES_HOME": os.fspath(hermes_home),
        "HINDSIGHT_API_KEY": inputs.api_key,
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join(python_paths),
        "BH27_PROVIDER": bank.provider,
        "BH27_CORPUS_PATH": os.fspath(corpus_path),
        "BH27_CORPUS_SHA256": corpus_sha256,
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TZ"):
        value = inherited.get(name)
        if value:
            result[name] = value
    return result


def validate_owned_profile(profile: object, bank: OwnedBank) -> None:
    """Refuse cleanup unless the live profile exactly matches our ownership marker."""

    if (
        type(profile) is not dict
        or profile.get("bank_id") != bank.bank_id
        or profile.get("name") != bank.ownership_name
    ):
        raise BenchmarkInputError("disposable profile ownership readback failed")


def collect_live_runs(
    corpus: BenchmarkCorpus,
    inputs: LiveInputs,
    bank_control: Any,
    runner: Callable[..., Mapping[str, object]],
    homes_root: Path,
) -> dict[str, Mapping[str, object]]:
    """Create both profiles first, run each provider, and always attempt cleanup."""

    homes_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    homes_root.chmod(0o700)
    owned: list[OwnedBank] = []
    results: dict[str, Mapping[str, object]] = {}
    primary_error: BaseException | None = None
    try:
        for provider in _PROVIDER_NAMES:
            nonce = uuid.uuid4().hex
            bank = OwnedBank(
                provider=provider,
                bank_id=f"bh27-{provider}-{nonce}",
                ownership_name=(f"better-hindsight-shadow:{corpus.corpus_id}:{provider}:{nonce}"),
            )
            owned.append(bank)
            bank_control.create(bank, corpus.missions)
        for bank in owned:
            config = provider_config(
                bank.provider,
                api_url=inputs.api_url,
                bank_id=bank.bank_id,
                missions=corpus.missions,
                retention_enabled=True,
                recall_timeout_seconds=_RECALL_TIMEOUT_SECONDS,
            )
            home = homes_root / bank.provider
            results[bank.provider] = runner(corpus, inputs, bank, home, config)
        return results
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        cleanup_failures: list[OwnedBank] = []
        for bank in reversed(owned):
            try:
                bank_control.delete(bank)
            except BaseException as exc:
                cleanup_failures.append(bank)
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            for bank in cleanup_failures:
                print(
                    "cleanup required for disposable profile "
                    f"{bank.bank_id!r} after verifying ownership name "
                    f"{bank.ownership_name!r}",
                    file=sys.stderr,
                )
            if primary_error is not None:
                raise BenchmarkInputError(
                    "benchmark failed and disposable-profile cleanup also failed"
                ) from cleanup_error
            raise BenchmarkInputError("disposable-profile cleanup failed") from cleanup_error


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[rank], 3)


def evaluate_provider(
    corpus: BenchmarkCorpus,
    samples: Sequence[Mapping[str, object]],
    fail_open_probe: Mapping[str, object],
    *,
    provider: str,
) -> dict[str, object]:
    """Score safe child samples; no recalled text or audit labels survive."""

    if provider not in _PROVIDER_NAMES:
        raise BenchmarkInputError("unknown benchmark provider result")
    cases = {case.case_id: case for case in corpus.cases}
    seen: set[str] = set()
    correct_counts = {kind: 0 for kind in ("factual", "negative", "temporal")}
    check_counts = {kind: 0 for kind in ("factual", "negative", "temporal")}
    latencies: list[float] = []
    returned_bytes = 0
    samples_with_noise = 0
    marker_free_output_samples = 0
    unexpected_hits = 0
    duplicate_hits = 0

    for sample in samples:
        if set(sample) != {"case_id", "context_bytes", "elapsed_ms", "markers"}:
            raise BenchmarkInputError("provider sample shape is invalid")
        case_id = sample.get("case_id")
        if type(case_id) is not str or case_id not in cases:
            raise BenchmarkInputError("provider sample ID is invalid")
        case = cases[case_id]
        seen.add(case_id)
        elapsed = sample.get("elapsed_ms")
        context_bytes = sample.get("context_bytes")
        raw_markers = sample.get("markers")
        if (
            not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or float(elapsed) < 0
            or not isinstance(context_bytes, int)
            or isinstance(context_bytes, bool)
            or context_bytes < 0
            or type(raw_markers) is not list
            or any(type(item) is not str or item not in corpus.all_markers for item in raw_markers)
        ):
            raise BenchmarkInputError("provider sample payload is invalid")
        elapsed_value = float(elapsed)
        markers = list(raw_markers)
        present = set(markers)
        expected = set(case.expected_markers)
        forbidden = set(case.forbidden_markers)
        marker_free_output = context_bytes > 0 and not present
        if case.kind == "negative":
            correct = not present and context_bytes == 0
            unexpected = len(markers)
        else:
            correct = expected.issubset(present) and not (forbidden & present)
            unexpected = sum(1 for marker in markers if marker not in expected)
        check_counts[case.kind] += 1
        correct_counts[case.kind] += int(correct)
        if unexpected or marker_free_output:
            samples_with_noise += 1
        marker_free_output_samples += int(marker_free_output)
        unexpected_hits += unexpected
        duplicate_hits += max(0, len(markers) - len(present))
        returned_bytes += context_bytes
        latencies.append(elapsed_value)

    if seen != set(cases):
        raise BenchmarkInputError("provider samples do not cover every case")
    correctness = {
        kind: {
            "accuracy": (
                round(correct_counts[kind] / check_counts[kind], 6) if check_counts[kind] else 0.0
            ),
            "correct": correct_counts[kind],
            "sample_checks": check_counts[kind],
        }
        for kind in ("factual", "negative", "temporal")
    }

    return {
        "provider": provider,
        "correctness": correctness,
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "samples": len(latencies),
        },
        "noise": {
            "marker_free_output_samples": marker_free_output_samples,
            "returned_context_bytes": returned_bytes,
            "samples_with_noise": samples_with_noise,
            "unexpected_marker_hits": unexpected_hits,
        },
        "redundancy": {"duplicate_hits": duplicate_hits},
        "fail_open": validate_fail_open_probe(fail_open_probe, provider=provider),
        "usage_cost": {
            "cost": None,
            "input_tokens": None,
            "output_tokens": None,
            "reason": "provider_lifecycle_exposes_no_usage_or_cost",
            "status": "unavailable",
        },
    }


def validate_fail_open_probe(raw: Mapping[str, object], *, provider: str) -> dict[str, object]:
    """Validate the exact two-call host timeout/fail-open evidence."""

    expected_keys = {"deadline_ms", "first", "mode", "provider", "retry", "status"}
    if set(raw) != expected_keys or raw.get("provider") != provider:
        raise BenchmarkInputError("fail-open probe shape is invalid")
    if raw.get("mode") != "fail_open" or raw.get("status") != "ok":
        raise BenchmarkInputError("fail-open probe shape is invalid")
    deadline = raw.get("deadline_ms")
    first = raw.get("first")
    retry = raw.get("retry")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        raise BenchmarkInputError("fail-open probe shape is invalid")
    deadline_value = float(deadline)
    if deadline_value <= 0 or type(first) is not dict or type(retry) is not dict:
        raise BenchmarkInputError("fail-open probe shape is invalid")
    if set(first) != {"context_empty", "elapsed_ms", "requests"} or set(retry) != {
        "context_empty",
        "elapsed_ms",
        "requests",
    }:
        raise BenchmarkInputError("fail-open probe shape is invalid")
    first_elapsed = first.get("elapsed_ms")
    retry_elapsed = retry.get("elapsed_ms")
    first_requests = first.get("requests")
    retry_requests = retry.get("requests")
    if (
        not isinstance(first_elapsed, (int, float))
        or isinstance(first_elapsed, bool)
        or not isinstance(retry_elapsed, (int, float))
        or isinstance(retry_elapsed, bool)
        or type(first_requests) is not int
        or type(retry_requests) is not int
    ):
        raise BenchmarkInputError("fail-open probe did not return empty")
    first_value = float(first_elapsed)
    retry_value = float(retry_elapsed)
    if (
        first.get("context_empty") is not True
        or retry.get("context_empty") is not True
        or first_requests != 1
        or retry_requests != 0
        or first_value < 0
        or retry_value < 0
    ):
        raise BenchmarkInputError("fail-open probe did not return empty")
    if first_value > deadline_value + 500.0 or retry_value > 500.0:
        raise BenchmarkInputError("fail-open probe exceeded its bound")
    return {
        "status": "pass",
        "returned_empty": True,
        "retry_returned_empty": True,
        "deadline_ms": round(deadline_value, 3),
        "first_elapsed_ms": round(first_value, 3),
        "retry_elapsed_ms": round(retry_value, 3),
        "backend_requests": {"first": first_requests, "retry": retry_requests},
    }


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_identity_payload(
    corpus: BenchmarkCorpus,
    inputs: LiveInputs,
    *,
    better: Mapping[str, object],
    hermes: Mapping[str, object],
    hindsight_version: str,
) -> dict[str, object]:
    """Capture exact source/model/policy identities without endpoint details."""

    if hindsight_version != inputs.expected_hindsight_version:
        raise BenchmarkInputError("Hindsight version identity does not match the opt-in")
    for identity_name, identity in (("better", better), ("hermes", hermes)):
        if type(identity) is not dict or not {
            "git_commit",
            "package_version",
            "tree_state",
        }.issubset(identity):
            raise BenchmarkInputError(f"{identity_name} source identity is invalid")
    return {
        "better": dict(better),
        "hermes": dict(hermes),
        "hindsight": {
            "api_version": hindsight_version,
            "build_id": _require_nonempty_string(inputs.hindsight_build_id, "hindsight_build_id"),
            "build_id_source": "operator_declared",
        },
        "model": {
            "provider": _require_nonempty_string(inputs.model_provider, "model_provider"),
            "model_id": _require_nonempty_string(inputs.model_id, "model_id"),
            "build_id": _require_nonempty_string(inputs.model_build_id, "model_build_id"),
            "build_id_source": "operator_declared",
        },
        "missions": {
            "retain_mission_sha256": hashlib.sha256(
                corpus.missions.retain_mission.encode("utf-8")
            ).hexdigest(),
            "observations_mission_sha256": hashlib.sha256(
                corpus.missions.observations_mission.encode("utf-8")
            ).hexdigest(),
        },
        "corpus": {
            "id": corpus.corpus_id,
            "sha256": inputs.corpus_sha256 or _sha256_json(dataclasses.asdict(corpus)),
        },
    }


def _public_provider_report(value: Mapping[str, object]) -> dict[str, object]:
    copied = json.loads(json.dumps(value, allow_nan=False, sort_keys=True))
    if type(copied) is not dict or type(copied.get("noise")) is not dict:
        raise BenchmarkInputError("provider report is invalid")
    noise = copied["noise"]
    noise["returned_bytes"] = noise.pop("returned_context_bytes")
    return copied


def build_report(
    corpus: BenchmarkCorpus,
    identities: Mapping[str, object],
    provider_reports: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Build the only persistent artifact; provider text and labels are omitted."""

    if set(provider_reports) != set(_PROVIDER_NAMES):
        raise BenchmarkInputError("benchmark provider result set is incomplete")
    providers = {
        provider: _public_provider_report(provider_reports[provider])
        for provider in _PROVIDER_NAMES
    }
    report: dict[str, object] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "result": "pass",
        "evidence": {
            "corpus": "checked_in_synthetic",
            "retrieval_quality": "live_provider_lifecycle",
        },
        "identities": dict(identities),
        "providers": providers,
    }
    report["human_summary"] = human_summary(report)
    serialized = json.dumps(report, allow_nan=False, sort_keys=True)
    forbidden_values = (*corpus.all_markers, *(case.prompt for case in corpus.cases))
    if any(value in serialized for value in forbidden_values):
        raise BenchmarkInputError("public report contains synthetic source text")
    for forbidden_key in ('"query', '"markers', '"context', '"bank', '"api_url'):
        if forbidden_key in serialized:
            raise BenchmarkInputError("public report contains a forbidden field")
    return report


def human_summary(report: Mapping[str, object]) -> str:
    providers = report.get("providers")
    if type(providers) is not dict:
        raise BenchmarkInputError("report is missing provider results")
    lines = ["Bundled-vs-Better provider shadow benchmark"]
    for provider in _PROVIDER_NAMES:
        value = providers.get(provider)
        if type(value) is not dict or type(value.get("correctness")) is not dict:
            raise BenchmarkInputError("report is missing a provider result")
        correctness = value["correctness"]
        factual = correctness.get("factual")
        temporal = correctness.get("temporal")
        if type(factual) is not dict or type(temporal) is not dict:
            raise BenchmarkInputError("report correctness result is invalid")
        noise = value.get("noise")
        latency = value.get("latency_ms")
        if type(noise) is not dict or type(latency) is not dict:
            raise BenchmarkInputError("report metric result is invalid")
        lines.append(
            f"{provider} factual={float(factual.get('accuracy', 0.0)):.3f} "
            f"temporal={float(temporal.get('accuracy', 0.0)):.3f} "
            f"noise={int(noise.get('unexpected_marker_hits', 0))} "
            f"p95_ms={float(latency.get('p95', 0.0)):.3f}"
        )
    lines.append("Raw recalled text was not persisted.")
    return "\n".join(lines)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class BankControl:
    """Minimal Hindsight profile lifecycle with exact ownership checks."""

    def __init__(self, inputs: LiveInputs) -> None:
        self._inputs = inputs
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _request(
        self, method: str, path: str, body: object | None = None, *, missing_ok: bool = False
    ) -> dict[str, object] | None:
        data = None if body is None else json.dumps(body, sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            f"{self._inputs.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._inputs.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "better-hermes-hindsight-provider-shadow",
            },
        )
        try:
            with self._opener.open(request, timeout=30.0) as response:
                raw = response.read(_MAX_HTTP_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.read(_MAX_HTTP_BYTES + 1)
            if missing_ok and exc.code == 404:
                return None
            raise BenchmarkInputError("Hindsight profile request failed") from None
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise BenchmarkInputError("Hindsight profile request failed") from exc
        if len(raw) > _MAX_HTTP_BYTES:
            raise BenchmarkInputError("Hindsight profile response exceeded its bound")
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BenchmarkInputError("Hindsight profile response was invalid") from exc
        if type(decoded) is not dict:
            raise BenchmarkInputError("Hindsight profile response was invalid")
        return dict(decoded)

    def verify_version(self) -> str:
        response = self._request("GET", "/version")
        assert response is not None
        version = response.get("api_version")
        if version != self._inputs.expected_hindsight_version:
            raise BenchmarkInputError("Hindsight server version does not match the opt-in")
        return str(version)

    @staticmethod
    def _path(bank: OwnedBank, suffix: str = "") -> str:
        quoted = urllib.parse.quote(bank.bank_id, safe="")
        return f"/v1/default/banks/{quoted}{suffix}"

    def get_profile(self, bank: OwnedBank) -> dict[str, object] | None:
        return self._request("GET", self._path(bank, "/profile"), missing_ok=True)

    def _apply_missions(self, bank: OwnedBank, missions: MissionSet) -> None:
        expected = {
            "retain_mission": missions.retain_mission,
            "observations_mission": missions.observations_mission,
        }
        response = self._request("PATCH", self._path(bank, "/config"), {"updates": expected})
        if type(response) is not dict:
            raise BenchmarkInputError("mission update response was invalid")
        readback = self._request("GET", self._path(bank, "/config"))
        if type(readback) is not dict:
            raise BenchmarkInputError("mission readback response was invalid")
        config = readback.get("config")
        if type(config) is not dict:
            raise BenchmarkInputError("mission readback response was invalid")
        if any(config.get(key) != value for key, value in expected.items()):
            raise BenchmarkInputError("mission readback did not match the pinned policy")

    def create(self, bank: OwnedBank, missions: MissionSet) -> None:
        try:
            if self.get_profile(bank) is not None:
                raise BenchmarkInputError("generated disposable profile already exists")
            response = self._request("PUT", self._path(bank), {"name": bank.ownership_name})
            if type(response) is not dict or response.get("bank_id") != bank.bank_id:
                raise BenchmarkInputError("Hindsight created an unexpected profile")
            validate_owned_profile(self.get_profile(bank), bank)
            self._apply_missions(bank, missions)
        except BaseException:
            with contextlib.suppress(BaseException):
                profile = self.get_profile(bank)
                if profile is not None:
                    validate_owned_profile(profile, bank)
                    self.delete(bank)
            raise

    def delete(self, bank: OwnedBank) -> None:
        profile = self.get_profile(bank)
        if profile is None:
            return
        validate_owned_profile(profile, bank)
        self._request("DELETE", self._path(bank))
        if self.get_profile(bank) is not None:
            raise BenchmarkInputError("disposable profile still exists after cleanup")


class _HangingHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server: Any = self.server
        with server.request_lock:
            server.request_count += 1
        time.sleep(3.0)
        with contextlib.suppress(OSError):
            self.send_response(504)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _CountingHangingServer(http.server.ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HangingHandler)
        self.request_lock = threading.Lock()
        self.request_count = 0


@contextlib.contextmanager
def _hanging_origin() -> Any:
    server = _CountingHangingServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host_value, port = server.server_address[:2]
    host = host_value.decode("ascii") if isinstance(host_value, bytes) else host_value

    def request_count() -> int:
        with server.request_lock:
            return server.request_count

    try:
        yield f"http://{host}:{port}", request_count
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _write_private_bytes(path: Path, encoded: bytes) -> None:
    parent_missing = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent_missing:
        path.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, value: object) -> None:
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _write_private_bytes(path, encoded)


def _load_control(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkInputError("benchmark child control could not be read") from exc
    if type(value) is not dict:
        raise BenchmarkInputError("benchmark child control is invalid")
    return dict(value)


def _materialize_provider_config(home: Path, provider: str, config: Mapping[str, object]) -> None:
    directory = home / ("better_hindsight" if provider == "better" else "hindsight")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    _write_private_json(directory / "config.json", dict(config))


def _new_provider(provider: str) -> Any:
    if provider == "better":
        module = importlib.import_module("better_hermes_hindsight.provider")
        return module.BetterHindsightMemoryProvider()
    if provider == "bundled":
        try:
            importlib.import_module("hindsight_client")
        except ImportError as exc:
            raise BenchmarkInputError(
                "selected Hermes interpreter lacks the bundled Hindsight client dependency"
            ) from exc
        module = importlib.import_module("plugins.memory.hindsight")
        return module.HindsightMemoryProvider()
    raise BenchmarkInputError("unknown benchmark provider")


def _start_manager(
    provider_name: str,
    home: Path,
    session_id: str,
    *,
    external_prefetch_timeout: float = 25.0,
) -> tuple[Any, Any]:
    manager_module = importlib.import_module("agent.memory_manager")
    provider = _new_provider(provider_name)
    manager = manager_module.MemoryManager(external_prefetch_timeout=external_prefetch_timeout)
    manager.add_provider(provider)
    manager.initialize_all(
        session_id,
        hermes_home=os.fspath(home),
        platform="cli",
        agent_context="primary",
        agent_identity="provider-shadow",
        agent_workspace="provider-shadow",
    )
    if provider_name == "better" and getattr(provider, "_runtime", None) is None:
        raise BenchmarkInputError("Better provider did not initialize")
    if manager.get_provider(provider.name) is not provider:
        raise BenchmarkInputError("Hermes did not activate the requested provider")
    return manager, provider


def _stop_manager(provider_name: str, manager: Any, provider: Any) -> None:
    failure: BaseException | None = None
    try:
        manager.shutdown_all()
    except BaseException as exc:
        failure = exc
    if provider_name == "better":
        try:
            runtime = importlib.import_module("better_hermes_hindsight.runtime")
            if runtime.finalize_process_runtime() is not True and failure is None:
                failure = BenchmarkInputError("Better runtime did not finalize")
        except BaseException as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise BenchmarkInputError("provider did not shut down cleanly") from failure


def _outbox_is_drained(inspection: object) -> bool:
    return bool(
        getattr(inspection, "outbox", None) == "ready"
        and getattr(inspection, "mismatch_count", -1) == 0
        and getattr(inspection, "pending_count", -1) == 0
        and getattr(inspection, "retry_count", -1) == 0
        and getattr(inspection, "sending_count", -1) == 0
    )


def _wait_for_better_delivery(home: Path) -> None:
    """Wait until every admitted Better segment has confirmed remote delivery."""

    config_module = importlib.import_module("better_hermes_hindsight.config")
    outbox_module = importlib.import_module("better_hermes_hindsight.outbox")
    config = config_module.load_config(home)
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    while True:
        if _outbox_is_drained(outbox_module.inspect_outbox(config)):
            return
        if time.monotonic() >= deadline:
            raise BenchmarkInputError("Better did not confirm the complete synthetic corpus")
        time.sleep(0.1)


def _wait_for_visible_recall(manager: Any, corpus: BenchmarkCorpus, session_id: str) -> None:
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    while True:
        context = manager.prefetch_all(corpus.readiness.prompt, session_id=session_id)
        if context.strip():
            return
        if time.monotonic() >= deadline:
            raise BenchmarkInputError("retained synthetic corpus did not become recall-visible")
        time.sleep(0.25)


def _run_quality_child(
    provider_name: str,
    home: Path,
    config: Mapping[str, object],
    corpus: BenchmarkCorpus,
    *,
    samples_per_case: int,
) -> list[dict[str, object]]:
    _materialize_provider_config(home, provider_name, config)
    session_id = corpus.turns[0].session_id
    manager, provider = _start_manager(provider_name, home, session_id)
    try:
        for turn in corpus.turns:
            manager.sync_all(
                turn.user,
                turn.assistant,
                session_id=turn.session_id,
                messages=[
                    {"role": "user", "content": turn.user},
                    {"role": "assistant", "content": turn.assistant},
                ],
            )
            if manager.flush_pending(timeout=10.0) is not True:
                raise BenchmarkInputError("Hermes did not admit a synthetic benchmark turn")
        if provider_name == "better":
            _wait_for_better_delivery(home)
        _wait_for_visible_recall(manager, corpus, session_id)
        samples: list[dict[str, object]] = []
        for case in corpus.cases:
            for _sample_number in range(samples_per_case):
                started = time.monotonic()
                context = manager.prefetch_all(case.prompt, session_id=session_id)
                elapsed_ms = (time.monotonic() - started) * 1000.0
                markers = [
                    marker
                    for marker in sorted(corpus.all_markers)
                    for _occurrence in range(context.count(marker))
                ]
                samples.append(
                    {
                        "case_id": case.case_id,
                        "context_bytes": len(context.encode("utf-8")),
                        "elapsed_ms": elapsed_ms,
                        "markers": markers,
                    }
                )
        return samples
    finally:
        _stop_manager(provider_name, manager, provider)


def _timeout_config(
    provider_name: str, base: Mapping[str, object], api_url: str
) -> dict[str, object]:
    loaded = json.loads(json.dumps(base))
    if type(loaded) is not dict:
        raise BenchmarkInputError("timeout policy is invalid")
    config = dict(loaded)
    config["api_url"] = api_url
    config["bank_id"] = "synthetic-timeout-probe"
    if provider_name == "better":
        recall = config.get("recall")
        retain = config.get("retain")
        if type(recall) is not dict or type(retain) is not dict:
            raise BenchmarkInputError("Better timeout policy is invalid")
        recall["timeout_seconds"] = _RECALL_TIMEOUT_SECONDS
        retain["enabled"] = False
    else:
        config["timeout"] = math.ceil(_RECALL_TIMEOUT_SECONDS)
        config["auto_retain"] = False
    return config


def _run_fail_open_child(
    provider_name: str, root: Path, base_config: Mapping[str, object]
) -> dict[str, object]:
    deadline_seconds = 0.25
    with _hanging_origin() as (origin, request_count):
        home = root / "fail-open"
        home.mkdir(parents=True, mode=0o700)
        config = _timeout_config(provider_name, base_config, origin)
        _materialize_provider_config(home, provider_name, config)
        old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = os.fspath(home)
        manager: Any | None = None
        provider: Any | None = None
        try:
            manager, provider = _start_manager(
                provider_name,
                home,
                "timeout-probe",
                external_prefetch_timeout=deadline_seconds,
            )
            if manager is None or provider is None:
                raise BenchmarkInputError("timeout probe provider did not initialize")
            measurements: list[dict[str, object]] = []
            first_request_count = 0
            for attempt in range(2):
                started = time.monotonic()
                context = manager.prefetch_all(
                    "Synthetic timeout probe with no retained data.",
                    session_id="timeout-probe",
                )
                if attempt == 1:
                    time.sleep(0.05)
                observed = request_count()
                if attempt == 0:
                    request_delta = observed
                    first_request_count = observed
                else:
                    request_delta = observed - first_request_count
                measurements.append(
                    {
                        "context_empty": context == "",
                        "elapsed_ms": (time.monotonic() - started) * 1000.0,
                        "requests": request_delta,
                    }
                )
            return {
                "deadline_ms": deadline_seconds * 1000.0,
                "first": measurements[0],
                "mode": "fail_open",
                "provider": provider_name,
                "retry": measurements[1],
                "status": "ok",
            }
        finally:
            if manager is not None and provider is not None:
                _stop_manager(provider_name, manager, provider)
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home


def _child_main(control_path: Path, output_path: Path) -> int:
    try:
        control = _load_control(control_path)
        provider_name = _require_nonempty_string(control.get("provider"), "provider")
        if provider_name not in _PROVIDER_NAMES:
            raise BenchmarkInputError("unknown child provider")
        corpus_path = Path(_require_nonempty_string(control.get("corpus_path"), "corpus_path"))
        config = control.get("config")
        if type(config) is not dict:
            raise BenchmarkInputError("child provider policy is invalid")
        expected_corpus_sha256 = _require_nonempty_string(
            os.environ.get("BH27_CORPUS_SHA256"), "corpus_sha256"
        )
        validate_corpus_digest(corpus_path, expected_corpus_sha256)
        corpus = load_corpus(corpus_path)
        samples_per_case = control.get("samples_per_case")
        if type(samples_per_case) is not int or not 1 <= samples_per_case <= 20:
            raise BenchmarkInputError("child sample count is invalid")
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        samples = _run_quality_child(
            provider_name,
            home,
            config,
            corpus,
            samples_per_case=samples_per_case,
        )
        fail_open = _run_fail_open_child(provider_name, home, config)
        _write_private_json(
            output_path,
            {
                "provider": provider_name,
                "samples": samples,
                "fail_open": fail_open,
                "usage": None,
            },
        )
        return 0
    except BaseException:
        print("provider shadow child failed", file=sys.stderr)
        return 1


def _child_timeout_seconds(corpus: BenchmarkCorpus, samples_per_case: int) -> float:
    """Bound a child from the permitted retain/recall operation budgets."""

    retain_budget = len(corpus.turns) * _RETAIN_TIMEOUT_SECONDS
    recall_budget = len(corpus.cases) * samples_per_case * _RECALL_TIMEOUT_SECONDS
    return (
        (2.0 * _READINESS_TIMEOUT_SECONDS)
        + retain_budget
        + recall_budget
        + 15.0
        + _CHILD_MARGIN_SECONDS
    )


def _subprocess_runner(
    corpus: BenchmarkCorpus,
    inputs: LiveInputs,
    bank: OwnedBank,
    home: Path,
    config: Mapping[str, object],
    *,
    corpus_path: Path,
) -> Mapping[str, object]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    control = home / "control.json"
    output = home / "raw-output.json"
    _write_private_json(
        control,
        {
            "provider": bank.provider,
            "corpus_path": os.fspath(corpus_path),
            "config": dict(config),
            "samples_per_case": inputs.samples_per_case,
        },
    )
    validate_corpus_digest(corpus_path, inputs.corpus_sha256)
    environment = clean_child_environment(
        inputs,
        bank,
        home,
        corpus_path=corpus_path,
        corpus_sha256=inputs.corpus_sha256,
        inherited=os.environ,
    )
    completed = subprocess.run(
        [
            os.fspath(inputs.hermes_python),
            os.fspath(Path(__file__).resolve()),
            "--child",
            os.fspath(control),
            os.fspath(output),
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_child_timeout_seconds(corpus, inputs.samples_per_case),
        check=False,
    )
    if completed.returncode != 0 or not output.is_file():
        raise BenchmarkInputError(f"{bank.provider} provider shadow child failed")
    try:
        value = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkInputError("provider shadow child output was invalid") from exc
    if type(value) is not dict or value.get("provider") != bank.provider:
        raise BenchmarkInputError("provider shadow child output was invalid")
    return dict(value)


def selected_executable(path: Path) -> Path:
    """Return an absolute executable path without resolving a virtualenv symlink."""

    return path.expanduser().absolute()


def _git_identity(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(path), "rev-parse", "HEAD"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=20.0,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise BenchmarkInputError("source commit identity could not be resolved")
    return value


def _patch_identity(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(path), "diff", "--binary", "HEAD", "--"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30.0,
        check=False,
    )
    if completed.returncode != 0:
        raise BenchmarkInputError("source patch identity could not be resolved")
    return hashlib.sha256(completed.stdout).hexdigest()


def _write_public_report(path: Path, report: Mapping[str, object]) -> None:
    if path.exists() and not path.is_file():
        raise BenchmarkInputError("report path is not a regular file")
    _write_private_json(path, dict(report))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--allow-endpoint", action="append", default=[])
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--hermes-python", type=Path, required=True)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--hermes-commit", required=True)
    parser.add_argument("--hindsight-build", required=True)
    parser.add_argument("--model-provider", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-build", required=True)
    parser.add_argument("--samples-per-case", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _tree_identity(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(path), "status", "--porcelain"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=20.0,
        check=False,
    )
    if completed.returncode != 0:
        raise BenchmarkInputError("source tree state could not be resolved")
    return "clean" if not completed.stdout else "dirty"


def require_clean_tree(tree_state: str, source_name: str) -> None:
    """Require immutable source inputs rather than incomplete dirty-tree digests."""

    if tree_state != "clean":
        raise BenchmarkInputError(f"{source_name} source tree must be clean")


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "source-checkout"


def _run_parent(arguments: argparse.Namespace) -> int:
    if os.environ.get("BETTER_HINDSIGHT_ALLOW_BENCHMARK_WRITES") != "1":
        raise BenchmarkInputError("benchmark writes require explicit opt-in")
    api_key = os.environ.get("HINDSIGHT_API_KEY", "")
    if not api_key:
        raise BenchmarkInputError("HINDSIGHT_API_KEY is required")
    if _VERSION_RE.fullmatch(arguments.expected_version) is None:
        raise BenchmarkInputError("expected server version is invalid")
    if not 1 <= arguments.samples_per_case <= 20:
        raise BenchmarkInputError("sample count must be between 1 and 20")
    api_url = validate_endpoint(arguments.api_url, arguments.allow_endpoint)
    hermes_python = selected_executable(arguments.hermes_python)
    if not hermes_python.is_file() or not os.access(hermes_python, os.X_OK):
        raise BenchmarkInputError("selected Hermes Python is not executable")
    hermes_source = arguments.hermes_source.resolve()
    actual_hermes_commit = _git_identity(hermes_source)
    if actual_hermes_commit != arguments.hermes_commit:
        raise BenchmarkInputError("Hermes source commit does not match the opt-in")
    better_commit = _git_identity(ROOT)
    better_tree_state = _tree_identity(ROOT)
    hermes_tree_state = _tree_identity(hermes_source)
    require_clean_tree(better_tree_state, "Better")
    require_clean_tree(hermes_tree_state, "Hermes")

    source_corpus_path = arguments.corpus.resolve()
    corpus_bytes = source_corpus_path.read_bytes()
    corpus_sha256 = hashlib.sha256(corpus_bytes).hexdigest()
    with tempfile.TemporaryDirectory(prefix="bh27-provider-shadow-") as raw_temp:
        homes_root = Path(raw_temp)
        homes_root.chmod(0o700)
        corpus_path = homes_root / "corpus.json"
        _write_private_bytes(corpus_path, corpus_bytes)
        validate_corpus_digest(corpus_path, corpus_sha256)
        corpus = load_corpus(corpus_path)
        inputs = LiveInputs(
            api_url=api_url,
            api_key=api_key,
            expected_hindsight_version=arguments.expected_version,
            hermes_python=hermes_python,
            hermes_source=hermes_source,
            hindsight_build_id=_require_nonempty_string(
                arguments.hindsight_build, "hindsight_build"
            ),
            model_provider=_require_nonempty_string(arguments.model_provider, "model_provider"),
            model_id=_require_nonempty_string(arguments.model_id, "model_id"),
            model_build_id=_require_nonempty_string(arguments.model_build, "model_build"),
            corpus_sha256=corpus_sha256,
            allowed_endpoints=tuple(arguments.allow_endpoint),
            samples_per_case=arguments.samples_per_case,
        )
        control = BankControl(inputs)
        server_version = control.verify_version()

        def runner(
            loaded: BenchmarkCorpus,
            current_inputs: LiveInputs,
            bank: OwnedBank,
            home: Path,
            config: Mapping[str, object],
        ) -> Mapping[str, object]:
            return _subprocess_runner(
                loaded,
                current_inputs,
                bank,
                home,
                config,
                corpus_path=corpus_path,
            )

        raw_runs = collect_live_runs(corpus, inputs, control, runner, homes_root)

    provider_reports: dict[str, Mapping[str, object]] = {}
    for provider in _PROVIDER_NAMES:
        run = raw_runs.get(provider)
        if type(run) is not dict or type(run.get("samples")) is not list:
            raise BenchmarkInputError("provider shadow child output was invalid")
        probe = run.get("fail_open")
        if type(probe) is not dict:
            raise BenchmarkInputError("provider shadow child output was invalid")
        provider_reports[provider] = evaluate_provider(
            corpus,
            run["samples"],
            probe,
            provider=provider,
        )

    better_identity = {
        "git_commit": better_commit,
        "package_version": _package_version("better-hermes-hindsight"),
        "patch_sha256": _patch_identity(ROOT),
        "tree_state": better_tree_state,
    }
    hermes_identity = {
        "git_commit": actual_hermes_commit,
        "package_version": _package_version("hermes-agent"),
        "patch_sha256": _patch_identity(hermes_source),
        "tree_state": hermes_tree_state,
    }
    identities = build_identity_payload(
        corpus,
        inputs,
        better=better_identity,
        hermes=hermes_identity,
        hindsight_version=server_version,
    )
    identities["policies"] = {
        provider: _sha256_json(
            provider_config(
                provider,
                "redacted-origin",
                "redacted-profile",
                corpus.missions,
                retention_enabled=True,
                recall_timeout_seconds=_RECALL_TIMEOUT_SECONDS,
            )
        )
        for provider in _PROVIDER_NAMES
    }
    report = build_report(corpus, identities, provider_reports)
    _write_public_report(arguments.output.resolve(), report)
    print(human_summary(report))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--child":
        if len(arguments) != 3:
            print("provider shadow child failed", file=sys.stderr)
            return 2
        return _child_main(Path(arguments[1]), Path(arguments[2]))
    try:
        return _run_parent(_parser().parse_args(arguments))
    except BenchmarkInputError as exc:
        print(f"provider shadow benchmark refused: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("provider shadow benchmark refused: child deadline expired", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
