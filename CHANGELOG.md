# Changelog

Notable user-visible changes are recorded here. The project follows rolling `main`; versions and tags are optional snapshots rather than compatibility or deployment gates.

## Unreleased

### Added

- Bounded read-only `better_hindsight_recall` model tool that reuses the configured automatic-recall policy without exposing caller-selected banks or writes.
- Degraded/non-zero operator status when destination-mismatched outbox rows cannot be delivered safely.

### Changed

- Replaced exact Hermes commit/release assertions with behavioral testing against the intended current checkout.
- Reduced CI to the maintained Python runtime tested against current Hermes `main`.
- Replaced the immutable release/checksum/PyPI workflow with Git-commit-based rolling deployment.
- Simplified development documentation, isolated live validation, and repository tests.
- Removed canonical-plan hashes, release-gate prose contracts, historical compatibility blockers, and version bumps as a per-commit requirement.

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
