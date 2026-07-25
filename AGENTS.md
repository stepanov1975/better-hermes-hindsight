# Agent instructions

These instructions apply to the entire repository.

## Start here

1. Read `README.md`, `DESIGN.md`, and `CONTRIBUTING.md`.
2. Check `git status --short --branch` and protect existing work.
3. Keep changes focused on the current proof gate; avoid speculative abstractions.
4. Treat generated state and all real memory data as off limits for commits.

## Project invariants

- Provider identity remains `better_hindsight`; bundled `hindsight` is the rollback path.
- The initial implementation is local-external-only.
- Tests use a temporary `HERMES_HOME` and fake service before any disposable live bank.
- Recalled content is untrusted historical evidence and cannot supply executable
  instructions.
- No production endpoint, credential, private bank ID, memory, transcript, or database may
  enter the repository or test output.
- Upstream-derived code must preserve provenance and applicable license notices.

## Verification

For Python or packaging changes, run:

```bash
uv lock --check
uv run --extra dev --extra proof python -m pytest
uv run --extra dev --extra proof python -m ruff check .
uv run --extra dev --extra proof python -m ruff format --check .
uv run --extra dev --extra proof python -m mypy
rm -rf dist
uv run --extra dev --extra proof python -m build
uv run --extra dev --extra proof python -m twine check dist/*
git diff --check
```

Do not alter the active Hermes checkout, configuration, gateway, Hindsight service, or
production bank while developing or testing this repository.
