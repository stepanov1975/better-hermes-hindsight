"""Tests for the active best-effort product and compatibility contracts."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PLAN_PATH = ROOT / ".hermes/plans/2026-07-27_071437-best-effort-plugin.md"
LOCAL_PLAN_INDEX_PATH = ROOT / ".hermes/plans/README.md"

_STATUS_COMPATIBILITY_START = b"<!-- better-hindsight-status-compatibility:start -->"
_STATUS_COMPATIBILITY_END = b"<!-- better-hindsight-status-compatibility:end -->"
_STATUS_STORAGE_START = b"<!-- better-hindsight-status-storage:start -->"
_STATUS_STORAGE_END = b"<!-- better-hindsight-status-storage:end -->"

_STATUS_COMPATIBILITY_CONTRACT = b"""\
## Status inspection compatibility

Inspection of an existing outbox requires `os.name == "posix"`, linked SQLite `>=3.22.0`,
Python URI connections, and SQLite's built-in POSIX `unix` VFS selected with `vfs=unix`.
A non-POSIX or older runtime returns fixed `status_unavailable` before `sqlite3.connect()`;
an unavailable `unix` VFS fails selection before the target database is opened. The command
does not support a process-default or custom VFS.
"""

_STATUS_STORAGE_CONTRACT = b"""\
## Status storage contract

- **Active WAL.** When WAL exists, status requires a pre-existing regular SHM file and uses
  SQLite `mode=ro&vfs=unix` with `PRAGMA query_only=ON` and one read transaction. SQLite may
  initialize, recover, resize, or otherwise change contents, size, atime, mtime, and ctime only
  on the same pre-existing regular SHM inode. Its inode, type, link count, mode, UID, GID, and
  xattrs/ACL xattrs remain unchanged.
- **Byte and lock effects.** Status issues no database, WAL, profile-lock, or row-byte writes.
  The point-in-time sender probe may acquire and release a transient kernel `flock` without
  changing lock-file bytes. An authorized writer may change database or WAL bytes and timestamps
  during the read; those external changes are not attributed to status.
- **Sidecar-free snapshot.** When WAL, SHM, and rollback journal are all absent, status uses
  `mode=ro&immutable=1&vfs=unix`, requires the main-file identity/size/mtime/ctime to remain
  unchanged, and requires all three sidecars to remain absent. Missing SHM is not an error in the
  all-sidecars-absent branch.
- **Malformed topology.** If WAL exists but SHM is missing, status fails before SQLite opens and
  creates nothing. A pre-existing rollback journal or SHM without WAL is unavailable. Active WAL
  never uses `immutable=1`.
- **Trusted topology.** Supported concurrency assumes stable file identities and journal mode.
  Observable same-principal races return `status_unavailable` when detected, but raced-path effects
  and undetectable ABA are not prevented; status is not safe against hostile same-UID replacement.
  This is not a zero-mutation claim because SQLite may change the derived SHM as described above.
"""

ACTIVE_CONTRACT_PATHS = (
    "README.md",
    "DESIGN.md",
    "docs/audit-findings.md",
    "docs/compatibility.md",
    "docs/configuration.md",
    "docs/operations.md",
    "docs/public-release-checklist.md",
)

TASK4_FROZEN_AUTHORITY_PATHS = (
    "IMPLEMENTATION.md",
    "README.md",
    "DESIGN.md",
    "docs/audit-findings.md",
    "docs/compatibility.md",
    "docs/configuration.md",
    "docs/operations.md",
    "docs/public-release-checklist.md",
    "src/better_hermes_hindsight/config.py",
    "src/better_hermes_hindsight/hermes_plugin/cli.py",
)

_TASK4_FROZEN_AUTHORITY_SHA256 = {
    "IMPLEMENTATION.md": "TASK4_IMPLEMENTATION_REQUIRED",
    "README.md": "TASK4_IMPLEMENTATION_REQUIRED",
    "DESIGN.md": "TASK4_IMPLEMENTATION_REQUIRED",
    "docs/audit-findings.md": "TASK4_IMPLEMENTATION_REQUIRED",
    "docs/compatibility.md": "TASK4_IMPLEMENTATION_REQUIRED",
    "docs/configuration.md": "TASK4_IMPLEMENTATION_REQUIRED",
    "docs/operations.md": "TASK4_IMPLEMENTATION_REQUIRED",
    "docs/public-release-checklist.md": "TASK4_IMPLEMENTATION_REQUIRED",
    "src/better_hermes_hindsight/config.py": "TASK4_IMPLEMENTATION_REQUIRED",
    "src/better_hermes_hindsight/hermes_plugin/cli.py": "TASK4_IMPLEMENTATION_REQUIRED",
}


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


def _extract_marked_contract(
    document: bytes,
    *,
    start: bytes,
    end: bytes,
) -> bytes:
    document.decode("utf-8", errors="strict")
    assert b"\r" not in document
    assert document.count(start) == 1
    assert document.count(end) == 1

    # CommonMark physical lines are LF-delimited; Unicode separators are ordinary content bytes.
    lines = document.split(b"\n")
    assert lines.count(start) == 1
    assert lines.count(end) == 1
    start_index = lines.index(start)
    end_index = lines.index(end)
    assert start_index < end_index
    assert end_index < len(lines) - 1

    before_lines = lines[:start_index]
    before = b"\n".join(before_lines)
    # The owner section must precede every raw-HTML opener, rather than partially parsing HTML.
    assert b"<" not in before

    fence_character: str | None = None
    fence_length = 0
    for raw_line in before_lines:
        line = raw_line.decode("utf-8")
        if fence_character is not None:
            closing = re.fullmatch(
                rf" {{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*",
                line,
            )
            if closing is not None:
                fence_character = None
                fence_length = 0
            continue

        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening is None:
            continue
        delimiter, info = opening.groups()
        if delimiter[0] == "`" and "`" in info:
            continue
        fence_character = delimiter[0]
        fence_length = len(delimiter)
    assert fence_character is None
    contract_lines = lines[start_index + 1 : end_index]
    return b"\n".join(contract_lines) + b"\n"


def _assert_task4_status_document_contract(contents: dict[str, bytes]) -> None:
    assert contents.keys() == dict.fromkeys(TASK4_FROZEN_AUTHORITY_PATHS).keys()
    for document in contents.values():
        document.decode("utf-8", errors="strict")
    marker_owners = {
        _STATUS_COMPATIBILITY_START: "docs/compatibility.md",
        _STATUS_COMPATIBILITY_END: "docs/compatibility.md",
        _STATUS_STORAGE_START: "docs/operations.md",
        _STATUS_STORAGE_END: "docs/operations.md",
    }
    for marker, owner in marker_owners.items():
        assert sum(document.count(marker) for document in contents.values()) == 1
        assert contents[owner].count(marker) == 1

    compatibility_contract = _extract_marked_contract(
        contents["docs/compatibility.md"],
        start=_STATUS_COMPATIBILITY_START,
        end=_STATUS_COMPATIBILITY_END,
    )
    storage_contract = _extract_marked_contract(
        contents["docs/operations.md"],
        start=_STATUS_STORAGE_START,
        end=_STATUS_STORAGE_END,
    )
    assert compatibility_contract == _STATUS_COMPATIBILITY_CONTRACT
    assert storage_contract == _STATUS_STORAGE_CONTRACT


def _assert_task4_frozen_authority_hashes(
    contents: dict[str, bytes],
    expected_hashes: dict[str, str] | None = None,
) -> None:
    if expected_hashes is None:
        expected_hashes = _TASK4_FROZEN_AUTHORITY_SHA256
    assert contents.keys() == expected_hashes.keys()
    for relative_path, content in contents.items():
        expected = expected_hashes[relative_path]
        assert re.fullmatch(r"[0-9a-f]{64}", expected) is not None
        assert hashlib.sha256(content).hexdigest() == expected


def _task4_literal_hash_map_from_source(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_TASK4_FROZEN_AUTHORITY_SHA256"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    value = assignments[0].value
    assert isinstance(value, ast.Dict)
    assert all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in value.keys)
    assert all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.values
    )
    return {
        key.value: item.value
        for key, item in zip(value.keys, value.values, strict=True)
        if isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and isinstance(item, ast.Constant)
        and isinstance(item.value, str)
    }


def _assert_task4_production_hash_map_is_source_literal() -> None:
    source = (ROOT / "tests/test_repository_contract.py").read_text(encoding="utf-8")
    literal = _task4_literal_hash_map_from_source(source)
    assert literal == _TASK4_FROZEN_AUTHORITY_SHA256
    assert tuple(literal) == TASK4_FROZEN_AUTHORITY_PATHS


def _read_local_plan_pair() -> tuple[bytes, bytes] | None:
    plan_exists = LOCAL_PLAN_PATH.is_file()
    index_exists = LOCAL_PLAN_INDEX_PATH.is_file()
    if not plan_exists and not index_exists:
        return None
    assert plan_exists and index_exists, "local canonical plan and index must exist together"
    return (
        LOCAL_PLAN_PATH.read_bytes(),
        LOCAL_PLAN_INDEX_PATH.read_bytes(),
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
    assert (
        "IMPLEMENTATION.md",
        *ACTIVE_CONTRACT_PATHS,
        "src/better_hermes_hindsight/config.py",
        "src/better_hermes_hindsight/hermes_plugin/cli.py",
    ) == TASK4_FROZEN_AUTHORITY_PATHS
    assert tuple(_TASK4_FROZEN_AUTHORITY_SHA256) == TASK4_FROZEN_AUTHORITY_PATHS


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
    readme = _read("README.md")
    _assert_terms(
        readme,
        "sender delivery is implemented",
        "completed sender-delivery checkpoint",
        "retention remains disabled by default",
        "managed installation",
        "isolated live-write proof remain incomplete",
    )

    delivery_contract = _read("docs/configuration.md") + _read("docs/operations.md")
    _assert_terms(
        delivery_contract,
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
        "active-plan Task 4",
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
    plan_bytes, _plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
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
    _assert_terms(
        router,
        "ef200c948b738a34f9a74a6ee3f2a964445c5126",
        "active-plan Task 4",
    )
    normalized_router = _normalized(router)
    for premature_completion in (
        "Task 4 is complete",
        "Tasks 0–4 are complete",
        "completed Task 4",
    ):
        assert premature_completion.casefold() not in normalized_router

    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        return
    plan_bytes, plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
    plan_index = plan_index_bytes.decode("utf-8", errors="strict")
    active_match = re.search(r"Canonical plan:\*\* `([^`]+)`", router)
    hash_match = re.search(r"Canonical SHA-256:\*\* `([0-9a-f]{64})`", router)
    index_hash_match = re.search(r"SHA-256: `([0-9a-f]{64})`", plan_index)
    assert active_match is not None
    assert hash_match is not None
    assert index_hash_match is not None
    assert active_match.group(1).endswith("2026-07-27_071437-best-effort-plugin.md")
    assert hash_match.group(1) == index_hash_match.group(1)
    _assert_terms(plan_index, "ef200c9", "active-plan Task 4")

    active_path = ROOT / active_match.group(1)
    assert active_path == LOCAL_PLAN_PATH
    assert hashlib.sha256(plan_bytes).hexdigest() == hash_match.group(1)
    crlf_plan_bytes = plan_bytes.replace(b"\n", b"\r\n")
    assert crlf_plan_bytes != plan_bytes
    assert hashlib.sha256(crlf_plan_bytes).hexdigest() != hash_match.group(1)
    assert "ACTIVE — CANONICAL IMPLEMENTATION PLAN" in plan[:1000]

    for retired_name in (
        "2026-07-25_194157-better-hermes-hindsight-implementation.md",
        "2026-07-27_055353-plugin-only-rescope.md",
    ):
        retired_path = ROOT / ".hermes" / "plans" / retired_name
        if retired_path.is_file():
            retired_header = "\n".join(retired_path.read_text(encoding="utf-8").splitlines()[:10])
            _assert_terms(retired_header, "RETIRED — DO NOT IMPLEMENT", "HISTORICAL RECORD ONLY")


def test_task4_production_hash_oracle_is_source_literal() -> None:
    _assert_task4_production_hash_map_is_source_literal()
    computed_forms = (
        "_TASK4_FROZEN_AUTHORITY_SHA256 = {p: p for p in ()}",
        '_TASK4_FROZEN_AUTHORITY_SHA256 = {"x": make_hash()}',
        '_TASK4_FROZEN_AUTHORITY_SHA256 = {"x": "a" + "b"}',
        '_TASK4_FROZEN_AUTHORITY_SHA256 = {**{"x": "y"}}',
    )
    for source in computed_forms:
        with pytest.raises(AssertionError):
            _task4_literal_hash_map_from_source(source)


def test_task4_status_public_documentation_is_frozen_in_clean_clones() -> None:
    if not (ROOT / "src/better_hermes_hindsight/management.py").is_file():
        return

    _assert_task4_production_hash_map_is_source_literal()
    contents: dict[str, bytes] = {}
    for relative_path in TASK4_FROZEN_AUTHORITY_PATHS:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        contents[relative_path] = path.read_bytes()
    _assert_task4_status_document_contract(contents)
    _assert_task4_frozen_authority_hashes(contents)


def test_task4_status_document_oracle_rejects_structural_and_authority_drift() -> None:
    compatibility = (
        _STATUS_COMPATIBILITY_START
        + b"\n"
        + _STATUS_COMPATIBILITY_CONTRACT
        + _STATUS_COMPATIBILITY_END
        + b"\n"
    )
    operations = (
        _STATUS_STORAGE_START + b"\n" + _STATUS_STORAGE_CONTRACT + _STATUS_STORAGE_END + b"\n"
    )
    contents = {
        relative_path: f"frozen authority: {relative_path}\n".encode()
        for relative_path in TASK4_FROZEN_AUTHORITY_PATHS
    }
    contents["docs/compatibility.md"] = compatibility
    contents["docs/operations.md"] = operations

    # The raw byte bodies include every valid branch qualification and explicit negation.
    _assert_task4_status_document_contract(contents)
    expected_hashes = {
        relative_path: hashlib.sha256(content).hexdigest()
        for relative_path, content in contents.items()
    }
    _assert_task4_frozen_authority_hashes(contents, expected_hashes)

    structural_counterfactuals: list[dict[str, bytes]] = []
    for changed_compatibility in (
        compatibility.replace(b"`vfs=unix`", b"`VFS=UNIX`", 1),
        compatibility.replace(
            _STATUS_COMPATIBILITY_START + b"\n",
            b"prefix " + _STATUS_COMPATIBILITY_START + b"\n",
            1,
        ),
        compatibility.replace(
            _STATUS_COMPATIBILITY_START + b"\n",
            b"  " + _STATUS_COMPATIBILITY_START + b"\n",
            1,
        ),
        b"````markdown\n```\n" + compatibility + b"````\n",
        b"~~~~markdown\n~~~\n" + compatibility + b"~~~~\n",
        b"<!-- open comment\n" + compatibility + b"-->\n",
        b'<script type="text/plain">\n' + compatibility + b"</script>\n",
        compatibility.replace(b"\n", b"\r\n"),
    ):
        changed = dict(contents)
        changed["docs/compatibility.md"] = changed_compatibility
        structural_counterfactuals.append(changed)

    for delimiter in (b"````", b"~~~~"):
        for non_commonmark_separator in (
            "\u2028".encode(),
            "\u2029".encode(),
            "\u0085".encode(),
            b"\x0b",
        ):
            changed = dict(contents)
            changed["docs/compatibility.md"] = (
                delimiter
                + b"\ntext"
                + non_commonmark_separator
                + delimiter
                + b"\n"
                + compatibility
                + delimiter
                + b"\n"
            )
            structural_counterfactuals.append(changed)

    changed_indentation = dict(contents)
    changed_indentation["docs/operations.md"] = operations.replace(
        b"  SQLite `mode=ro", b"    SQLite `mode=ro", 1
    )
    structural_counterfactuals.append(changed_indentation)

    duplicate_elsewhere = dict(contents)
    duplicate_elsewhere["README.md"] += b"\n" + compatibility
    structural_counterfactuals.append(duplicate_elsewhere)

    for changed in structural_counterfactuals:
        recomputed_hashes = {
            relative_path: hashlib.sha256(content).hexdigest()
            for relative_path, content in changed.items()
        }
        _assert_task4_frozen_authority_hashes(changed, recomputed_hashes)
        with pytest.raises(AssertionError):
            _assert_task4_status_document_contract(changed)

    contradictions = (
        b"Status fails whenever S&#72;M is absent.",
        b"The shared-memory coordination file and all timestamps remain unchanged.",
        b"SQLite's default V&#70;S is supported too.",
        b"Every concurrency race is prevented.",
    )
    for relative_path in TASK4_FROZEN_AUTHORITY_PATHS:
        for contradiction in contradictions:
            changed = dict(contents)
            changed[relative_path] += b"\n" + contradiction + b"\n"
            with pytest.raises(AssertionError):
                _assert_task4_frozen_authority_hashes(changed, expected_hashes)


def test_task4_sqlite_wal_contract_amendment_is_frozen_when_local_plan_is_present() -> None:
    router = _read("IMPLEMENTATION.md")
    _assert_terms(
        router,
        "f4d71a33f327510f70e64a1e3d0533281fd8a22862c42e5fbe54d54c08fb6562",
        "ordinary read of a fully checkpointed closed WAL-mode file may create empty sidecars",
        (
            "No Task 4 implementation candidate may be finalized until this amendment "
            "is independently approved"
        ),
        "no retry/drain command",
    )

    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        return
    plan_bytes, plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
    plan_index = plan_index_bytes.decode("utf-8", errors="strict")
    task4 = plan.split("### Task 4:", maxsplit=1)[1].split("### Task 5:", maxsplit=1)[0]
    _assert_terms(
        task4,
        "src/better_hermes_hindsight/hermes_plugin/cli.py",
        "synchronous `better_hindsight_command(args)`",
        "mutually exclusive ordered partition",
        "`1m_to_lt_1h`",
        "SQLite `mode=ro&vfs=unix`",
        "`PRAGMA query_only=ON`",
        "existing regular `-shm` is also required at preflight",
        "`-shm` is derived SQLite coordination state",
        "initialize, recover, resize, or otherwise update WAL-index contents",
        "link count, mode, UID, GID, and xattrs/ACL xattrs must remain unchanged",
        "may only acquire/release a transient kernel `flock`",
        "Quiescent mutation-oracle tests require database/WAL/profile-lock bytes",
        "authorized same-principal writer may legitimately append, checkpoint, or update",
        "misattributing external byte or timestamp changes to status",
        "exact `single_principal=true` assertion is also the filesystem threat boundary",
        (
            "supported concurrency case is ordinary row work against stable "
            "database/sidecar identities"
        ),
        "journal-mode transitions, sidecar teardown, and ABA substitution",
        "cannot prevent SQLite from touching/creating a raced pathname",
        "same-principal TOCTOU side effects and undetectable ABA limit",
        "neither `-wal`, `-shm`, nor rollback `-journal` exists at preflight",
        "`mode=ro&immutable=1&vfs=unix` for that sidecar-absent main-file snapshot",
        "regardless of the persisted journal mode",
        "requires all three sidecars to remain absent",
        "pre-existing rollback journal or SHM without WAL is unavailable",
        "`immutable=1` is forbidden whenever a WAL exists",
        "avoid a bespoke WAL parser",
        "commits a row present only in uncheckpointed WAL frames",
        "active `immutable=1` would return the stale main-file count",
        '`os.name == "posix"`',
        "SQLite `>=3.22.0`",
        "before `sqlite3.connect()`",
        "built-in POSIX `unix` VFS selected explicitly with `vfs=unix`",
        ("unavailable `unix` VFS fails connection selection before the target database is opened"),
        "public operations documentation",
        "`single_principal=true`",
        "exact pinned SDK `BankConfigResponse`",
        "write_attempted` immediately before PATCH dispatch",
        "write_attempted_outcome_unknown",
        "Exit codes are fixed",
        "`outbox` is exactly `ready|uninitialized`",
        "`authorization_required`",
        "`mission_prewrite_unavailable`",
        "`runtime_cleanup_failed`",
        "src/better_hermes_hindsight/config.py",
        "host-owned stderr may echo arbitrarily long malformed argv",
        "all-unconfigured failed-GET case",
        "docs/audit-findings.md",
        "Define the Task 4 frozen authority corpus exactly as tracked `IMPLEMENTATION.md`",
        "public configuration docstring owner",
        "public CLI-help owner",
        "Once tracked `management.py` exists, a separate unconditional clean-clone repository test",
        "read that complete corpus as raw UTF-8 bytes",
        "`better-hindsight-status-compatibility`",
        "`better-hindsight-status-storage`",
        "none of the four marker tokens may occur in another authority file",
        "marker must precede every `<` byte in its file",
        "Physical lines are split only on the LF byte",
        "U+2028, U+2029, NEL, VT",
        "explicit source-literal dictionary",
        "AST discriminator rejects comprehensions",
        "Canonical ignored-plan SHA checks likewise hash `read_bytes()` directly",
        "CRLF mutation misses the tracked digest",
        "same-line/indented markers, shorter pseudo-closes",
        "import/help perform no database or lock access",
        "Do not add IPC, a retry/drain command",
    )
    _assert_terms(
        plan,
        (
            "Handler-controlled JSON from mission status/check/apply is bounded and sanitized; "
            "released host-owned argparse stderr is outside that guarantee"
        ),
        "never claims a custom VFS or hostile same-UID safety",
        "An unconditional clean-clone test activated by tracked `management.py`",
        "complete explicitly scoped authority corpus as raw bytes",
        "globally unique owner-only case-sensitive LF marker lines",
        "fences parsed on LF bytes only",
        "ignored canonical-plan SHA against raw bytes",
        "AST-proven source-literal map",
        "Unicode-separator pseudo-closes",
        "sentinels, computed hashes, CRLF/case/structure drift",
        "without trying to interpret arbitrary English",
    )
    assert "Mission status/check/apply commands are bounded and sanitized" not in plan

    _assert_terms(
        plan_index,
        "f4d71a33f327510f70e64a1e3d0533281fd8a22862c42e5fbe54d54c08fb6562",
        "requires independent approval before implementation resumes",
    )


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
