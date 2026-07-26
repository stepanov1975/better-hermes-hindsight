# Better Hermes Hindsight

[![CI](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml)
[![Security scans](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml)

An unofficial, external/self-hosted-only Hindsight memory provider project for Hermes Agent. The
provider ID is `better_hindsight`, deliberately distinct from bundled `hindsight` so rollback is a
configuration change.

> **Status: pre-alpha scaffold.** No usable Hermes memory provider is registered yet.
> Do not install or select this project in a production Hermes profile.

## Goal

Build a smaller, testable provider that recalls against the current user query, admits each eligible
completed turn durably to local storage before turn completion, and replays admitted work safely to
Hindsight. Development happens in this repository; proof runtime state and test banks remain
disposable.

“Better” is narrower: it means better for the documented external/self-hosted use case and proof
criteria. It is not universally better than official Hermes or Hindsight.

## Initial scope

- External/self-hosted only; no cloud or embedded-daemon management.
- Exact initial target: `hindsight-client==0.8.5` with Hindsight server 0.8.5.
- Current-query recall is the only remote or potentially long-running memory work before the first
  model call, with a bounded fail-open deadline.
- After generation, inline local SQLite admission commits the redacted immutable turn before Hermes
  reports the turn complete; remote retention stays off the response path.
- One profile-wide POSIX advisory lock elects the remote sender across processes.
- Pending rows are destination fingerprint matched and replay the stable document ID with
  `update_mode="replace"` until synchronous confirmation.
- Source documents are the preserved record; facts, observations, embeddings, and summaries are
  derived indexes.
- Correct shared observation scope, separate retain/reflect/observation missions, calibrated score
  floors, and observation preference.
- A distinct `better_hindsight` identity for safe rollback to bundled `hindsight` without bank
  migration or deletion.

There is no production auto-install, provider selection, service restart, or production bank
mutation. Cloud setup, production-bank migration, automatic reflection, and same-ID rewind recovery
are deliberately outside the first proof.

## Implementation boundary

The order is recall-first, then a separate generic Hermes core prerequisite, then durable retain.
The core prerequisite is required because released Hermes documents `sync_turn()` as non-blocking;
it must add backward-compatible opt-ins for structured origin, historical-memory trust framing, and
inline local admission without changing legacy providers.

The lifecycle, exact version/source baseline, public API boundary, caller inventory, preservation
checklist, and current go/no-go decision are frozen in
[docs/compatibility.md](docs/compatibility.md). Sanitized operational aggregates and their limited
interpretation are in [docs/audit-findings.md](docs/audit-findings.md).

## Repository state

`main` contains the public-safe project contract, package scaffold, documentation, and verification
automation. Provider implementation belongs on `spike/local-external-provider` until the proof gates
in [DESIGN.md](DESIGN.md) pass.

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

See [CONTRIBUTING.md](CONTRIBUTING.md) and [DESIGN.md](DESIGN.md) before changing code.

## Safety

Never commit endpoints, credentials, private bank names, principal identifiers, raw memories,
transcripts, databases, logs, or local runtime state. Proofs must use a temporary `HERMES_HOME`, a
fake server first, and a clearly disposable test bank only after deterministic tests pass. Snapshot,
logical export, hash/count reconciliation, disposable restore proof, and rollback must precede any
separately authorized production write.

## Licensing and attribution

The project is MIT licensed. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream
attribution requirements and [SECURITY.md](SECURITY.md) for reporting security problems.
