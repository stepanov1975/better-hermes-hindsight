"""Tests for the public compatibility and preservation contract."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def test_best_effort_provider_scope_and_lifecycle_are_explicit() -> None:
    public_contract = (
        _read("IMPLEMENTATION.md")
        + _read("README.md")
        + _read("DESIGN.md")
        + _read("docs/audit-findings.md")
    )

    _assert_terms(
        public_contract,
        "canonical plan",
        "better_hindsight",
        "external/self-hosted only",
        "hindsight-client==0.8.5",
        "Hindsight server 0.8.5",
        "current-query recall",
        "only remote or potentially long-running memory work before the first model call",
        "opt-in automatic retention",
        "sync_turn()",
        "durability begins at that plugin commit",
        "no pre-callback or pre-turn-return zero-loss claim",
        "profile-wide POSIX advisory lock",
        "bounded SQLite polling",
        "destination fingerprint",
        'update_mode="replace"',
        "stable document ID",
        "source documents are the preserved record",
        "no Hermes-core prerequisite",
        "no model-facing memory tools in the first prerelease",
        "isolated Hindsight development/canary instances",
    )


def test_compatibility_baseline_and_public_apis_are_frozen() -> None:
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
        "MUST be non-blocking",
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
        "completed-turn callbacks Hermes actually supplies",
        "do not infer human/synthetic origin from text",
        "callbacks lost before Hermes executes the provider hook are outside that guarantee",
        "retired plans",
        "2026-07-25_194157-better-hermes-hindsight-implementation.md",
        "2026-07-27_055353-plugin-only-rescope.md",
        "must never drive implementation",
    )
    assert "separate generic Hermes core prerequisite" not in _normalized(_read("README.md"))
    assert "before hermes reports the turn complete" not in _normalized(_read("README.md"))


def test_sanitized_audit_evidence_is_recorded_without_universal_claims() -> None:
    audit = _read("docs/audit-findings.md")
    public_contract = audit + _read("README.md") + _read("DESIGN.md")

    _assert_terms(
        audit,
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


def test_preservation_evidence_and_active_implementation_order_are_frozen() -> None:
    compatibility = _read("docs/compatibility.md")
    design = _read("DESIGN.md")
    router = _read("IMPLEMENTATION.md")

    _assert_terms(
        compatibility,
        "storage snapshot",
        "logical bank export",
        "baseline counts and hashes",
        "disposable restore proof",
        "rollback",
        "before any production write",
        "#61263",
        "#64914",
        "#70278",
        "still open",
        "no durable SQLite admission/replay",
        "go — continue",
    )
    _assert_terms(
        router,
        "Plan state",
        "Active and independently approved",
        "no Blocking or Important plan finding remains open",
        "Next action",
        "Begin active-plan Task 0",
        "No Hermes-core prerequisite",
        "If the canonical file is missing or its hash differs, stop",
    )
    _assert_terms(
        design,
        "Implementation precedence notice",
        "not implementation authority",
        "The two older plans are retired",
    )


def test_local_plan_files_match_the_tracked_router_when_present() -> None:
    router = _read("IMPLEMENTATION.md")
    active_match = re.search(r"Canonical plan:\*\* `([^`]+)`", router)
    hash_match = re.search(r"Canonical SHA-256:\*\* `([0-9a-f]{64})`", router)
    assert active_match is not None
    assert hash_match is not None

    active_path = ROOT / active_match.group(1)
    if active_path.is_file():
        active_bytes = active_path.read_bytes()
        assert hashlib.sha256(active_bytes).hexdigest() == hash_match.group(1)
        assert "ACTIVE — CANONICAL IMPLEMENTATION PLAN" in active_bytes.decode("utf-8")[:1000]

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
        "docs/audit-findings.md",
    )

    for relative_target in (
        "IMPLEMENTATION.md",
        "docs/compatibility.md",
        "docs/audit-findings.md",
        "DESIGN.md",
    ):
        assert (ROOT / relative_target).is_file(), f"broken repository link: {relative_target}"
