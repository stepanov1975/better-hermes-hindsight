# Public release checklist

Do not change repository visibility or publish a package until every applicable item is
verified against the intended release commit.

## Product and compatibility

- [ ] Provider proof gates in `DESIGN.md` pass.
- [ ] Supported Hermes, Hindsight server, and client versions are documented and tested.
- [ ] Fresh installation in a temporary `HERMES_HOME` works from documented commands.
- [ ] Rollback to bundled `hindsight` is verified without bank migration.
- [ ] Pre-alpha warnings are replaced with accurate release status.

## Public safety

- [ ] Scan the full Git history with Gitleaks and Semgrep secrets rules.
- [ ] Confirm no private endpoints, credentials, bank IDs, paths, memories, or transcripts.
- [ ] Review wheel and source-distribution contents.
- [ ] Review dependency licenses and preserve upstream source attribution.
- [ ] Review README, issues, examples, and generated logs as public artifacts.

## Repository health

- [ ] CI and security workflows pass for the exact release commit.
- [ ] Dependabot alerts and dependency review are clear or explicitly resolved.
- [ ] Enable GitHub-native secret scanning, push protection, private vulnerability
      reporting, and CodeQL where available after publication.
- [ ] Add a guarded, reviewed release workflow only when release behavior is defined.
- [ ] Configure branch/ruleset protections after stable check names exist.

## Release

- [ ] Replace version `0.0.0` with an explicit pre-release version.
- [ ] Update `CHANGELOG.md` and create release notes.
- [ ] Build artifacts from a clean checkout and verify them in a fresh environment.
- [ ] Obtain independent code/security review.
- [ ] Change visibility only after explicit owner approval.
- [ ] Read back repository visibility, release tag, assets, checks, and security settings.
