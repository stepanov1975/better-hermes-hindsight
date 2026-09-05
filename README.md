# Better Hermes Hindsight

[![CI](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/ci.yml)
[![Security scans](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/better-hermes-hindsight/actions/workflows/security.yml)

Better Hermes Hindsight is an unofficial Hermes memory provider for supported external/self-hosted Hindsight services. It is developed against a rolling Hermes checkout for a Linux, single-principal deployment.

The provider ID is `better_hindsight`, deliberately separate from bundled `hindsight`, so the existing provider and bank remain available for rollback.

## Quick start

You need a working Hermes installation and an external Hindsight 0.8.5, 0.9.1, or 0.9.2 service.

```bash
hermes plugins install --enable stepanov1975/better-hermes-hindsight
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
  "planner": {
    "mode": "off"
  },
  "reflect": {
    "enabled": false
  },
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

- bounded recall for the **current** user query, with a default-off context-aware planner that can skip,
  reuse existing conversational context, or issue one rewritten historical query;
- four bounded model tools for recall, opt-in read-only reflection, durable retention admission, and
  compact passive queue status;
- opt-in automatic retention through a durable SQLite outbox;
- semantic, independently decodable retention segments with per-occurrence event identity and timestamps;
- stable replace-mode retries after timeout or restart;
- explicit principal and destination policy;
- operator status and mission management; and
- privacy-safe structured diagnostics plus opt-in synthetic canary and alert-evaluator commands.

It is narrower than bundled Hindsight. Its optional reflection tool is fixed to the configured bank,
principal, tags, budget, and bounds; it does not provide caller-selected reflection policy,
embedded/cloud service management, multi-user bank routing, previous-query background recall,
migrations, or automatic deletion.

## Hermes profile compatibility

Better Hindsight can serve multiple Hermes profiles on one host when each Better-enabled profile runs
in its own CLI or gateway process. Install, select, and configure the plugin in each profile. Its
configuration, SQLite outbox, and diagnostics remain under that profile's `$HERMES_HOME`; use a
different Hindsight `bank_id` for each profile that requires remote memory isolation.

| Profile arrangement | Support |
| --- | --- |
| Multiple profiles with separate Hermes/gateway processes | Supported; each process owns one profile-local Better runtime |
| Multiple profiles intentionally using the same bank | Supported, but their remote memory is deliberately combined |
| One multiplexed gateway with Better selected by only one profile | Supported |
| One multiplexed gateway with Better selected by multiple profiles | Unsupported; the first profile owns the one process runtime and later profiles fail open |

Hermes multiplexing (`gateway.multiplex_profiles: true`) changes the active profile inside one
process. Better intentionally accepts only one exact configuration per process, so it never reuses one
profile's destination or outbox for another. `HINDSIGHT_API_KEY` is likewise process-scoped. See the
[compatibility guide](docs/compatibility.md) for the complete boundary.

## Comparison with Hermes's bundled Hindsight provider

Hermes also ships a memory provider named `hindsight`. That provider is separate from Hermes's
built-in `MEMORY.md` / `USER.md` memory. Both providers use Hindsight for storage and retrieval; the
table compares their Hermes integration behavior rather than Hindsight's underlying knowledge graph
or retrieval quality.

| Area | Better Hindsight | Bundled Hermes Hindsight |
| --- | --- | --- |
| Installation target | Standard Git plugin for a supported external Hindsight 0.8.5, 0.9.1, or 0.9.2 service | Included with Hermes; interactive setup supports Hindsight Cloud, a local embedded service, or an external service |
| Automatic recall | Synchronous current-turn recall under character, token, response-size, and total-time bounds. Optional `pre_llm_call` planning uses bounded recent conversation context to skip/reuse or issue one self-contained rewritten query. | Background previous-query recall by default; optional synchronous current-query recall |
| Recalled context | Complete byte-bounded JSONL records with recalled text and available time metadata; redacted and explicitly framed in a provider-neutral envelope as stale, untrusted evidence. Explicit recall results also retain the available type. | Formatted memory text or a reflect synthesis with configurable preamble, token budget, types, and tags |
| Automatic retention | Opt-in; authorized, eligible turns are admitted all-or-none to a bounded private SQLite rollback-journal outbox before asynchronous delivery | Enabled by default; completed turns enter a process-local FIFO writer and then optional server-side async processing |
| Retained payload | Pattern-redacted, independently decodable event records with a per-admission event ID and occurrence time, hashed session identity, and bounded provenance | Labeled user/assistant transcripts with richer session, platform, user, chat, and lineage metadata |
| Crash behavior before remote delivery | Committed outbox rows survive process restart and are retried | Locally queued writer jobs are not persistent; shutdown drains them only within a bounded wait |
| Retry/document strategy | Stable IDs for every admitted occurrence segment, `update_mode="replace"`, destination binding, and bounded retry backoff | Session-scoped `update_mode="append"` where supported, with a process-unique document fallback for older APIs |
| Read-after-write freshness | Eventual: an immediately following recall can race the outbox sender | Background prefetch can wait for the local writer and server-side async retain operations before recalling |
| Model-facing tools | Bounded structured recall, default-off read-only reflection, durable local retain admission, and compact passive queue status; no caller-selected bank or policy overrides | Recall, retain, and reflect tools in tools or hybrid mode |
| Routing and authorization | One static bank with an exact single-principal allowlist | Static or templated banks across profile, workspace, platform, user, or session contexts |
| Bank policy operations | Explicit operator check/apply for retain and observations missions | No equivalent mission drift check/apply/readback operator command |
| Operations | Automatic recall status indicator, passive outbox status, structured diagnostics, replay, synthetic canary, watchdog evaluator, and mission drift checks | Interactive setup, recall/retain status indicators, embedded-service lifecycle, and normal provider logs |

Choose Better Hindsight when a narrow self-hosted deployment prioritizes current-query alignment,
crash-durable delivery, explicit trust framing, and operator diagnostics. Choose the bundled provider
when setup breadth, cloud or embedded operation, dynamic bank routing, broader caller-controlled
reflection, or lower independent maintenance matters more. Better Hindsight is deliberately not a
drop-in superset.

This comparison follows the current intended rolling Hermes checkout. Review the
[bundled provider source](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/hindsight)
when upgrading either project because its behavior continues to evolve.

## Reliability boundary

Recall fails open: timeout, service failure, invalid data, or unavailable runtime yields no external context rather than stopping Hermes. Queries are bounded by both characters and the exact `cl100k_base` token rule used by supported Hindsight servers. Recalled records are bounded, redacted, and framed as potentially stale historical evidence.

The optional planner is disabled by default. In `shadow` mode it records only action/latency metadata and
keeps direct-query recall. In `active` mode, `skip` and `reuse` avoid a Hindsight request while `recall`
substitutes one validated self-contained query. The hook never stores the transcript; a short-lived
process-local handoff stores only query hashes and the planned action/query in memory. Session-scoped
reservations are consumed once. After validating current session/turn identity, every hook publishes the
newest pending fence before configuration or model work, so an early return cannot expose an older plan.
Consuming a pending reservation fences out an abandoned late hook worker. Finalization samples the clock
and rejects model-derived `recall` or `reuse` after the
planner deadline while holding the registry lock, while still allowing the deterministic active-mode
`skip` failure policy. Missing, stale, late, or mismatched handoff state falls back to direct current-query
recall. Earlier branch-preview revisions wrote planner decisions to SQLite. The runtime deliberately leaves
that legacy path untouched: stop every Hermes process using the profile, remove the configured database
and sidecars offline, then remove the obsolete planner keys.

Reflection is disabled by default and is never automatic. When enabled, `better_hindsight_reflect`
accepts one nonblank bounded query for the configured bank under the authorized principal and returns
only a redacted synthesis inside the same untrusted recalled-memory-evidence envelope, explicitly framed
as stale, untrusted generated evidence. Its configured output cap counts the complete serialized UTF-8
tool response. It does not
return Hindsight traces, source payloads, usage details, or policy controls. Better's timeout and byte
limits bound the local Hermes call and returned context; Hindsight's agentic LLM work and provider cost
also require appropriate server-side iteration, context, wall-time, and completion-token limits. A
local timeout does not guarantee that backend model work is cancelled or that incurred cost is
refunded.

Retention is disabled by default. When enabled, the Hermes callback performs only bounded local redaction, segmentation, and one SQLite admission. Remote delivery runs in the background. Durability begins after admission commits; callbacks Hermes never executes are outside the guarantee.

Retries use a stable document ID and `update_mode="replace"`. A timed-out write may already have committed remotely, so the plugin does **not** claim exactly-once transport.

## Requirements

- Linux/POSIX;
- the current intended Hermes checkout;
- at most one Better-enabled profile per Hermes process;
- Python 3.11, 3.12, or 3.13 on Linux;
- an external Hindsight 0.8.5, 0.9.1, or 0.9.2 server;
- `aiohttp>=3.14.1,<4` and `tiktoken>=0.12,<0.14`, which the plugin declares through Hermes's
  standard memory-plugin dependency mechanism.

The plugin packages the official hash-verified `cl100k_base` encoding table, so recall and reflection
query counting does not make a first-use network request outside the configured operation deadline.

Compatibility is behavioral rather than release-matrix based. Required CI uses one reviewed Hermes
commit for reproducibility across Python 3.11–3.13, while a weekly/manual Python 3.13 canary follows
Hermes `main` and records the resolved commit.

## Installation

Better Hermes Hindsight is a regular, self-contained Hermes memory plugin. Install it into the
current Hermes configuration with the same plugin commands used for other Git plugins:

```bash
hermes plugins install --enable stepanov1975/better-hermes-hindsight
hermes memory setup better_hindsight
```

It does not need another Hermes profile, Python environment, package installation, launcher, or
custom gateway startup procedure. Its internal HTTP adapter does not import the Hindsight Python
SDK, so the bundled provider and its client remain untouched.

See [installation](docs/installation.md), then configure the endpoint, bank, principal, and
credential. Leave reflection and retention disabled until recall and status work. See
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
and require explicit confirmation. There is no retry-now, row-deletion, arbitrary-bank, or
model-facing policy-change command. Reflection is available only through the default-off bounded
memory tool; there is no operator command or caller-selected reflection policy.

The included `hermes better_hindsight canary` and `hermes better_hindsight watchdog` commands
provide an adapter-backed synthetic E2E check and transition-only alert evaluation over
privacy-safe per-operation HTTP, lifecycle, recall, and retention events. Neither is scheduled or activated by installation.

See [operations](docs/operations.md) and [rollback](docs/rollback.md).

## Intentional limitations

The initial product is external-service-only, Linux/POSIX, one principal, one static bank, one
Better-enabled profile per process, and normal-Hermes-loop-only. It does not support multiplexed
multi-profile Better runtimes, `codex_app_server`, Windows sender election, hot reload, typed turn
provenance, automatic bank/outbox migration, remote rewind, or exactly-once delivery. These are accepted
limits, not prerequisites for a usable version.

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
uvx --from 'twine==7.0.0' twine check dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/check_sdist.py dist/*.tar.gz
```

On later runs, update the checkout with
`git -C .compat/hermes-current pull --ff-only` before reinstalling it into the development
environment. The test suite imports the real Hermes host interfaces; `uv sync` alone does not
install Hermes. Run the checks through `.venv/bin/python` rather than `uv run`, which may resync
the environment and replace dependencies selected by the current Hermes checkout.

Development follows rolling `main`; ordinary-user deployment uses Hermes's standard Git-plugin
installer. PyPI publication is not required.

Use the [recall-quality evaluator](docs/recall-quality-evaluation.md) for synthetic regression fixtures
or an owner-only historical-query capture and read-only comparison of the configured real bank with
`prefer_observations=true`. Reusable collection and capture code is tracked; private queries,
responses, IDs, and labels remain under the ignored `.hermes/` directory.

Use the [provider shadow benchmark](docs/provider-shadow-benchmark.md) for a release-gated,
public-safe comparison of the actual bundled and Better provider lifecycles against separate
disposable banks and the same synthetic corpus.

See [implementation status](IMPLEMENTATION.md), [design](DESIGN.md), [contributing](CONTRIBUTING.md),
and [GitHub security](docs/github-security.md).

## Safety

Never commit endpoints, credentials, private bank names, principal identifiers, raw memories, transcripts, databases, logs, or local runtime state. Live writes require explicit opt-in and an isolated Hindsight service/bank with synthetic content.

## License

MIT. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [SECURITY.md](SECURITY.md).
