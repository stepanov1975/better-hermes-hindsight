# Changelog

Notable user-visible changes are recorded here. The project follows rolling `main`; versions and tags are optional snapshots rather than compatibility or deployment gates.

## Unreleased

## 0.1.0a5 - 2026-08-14

### Fixed

- Release installation now tolerates an unchanged set of unrelated, pre-existing Hermes
  environment dependency incompatibilities while still rejecting any conflict introduced by
  Better.

## 0.1.0a4 - 2026-08-14

### Changed

- Replaced the `hindsight-client==0.8.5` runtime dependency with a narrow asynchronous HTTP adapter for the four Hindsight 0.8.5 operations Better uses.
- Better can now share the normal Hermes interpreter with bundled `hindsight-client==0.6.1`; a separate profile remains optional for operational separation.
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
- Checksum-verified tagged-release installer for a dedicated Hermes interpreter/profile.

### Changed

- Replaced exact Hermes commit/release assertions with behavioral testing against the intended current checkout.
- Reduced CI to the maintained Python runtime tested against current Hermes `main`.
- Ordinary-user deployment now uses a non-editable wheel and exact tagged plugin checkout;
  development continues to follow rolling Git commits.
- Simplified development documentation, isolated live validation, and repository tests.
- Removed canonical-plan hashes, release-gate prose contracts, historical compatibility blockers, and version bumps as a per-commit requirement.

### Fixed

- Generated launcher now binds the selected profile to its dedicated Hermes interpreter.
- Generic Hermes doctor registration no longer mistakes the exclusive memory-provider bridge
  for a general plugin failure.
- Status derives package and release-commit provenance from the verified install receipt.
- Public installation no longer requires an editable checkout or private repository access.

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
