# Design

## Purpose

Better Hermes Hindsight is an unofficial memory provider optimized for one practical deployment: a Linux Hermes installation using a supported external Hindsight service. It aims to improve recall timing and retention reliability without modifying Hermes core.

“Better” is contextual, not universal. Bundled Hindsight remains preferable when embedded/cloud operation, model-facing retain/reflect, or minimum maintenance is more important.

## Goals

- Recall relevant memory for the current query before the model call.
- Bound recall time, input projection, output size, and exposed record fields.
- Treat recalled content as potentially stale, untrusted historical evidence.
- Keep automatic retention optional and off the remote network path of the Hermes callback.
- Persist admitted retained turns locally and retry them after restart.
- Keep model authority narrow: one read-only recall tool and no model-directed writes.
- Keep destination and mission changes explicit and operator controlled.
- Preserve straightforward rollback to bundled Hindsight.

## Non-goals

- Embedded or cloud Hindsight lifecycle management.
- Multi-user routing or dynamic bank templates.
- Multiplexed multi-profile Better runtimes in one Hermes process.
- Model-facing retain or reflect tools.
- Previous-query background recall.
- Bank migration, pruning, deduplication, reconsolidation, or remote rewind.
- Windows support, hot reload, or general process supervision.
- Exactly-once delivery.
- Recovering Hermes callbacks that the host never invokes.
- Inferring turn provenance from message text.

## Data flow

### Recall

1. Hermes supplies the current user query and agent context.
2. The provider verifies enabled context and principal policy.
3. It projects and bounds the query by characters and by Hindsight's exact `cl100k_base` input-token
   rule, preserving bounded head-and-tail context.
4. The async client performs one deadline-bounded Hindsight recall.
5. The formatter projects allowlisted fields, redacts likely credentials, frames records as untrusted evidence, and enforces the output-byte limit.
6. Errors and timeouts return no external context rather than failing Hermes.

The model-facing `better_hindsight_recall` tool reuses this configured path. It cannot select a different bank, destination, principal, or retention policy.

### Retention

1. Hermes invokes `sync_turn()` after a completed turn.
2. The provider verifies retention, context, and principal policy.
3. It redacts and deterministically segments the source.
4. One SQLite transaction admits every segment or none, subject to configured limits.
5. A background sender claims due rows for the current destination fingerprint.
6. Each row is sent with a stable document ID, synchronous Hindsight retention, and `update_mode="replace"`.
7. Confirmed rows are removed; unconfirmed rows are rescheduled with bounded backoff.

Durability begins only after admission commits. A network timeout may be ambiguous and cause a safe replace-mode replay; this is not exactly-once transport.

## Safety boundary

- API credentials come from the environment and are not part of destination fingerprints or persisted payload metadata.
- Outbox rows bind to a credential-free fingerprint of endpoint, bank, schema, tags, and observation scopes.
- Rows for another destination remain blocked until an operator deliberately restores the old configuration or performs a separately reviewed recovery.
- Status is passive: it uses SQLite read-only URI opens, performs no application-owned schema
  creation, migration, queue recovery, or row writes, and must surface blocked work as degraded rather
  than healthy. SQLite may update existing SHM coordination state while reading active WAL content.
- Mission application requires `--confirm`, patches only allowlisted drifted fields, and verifies readback.
- Live tests use synthetic content and an isolated Hindsight environment, never production data.

## Deployment model

Better is a self-contained standard Hermes Git plugin. Each Better-enabled Hermes profile may run in
its own process with profile-local configuration, outbox, and diagnostics. One process owns one exact
Better configuration and runtime; another profile in that process fails open rather than crossing the
profile boundary. Its root entry points and
`better_hermes_hindsight` implementation package are installed together by `hermes plugins
install`; no second package installation or runtime environment is part of deployment. Better
implements its narrow Hindsight 0.8.5/0.9.1 wire contract over `aiohttp`, uses `tiktoken` only to
match those servers' recall input validation, and does not import the Hindsight Python SDK, so the
untouched bundled provider remains available.

The Git commit is the working identity. A tag or version bump is optional and does not define compatibility. Validation records the current Better and Hermes commits and tests behavior against that checkout.

## Accepted limitations

The intended deployment is personal Linux/POSIX with one trusted local operator/principal, one bank,
one Better-enabled profile per process, one supported external Hindsight service, a stable
Hermes-home/outbox pathname topology, and the normal Hermes memory-provider lifecycle. Passive status
is an operational snapshot under that model, not a defense
against concurrent pathname replacement or an adversarial local writer. `codex_app_server`, typed
provenance, automatic migration/deletion, and cross-platform sender election are outside the initial
product. They do not block use in the intended environment.
