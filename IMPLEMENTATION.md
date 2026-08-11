# Implementation status

Better Hermes Hindsight is developed as a rolling plugin for the current Hermes checkout. The source commit and the Hermes commit used for validation are useful diagnostics, not permanent compatibility gates.

There is no separate canonical implementation plan, plan hash, immutable candidate, or release train. The current user request or tracked issue defines the work; Git records what changed.

## Working implementation

The provider currently includes:

- external Hindsight 0.8.5 connectivity;
- bounded, fail-open current-query recall;
- the read-only `better_hindsight_recall` model tool;
- optional redacted automatic retention;
- deterministic segmentation and reconstructable source metadata;
- a SQLite-backed durable outbox with bounded retries;
- process-shared sender/runtime ownership for the current Linux deployment;
- principal and destination policy;
- passive status plus explicit mission check/apply commands;
- thin Hermes plugin bridges at the repository root.

The existing runtime should not be rewritten merely to reduce line count. Simplify it only when real use exposes a bug or a clear maintenance burden.

## Development and compatibility model

- Test against Alex's intended Hermes checkout and Python runtime.
- Record observed Hermes, Better, and Hindsight versions/commits in validation results.
- Fail for missing or incompatible interfaces and broken behavior—not for an unknown but compatible Hermes commit.
- Use the Git commit as the deployable identity. Version bumps and tags are optional snapshots.
- Use an editable install from the same checkout for development/canary deployment.
- Keep `hindsight-client==0.8.5` exact because the SDK/server contract is real.
- Keep the dedicated interpreter/profile and isolated Hindsight instance because bundled Hermes currently requires an incompatible Hindsight SDK.

## Validation bar

A development commit is usable when:

1. focused and full deterministic tests pass;
2. Ruff and mypy pass;
3. the root bridge imports through the intended Hermes checkout;
4. the current Hermes checkout discovers and initializes the provider;
5. the isolated Hindsight smoke test proves bounded recall, retained delivery, restart recovery, and cleanup or clear manual-cleanup reporting; and
6. rollback to the untouched bundled-provider environment remains available.

Historical Hermes versions, PyPI publication, exact source/artifact equality, checksum manifests, prose contracts, and repeated review checkpoints are not release prerequisites.

## Intentional limitations

The first usable version remains Linux/POSIX, external-service-only, single-principal, single-bank, and normal-Hermes-loop-only. It does not promise exactly-once delivery, typed turn provenance, automatic migration, multi-user routing, model-directed writes/reflection, hot reload, or automatic remote deletion. These are accepted scope limits rather than incomplete release tasks.
