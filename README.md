# Better Hermes Hindsight

[![CI](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml)
[![Secret scan](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml)

Better Hermes Hindsight is an unofficial Hermes memory provider for supported external/self-hosted Hindsight services. It is developed against a rolling Hermes checkout for a Linux, single-principal deployment.

The provider ID is `better_hindsight`, deliberately separate from bundled `hindsight`, so the existing provider and bank remain available for rollback.

## Quick start

You need a working Hermes installation and an external Hindsight 0.8.5 or 0.9.1 service.

```bash
hermes plugins install stepanov1975/better-hermes-hindsight
hermes memory setup better_hindsight
```

Create `$HERMES_HOME/better_hindsight/config.json` (normally
`~/.hermes/better_hindsight/config.json`) with your endpoint, bank, and exact Hermes principal. For
example, replace the placeholder principal with the platform and user ID used by your gateway:

```json
{
  "api_url": "http://localhost:8888",
  "bank_id": "hermes",
  "single_principal": true,
  "allowed_principals": [
    {
      "platform": "telegram",
      "identifier_kind": "user_id",
      "identifier": "YOUR_USER_ID"
    }
  ],
  "retain": {
    "enabled": false
  }
}
```

Provide `HINDSIGHT_API_KEY` to both your shell and Hermes gateway through your normal secret
mechanism; never place it in the JSON file. Then verify the selected provider and passive outbox
status:

```bash
hermes config get memory.provider
hermes better_hindsight status
```

The provider should be `better_hindsight`, and status should report `"result":"ok"`. A fresh
installation normally reports `"outbox":"uninitialized"` until the first retained turn; this is
healthy. Confirm one synthetic recall before enabling retention. See the full
[installation](docs/installation.md) and [configuration](docs/configuration.md) guides for the
remaining policy and verification options.

## Why it exists

Compared with bundled Hindsight, this plugin deliberately focuses on:

- bounded recall for the **current** user query;
- one bounded read-only model tool, `better_hindsight_recall`;
- opt-in automatic retention through a durable SQLite outbox;
- deterministic segmentation and reconstructable source metadata;
- stable replace-mode retries after timeout or restart;
- explicit principal and destination policy;
- operator-only status and mission management; and
- privacy-safe structured diagnostics plus opt-in synthetic canary and alert-evaluator commands.

It is narrower than bundled Hindsight. It does not provide embedded/cloud service management, model-facing retain or reflect tools, multi-user bank routing, previous-query background recall, migrations, or automatic deletion.

## Reliability boundary

Recall fails open: timeout, service failure, invalid data, or unavailable runtime yields no external context rather than stopping Hermes. Queries are bounded by both characters and the exact `cl100k_base` token rule used by supported Hindsight servers. Recalled records are bounded, redacted, and framed as potentially stale historical evidence.

Retention is disabled by default. When enabled, the Hermes callback performs only bounded local redaction, segmentation, and one SQLite admission. Remote delivery runs in the background. Durability begins after admission commits; callbacks Hermes never executes are outside the guarantee.

Retries use a stable document ID and `update_mode="replace"`. A timed-out write may already have committed remotely, so the plugin does **not** claim exactly-once transport.

## Requirements

- Linux/POSIX;
- the current intended Hermes checkout;
- Python supported by that checkout (the maintained development lane uses Python 3.13);
- an external Hindsight 0.8.5 or 0.9.1 server;
- `aiohttp>=3.14.1,<4` and `tiktoken>=0.12,<0.13`, which the plugin declares through Hermes's
  standard memory-plugin dependency mechanism.

The plugin packages the official hash-verified `cl100k_base` encoding table, so query counting does
not make a first-use network request outside the configured recall deadline.

Compatibility is behavioral rather than release-matrix based. Validation records the tested Hermes commit, but another commit is not rejected solely because its identity changed.

## Installation

Better Hermes Hindsight is a regular, self-contained Hermes memory plugin. Install it into the
current Hermes configuration with the same plugin commands used for other Git plugins:

```bash
hermes plugins install stepanov1975/better-hermes-hindsight
hermes memory setup better_hindsight
```

It does not need another Hermes profile, Python environment, package installation, launcher, or
custom gateway startup procedure. Its internal HTTP adapter does not import the Hindsight Python
SDK, so the bundled provider and its client remain untouched.

See [installation](docs/installation.md), then configure the endpoint, bank, principal, and
credential. Leave retention disabled until recall and status work. See
[configuration](docs/configuration.md).

## Operator commands

```text
hermes better_hindsight status
hermes better_hindsight diagnostics list
hermes better_hindsight diagnostics replay <record-id>
hermes better_hindsight missions check
hermes better_hindsight missions apply --confirm
hermes better_hindsight canary
hermes better_hindsight watchdog --help
```

`status` reads the existing outbox without initializing or draining it. Opt-in slow-recall
diagnostics keep exact projected queries only in bounded private local files; list output is query-free,
and replay is an operator-only read against the configured bank. Mission changes are never automatic
and require explicit confirmation. There is no retry-now, row-deletion, arbitrary-bank, or model-facing
write command.

The included `hermes better_hindsight canary` and `hermes better_hindsight watchdog` commands
provide an adapter-backed synthetic E2E check and transition-only alert evaluation over
privacy-safe per-operation HTTP, lifecycle, recall, and retention events. Neither is scheduled or activated by installation.

See [operations](docs/operations.md) and [rollback](docs/rollback.md).

## Intentional limitations

The initial product is external-service-only, Linux/POSIX, one principal, one static bank, and normal-Hermes-loop-only. It does not support `codex_app_server`, Windows sender election, hot reload, typed turn provenance, automatic migration, remote rewind, or exactly-once delivery. These are accepted limits, not prerequisites for a usable version.

## Development

```bash
mkdir -p .compat
git clone --depth 1 https://github.com/NousResearch/hermes-agent.git .compat/hermes-current
uv sync --extra dev
uv pip install --python .venv/bin/python -e .compat/hermes-current
uv pip check --python .venv/bin/python
uv lock --check
.venv/bin/python -m ruff check better_hermes_hindsight tests scripts __init__.py cli.py
.venv/bin/python -m ruff format --check better_hermes_hindsight tests scripts __init__.py cli.py
.venv/bin/python -m mypy
.venv/bin/python -m pytest -p no:cacheprovider
rm -rf dist
uv build --out-dir dist
uvx --from twine twine check dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/check_sdist.py dist/*.tar.gz
```

On later runs, update the checkout with
`git -C .compat/hermes-current pull --ff-only` before reinstalling it into the development
environment. The test suite imports the real Hermes host interfaces; `uv sync` alone does not
install Hermes. Run the checks through `.venv/bin/python` rather than `uv run`, which may resync
the environment and replace dependencies selected by the current Hermes checkout.

Development follows rolling `main`; ordinary-user deployment uses Hermes's standard Git-plugin
installer. PyPI publication is not required.

See [implementation status](IMPLEMENTATION.md), [design](DESIGN.md), and [contributing](CONTRIBUTING.md).

## Safety

Never commit endpoints, credentials, private bank names, principal identifiers, raw memories, transcripts, databases, logs, or local runtime state. Live writes require explicit opt-in and an isolated Hindsight service/bank with synthetic content.

## License

MIT. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [SECURITY.md](SECURITY.md).
