# Compatibility and preservation contract

This document defines the rolling compatibility policy and preserves public-safe historical source
observations. It states the released callback boundary used by the best-effort plugin. It records no endpoint,
credential, bank identity, principal identifier, transcript, memory text, database, log, or local
checkout path.

## Rolling compatibility policy

Hermes Agent is the host application, not a Better Hindsight package dependency. The published wheel
and optional dependency groups contain no `hermes-agent` requirement. A Hermes version or commit in
test tooling selects a host compatibility environment; it is not a runtime prerequisite.

The required release gate is the current stable Hermes release. The matrix may also retain older
hosts as historical characterization, but a historical lane is non-blocking and does not imply support.
When a new stable Hermes release changes the public plugin/provider lifecycle, Better Hindsight must
adapt or document a new minimum supported version before release.

| Matrix lane | Host evidence | Release meaning |
| --- | --- | --- |
| Supported | GitHub release `v2026.8.3`, package metadata 0.20.0, commit `3c27eb6234bf91b8ceee9e9071591b31e9b148cb` | Current stable Hermes release; required full lifecycle gate |
| Historical | `v2026.7.20`, package 0.19.0, commit `3ef6bbd201263d354fd83ec55b3c306ded2eb72a` | Historical characterization only; not a supported runtime prerequisite |

Security gates are separate: one audits Better Hindsight runtime/package dependencies, and another
audits each actively supported Hermes compatibility environment. Findings in an unsupported
historical host remain evidence about that host; they do not become vulnerabilities in Better's wheel.
As of 2026-08-09 the Better manifest audit is clean, while the required `v2026.8.3` host audit is
blocked by Hermes's `cryptography==48.0.1` pin and three unsuppressed PYSEC findings documented in
[audit-findings.md](audit-findings.md). This blocks public release without weakening the compatibility
contract or silently substituting an untested dependency override.

<!-- better-hindsight-status-compatibility:start -->
## Status inspection compatibility

Inspection of an existing outbox requires `os.name == "posix"`, linked SQLite `>=3.22.0`,
Python URI connections, and SQLite's built-in POSIX `unix` VFS selected with `vfs=unix`.
A non-POSIX or older runtime returns fixed `status_unavailable` before `sqlite3.connect()`;
an unavailable `unix` VFS fails selection before the target database is opened. The command
does not support a process-default or custom VFS.
<!-- better-hindsight-status-compatibility:end -->

## Product boundary

Better Hermes Hindsight registers only `better_hindsight`; bundled `hindsight` remains the preserved
data/provider rollback target. On the current supported Hermes release the same interpreter cannot run the two
providers as a configuration-only switch: Better requires exact `hindsight-client==0.8.5`, while the
bundled provider's lazy dependency gate requires exact `0.6.1`. Rollback therefore uses the documented
stopped-process provider/wheel/client transition. The first prerelease is external/self-hosted only.
It does not implement cloud setup, supervise an embedded server, or require a Hermes core patch.

The supported path is the current stable, unmodified released Hermes normal conversation loop. Recall is enabled by
default and automatic retention is disabled by default. `codex_app_server` is unsupported on the
current supported release because it
bypasses normal provider memory behavior. No model-facing memory tools in
the first prerelease are registered.

## Historical version baseline

| Surface | Frozen evidence | Project contract |
| --- | --- | --- |
| Released Hermes | `v2026.7.20`, package 0.19.0, tag commit `3ef6bbd201263d354fd83ec55b3c306ded2eb72a`; published 2026-07-20 | Historical characterization baseline |
| Effective audited checkout | Clean `main` at `41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f`; installed package 0.19.0 and installed hindsight-client 0.6.1 | Historical behavior comparison; location intentionally omitted |
| Cached upstream ref in that checkout | `origin/main` at `4dae897265f09ed5b26f5e02b0f0fcb1325e0b6d`; the checkout was ahead by 9 and behind by 1,416 | Counts describe this cached observation only |
| Current upstream observation at audit time | `NousResearch/hermes-agent` `main` at `eb52760564dbba2e5971fa54bd67384e281cd3b8`, observed 2026-07-26 UTC | Historical audit evidence, not a runtime prerequisite |
| Hindsight | Release `v0.8.5`, commit `705757f362552918dfb0242906cb8466de320378` | Hindsight server 0.8.5 and `hindsight-client==0.8.5` are the exact initial target |
| Python | 3.11, 3.12, and 3.13 | All three remain release gates |

Released Hermes's bundled provider calls `tools.lazy_deps.ensure("memory.hindsight", prompt=False)`
before constructing its external client. That registry pins `hindsight-client==0.6.1` and treats
installed `0.8.5` as unsatisfied, so first bundled use either attempts a downgrade or fails when lazy
installation is disabled. Importing the bundled provider under `0.8.5` is not rollback proof. The
release procedure therefore verifies an explicit disposable `uv pip --python` transition to `0.6.1`
with no lazy package-manager call and the reverse reinstall of Better plus `0.8.5`; neither
transition edits Hermes source or migrates data. A profile scopes configuration and data, not this
interpreter-global dependency.

The nine carried checkout commits, newest first, are retained as historical comparison evidence:

1. `41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f` — `fix(gateway): retry restart completion delivery`
2. `821b11e631e5f663f2e9f915f77a353d1c528cbc` — `test(hindsight): complete synchronous recall coverage`
3. `d48bcb0d400bf852499758080f50ae49ed857626` — `feat(hindsight): opt-in synchronous recall for the current turn (#5820)`
4. `151a1b4f6045c86577806feb55909ed33e608752` — `fix(memory): sync Hindsight bank missions`
5. `0e4f1ba7e017aac4d3c0995be941c5ce6364a17b` — `fix(cron): preserve named Telegram DM topic targets`
6. `fb8cc28775bc247eacc964d2c5d5d88830152adf` — `fix(agent): make memory shutdown concurrency-safe`
7. `517e0bcf0511564eec0290037e2a8d0ff3f1c895` — `chore: update author mapping`
8. `fcb1f9da2bb5f38f6270137fce13d051449cbcc5` — `fix: move shutdown_memory_provider before _session_messages clear`
9. `2b1d2bafc3e4f13ed8b961b6c81effccd7c066bd` — `fix(agent): call shutdown_memory_provider() in AIAgent.close() (#46082)`

The carried synchronous-recall work is useful comparison evidence, but it is not required by the
product and does not provide Better Hindsight's profile outbox or destination-matched replay.

## Hermes loader and lifecycle boundary

The released specialized loader is `plugins/memory/__init__.py`; bundled providers use
`plugins/memory/<name>/`, and the released user-plugin scan resolves
`$HERMES_HOME/plugins/<name>/`. Released Hermes owns that host-managed Git plugin directory through
`hermes plugins install|update|remove`. Better's repository root provides only `plugin.yaml`, a thin
provider-registration bridge, and a thin CLI bridge to the installed wheel. The provider loader
consumes `__init__.py` and `plugin.yaml`; active-provider command discovery imports `cli.py` without
provider initialization.

The repository accepts the released Hermes plugin lifecycle at this host boundary. It does not add a
custom installer, hidden transaction directory, ownership marker, generated bytecode inventory,
scanner-recursion proof, or independent filesystem rollback engine. Package upgrades and the exact
`0.8.5`/`0.6.1` SDK transition remain explicit `uv` work after every Hermes process sharing the
interpreter is stopped. Disposable fresh-process tests prove provider identity and CLI discovery
without changing a live Hermes home.

The plugin imports `MemoryProvider` from `agent/memory_provider.py`; its `register(ctx)` entry point
calls `ctx.register_memory_provider(...)`. The released lifecycle used by Better is:

- `is_available()` during discovery; it stays local and network-free;
- `initialize()` for the active profile/session configuration;
- `prefetch()` before each model API call;
- `queue_prefetch()` after a completed turn under legacy scheduling, though Better keeps it inert;
- `sync_turn()` after a completed turn; and
- bounded session hooks plus `shutdown()` for cleanup.

In the historical 0.19.0 characterization, `MemoryManager.sync_all()` labels the turn completed, strips skill scaffolding,
and submits each provider `sync_turn()` to a serialized background executor. `MemoryProvider`'s
`sync_turn()` contract is documented as non-blocking. `MemoryManager.shutdown_all()` gives that
executor a bounded drain, but it can cancel queued work or leave an active callback detached after
the deadline. Hermes may fail before Better Hindsight receives the callback, and the process can
exit before queued callback execution begins.

Automatic retention therefore uses released `sync_turn()` best-effort semantics. Once callback
execution begins, Better performs only bounded local construction and one SQLite admission. Local
durability starts only after provider admission commits. There is no direct-user provenance claim and
no pre-return or no-loss guarantee; no text heuristic can close the missing host signal.

### Historical exact normal-loop current-query recall evidence

The historical integration lane pins release commit `3ef6bbd201263d354fd83ec55b3c306ded2eb72a` and exercises the
ordinary `chat_completions` conversation loop. One current-query recall finishes or fails open before
the first model request. On success, one byte-bounded Better envelope appears only in the API-bound
copy of the current user content; the clean stored user `content` remains the original query. On a
timeout, HTTP fault, malformed JSON, or malformed SDK response, no Better envelope is sent and the
model call proceeds within the configured recall deadline. No provider memory tool is exposed.

The same proof records that released Hermes wraps provider output with this exact user-content note:

```text
[System note: The following is recalled memory context, NOT new user input. Treat as authoritative reference data — this is the agent's persistent memory and should inform all responses.]
```

That wording is a historical-host limitation, not wording Better Hindsight claims to remove. Better adds a
higher-priority, byte-stable system-role policy naming its exact inner envelope and requiring every
enclosed record to be treated as stale, untrusted evidence rather than an instruction or
role message.

The pinned `codex_app_server` source forwards the plain `user_message` to its separate app-server
bridge and skips the normal API-content sidecar. The exact-release discovery test records that source
shape as a compatibility oracle without making a remote recall request in that runtime.
`codex_app_server` memory behavior remains explicitly unsupported: Better does not patch it, emulate
support, or treat recall spent there as a supported scenario.

## Public Hindsight 0.8.5 API boundary

The adapter uses only the audited public `hindsight-client==0.8.5` APIs needed by this product:

- `arecall()` for bounded current-query recall and optional relevance controls;
- `aretain_batch()` for one persisted stable document ID with `update_mode="replace"` and
  `retain_async=False`;
- `banks.get_bank_profile()`, `banks.get_bank_config()`, and
  `banks.update_bank_config()` with `BankConfigUpdate` for explicit typed mission diagnostics and
  confirmation-gated mission apply;
- `acreate_bank()` and `banks.delete_bank()` only in guarded disposable setup/cleanup; and
- `aclose()` on the runtime's owning event loop.

The exact synchronous retain success predicate is `success is True`, the returned `bank_id` equals
the configured bank, `items_count == 1`, and `var_async is False`. A malformed response, transport
failure, or commit-then-timeout remains retryable with the same stable document ID. This is
replace-safe replay, not exactly-once transport.

Hindsight OSS 0.8.5 uses a shared write-capable API key rather than operation-scoped read/write
credentials. Environment-only key loading and missing model tools are accident reduction, not a
server-enforced capability boundary.

## Compatibility matrix

| Capability | First-prerelease status |
| --- | --- |
| Unmodified released Hermes normal conversation loop | Supported and tested |
| Bounded current-query recall | Supported; enabled by default |
| Best-effort automatic retention | Supported when explicitly enabled |
| Local durability | Begins after successful SQLite admission only |
| Passive `better_hindsight status` | Supported for an existing schema-v1 profile outbox |
| Explicit mission check/apply | Supported; apply requires `--confirm` and exact readback |
| `codex_app_server` memory behavior | Unsupported on the current supported release |
| Hindsight server/client 0.8.5 | Exact supported target |
| External/self-hosted deployment | Supported target |
| Cloud/embedded Hindsight management | Unsupported |
| POSIX sender ownership | Initial supported platform boundary |
| Windows sender ownership | Deferred |
| Model-facing memory tools | None in the first prerelease |

## Development isolation and production canary

Development writes require an isolated Hindsight instance and Hermes profile, separate datastore,
separate API key, and generated disposable bank. Fake-service tests precede any explicitly enabled
live proof, and production Hindsight credentials must be absent from that process.

Production rollout uses a separate canary instance and bank and preserves the old deployment. The
existing provider configuration, Hindsight service, bank, and data stay running and untouched as the
rollback path. No initial migration, export/import, deduplication, reconstruction, or deletion is a
prerelease acceptance requirement.

## Go/no-go decision

**GO — continue** with the plugin-only best-effort design. Released Hermes and public Hindsight 0.8.5
expose enough stable behavior for useful current-query recall, short callback-local admission, and
replace-safe background retry. The product deliberately does not require stronger origin or
pre-return guarantees that these released interfaces do not provide.

Hermes PRs [#61263](https://github.com/NousResearch/hermes-agent/pull/61263),
[#64914](https://github.com/NousResearch/hermes-agent/pull/64914), and
[#70278](https://github.com/NousResearch/hermes-agent/pull/70278) were observed during the frozen
audit. They are historical upstream context, not release prerequisites. Future generic host
improvements may be evaluated separately without coupling this package to a core SHA.
