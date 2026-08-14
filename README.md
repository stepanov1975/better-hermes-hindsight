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
- an external Hindsight 0.8.5 server;
- `aiohttp>=3.14.1,<4`, which is compatible with the current Hermes messaging environment; and
- `uv` for installation and development.

Compatibility is behavioral rather than release-matrix based. Validation records the tested Hermes commit, but another commit is not rejected solely because its identity changed.

## Installation

Install a tagged GitHub prerelease with its checksum-verified wheel into the existing Hermes
interpreter. The installer creates or updates the selected profile, installs the exact plugin
bridge, selects `better_hindsight`, and writes an interpreter-bound launcher. It never
creates a bank, stores a credential, enables retention, starts a gateway, or schedules a
canary/watchdog.

Better's internal HTTP adapter does not import the Hindsight Python SDK, so Hermes's bundled
`hindsight-client==0.6.1` can remain installed for the bundled provider. The published
`v0.1.0a3` release predates this change and still requires its documented isolated interpreter;
use the shared-interpreter procedure with `v0.1.0a5` or newer. `v0.1.0a4` contains the internal
client but its installer can reject an unrelated dependency mismatch already present in Hermes.

See the exact commands in [installation](docs/installation.md), then configure the endpoint,
bank, principal, and credential for that profile. Leave retention disabled until recall and
status work against the isolated service. See [configuration](docs/configuration.md).

## Operator commands

```text
hermes --profile <profile> better_hindsight status
hermes --profile <profile> better_hindsight missions check
hermes --profile <profile> better_hindsight missions apply --confirm
```

`status` reads the existing outbox without initializing or draining it. Mission changes are never automatic and require explicit confirmation. There is no retry-now, row-deletion, arbitrary-bank, or model-facing write command.

The included `python -m better_hermes_hindsight.canary` and
`python -m better_hermes_hindsight.watchdog` modules provide an adapter-backed synthetic E2E check
and transition-only alert evaluation over privacy-safe per-operation HTTP, lifecycle, recall, and
retention events. Neither is scheduled or activated by installation.

See [operations](docs/operations.md) and [rollback](docs/rollback.md).

## Intentional limitations

The initial product is external-service-only, Linux/POSIX, one principal, one static bank, and normal-Hermes-loop-only. It does not support `codex_app_server`, Windows sender election, hot reload, typed turn provenance, automatic migration, remote rewind, or exactly-once delivery. These are accepted limits, not prerequisites for a usable version.

## Development

```bash
uv sync --extra dev
uv lock --check
uv run --frozen --extra dev python -m ruff check src tests scripts __init__.py cli.py
uv run --frozen --extra dev python -m ruff format --check src tests scripts __init__.py cli.py
uv run --frozen --extra dev python -m mypy
uv run --frozen --extra dev python -m pytest -p no:cacheprovider
rm -rf dist
uv build --out-dir dist
uvx --from twine twine check dist/*.whl dist/*.tar.gz
uv run --frozen --extra dev python scripts/check_sdist.py dist/*.tar.gz
```

Development follows rolling `main`; ordinary-user deployment uses immutable tagged GitHub
prereleases. PyPI publication is not required.

See [implementation status](IMPLEMENTATION.md), [design](DESIGN.md), and [contributing](CONTRIBUTING.md).

## Safety

Never commit endpoints, credentials, private bank names, principal identifiers, raw memories, transcripts, databases, logs, or local runtime state. Live writes require explicit opt-in and an isolated Hindsight service/bank with synthetic content.

## License

MIT. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [SECURITY.md](SECURITY.md).
