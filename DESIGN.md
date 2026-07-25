# Design and proof contract

## Status

Pre-alpha repository scaffold, created 2026-07-25. The repository is private during the
proof and becomes public only after the release checklist passes.

## Primary goal

Provide a reliable Hermes memory-provider boundary for a self-hosted external Hindsight
service without requiring Hindsight-specific patches to be reapplied to Hermes after
every update.

The implementation should reduce recurring integration maintenance, not recreate every
feature of the bundled Hindsight provider.

## Architecture boundary

The project owns:

- provider-specific configuration and validation;
- Hindsight client compatibility;
- recall and retention payload construction;
- background write ordering and flush semantics;
- provider tools and evidence formatting;
- compatibility tests against supported Hermes contracts.

Hermes core continues to own turn classification, external-memory trust framing, provider
lifecycle invocation, and the one-active-provider policy. Generic fixes in those areas
remain candidates for focused upstream pull requests.

## Stable identities

- Repository/distribution: `better-hermes-hindsight`
- Python package: `better_hermes_hindsight`
- Hermes provider ID: `better_hindsight`

The provider ID intentionally differs from bundled `hindsight`, allowing configuration-only
rollback. It also avoids relying on same-name user-plugin override behavior, which differs
between Hermes releases and documentation revisions.

## Proof scope

The first proof supports only `local_external` operation. It must not implement cloud
account setup or manage an embedded Hindsight daemon.

Implementation order:

1. Fresh-profile discovery and provider-contract harness.
2. Read-only current-query recall against a fake server.
3. Shared-scope retain payloads and mission synchronization.
4. FIFO asynchronous retention and final flush.
5. Session switch, reset, rewind, and same-ID epoch behavior.
6. Explicit tools, provenance, score floors, and truthful operation status.
7. Read-only canary against a disposable live test bank.

## Proof acceptance gates

Promotion from `spike/local-external-provider` requires all of the following:

- A fresh temporary `HERMES_HOME` discovers and selects `better_hindsight`.
- The first turn recalls against the current query, not a previous query.
- Recall fails open within a documented bounded deadline.
- `shared` is present in the emitted retain request when configured.
- Retain, reflect, and observation missions reach their distinct API fields.
- Score floors affect automatic and explicit recall consistently.
- Writes remain FIFO and final shutdown flushes or reports unfinished operations.
- Session switches and reused session IDs cannot append into a stale document epoch.
- Existing Hindsight bank/document formats require no migration for read compatibility.
- Switching back to bundled `hindsight` requires only a configuration change.
- Package, lint, typing, unit, integration, and fresh-install checks pass.
- Public-facing files contain no private paths, endpoints, bank names, or memory content.

## Stop condition

Stop the custom-provider approach if the proof begins reproducing most of the bundled
provider's cloud, embedded, installer, or compatibility surface. At that point a managed
upstream patch series is likely the smaller maintenance boundary.

## Runtime isolation

Proof work uses disposable resources:

- temporary virtual environment;
- temporary `HERMES_HOME` and profile;
- deterministic fake Hindsight HTTP service;
- separately named live test bank only after fake-server tests pass.

The active Hermes configuration, gateway, bundled provider, and production Hindsight bank
are outside this repository's proof workflow.

## Publication policy

No automated release workflow is present during the proof. Before publication, complete
`docs/public-release-checklist.md`, select a supported version matrix, add verified install
instructions, and cut an explicit pre-release rather than publishing from every main-branch
commit.
