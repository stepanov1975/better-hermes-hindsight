# Better Hermes Hindsight

[![CI](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml)
[![Secret scan](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml)

Better Hermes Hindsight is an unofficial Hermes memory provider for an external/self-hosted Hindsight 0.8.5 service. It is developed against a rolling Hermes checkout for a Linux, single-principal deployment.

The provider ID is `better_hindsight`, deliberately separate from bundled `hindsight`, so the existing provider and bank remain available for rollback.

## Why it exists

Compared with bundled Hindsight, this plugin deliberately focuses on:

- bounded recall for the **current** user query;
- one bounded read-only model tool, `better_hindsight_recall`;
- opt-in automatic retention through a durable SQLite outbox;
- deterministic segmentation and reconstructable source metadata;
- stable replace-mode retries after timeout or restart;
- explicit principal and destination policy; and
- operator-only status and mission management; and
- privacy-safe structured diagnostics plus opt-in synthetic canary/alert evaluator executables.

It is narrower than bundled Hindsight. It does not provide embedded/cloud service management, model-facing retain or reflect tools, multi-user bank routing, previous-query background recall, migrations, or automatic deletion.

## Reliability boundary

Recall fails open: timeout, service failure, invalid data, or unavailable runtime yields no external context rather than stopping Hermes. Recalled records are bounded, redacted, and framed as potentially stale historical evidence.

Retention is disabled by default. When enabled, the Hermes callback performs only bounded local redaction, segmentation, and one SQLite admission. Remote delivery runs in the background. Durability begins after admission commits; callbacks Hermes never executes are outside the guarantee.

Retries use a stable document ID and `update_mode="replace"`. A timed-out write may already have committed remotely, so the plugin does **not** claim exactly-once transport.

## Requirements

- Linux/POSIX;
- the current intended Hermes checkout;
- Python supported by that checkout (the maintained development lane uses Python 3.13);
- external Hindsight server and `hindsight-client==0.8.5`;
- a dedicated Hermes interpreter/profile when bundled Hindsight's incompatible SDK must remain available elsewhere; and
- `uv` for installation and development.

Compatibility is behavioral rather than release-matrix based. Validation records the tested Hermes commit, but another commit is not rejected solely because its identity changed.

## Installation

Use the same Git checkout for the installed package and Hermes plugin bridge:

```bash
PROFILE=better-hindsight-dev
SOURCE_DIR=/path/to/better-hermes-hindsight
HERMES_PYTHON=/path/to/dedicated/hermes/python

uv pip install --python "$HERMES_PYTHON" -e "$SOURCE_DIR" 'hindsight-client==0.8.5'
uv pip check --python "$HERMES_PYTHON"
hermes --profile "$PROFILE" plugins install "file://$SOURCE_DIR" --force --enable
hermes --profile "$PROFILE" config set memory.provider better_hindsight
```

Configure endpoint, bank, principal, and credential for the selected profile. Leave retention disabled until recall and status work against the isolated service. See [installation](docs/installation.md) and [configuration](docs/configuration.md).

## Operator commands

```text
hermes --profile <profile> better_hindsight status
hermes --profile <profile> better_hindsight missions check
hermes --profile <profile> better_hindsight missions apply --confirm
```

`status` reads the existing outbox without initializing or draining it. Mission changes are never automatic and require explicit confirmation. There is no retry-now, row-deletion, arbitrary-bank, or model-facing write command.

The included `python -m better_hermes_hindsight.canary` and `python -m better_hermes_hindsight.watchdog` modules provide an explicit synthetic E2E check and transition-only alert evaluation. Neither is scheduled or activated by installation.

See [operations](docs/operations.md) and [rollback](docs/rollback.md).

## Intentional limitations

The initial product is external-service-only, Linux/POSIX, one principal, one static bank, and normal-Hermes-loop-only. It does not support `codex_app_server`, Windows sender election, hot reload, typed turn provenance, automatic migration, remote rewind, or exactly-once delivery. These are accepted limits, not prerequisites for a usable version.

## Development

```bash
uv sync --extra dev
uv lock --check
uv run --frozen --extra dev python -m ruff check src tests __init__.py cli.py
uv run --frozen --extra dev python -m ruff format --check src tests __init__.py cli.py
uv run --frozen --extra dev python -m mypy
uv run --frozen --extra dev python -m pytest -p no:cacheprovider
```

The project follows rolling `main`. A Git commit is enough to identify a deployed build. Versions and tags are optional snapshots; they are not bumped for every development change, and PyPI publication is not part of the normal workflow.

See [implementation status](IMPLEMENTATION.md), [design](DESIGN.md), and [contributing](CONTRIBUTING.md).

## Safety

Never commit endpoints, credentials, private bank names, principal identifiers, raw memories, transcripts, databases, logs, or local runtime state. Live writes require explicit opt-in and the existing isolated Hindsight environment with synthetic content.

## License

MIT. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [SECURITY.md](SECURITY.md).
