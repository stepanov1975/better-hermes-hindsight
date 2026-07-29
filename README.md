# Better Hermes Hindsight

[![CI](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml)
[![Security scans](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml)

Better Hermes Hindsight is an unofficial Hermes memory provider for external/self-hosted Hindsight.
The provider ID is `better_hindsight`, deliberately distinct from bundled `hindsight` so rollback is
a configuration change rather than a data migration.

> **Status: pre-alpha.** Sender delivery is implemented for opt-in automatic retention in the tested
> repository checkpoint, while retention remains disabled by default. Managed installation and
> isolated live-write proof remain incomplete. Do not install or select this project in a production
> Hermes profile. Read
> [IMPLEMENTATION.md](IMPLEMENTATION.md) before changing code; it identifies the only active plan.

## What it is for

The primary goal is useful memory on unmodified released Hermes:

- bounded recall against the current user query;
- opt-in best-effort retention of the completed-turn callbacks released Hermes actually supplies;
- short local admission followed by retryable background delivery to self-hosted Hindsight;
- passive bounded queue diagnostics plus explicit confirmation-gated mission management; and
- easy rollback to the bundled provider while old data stays untouched.

Recall is enabled by default. Automatic retention is disabled by default until an operator proves
writes against an isolated development deployment and explicitly enables it for a canary.

“Better” is narrower: it means better for this documented external/self-hosted use case and its proof
criteria. It is not universally better than official Hermes or Hindsight.

## Honest best-effort boundary

The product has **no Hermes-core prerequisite**. It uses the public lifecycle in released Hermes
`v2026.7.20` / package 0.19.0. Automatic retention uses released `sync_turn()` best-effort semantics:
Hermes schedules the callback on its memory worker, and Better Hindsight does short local work only
after that callback starts.

Local durability starts only after provider admission commits the complete redacted turn to Better
Hindsight's SQLite outbox. Callbacks lost before Hermes executes the provider hook are outside that
guarantee. Local admission can also fail because of shutdown, contention, invalid input, queue
saturation, or local I/O. There is no direct-user provenance claim and no pre-return or no-loss
guarantee. Retried remote delivery uses a stable document ID and `update_mode="replace"`; it does
not claim exactly-once transport.

`codex_app_server` is unsupported on the pinned release because that runtime bypasses normal provider
memory behavior. No model-facing memory tools in the first prerelease are registered; recall is
automatic context and mission changes require an explicit operator command.

## Initial scope

- External/self-hosted only; no cloud or embedded-daemon management.
- Exact initial target: `hindsight-client==0.8.5` with Hindsight server 0.8.5.
- Current-query recall is the only remote or potentially long-running memory work before the first
  model call. It has a bounded fail-open deadline.
- Retention accepts non-empty user/final-assistant text from the released callback as-is. It does not
  infer authoritative human-versus-synthetic origin from text, platform names, or transcript shape.
- The callback path performs bounded redaction, segmentation, and one atomic SQLite admission. It
  performs no Hindsight request and does not wait for remote drain.
- One profile-wide POSIX advisory lock elects the sender. Bounded SQLite polling lets that owner see
  rows admitted by another process.
- Pending rows are matched to a credential-free destination fingerprint and replay the same stable
  document ID with replace mode until synchronous response validation succeeds.
- Logical pending-row and payload-byte limits bound admitted work; they are not an exact SQLite/WAL
  file-size guarantee.
- Source documents are the preserved record. Facts, observations, embeddings, and summaries are
  derived indexes.
- Retain and observation mission text remain distinct. `better_hindsight missions check` is
  read-only; `better_hindsight missions apply --confirm` changes only configured drifted fields and
  verifies exact readback. Neither command is automatic initialization policy.

## Operator commands

Released Hermes discovers the underscore-only command from the active memory-provider shim:

```text
hermes better_hindsight status
hermes better_hindsight missions check
hermes better_hindsight missions apply --confirm
```

`status` passively inspects an existing schema-v1 SQLite outbox and probes the existing sender lock;
an absent outbox is reported as `uninitialized` without creating files. Mission commands require
`single_principal=true` and own a short-lived client-only runtime that never opens the retention
outbox or starts its sender. Handler-controlled output is canonical JSON bounded to 1,024 UTF-8
bytes. Exit statuses are `0` for success/equality, `1` for drift or missing mission state, `2` for
host-owned usage errors, `3` for fixed pre-write/local failures, and `4` once a remote write was
attempted but the final outcome cannot be proven. There is no retry or drain command.

## Isolation and rollback

Development writes require an isolated Hindsight instance and Hermes profile, with separate storage,
API key, and disposable bank. Deterministic fake-service tests run first and no production credential
belongs in the test process.

Production rollout uses a separate canary instance and bank and preserves the old deployment. The
existing Hindsight instance and bank remain running, unmodified, and available for rollback; this
prerelease performs no initial migration, deduplication, reconstruction, or deletion. Canary
activation, publication, and any production mutation remain separately authorized.

## Repository and implementation authority

The tracked [implementation router](IMPLEMENTATION.md) identifies the canonical local plan, its hash,
the completed sender-delivery checkpoint, the active diagnostics/mission-command stage, and two
explicitly retired plans. Never infer implementation requirements from a retired plan or from stale
proof wording. The separate Hermes-core worktree is frozen research and must not be imported,
installed, committed, or treated as a prerequisite.

The exact version/source observations are in [docs/compatibility.md](docs/compatibility.md). Sender
recovery, retry, and shutdown semantics are in [docs/operations.md](docs/operations.md). Sanitized
operational aggregates and their limited interpretation are in
[docs/audit-findings.md](docs/audit-findings.md).

## Development

Requires Python 3.11-3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev --extra proof
uv run --frozen --extra dev --extra proof python -m pytest
uv run --frozen --extra dev --extra proof python -m ruff check .
uv run --frozen --extra dev --extra proof python -m ruff format --check .
uv run --frozen --extra dev --extra proof python -m mypy
uv run --frozen --extra dev --extra proof python -m build
```

Read [IMPLEMENTATION.md](IMPLEMENTATION.md), then [CONTRIBUTING.md](CONTRIBUTING.md) and
[DESIGN.md](DESIGN.md), before changing code.

## Safety

Never commit endpoints, credentials, private bank names, principal identifiers, raw memories,
transcripts, databases, logs, or local runtime state. Use a temporary `HERMES_HOME`, synthetic
fixtures, and a fake service before any explicitly enabled isolated live proof.

## Licensing and attribution

The project is MIT licensed. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream
attribution requirements and [SECURITY.md](SECURITY.md) for reporting security problems.
