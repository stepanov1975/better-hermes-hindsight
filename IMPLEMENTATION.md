# Implementation status

Better Hermes Hindsight is developed as a rolling plugin for the current Hermes checkout. The source commit and the Hermes commit used for validation are useful diagnostics, not permanent compatibility gates.

There is no separate canonical implementation plan, plan hash, immutable candidate, or release train. The current user request or tracked issue defines the work; Git records what changed.

## Working implementation

The provider currently includes:

- external Hindsight 0.8.5, 0.9.1, and 0.9.2 connectivity;
- bounded, fail-open current-query recall;
- model tools for bounded recall, default-off read-only reflection, durable retention admission, and
  compact passive queue status;
- optional redacted automatic retention;
- semantic, independently decodable segmentation with persisted event identity, occurrence time, and
  reconstructable source metadata;
- a SQLite-backed durable outbox with bounded retries;
- process-shared sender/runtime ownership for the current Linux deployment;
- principal and destination policy;
- passive status plus explicit mission check/apply commands;
- opt-in bounded private slow-recall capture with query-free listing and trace-enabled operator replay;
- standard Hermes plugin entry points and a self-contained implementation package at the repository root.

The existing runtime should not be rewritten merely to reduce line count. Simplify it only when real use exposes a bug or a clear maintenance burden.

## Development and compatibility model

- Keep required pull-request checks reproducible against one reviewed Hermes commit across Python
  3.11–3.13 on Linux.
- Follow Hermes `main` in a scheduled/manual Python 3.13 compatibility canary; record the resolved
  commit and update the required pin deliberately after a successful proof.
- Record observed Hermes, Better, and Hindsight versions/commits in validation results.
- Fail for missing or incompatible interfaces and broken behavior—not for an unknown but compatible Hermes commit.
- Use the Git commit as the deployable identity. Version bumps and tags are optional snapshots.
- When a new synchronized version reaches verified `main`, publish only a changelog-derived source
  snapshot; PyPI and uploaded distribution assets remain outside the deployment path.
- Ordinary-user deployment uses `hermes plugins install`; no separate package installation is
  required.
- Keep the narrow internal HTTP contract aligned with supported Hindsight API versions 0.8.5,
  0.9.1, and 0.9.2.
- Run Better through Hermes's normal memory-provider lifecycle alongside the untouched bundled
  Hindsight client; keep live-write validation on an isolated Hindsight service/bank.

## Validation bar

A development commit is usable when:

1. focused and full deterministic tests pass;
2. Ruff and mypy pass;
3. the complete Git plugin installs and imports through the intended Hermes checkout without an
   external Better package;
4. the current Hermes checkout discovers and initializes the provider;
5. the isolated Hindsight smoke test proves bounded recall, retained delivery, restart recovery, and
   cleanup or clear manual-cleanup reporting;
6. a deployment that will enable reflection separately proves one synthetic query against its
   isolated Hindsight LLM configuration; and
7. rollback to the untouched bundled-provider environment remains available.

Historical Hermes versions, PyPI publication, exact source/artifact equality, checksum manifests, prose contracts, and repeated review checkpoints are not release prerequisites.

## Intentional limitations

The first usable version remains Linux/POSIX, external-service-only, single-principal, single-bank, and
normal-Hermes-loop-only. It does not promise exactly-once delivery, typed direct-turn provenance for
provenance, automatic migration, multi-user routing, caller-selected reflection or policy changes,
hot reload, or automatic remote deletion. These are accepted scope limits rather than
incomplete release tasks.
