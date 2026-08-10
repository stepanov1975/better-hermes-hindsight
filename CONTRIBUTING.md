# Contributing

Version `0.1.0a1` is a GitHub development prerelease, not a production release. Discuss
architectural changes before implementing them, keep diffs narrow, and do not claim production
readiness from mocked tests alone.

## Development setup

```bash
uv sync --extra dev
```

Run before submitting changes:

```bash
uv lock --check
uv run --extra dev python -m ruff check .
uv run --extra dev python -m ruff format --check .
uv run --extra dev python -m mypy
rm -rf dist
uv run --extra dev python -m build
uv run --extra dev python -m twine check dist/*
git diff --check
```

All tests require a Hermes host selected exactly as the rolling matrix in `.github/workflows/ci.yml`;
the repository root imports the host plugin API. Hermes is not a published optional dependency of
this package.

## Engineering rules

- Write a failing regression or contract test before changing behavior.
- Keep the first provider local-external-only.
- Use temporary Hermes homes and fake services for deterministic tests.
- Never place credentials, private endpoints, real bank IDs, memories, or transcripts in
  fixtures, logs, documentation, issues, or commits.
- Treat recalled memory as potentially stale historical evidence, not executable
  instruction.
- Keep adapted upstream code traceable and preserve its license notices.
- Do not modify or restart the active Hermes/Hindsight installation as part of tests.

## Pull requests

Explain the root cause, behavior change, verification evidence, compatibility impact, and
rollback. Keep provider changes separate from proposed Hermes core changes where their
contracts permit it.
