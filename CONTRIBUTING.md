# Contributing

Better Hermes Hindsight follows rolling Hermes development. Keep changes practical for the current Linux, single-principal, external-Hindsight deployment.

## Setup

```bash
mkdir -p .compat
git clone --depth 1 https://github.com/NousResearch/hermes-agent.git .compat/hermes-current
uv sync --extra dev
uv pip install --python .venv/bin/python -e .compat/hermes-current
uv pip check --python .venv/bin/python
```

The tests import Hermes's real provider and plugin interfaces, so `uv sync` alone is not a complete
development environment. Before validating against a newer Hermes checkout, run
`git -C .compat/hermes-current pull --ff-only` and reinstall the editable checkout with the final
`uv pip install` command above.

## Before committing runtime changes

```bash
uv lock --check
.venv/bin/python -m ruff check better_hermes_hindsight tests scripts __init__.py cli.py
.venv/bin/python -m ruff format --check better_hermes_hindsight tests scripts __init__.py cli.py
.venv/bin/python -m mypy
.venv/bin/python -m pytest -p no:cacheprovider
uv pip check --python .venv/bin/python

git diff --check
```

Focused tests are sufficient while iterating. Documentation-only changes need link and diff review rather than the full runtime suite.

## Rules

- Fix observed problems with the smallest reasonable change.
- Preserve the scope and invariants in `AGENTS.md` and `DESIGN.md`.
- Add tests for public behavior and realistic failure paths, not for prose, commit hashes, plans, or workflow implementation details.
- Test against the intended current Hermes checkout. Record its identity, but do not hard-code it as a permanent compatibility requirement.
- Keep the internal HTTP contract pinned behaviorally to supported Hindsight API versions 0.8.5 and 0.9.1; do not add the Hindsight Python SDK back as a runtime dependency.
- Never place credentials, private endpoints, bank IDs, principal IDs, memories, transcripts, databases, or logs in the repository.
- Use fake services and temporary Hermes homes before an explicitly enabled isolated live test.
- Do not modify the active bundled Hermes/Hindsight deployment as part of tests.
- Do not bump the package version for every development commit. Versions and tags are optional snapshots.

A pull request or commit message should explain the problem, the change, and the verification performed. Additional review is proportionate to risk; concurrency, durability, authorization, and destructive-data changes deserve independent review.
