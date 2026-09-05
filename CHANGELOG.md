# Changelog

Notable user-visible changes are recorded here. The project follows rolling `main`; versions and tags are optional snapshots rather than compatibility or deployment gates.

## Unreleased

### Added

- Added a default-off context-aware automatic-recall planner that runs through Hermes's public
  `pre_llm_call` and `ctx.llm` plugin surfaces, chooses `skip`, `reuse`, or one self-contained recall
  query from bounded ordinary user/assistant history, and hands the decision to the memory provider
  through a profile-keyed, short-lived process-local reservation registry. Pending reservations
  fence out late hook workers before provider consumption, publication deadlines are enforced while
  finalizing under the registry lock, and finalized decisions are consume-once. Companion registration
  or provider initialization idempotently removes only schema-verified obsolete branch-preview SQLite
  planner mailboxes and sidecars while continuing to accept their former settings only for validated
  migration cleanup.
  Shadow mode records only safe action/latency metadata. Missing, stale, or mismatched handoff state
  preserves direct current-query recall. In active mode, a planner timeout,
  exception, or invalid result finalizes a bounded `skip` decision only while the reservation and its
  publication deadline remain valid; otherwise direct-query recall is preserved.

### Changed

- Replaced the provider-specific automatic-recall markers with the provider-neutral
  `RECALLED_MEMORY_EVIDENCE` envelope, while continuing to strip legacy envelopes from recall
  queries during migration.
- Removed Hindsight's `type` label from automatic recall records to reduce repeated model-facing
  metadata. Explicit `better_hindsight_recall` results continue to include the type when available.

## 0.5.0 - 2026-09-01

### Added

- Added a weekly and manually dispatchable isolated live CI proof against the digest-pinned Hindsight
  0.9.2 release image and current Hermes `main`, using an ephemeral datastore and deterministic mock LLM
  while recording exact Better, Hermes, and Hindsight identities and exercising mission round-trip and
  real-service error mapping alongside retain, restart recovery, recall, and cleanup.
- Added default-off `better_hindsight_reflect`, a fixed-bank, principal-authorized, read-only model
  tool with independent query, deadline, raw-response, decoded-text, and serialized-output bounds.
  It returns only redacted Hindsight synthesis inside the existing untrusted historical-evidence
  envelope and exposes no caller-selected destination, tags, policy, trace, or source payload.
- Added deterministic reflection contract coverage for the shared Hindsight 0.8.5/0.9.1/0.9.2
  endpoint and minimal request/response fields.
- Added explicit support for Hindsight API version 0.9.2 after review of Better's narrow HTTP
  contract and an isolated live retain, outbox restart recovery, recall, replay, and cleanup proof.
- Added contract coverage confirming Better continues to omit the optional 0.9.2 retain
  `resolve_entities` field.
- Added a strict privacy-preserving recall-quality evaluator with a synthetic regression fixture and a
  read-only comparison mode that changes only `prefer_observations`.
- Added a release-gated, public-safe bundled-vs-Better provider shadow benchmark with separate owned
  disposable banks, aligned synthetic corpus/missions/policies, aggregate quality and latency metrics,
  bounded host fail-open evidence, immutable source/model/build identities, and verified cleanup.
- Added an owner-only historical-query collector and private live-response capture so real-bank
  evaluations can be labeled and scored without committing or printing queries, recalled text, IDs,
  or labels.

### Fixed

- Enable SQLite `secure_delete` on every outbox writer connection so confirmed-row deletion
  overwrites payload cells in the live database, document the remaining storage and backup residue
  boundary, and correct the outbox journal-mode description from WAL to the actual rollback journal.
- Reduced model-facing output to actionable state: recall omits internal score and source-count
  metadata, explicit recall returns structured memories, retain returns one canonical local-admission
  outcome, and status no longer exposes operator-only diagnostic records or healthy-path internals.
- Report the active standard Git plugin commit in passive status by reading Hermes installation
  metadata, with a read-only local checkout fallback for legacy installs.
- Register the recall trust policy through Hermes's cache-safe plugin system-prompt section when
  available, while retaining the provider-block fallback for older or generic host contexts. This
  keeps automatic recall evidence governed when model-facing memory tools are disabled.
- Remove later exact duplicates from automatic and explicit model-facing recall after redaction,
  Unicode compatibility normalization, and whitespace collapsing while preserving first rank and
  distinct occurrence metadata.
- Make recall formatting linear in emitted JSONL bytes and apply one deadline across query
  projection, network/decode, redaction, and formatting. Bound recall response bytes, result and
  nested-collection counts, and per-record input before synchronous processing.
- Preserve retained-event occurrence time and repeated-event identity across delayed delivery and
  restart retries while continuing to deliver legacy pending v1 rows safely.
- Replace arbitrary UTF-8 byte slicing with independently decodable role/paragraph-bounded retained
  records, including common blank-line conventions; reject unusable enabled segment limits at
  configuration and reject admissions when a semantic unit still cannot fit.
- Bound automatic segment construction by the configured queue row limit, validate one-row envelope
  feasibility and aggregate byte capacity for the complete smallest segmented event, and reject
  surrogate tags through the sanitized configuration boundary.
- Bind timestamp-bearing retained records to a distinct v2 payload/fingerprint so pre-v2 senders cannot
  claim them while upgraded senders continue to drain exact legacy v1 rows.
- Preserve model-retain retry idempotency with a stable model-memory identity and repeat optional context
  on every split content record instead of retaining a standalone context-only document.

## 0.4.0 - 2026-08-22

### Added

- Added `better_hindsight_retain`, an agent-oriented write tool that accepts self-contained durable
  content plus an optional context label and reuses the authorized redacted SQLite outbox path. Its
  structured result reports local admission separately from asynchronous remote delivery.
- Added `better_hindsight_status`, a no-argument passive health tool combining outbox state with
  bounded query-free diagnostic summaries; it makes no remote call and cannot replay captured queries.

### Fixed

- Bounded model-directed retention before segment hashing with explicit content, context, and
  canonical-segment limits, preventing pathological small segment settings from turning one rejected
  tool call into unbounded construction work.

## 0.3.2 - 2026-08-21

### Added

- Automatic recall now uses Hermes's deterministic memory indicator to report the number of
  memories actually injected after Better Hindsight's output-byte bound is applied.

## 0.3.1 - 2026-08-21

### Added

- Added a prominent Quick Start and corrected contributor setup against the current rolling Hermes
  checkout.
- Added a balanced functionality comparison with Hermes's bundled Hindsight provider.
- Documented and regression-tested the profile boundary: separate profile processes are supported,
  while multiple Better-enabled profiles in one multiplexed gateway fail open.

### Changed

- Expanded the supported tokenizer range through `tiktoken` 0.13 and aligned the host-facing plugin
  manifest, package metadata, lockfile, installation guide, and contract tests.
- Expanded the development build range through `setuptools` 84.
- Made profile configuration, outbox, and diagnostics isolation explicit, including guidance to use
  distinct Hindsight banks when remote memory must remain separate.

### Fixed

- The development lock now actually resolves `tiktoken` 0.13 instead of widening only the project
  constraint.
- Standard Git-plugin installation now passes Hermes 0.20.5's default repository security scan;
  contributor rules and synthetic test fixtures no longer resemble scanner-reserved persistence or
  credential patterns, and a compatibility test guards the install-time verdict.

## 0.3.0 - 2026-08-16

### Added

- Added opt-in bounded local capture of exact projected queries and credential-free request parameters
  for slow or failed recalls, plus operator list/replay commands that use Hindsight's existing trace
  response to report privacy-safe phase timings and candidate-collection counts.
- Diagnostic replay results are saved back to the private record while ordinary logs and command output
  remain query-free. This is a plugin-only best-effort mechanism; timed-out server phases are available
  only when a later trace-enabled replay completes.

### Fixed

- Diagnostic listing now reports unreadable or corrupt stores as unavailable instead of empty, and
  replay rejects captured request data that no longer exactly matches the current typed recall policy
  before making a network request.

## 0.2.3 - 2026-08-14

### Fixed

- Recall query projection now enforces Hindsight's `cl100k_base` input-token ceiling in addition to
  the existing character ceiling, preventing long token-dense queries from being rejected with HTTP
  400 while preserving bounded head-and-tail context.

## 0.2.2 - 2026-08-14

### Added

- Added explicit support for exact Hindsight API version 0.9.1 after contract review and a live
  retain, restart recovery, recall, stable replay, and cleanup proof against the official image.
- Added regression coverage for the optional 0.9.1 `source_facts_truncated` recall field and the
  optional retain `operation_id` request field.

### Changed

- The canary accepts only the supported Hindsight API versions 0.8.5 and 0.9.1 and reports the
  observed version.
- The isolated live proof requires an explicit expected Hindsight version.

### Fixed

- The isolated live proof now copies the complete self-contained plugin payload into its temporary
  Hermes home and finalizes the runtime module actually loaded by Hermes.

## 0.2.1 - 2026-08-14

### Fixed

- Standard memory setup can now discover the provider before Hermes installs the manifest-declared
  `aiohttp` dependency. The HTTP dependency is imported only when the runtime client is created.
- Contributing commands use the self-contained top-level package layout rather than the removed
  `src/` tree.

### Changed

- Release metadata now identifies final releases as production/stable.
- Gateway guidance distinguishes installation from a separately authorized normal restart of an
  already-running process.

## 0.2.0 - 2026-08-14

### Changed

- Converted Better Hermes Hindsight into a self-contained standard Hermes Git plugin.
- Replaced the custom release installer with `hermes plugins install` and `hermes memory setup`.
- Bundled the implementation with the plugin entry points so no second package installation is
  required.
- Removed the dedicated installation launcher and all deployment-isolation requirements.
- Changed source layout from `src/better_hermes_hindsight` to the repository-root plugin package.

### Fixed

- Plugin loading no longer depends on an independently installed Better Python distribution.
- Source identity now comes from the self-contained plugin version rather than unrelated installed
  package metadata.

## 0.1.0a5 - 2026-08-14

### Fixed

- Release installation now tolerates an unchanged set of unrelated, pre-existing Hermes
  environment dependency incompatibilities while still rejecting any conflict introduced by
  Better.

## 0.1.0a4 - 2026-08-14

### Changed

- Replaced the `hindsight-client==0.8.5` runtime dependency with a narrow asynchronous HTTP adapter for the four Hindsight 0.8.5 operations Better uses.
- Better can share Hermes with bundled `hindsight-client==0.6.1` without importing that SDK.
- Added privacy-safe per-operation adapter outcomes, lifecycle diagnostics, precise sender/recall
  reasons, broader low-noise watchdog coverage, and an adapter-backed E2E canary.

### Fixed

- HTTP failures, malformed or oversized JSON, cancellation, and response-schema errors preserve Better's bounded fail-open and durable-retry behavior without exposing raw service errors.

## 0.1.0a3 - 2026-08-13

### Fixed

- Canary now accepts Hindsight 0.8.5 synthesized recall text while still requiring the exact source document ID and unique canary tag.

## 0.1.0a2 - 2026-08-13

### Added

- Bounded read-only `better_hindsight_recall` model tool that reuses the configured automatic-recall policy without exposing caller-selected banks or writes.
- Degraded/non-zero operator status when destination-mismatched outbox rows cannot be delivered safely.

### Changed

- Replaced exact Hermes commit/release assertions with behavioral testing against the intended current checkout.
- Reduced CI to the maintained Python runtime tested against current Hermes `main`.
- Development continues to follow rolling Git commits.
- Simplified development documentation, isolated live validation, and repository tests.
- Removed canonical-plan hashes, release-gate prose contracts, historical compatibility blockers, and version bumps as a per-commit requirement.

### Fixed

- Generic Hermes doctor registration no longer mistakes the exclusive memory-provider bridge
  for a general plugin failure.
- Public installation does not require private repository access.

## 0.1.0a1 - 2026-08-10

### Added

- Current-query, fail-open recall through Hermes's memory-provider lifecycle.
- Opt-in redacted turn retention with bounded SQLite admission, asynchronous delivery, stable replace-mode replay, and reconstructable segmented source metadata.
- Passive status diagnostics and confirmation-gated mission management.
- Thin root bridges for Hermes Git-plugin discovery and CLI registration.

### Known limitations

- External/self-hosted Hindsight 0.8.5 only.
- Linux/POSIX, one principal, one static bank, and normal Hermes loop only.
- Better and bundled Hindsight need incompatible client SDK versions in one interpreter.
- No model-facing writes/reflection, migration framework, automatic deletion, Windows sender election, or exactly-once guarantee.
