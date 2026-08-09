# Better Hermes Hindsight

[![CI](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml)
[![Security scans](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml)

Better Hermes Hindsight is an unofficial Hermes memory provider for external/self-hosted Hindsight.
The provider ID is `better_hindsight`, deliberately distinct from bundled `hindsight` so rollback
does not require a data migration. On the current supported Hermes release it is not configuration-only: Better
requires `hindsight-client==0.8.5`, while bundled `hindsight` requires exact `0.6.1`, so switching
providers also requires the documented stopped-process package-version transition.

> **Status: pre-alpha.** Tasks 0–6 are complete at checkpoint `3f542d4`. The Task 6 proof ran once against a dedicated
> Hermes 0.19.0 interpreter and isolated Hindsight 0.8.5 instance using only synthetic data; its
> generated bank was removed and an authenticated post-run listing found zero banks. Its independent
> findings were closed. The rolling compatibility/release rebaseline is complete at checkpoint
> `2a05a10`; Task 7 and publication remain separately authorized work. The current-Hermes
> `cryptography` findings remain visible in the supported-host audit as upstream observations, not
> plugin release blockers. The live
> node is not an operator next action and must not be rerun without a changed candidate plus renewed
> explicit authorization.
> The plugin
> preserves multi-segment reconstruction metadata and installs through Hermes's released Git-plugin
> lifecycle; no custom installer or package manager ships. Do not install or select this project in a
> production Hermes profile. Read [IMPLEMENTATION.md](IMPLEMENTATION.md) before changing code; it
> identifies the only active plan.

## What it is for

The primary goal is useful memory on unmodified released Hermes:

- bounded recall against the current user query;
- opt-in best-effort retention of the completed-turn callbacks released Hermes actually supplies;
- short local admission followed by retryable background delivery to self-hosted Hindsight;
- passive bounded queue diagnostics plus explicit confirmation-gated mission management; and
- documented rollback to the bundled provider while Better's outbox and both banks stay untouched.

Recall is enabled by default. Automatic retention is disabled by default until an operator proves
writes against an isolated development deployment and explicitly enables it for a canary.

“Better” is narrower: it means better for this documented external/self-hosted use case and its proof
criteria. It is not universally better than official Hermes or Hindsight.

## Honest best-effort boundary

The product has **no Hermes-core prerequisite**. A rolling compatibility matrix exercises the public
lifecycle in the current stable Hermes release while retaining 0.19.0 as historical characterization.
Hermes is the host, not a package dependency or runtime prerequisite. Automatic retention uses
released `sync_turn()` best-effort semantics:
Hermes schedules the callback on its memory worker, and Better Hindsight does short local work only
after that callback starts.

Local durability starts only after provider admission commits the complete redacted turn to Better
Hindsight's SQLite outbox. Callbacks lost before Hermes executes the provider hook are outside that
guarantee. Local admission can also fail because of shutdown, contention, invalid input, queue
saturation, or local I/O. There is no direct-user provenance claim and no pre-return or no-loss
guarantee. Retried remote delivery uses a stable document ID and `update_mode="replace"`; it does
not claim exactly-once transport. Every remote segment also carries string metadata for payload
schema, source digest, segment index, and segment count so a long source remains reconstructable
after its completed local outbox rows are deleted.

`codex_app_server` is unsupported because that runtime bypasses normal provider
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
- Source documents are the preserved record. Multi-segment reconstruction metadata is forwarded
  through Hindsight's public item metadata; facts, observations, embeddings, and summaries remain
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

## Installation model

Hermes-managed plugin installation uses the released host lifecycle: the repository root contains a
manifest and thin provider/CLI bridges consumed by `hermes plugins install|update|remove`. No custom
installer, transaction tree, tombstone, quarantine, or package rollback engine is part of this
project. `uv` owns the Python wheel and exact Hindsight SDK while every Hermes process sharing the
interpreter is stopped, because the supported Hermes environment cannot run Better's
`hindsight-client==0.8.5` and the bundled provider's exact `0.6.1` in one interpreter at the same
time. A profile scopes config and data, not packages.

See [installation](docs/installation.md) and [rollback](docs/rollback.md) for the bounded workflow.
Task 5 tests use only disposable Git repositories and temporary Hermes homes; they do not change a
live profile, interpreter, service, outbox, or Hindsight bank.

## Isolation and rollback

Development writes require a dedicated Hermes interpreter/profile plus an isolated Hindsight
instance, separate storage, API key, and disposable bank. Deterministic fake-service tests run first
and no production credential belongs in the test process.

Production rollout uses a dedicated Hermes interpreter/profile plus a separate canary instance and
bank, preserving the old deployment. The existing Hermes installation, Hindsight instance, and bank
remain running, unmodified, and available for rollback; this prerelease performs no initial
migration, deduplication, reconstruction, or deletion. Canary activation, publication, and any
production mutation remain separately authorized.

## Repository and implementation authority

The tracked [implementation router](IMPLEMENTATION.md) identifies the canonical local plan, its hash,
the completed Tasks 0–4 checkpoint, the abandoned overgrown installer oracle, and the active
product-aligned Task 5 scope. Never infer implementation requirements from a retired plan, stale
proof wording, or cached review transcript. The separate Hermes-core worktree is frozen research and
must not be imported, installed, committed, or treated as a prerequisite.

The rolling compatibility policy and historical version/source observations are in
[docs/compatibility.md](docs/compatibility.md). Sender
recovery, retry, and shutdown semantics are in [docs/operations.md](docs/operations.md). Sanitized
operational aggregates and their limited interpretation are in
[docs/audit-findings.md](docs/audit-findings.md).

## Development

Requires Python 3.11-3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run --frozen --extra dev python -m ruff check .
uv run --frozen --extra dev python -m ruff format --check .
uv run --frozen --extra dev python -m mypy
uv run --frozen --extra dev python -m build
```

All tests run in CI's selected Hermes compatibility environments rather than via a published `proof`
extra; the repository root is itself the Hermes plugin bridge and therefore imports the host API.

Read [IMPLEMENTATION.md](IMPLEMENTATION.md), then [CONTRIBUTING.md](CONTRIBUTING.md) and
[DESIGN.md](DESIGN.md), before changing code.

## Safety

Never commit endpoints, credentials, private bank names, principal identifiers, raw memories,
transcripts, databases, logs, or local runtime state. Use a temporary `HERMES_HOME`, synthetic
fixtures, and a fake service before any explicitly enabled isolated live proof.

## Licensing and attribution

The project is MIT licensed. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream
attribution requirements and [SECURITY.md](SECURITY.md) for reporting security problems.
