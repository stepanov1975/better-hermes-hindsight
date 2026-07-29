"""Tests for the active best-effort product and compatibility contracts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PLAN_PATH = ROOT / ".hermes/plans/2026-07-27_071437-best-effort-plugin.md"
LOCAL_PLAN_INDEX_PATH = ROOT / ".hermes/plans/README.md"

ACTIVE_CONTRACT_PATHS = (
    "README.md",
    "DESIGN.md",
    "docs/audit-findings.md",
    "docs/compatibility.md",
    "docs/configuration.md",
    "docs/operations.md",
    "docs/public-release-checklist.md",
)


def _read(relative_path: str) -> str:
    path = ROOT / relative_path
    assert path.is_file(), f"required repository contract is missing: {relative_path}"
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold()


def _assert_terms(text: str, *terms: str) -> None:
    normalized = _normalized(text)
    missing = [term for term in terms if term.casefold() not in normalized]
    assert not missing, f"missing repository contract terms: {missing}"


def _read_local_plan_pair() -> tuple[str, str] | None:
    plan_exists = LOCAL_PLAN_PATH.is_file()
    index_exists = LOCAL_PLAN_INDEX_PATH.is_file()
    if not plan_exists and not index_exists:
        return None
    assert plan_exists and index_exists, "local canonical plan and index must exist together"
    return (
        LOCAL_PLAN_PATH.read_text(encoding="utf-8"),
        LOCAL_PLAN_INDEX_PATH.read_text(encoding="utf-8"),
    )


def test_owned_active_contract_inventory_is_complete() -> None:
    assert ACTIVE_CONTRACT_PATHS == (
        "README.md",
        "DESIGN.md",
        "docs/audit-findings.md",
        "docs/compatibility.md",
        "docs/configuration.md",
        "docs/operations.md",
        "docs/public-release-checklist.md",
    )
    for relative_path in ACTIVE_CONTRACT_PATHS:
        assert (ROOT / relative_path).is_file()


def test_best_effort_provider_scope_and_lifecycle_are_explicit() -> None:
    public_contract = "\n".join(_read(path) for path in ACTIVE_CONTRACT_PATHS)

    _assert_terms(
        public_contract,
        "better_hindsight",
        "external/self-hosted only",
        "hindsight-client==0.8.5",
        "Hindsight server 0.8.5",
        "current-query recall",
        "recall is enabled by default",
        "automatic retention is disabled by default",
        "released `sync_turn()`",
        "best-effort",
        "local durability starts only after provider admission",
        "no direct-user provenance claim",
        "no pre-return or no-loss guarantee",
        "no Hermes-core prerequisite",
        "`codex_app_server` is unsupported on the pinned release",
        "isolated Hindsight instance and Hermes profile",
        "separate canary instance and bank",
        "preserves the old deployment",
        "no model-facing memory tools in the first prerelease",
        "profile-wide POSIX advisory lock",
        "bounded SQLite polling",
        "destination fingerprint",
        'update_mode="replace"',
        "stable document ID",
        "source documents are the preserved record",
    )


def test_task3_delivery_checkpoint_is_documented_without_task4_or_rollout_claims() -> None:
    public_contract = "\n".join(
        _read(path) for path in ("README.md", "docs/configuration.md", "docs/operations.md")
    )

    _assert_terms(
        public_contract,
        "sender delivery is implemented",
        "retention remains disabled by default",
        "managed installation and isolated live-write proof remain incomplete",
        "profile-wide POSIX advisory lock",
        "bounded cross-process polling",
        "typed confirmation",
        "retain_timeout",
        "retain_failed",
        "retain_unconfirmed",
        "stable document ID",
        "replace mode",
        "not exactly-once transport",
        "operator-visible queue counts and management commands remain Task 4",
    )


def test_active_contracts_do_not_reinstate_retired_requirements() -> None:
    for relative_path in ACTIVE_CONTRACT_PATHS:
        normalized = _normalized(_read(relative_path))
        assert "separate generic core prerequisite" not in normalized, relative_path
        assert "unknown origin remains ineligible" not in normalized, relative_path
        assert "before hermes reports the turn complete" not in normalized, relative_path
        assert "guarantees exactly-once transport" not in normalized, relative_path
        assert "provides exactly-once transport" not in normalized, relative_path
        assert "integration_mode" not in normalized, relative_path


def test_compatibility_baseline_and_released_callback_boundary_are_frozen() -> None:
    compatibility = _read("docs/compatibility.md")

    _assert_terms(
        compatibility,
        "v2026.7.20",
        "package 0.19.0",
        "3ef6bbd201263d354fd83ec55b3c306ded2eb72a",
        "41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f",
        "4dae897265f09ed5b26f5e02b0f0fcb1325e0b6d",
        "ahead by 9",
        "behind by 1,416",
        "eb52760564dbba2e5971fa54bd67384e281cd3b8",
        "705757f362552918dfb0242906cb8466de320378",
        "installed hindsight-client 0.6.1",
        "hindsight-client==0.8.5",
        "Hindsight server 0.8.5",
        "plugins/memory/__init__.py",
        "plugins/memory/<name>/",
        "$HERMES_HOME/plugins/<name>/",
        "ctx.register_memory_provider",
        "MemoryProvider",
        "is_available()",
        "prefetch()",
        "queue_prefetch()",
        "sync_turn()",
        "serialized background executor",
        "documented as non-blocking",
        "may fail before Better Hindsight receives the callback",
        "arecall()",
        "aretain_batch()",
        "get_bank_profile()",
        "get_bank_config()",
        "update_bank_config()",
        "acreate_bank()",
        "delete_bank()",
        "aclose()",
    )

    for commit in (
        "41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f",
        "821b11e631e5f663f2e9f915f77a353d1c528cbc",
        "d48bcb0d400bf852499758080f50ae49ed857626",
        "151a1b4f6045c86577806feb55909ed33e608752",
        "0e4f1ba7e017aac4d3c0995be941c5ce6364a17b",
        "fb8cc28775bc247eacc964d2c5d5d88830152adf",
        "517e0bcf0511564eec0290037e2a8d0ff3f1c895",
        "fcb1f9da2bb5f38f6270137fce13d051449cbcc5",
        "2b1d2bafc3e4f13ed8b961b6c81effccd7c066bd",
    ):
        assert commit in compatibility


def test_callback_boundary_and_retired_plan_precedence_are_explicit() -> None:
    router = _read("IMPLEMENTATION.md")
    public_contract = router + _read("README.md") + _read("docs/audit-findings.md")

    _assert_terms(
        public_contract,
        "completed-turn callbacks released Hermes actually supplies",
        "do not infer human/synthetic origin from text",
        "callbacks lost before Hermes executes the provider hook are outside that guarantee",
        "retired plans",
        "2026-07-25_194157-better-hermes-hindsight-implementation.md",
        "2026-07-27_055353-plugin-only-rescope.md",
        "must never drive implementation",
        "active-plan Task 3",
    )


def test_sanitized_audit_evidence_and_superseded_ideals_are_preserved() -> None:
    audit = _read("docs/audit-findings.md")
    public_contract = audit + _read("README.md") + _read("DESIGN.md")

    _assert_terms(
        audit,
        "superseded ideal requirements",
        "authoritative structured origin",
        "inline admission before turn return",
        "8.5–11 kB per query",
        "2.41 seconds median",
        "2.65 seconds p95",
        "874 documents",
        "13,197 raw memory units",
        "8,451 active observations",
        "82.65%",
        "73.67%",
        "0.54%",
        "min_scores.final = 0.10",
        "project-specific",
    )
    _assert_terms(public_contract, "narrower", "not universally better")


def test_configuration_contract_has_only_direct_capability_switches_and_finite_bounds() -> None:
    configuration = _read("docs/configuration.md")
    config_source = _read("src/better_hermes_hindsight/config.py")

    assert "integration_mode" not in _normalized(configuration + config_source)
    assert "IntegrationMode" not in config_source
    assert "MissionPolicy" not in config_source
    assert "missions.policy" not in _normalized(configuration + config_source)
    assert "outbox.retry_multiplier" not in _normalized(configuration + config_source)
    assert "outbox.shutdown_join_seconds" not in _normalized(configuration + config_source)
    _assert_terms(
        configuration,
        "recall.enabled` | `true`",
        "retain.enabled` | `false`",
        "retain.timeout_seconds` | `60.0`",
        "at most 300 seconds",
        "outbox.max_pending_rows` | `2000`",
        "Integer from 1 through 100,000",
        "outbox.max_pending_bytes` | `134217728`",
        "Integer from 1 through 1,073,741,824",
        "outbox.busy_timeout_seconds` | `1.0`",
        "outbox.poll_interval_seconds` | `2.0`",
        "0.1 through 60.0 seconds",
        "outbox.retry_initial_seconds` | `2.0`",
        "outbox.retry_max_seconds` | `300.0`",
        "at most 3,600 seconds",
        "segment_max_bytes",
        "must not exceed `outbox.max_pending_bytes`",
        "retry_initial_seconds",
        "must not exceed `outbox.retry_max_seconds`",
    )


def test_release_gate_requires_isolated_development_and_reversible_canary() -> None:
    checklist = _read("docs/public-release-checklist.md")

    _assert_terms(
        checklist,
        "no Hermes core patch or patched SHA",
        "released `sync_turn()` callback",
        "durability begins only after the provider admission commit",
        "no direct-user provenance",
        "no pre-return or no-loss guarantee",
        "`codex_app_server` remains unsupported",
        "retention is disabled by default",
        "no model-facing memory tools",
        "isolated Hindsight instance and Hermes profile",
        "separate canary instance and bank",
        "preserves the old deployment",
    )


def test_task3_sender_contract_is_frozen_before_red_tests() -> None:
    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        return
    plan, _plan_index = local_plan_pair
    task3 = plan.split("### Task 3:", maxsplit=1)[1].split("### Task 4:", maxsplit=1)[0]

    _assert_terms(
        task3,
        "schema version 1 remains unchanged",
        "canonical redacted and sorted retain tags",
        "normalized observation scopes",
        "literal golden document IDs remain byte-for-byte unchanged",
        "observation scopes bind the destination fingerprint and wire request only",
        "reset every stale `sending` row",
        "cap-first saturating retry loop",
        "`attempt_count=10_000`",
        "network I/O occurs outside SQLite transactions",
        "`retain_timeout`, `retain_failed`, or `retain_unconfirmed`",
        "typed `RetainConfirmation`",
        "retain-enabled runtime starts exactly one daemon sender before the runtime is published",
        "passive contender",
        "non-owner admission remains allowed",
        "must not close the outbox or client beneath a live sender",
        "explicit draining/unsettled state",
        "per-call unsettled token set",
        "one runner lock/condition",
        "atomically rechecks completion and publishes",
        "already-admitted prepublication calls",
        "two cancellation-resistant calls",
        "completion/registration race",
        "`AsyncRunnerUnsettledError`",
        "no second SDK call",
        "crossed retain deadline remains `retain_timeout`",
        "cancellation-resistant retain",
        "strictly valid late success",
        "competing process proves the profile lock remains held",
        "sender has already joined",
        "second `finalize_process_runtime()`",
        "outbox/client/runner close counts remain zero",
        "real pinned `MemoryManager` and real pinned SDK adapter",
        "secret-shaped retain tags",
        "boolean/integer lookalikes",
        "`tests/integration/test_released_hermes_admission.py`",
        "`tests/unit/test_provider_retention.py`",
        "`README.md`",
        "`tests/test_repository_contract.py`",
        "`docs/operations.md` enters `ACTIVE_CONTRACT_PATHS`",
        "route to active-plan Task 4",
        "operator-visible status counts remain Task 4",
    )


def test_local_plan_files_match_the_tracked_router_when_present() -> None:
    router = _read("IMPLEMENTATION.md")
    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        return
    plan, plan_index = local_plan_pair
    active_match = re.search(r"Canonical plan:\*\* `([^`]+)`", router)
    hash_match = re.search(r"Canonical SHA-256:\*\* `([0-9a-f]{64})`", router)
    index_hash_match = re.search(r"SHA-256: `([0-9a-f]{64})`", plan_index)
    assert active_match is not None
    assert hash_match is not None
    assert index_hash_match is not None
    assert active_match.group(1).endswith("2026-07-27_071437-best-effort-plugin.md")
    assert hash_match.group(1) == index_hash_match.group(1)
    _assert_terms(plan_index, "8a1aa51", "active-plan Task 3")

    active_path = ROOT / active_match.group(1)
    assert active_path == LOCAL_PLAN_PATH
    active_bytes = plan.encode("utf-8")
    assert hashlib.sha256(active_bytes).hexdigest() == hash_match.group(1)
    assert "ACTIVE — CANONICAL IMPLEMENTATION PLAN" in plan[:1000]

    for retired_name in (
        "2026-07-25_194157-better-hermes-hindsight-implementation.md",
        "2026-07-27_055353-plugin-only-rescope.md",
    ):
        retired_path = ROOT / ".hermes" / "plans" / retired_name
        if retired_path.is_file():
            retired_header = "\n".join(retired_path.read_text(encoding="utf-8").splitlines()[:10])
            _assert_terms(retired_header, "RETIRED — DO NOT IMPLEMENT", "HISTORICAL RECORD ONLY")


def test_changed_markdown_links_resolve_inside_repository() -> None:
    readme = _read("README.md")
    _assert_terms(
        readme,
        "IMPLEMENTATION.md",
        "docs/compatibility.md",
        "docs/operations.md",
        "docs/audit-findings.md",
    )

    for relative_target in (
        "IMPLEMENTATION.md",
        "docs/compatibility.md",
        "docs/operations.md",
        "docs/audit-findings.md",
        "DESIGN.md",
    ):
        assert (ROOT / relative_target).is_file(), f"broken repository link: {relative_target}"
