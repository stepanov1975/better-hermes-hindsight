# Design and proof contract

## Status and authority

Better Hermes Hindsight `0.1.0a1` is a development prerelease candidate for exter...[truncated]
[IMPLEMENTATION.md](IMPLEMENTATION.md) router names the only active implementation plan. Two earlier
plans are retired historical records and cannot supply active requirements.

The repository remains private during proof work and becomes public only after the release checklist
passes. This design describes the implementable best-effort plugin, not an ideal host API.

## Primary goal

Provide useful current-query recall and opt-in automatic retention on unmodified released Hermes,
with bounded local work, retryable Hindsight delivery, isolated rollout, and documented rollback. The
project should reduce recurring integration maintenance without recreating every cloud, embedded,
installer, or control-plane feature of the bundled provider.

Recall is enabled by default. Automatic retention is disabled by default. No model-facing memory
tools in the first prerelease are exposed.

## Product boundary

The project owns:

- pure profile-scoped configuration and exact principal authorization;
- the public `hindsight-client==0.8.5` adapter for Hindsight server 0.8.5;
- bounded current-query recall and compact untrusted-evidence formatting;
- deterministic redaction, segmentation, and stable retain document identity;
- atomic profile-local SQLite admission with logical row/payload caps;
- destination-matched replace-mode retry and one POSIX sender owner;
- passive queue status plus explicit mission check/apply commands; and
- compatibility, fake-service, isolated-development, and rollback proofs.

Released Hermes owns whether and when a provider callback is invoked. Hindsight owns remote commit
and derived indexing. Better Hindsight narrows its claims at those boundaries rather than patching
private host code or promising remote transport outcomes it cannot observe.

Stable identities are:

- repository/distribution: `better-hermes-hindsight`;
- Python package: `better_hermes_hindsight`;
- Hermes provider ID: `better_hindsight`; and
- initial deployment: external/self-hosted only.

Bundled `hindsight` remains selectable and untouched.

## Released lifecycle

1. **Local discovery.** Imports, `is_available()`, configuration loading, and initialization perform
   no network call, package installation, bank mutation, or service restart.
2. **Current-query recall.** `prefetch()` recalls against the current projected user query before a
   model call. It is the only remote or potentially long-running memory operation on that path and
   fails open within a configured deadline. `queue_prefetch()` remains inert for this provider.
3. **Best-effort callback.** Released Hermes labels a turn complete and submits provider
   `sync_turn()` to its serialized background memory executor. Better accepts non-empty
   user/final-assistant text from that callback on an authorized primary handle. It does not infer
   human versus synthetic origin.
4. **Short local admission.** After the callback starts, Better redacts, segments, derives stable
   IDs, and attempts one bounded atomic SQLite transaction. The callback performs no Hindsight
   request and does not wait for remote delivery.
5. **Background delivery.** A profile-wide POSIX advisory lock elects one sender. The lock owner uses
   bounded SQLite polling to observe cross-process admissions, claims only rows matching the current
   credential-free destination fingerprint and payload schema, and retries with bounded backoff.
6. **Replace-safe replay and reconstruction.** Every retry reuses the same stable document ID and
   `update_mode="replace"` with synchronous Hindsight response validation. Each item forwards the
   outbox payload schema, source digest, segment index, and segment count as public string metadata,
   so shuffled remote segments can reconstruct the source after local completion. Ambiguous
   completion remains retryable; this is idempotent source replacement, not exactly-once transport.
7. **Bounded shutdown.** Shutdown stops new local work, joins the sender only for a bounded interval,
   closes owned resources where possible, and leaves unconfirmed admitted rows recoverable.

## Durability and failure contract

Local durability starts only after provider admission commits all segments of the redacted callback
to SQLite. Work can still be absent because Hermes never invoked the callback, abandoned queued work
during shutdown, exited before callback execution, or because admission rejected the turn. There is
no direct-user provenance claim and no pre-return or no-loss guarantee.

Admission can reject invalid or empty content, redaction failure, path or destination mismatch,
queue saturation, lock contention, local I/O failure, collision, or shutdown. It emits only fixed
sanitized status and does not change the generated assistant response.

Queue limits account for logical pending rows and retained payload bytes plus documented allowance;
SQLite pages, indexes, and WAL files can consume more physical disk. Retry does not promise global
FIFO or exactly-once transport. Repeated byte-identical content in one session may coalesce under the
stable source identity, which is accepted for the first prerelease.

## Trust and authorization

Recalled material is potentially stale, untrusted historical evidence, never executable instruction.
High-confidence credential patterns are redacted before model formatting and before retention
admission. Redaction is intentionally bounded and does not claim universal secret detection.

The first prerelease has one static Hindsight bank for one explicitly asserted principal. Gateway
identity requires an exact configured `(platform, identifier_kind, identifier)` tuple;
`single_principal=true` is required, and only `agent_context="primary"` may retain. A secondary
context may recall when its exact identity is authorized.

Hindsight OSS 0.8.5 uses a shared write-capable API key. Environment-only key loading, absent model
memory tools, and confirmation-gated operator commands reduce accidents but are not a server-enforced
read/write boundary against a process with terminal access and the key.

## Missions

Retain and observation mission text are distinct optional configuration fields. Provider
initialization neither reads nor applies remote mission policy. `better_hindsight missions check`
performs one typed bank-config read through a client-only runtime. It compares configured fields
byte-for-byte and reports only `equal`, `drift`, `missing`, or `error`; it never emits mission text.
`better_hindsight missions apply --confirm` performs one pre-read, at most one changed-field-only
PATCH, and one exact readback. Untouched allowlisted fields must remain byte-for-byte identical.
There is no automatic retry or rollback after PATCH dispatch: an unprovable post-dispatch outcome is
reported as `write_attempted_outcome_unknown` with exit status 4.

## Local operator diagnostics

`better_hindsight status` opens only an existing schema-v1 outbox in SQLite read-only/query-only
mode and reports one exclusive queue partition, logical queued bytes, oldest-age bucket, latest fixed
error category, and a point-in-time nonblocking sender-lock observation. It does not initialize,
recover, claim, complete, reschedule, or drain rows. A missing outbox is a successful
`uninitialized` result and creates nothing. Import and help paths do not inspect configuration,
SQLite, locks, clients, or sender state.

## Supported and unsupported runtime paths

The supported host path is the normal conversation loop in the current stable Hermes release selected
by the rolling compatibility matrix. Hermes 0.19.0 remains historical characterization, not a runtime
prerequisite. `codex_app_server` is unsupported because it bypasses normal provider
context and does not expose this provider's lifecycle behavior. Windows sender election, cloud
Hindsight, embedded-daemon management, and multi-user routing are also outside the first prerelease.

## Installation ownership

Released Hermes owns the host-managed Git plugin directory through
`hermes plugins install|update|remove`. The repository root supplies only `plugin.yaml` and thin
provider/CLI bridges to the installed wheel. Better does not implement a transaction tree, custom
installer, bytecode ownership scheme, or filesystem rollback engine. `uv` owns the Better wheel and
incompatible SDK transition while every Hermes process sharing the interpreter is stopped. A profile
scopes configuration and local data, not interpreter packages.

## Isolation, canary, and rollback

Development writes require a dedicated Hermes interpreter/profile and isolated Hindsight instance,
separate datastore, separate API key, and generated disposable bank. Fake HTTP proof always precedes
explicitly enabled live proof. The active Hermes installation, existing Hindsight deployment, and
existing bank remain outside development tests.

Production rollout uses a dedicated Hermes interpreter/profile and separate canary instance/bank,
preserving the old deployment. The old Hermes installation, provider configuration, instance, and
bank remain intact as the rollback source. Prerelease proof does not migrate, copy, rebuild,
deduplicate, reconsolidate, prune, or delete existing data.

Rollback preserves the Better outbox plus both banks, but it is not configuration-only in the supported
Hermes environment. The operator stops every process sharing the interpreter, selects bundled `hindsight`
for the target named profile, removes that profile's host-owned Git plugin, uses `uv` to remove the
Better wheel and restore exact `hindsight-client==0.6.1`, then restarts only compatible profiles and
verifies recall without lazy installation. Returning to Better reverses the exact package/profile
transition and verifies discovery before restart.

## Proof acceptance gates

Before prerelease, prove all of the following against one stable candidate:

- temporary-profile discovery and selection of only `better_hindsight`;
- no Hermes core patch or patched SHA requirement;
- bounded fail-open current-query recall before the first model request;
- untrusted, compact, redacted context and no model-facing memory tools;
- retention disabled by default and enabled only on an authorized primary handle;
- released `sync_turn()` best-effort callback behavior without origin inference;
- all-or-none local admission after callback execution, with no network request in the callback;
- profile path confinement, logical queue bounds, collision rejection, and fixed sanitized errors;
- one sender across process-shaped contenders and bounded cross-process polling;
- destination mismatch blocking and stable replace-mode retries until exact typed success;
- multi-segment reconstruction from remote item metadata after local completion;
- released Hermes host-managed Git plugin installation and fresh-process provider/CLI discovery;
- fake-service restart, timeout, response-loss, and shutdown recovery;
- explicit mission check/apply behavior without initialization-time mutation;
- isolated development proof with zero production credentials or resources;
- separate production canary and version-aware provider/package rollback while the old deployment
  stays untouched;
- Python 3.11-3.13 tests, lint, formatting, typing, lock, build, package, security, and independent
  review gates; and
- no private endpoint, credential, bank name, principal identifier, memory, transcript, database, or
  log in public artifacts.

## Deferred ideal and upstream improvements

These may be useful platform improvements, but they are not prerelease defects or prerequisites:

- authoritative typed direct-human versus synthetic origin across every Hermes ingress;
- inline admission before turn return or replay of callbacks the host never delivered;
- stale/untrusted framing owned generically by Hermes core;
- exactly-once transport or global FIFO;
- operation-scoped Hindsight credentials or a proxy/control plane;
- `codex_app_server` memory support;
- model-facing recall, retain, or mission tools;
- Windows/non-POSIX sender election;
- multi-user/per-user bank routing;
- cloud or embedded Hindsight supervision;
- automatic reflection or consolidation scheduling; and
- automatic legacy-bank migration, rebuilding, pruning, deduplication, or reconsolidation.

Revisit a deferred item only after observed use shows that its absence materially harms the primary
product goal.

## Publication policy

A manual, tag-bound prerelease workflow is present for candidate build and optional publication. It
requires an exact prerelease tag/commit match; PyPI publication additionally requires an explicit
boolean input, an operator-protected `pypi` environment, trusted-publishing OIDC, and a repository
configuration variable that remains absent until those protections are verified. Before publication,
complete [docs/public-release-checklist.md](docs/public-release-checklist.md), verify the supported
version matrix and artifacts in fresh environments, and cut an explicit prerelease rather than
publishing from every main-branch commit.
