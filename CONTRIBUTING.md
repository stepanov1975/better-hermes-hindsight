# Contributing

Better Hermes Hindsight follows rolling Hermes development. Keep changes practical for the current Linux, single-principal, external-Hindsight deployment.

## Setup

```bash
uv sync --extra dev
```

## Before committing runtime changes

```bash
uv lock --check
uv run --frozen --extra dev python -m ruff check .
uv run --frozen --extra dev python -m ruff format --check .
uv run --frozen --extra dev python -m mypy
uv run --frozen --extra dev python -m pytest -p no:cacheprovider
uv run --frozen --extra dev python -m build
uv run --frozen --extra dev python -m pip check

git diff --check
```

Focused tests are sufficient while iterating. Documentation-only changes need link and diff review rather than the full runtime suite.

## Rules

- Fix observed problems with the smallest reasonable change.
- Preserve the scope and invariants in `AGENTS.md` and `DESIGN.md`.
- Add tests for public behavior and realistic failure paths, not for prose, commit hashes, plans, or workflow implementation details.
- Test against the intended current Hermes checkout. Record its identity, but do not hard-code it as a permanent compatibility requirement.
- Keep `hindsight-client==0.8.5` exact.
- Never place credentials, private endpoints, bank IDs, principal IDs, memories, transcripts, databases, or logs in the repository.
- Use fake services and temporary Hermes homes before an explicitly enabled isolated live test.
- Do not modify the active bundled Hermes/Hindsight deployment as part of tests.
- Do not bump the package version for every development commit. Versions and tags are optional snapshots.

A pull request or commit message should explain the problem, the change, and the verification performed. Additional review is proportionate to risk; concurrency, durability, authorization, and destructive-data changes deserve independent review.
