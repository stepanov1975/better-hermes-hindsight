# Better Hermes Hindsight

[![CI](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml)
[![Security scans](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml)

An unofficial, external/self-hosted-only Hindsight memory provider project for Hermes Agent. The
provider ID is `better_hindsight`, deliberately distinct from bundled `hindsight` so rollback is a
configuration change.

> **Status: pre-alpha.** A recall-only development checkpoint exists, but automatic retention,
> retry delivery, managed installation, and isolated live-write proof are not implemented yet. Do
> not install or select this project in a production Hermes profile. Read
> [IMPLEMENTATION.md](IMPLEMENTATION.md) before changing code; it identifies the only active plan.

## Goal

Build a smaller, testable plugin for released Hermes that recalls against the current user query and,
when explicitly enabled, best-effort retains the completed-turn callbacks Hermes actually supplies.
Local retry durability begins only after Better Hindsight's own SQLite admission commits; callbacks
lost before Hermes executes the provider hook are outside that guarantee.

“Better” is narrower: it means better for the documented external/self-hosted use case and proof
criteria. It is not universally better than official Hermes or Hindsight.

## Initial scope

- External/self-hosted only; no cloud or embedded-daemon management.
- Exact initial target: `hindsight-client==0.8.5` with Hindsight server 0.8.5.
- Current-query recall is the only remote or potentially long-running memory work before the first
  model call, with a bounded fail-open deadline.
- Automatic retention is opt-in and accepts non-empty user/final-assistant text from released
  Hermes `sync_turn()` without claiming authoritative human-versus-synthetic origin.
- `sync_turn()` performs only bounded local redaction, segmentation, and atomic SQLite admission;
  remote retention stays off the response path. No pre-callback or pre-turn-return durability is
  claimed.
- One profile-wide POSIX advisory lock elects the remote sender across processes; bounded SQLite
  polling lets the owner observe rows admitted by another process.
- Pending rows are destination fingerprint matched and replay the stable document ID with
  `update_mode="replace"` until synchronous confirmation.
- Source documents are the preserved record; facts, observations, embeddings, and summaries are
  derived indexes.
- Recall controls and separate retain/observation mission check/apply operations use audited public
  Hindsight 0.8.5 surfaces; automatic reflection is outside the first prerelease.
- A distinct `better_hindsight` identity for safe rollback to bundled `hindsight` without bank
  migration or deletion.

There is no production auto-install, provider selection, service restart, or production-bank
mutation during implementation. Fake tests run first; live writes require a separate Hindsight
development instance, datastore, key, generated bank, and Hermes profile. Cloud setup,
`codex_app_server` memory support, production-bank migration, model-facing memory tools, and
automatic reflection are outside the first prerelease.

## Implementation boundary

The product has **no Hermes-core prerequisite**. It uses released public `MemoryProvider` hooks and
documents the host limitations it cannot close instead of patching Hermes. The tracked
[implementation router](IMPLEMENTATION.md) identifies the canonical plan, its hash, current
checkpoint, and two explicitly retired plans. Never infer an implementation path from an older plan
or from stale proof wording in a detailed document.

The exact version/source baseline and public API observations remain in
[docs/compatibility.md](docs/compatibility.md). Sanitized operational aggregates and their limited
interpretation remain in [docs/audit-findings.md](docs/audit-findings.md). Active-plan Task 0 owns
their complete best-effort contract rewrite.

## Repository state

The active implementation branch is `spike/local-external-provider`; commit `4e437fc` is the
completed recall-only baseline. The next implementation slice is Task 0 from the canonical plan,
after its amendment-only review is closed. The separate Hermes-core worktree is frozen research and
must not be imported, installed, committed, or used as a prerequisite.

## Development

Requires Python 3.11-3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev --extra proof
uv run --extra dev --extra proof python -m pytest
uv run --extra dev --extra proof python -m ruff check .
uv run --extra dev --extra proof python -m ruff format --check .
uv run --extra dev --extra proof python -m mypy
uv run --extra dev --extra proof python -m build
```

Read [IMPLEMENTATION.md](IMPLEMENTATION.md), then [CONTRIBUTING.md](CONTRIBUTING.md) and
[DESIGN.md](DESIGN.md), before changing code.

## Safety

Never commit endpoints, credentials, private bank names, principal identifiers, raw memories,
transcripts, databases, logs, or local runtime state. Proofs must use a temporary `HERMES_HOME`, a
fake server first, and a separately credentialed Hindsight development instance only after
deterministic tests pass. The live guard must fail closed on development endpoint/fingerprint or
pre-upsert bank-absence mismatch. Production canary activation remains separately authorized.

## Licensing and attribution

The project is MIT licensed. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream
attribution requirements and [SECURITY.md](SECURITY.md) for reporting security problems.
