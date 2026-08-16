# Changelog

Notable user-visible changes are recorded here. The project follows rolling `main`; versions and tags are optional snapshots rather than compatibility or deployment gates.

## Unreleased

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
