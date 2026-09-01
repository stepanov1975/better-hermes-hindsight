# Design

## Purpose

Better Hermes Hindsight is an unofficial memory provider optimized for one practical deployment: a Linux Hermes installation using a supported external Hindsight service. It aims to improve recall timing and retention reliability without modifying Hermes core.

“Better” is contextual, not universal. Bundled Hindsight remains preferable when embedded/cloud
operation, broader caller-controlled reflection, dynamic routing, or minimum maintenance is more
important.

## Goals

- Recall relevant memory for the current query before the model call.
- Bound recall time, input projection, output size, and exposed record fields.
- Treat recalled content as potentially stale, untrusted historical evidence.
- Keep automatic retention optional and off the remote network path of the Hermes callback.
- Persist admitted retained turns locally and retry them after restart.
- Keep model authority narrow: bounded recall, default-off read-only reflection, opt-in durable
  retention admission, and compact passive queue status only.
- Keep destination and mission changes explicit and operator controlled.
- Preserve straightforward rollback to bundled Hindsight.

## Non-goals

- Embedded or cloud Hindsight lifecycle management.
- Multi-user routing or dynamic bank templates.
- Multiplexed multi-profile Better runtimes in one Hermes process.
- Remote diagnostic replay, policy changes, or caller-selected banks/tags/reflection controls.
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
5. The formatter projects allowlisted fields, redacts likely credentials, removes later exact
   duplicates using normalized model-facing text plus identical occurrence metadata, frames records
   as untrusted evidence, and enforces the output-byte limit.
6. Errors and timeouts return no external context rather than failing Hermes.

The model-facing `better_hindsight_recall` tool reuses this configured path and returns structured
records without internal ranking or source-count telemetry. It cannot select a different bank,
destination, principal, or retention policy.

### Reflection

1. The model explicitly calls `better_hindsight_reflect`; reflection is never automatic and remains
   disabled unless the operator opts in locally.
2. The provider verifies the same exact principal policy and projects the query under independent
   character and token limits.
3. One deadline-bounded request uses the configured bank, budget, final-answer token target, tags, and
   tag mode. The caller cannot select an endpoint, bank, mission, policy, trace, response schema, or
   source payload.
4. The client enforces a raw response-byte cap and accepts only one bounded non-empty text field.
5. The formatter redacts the synthesis, keeps marker text inside one serialized JSONL record,
   truncates only its text field, and returns that complete record inside the normal untrusted
   historical-evidence envelope, while the serialized outer tool response has its own byte cap.
6. Timeouts and malformed, empty, oversized, or exceptional responses become one sanitized unavailable
   result; the provider does not retry agentic reflection automatically.

Reflection is read-only with respect to Hindsight bank memory, but it invokes Hindsight's configured
LLM and may create service-side audit/usage records. Better's local timeout and output limits do not
bound all server-side model work or cost; deployments must configure the corresponding Hindsight
iteration, context, wall-time, and completion-token limits.

### Retention

1. Hermes invokes `sync_turn()` after a completed turn.
2. The provider verifies retention, context, and principal policy.
3. It captures one local event ID and fixed-width UTC occurrence time for the automatic admission,
   redacts the turn, and builds either one complete turn record or role/paragraph-bounded records that
   are each independently decodable. A semantic unit that cannot fit the configured exact UTF-8 byte
   limit rejects the whole admission instead of being split arbitrarily.
4. One SQLite transaction admits every segment or none, subject to configured limits. Configuration
   proves the complete smallest segmented event fits the aggregate byte capacity. The event time is
   persisted inside each record, so sender retries and process restarts retain the original occurrence.
5. A background sender claims due rows for the current destination fingerprint or the exact compatible
   legacy v1 schema/fingerprint pair.
6. Each new row uses the distinct `better-hindsight-turn-v2` payload/fingerprint identity, a stable
   document ID, synchronous Hindsight retention, and `update_mode="replace"`; a pre-v2 sender cannot
   claim timestamp-bearing v2 rows. The upgraded sender still delivers legacy pending v1 fragments with
   a null timestamp.
7. Confirmed rows are removed; unconfirmed rows are rescheduled with bounded backoff.

The model-facing `better_hindsight_retain` tool accepts one self-contained durable memory plus an
optional short context label. It marks the source as agent-selected rather than a direct user quote,
applies the same redaction and semantic segmentation path, repeats the context on every split content
record, and returns one canonical local admission outcome. A stable content-derived model-memory
identity makes an exact reconstructed call idempotent while it remains queued; automatic callback
occurrences remain random and distinct. The tool cannot override the configured bank, tags, scopes,
limits, or retry policy. Acceptance means the outbox transaction committed; remote delivery remains
asynchronous.

The model-facing `better_hindsight_status` tool projects the operator outbox snapshot into compact
healthy state or conditionally detailed degraded state. It performs no remote call; deployment and
diagnostic detail remain operator-only.

Durability begins only after admission commits. A network timeout may be ambiguous and cause a safe replace-mode replay; this is not exactly-once transport.

## Safety boundary

- API credentials come from the environment and are not part of destination fingerprints or persisted payload metadata.
- Reflection is explicit, default-off, fixed to the configured destination/policy, and returned only as
  untrusted generated evidence; its source records, traces, directives, and usage metadata are not
  exposed to the model.
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
implements its narrow Hindsight 0.8.5/0.9.1/0.9.2 wire contract over `aiohttp`, uses `tiktoken` for
bounded recall and reflection query projection, and does not import the Hindsight Python SDK, so the
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
