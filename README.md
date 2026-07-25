# Better Hermes Hindsight

[![CI](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml)
[![Security scans](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml)

An unofficial, local-external-first Hindsight memory provider project for Hermes Agent.
The intended provider ID is `better_hindsight`.

> **Status: pre-alpha scaffold.** No usable Hermes memory provider is registered yet.
> Do not install or select this project in a production Hermes profile.

## Goal

Build a smaller, testable provider that preserves Hermes's proven memory lifecycle while
adding current-query recall, modern Hindsight controls, evidence-aware context, and
durable asynchronous retention. Development happens in this final repository; proof
runtime state and test banks remain disposable.

## Initial scope

- External/self-hosted Hindsight only.
- Current-query synchronous recall with a bounded fail-open deadline.
- Correct shared observation scope and separate retain/reflect/observation missions.
- Configurable score floors and observation preference.
- FIFO retention, session-epoch isolation, final flushing, and truthful operation status.
- A distinct `better_hindsight` provider identity for safe rollback to bundled `hindsight`.

Cloud setup, embedded-daemon management, production-bank migration, and automatic
reflection are deliberately outside the first proof.

## Repository state

`main` contains the public-safe project contract, package scaffold, documentation, and
verification automation. Provider implementation belongs on
`spike/local-external-provider` until the proof gates in [DESIGN.md](DESIGN.md) pass.

## Development

Requires Python 3.11-3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev --extra proof
uv run --extra dev --extra proof python -m pytest
uv run --extra dev --extra proof python -m ruff check .
uv run --extra dev --extra proof python -m ruff format --check .
uv run --extra dev --extra proof python -m mypy
uv run --extra dev --extra proof python -m build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [DESIGN.md](DESIGN.md) before changing code.

## Safety

Never commit endpoints, credentials, private bank names, raw memories, transcripts,
databases, or local runtime state. Proofs must use a temporary `HERMES_HOME`, a fake
server first, and a clearly disposable test bank only after deterministic tests pass.

## Licensing and attribution

The project is MIT licensed. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for
upstream attribution requirements and [SECURITY.md](SECURITY.md) for reporting security
problems.
