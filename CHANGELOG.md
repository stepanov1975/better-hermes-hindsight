# Changelog

All notable changes are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project remains pre-1.0 and uses
semantic prerelease versions while its public compatibility contract is proven.

## [Unreleased]

### Added

- Bounded read-only `better_hindsight_recall` model tool that reuses the authorized automatic-recall
  policy and untrusted-evidence formatter without exposing writes or caller-selected bank/policy
  controls.

## [0.1.0a1] - 2026-08-10

### Added

- First-turn, fail-open current-query recall through Hermes's public memory-provider lifecycle.
- Opt-in redacted turn retention with bounded local admission, asynchronous Hindsight delivery,
  stable replace-mode replay, and reconstructable multi-segment source metadata.
- Passive bounded status diagnostics and explicit confirmation-gated mission management.
- Thin root bridges for Hermes's released Git-plugin install, update, discovery, and removal lifecycle.
- Rolling current-Hermes compatibility checks, package-closure security gates, isolated value proof,
  rollback guidance, and a manual tag-bound prerelease publication workflow.

### Security

- Recall is framed as potentially stale historical evidence rather than executable instruction.
- High-confidence credential patterns are redacted before formatting or retention admission.
- Automatic retention is disabled by default; development writes require explicit opt-in and an
  isolated interpreter, profile, Hindsight 0.8.5 service, and disposable bank.
- Release publication requires an exact prerelease tag and commit, a manual publish input, the
  operator-protected `pypi` GitHub environment, trusted-publishing configuration, and the
  `PYPI_RELEASE_CONFIGURED=true` repository variable set only after both protections exist.
- The source distribution includes the root Hermes bridge, operator documentation, workflows, and
  tests needed to inspect and exercise the source artifact independently.

### Known limitations

- External/self-hosted Hindsight 0.8.5 only; no cloud or embedded-daemon management.
- One explicitly asserted principal and one static bank; no multi-user routing.
- `codex_app_server`, Windows sender election, production migration, and automatic rollback are not
  supported in this prerelease.
- Better and bundled Hindsight require incompatible client SDK versions in the same interpreter, so
  provider transitions require the documented stopped-process package change.

[Unreleased]: https://github.com/stepanov1975/better-hermes-hindsight/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/stepanov1975/better-hermes-hindsight/releases/tag/v0.1.0a1
