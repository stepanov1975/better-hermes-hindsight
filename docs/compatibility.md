# Compatibility and preservation contract

> **Implementation precedence notice:** Version/source evidence in this document remains useful,
> but its core-prerequisite, structured-origin, inline-admission, and rollout conclusions predate the
> best-effort rescope. Until active-plan Task 0 rewrites them, they must not direct implementation.
> Read tracked [IMPLEMENTATION.md](../IMPLEMENTATION.md) and only the canonical active plan it names.

This document freezes the public-safe compatibility baseline inspected on 2026-07-26 UTC.
It records source revisions and aggregate evidence only: no endpoint, credential, bank identity,
principal identifier, transcript, memory text, database, log, or local checkout path belongs here.

## Product boundary

Better Hermes Hindsight registers only the distinct provider ID `better_hindsight`; bundled
`hindsight` remains the configuration-only rollback target. The first prerelease is for
external/self-hosted Hindsight only. It does not implement cloud setup or supervise an embedded
server.

“Better” is deliberately narrower: it means better for this documented external/self-hosted use
case and its proof criteria. It is not universally better than official Hermes or Hindsight.
Only one external Hermes memory provider may be active at a time.

## Frozen version baseline

| Surface | Frozen evidence | Project contract |
| --- | --- | --- |
| Released Hermes | `v2026.7.20`, package 0.19.0, tag commit `3ef6bbd201263d354fd83ec55b3c306ded2eb72a`; published 2026-07-20 | Release compatibility baseline |
| Effective audited checkout | Clean `main` at `41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f`; installed package 0.19.0 and installed hindsight-client 0.6.1 | Exact behavior baseline; location intentionally omitted |
| Cached upstream ref in that checkout | `origin/main` at `4dae897265f09ed5b26f5e02b0f0fcb1325e0b6d`; the checkout was ahead by 9 and behind by 1,416 | The counts are against this cached ref, not the later remote observation |
| Current upstream observation | `NousResearch/hermes-agent` `main` at `eb52760564dbba2e5971fa54bd67384e281cd3b8`, observed 2026-07-26 UTC | Recheck at Tasks 11 and release time |
| Hindsight | Release `v0.8.5`, commit `705757f362552918dfb0242906cb8466de320378` | Hindsight server 0.8.5 and `hindsight-client==0.8.5` are the exact initial target |
| Python | 3.11, 3.12, and 3.13 | All three remain release gates |

The nine carried checkout commits, newest first, are part of the exact local comparison:

1. `41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f` — `fix(gateway): retry restart completion delivery`
2. `821b11e631e5f663f2e9f915f77a353d1c528cbc` — `test(hindsight): complete synchronous recall coverage`
3. `d48bcb0d400bf852499758080f50ae49ed857626` — `feat(hindsight): opt-in synchronous recall for the current turn (#5820)`
4. `151a1b4f6045c86577806feb55909ed33e608752` — `fix(memory): sync Hindsight bank missions`
5. `0e4f1ba7e017aac4d3c0995be941c5ce6364a17b` — `fix(cron): preserve named Telegram DM topic targets`
6. `fb8cc28775bc247eacc964d2c5d5d88830152adf` — `fix(agent): make memory shutdown concurrency-safe`
7. `517e0bcf0511564eec0290037e2a8d0ff3f1c895` — `chore: update author mapping`
8. `fcb1f9da2bb5f38f6270137fce13d051449cbcc5` — `fix: move shutdown_memory_provider before _session_messages clear`
9. `2b1d2bafc3e4f13ed8b961b6c81effccd7c066bd` — `fix(agent): call shutdown_memory_provider() in AIAgent.close() (#46082)`

The carried synchronous-recall work proves a useful comparison point, but it does not add the
profile SQLite admission/outbox and destination-safe replay required by this project.

## Hermes loader and lifecycle contract

The released specialized loader is `plugins/memory/__init__.py`. The canonical plugin bundle is
`plugins/memory/<name>/{__init__.py,plugin.yaml,README.md}`; the released user-plugin scan resolves
`$HERMES_HOME/plugins/<name>/`. Better's later managed installer must place only a marked,
owned shim at the path actually used by the tested Hermes revision. It must never shadow bundled
`hindsight`.

The plugin imports `MemoryProvider` from `agent/memory_provider.py`; its `register(ctx)` entry point
calls `ctx.register_memory_provider(...)`. The public lifecycle used by Better is:

- `is_available()` during discovery; it must be network-free and must not install anything;
- `initialize()` once for the active session/profile contract;
- `prefetch()` before each model API call;
- `queue_prefetch()` after a completed turn under legacy scheduling;
- `sync_turn()` after a completed turn;
- bounded session hooks and `shutdown()` for lifecycle cleanup.

Released documentation says **`sync_turn()` MUST be non-blocking**. Better therefore cannot simply
make legacy `sync_turn()` perform an inline SQLite commit. A separate, backward-compatible generic
Hermes core prerequisite must add an opt-in inline-local durable-admission capability while leaving
legacy providers on their existing non-blocking scheduling. That prerequisite also owns structured
turn origin and stale/untrusted historical-memory framing.

## Public Hindsight 0.8.5 API boundary

The adapter may use only public `hindsight-client==0.8.5` APIs needed by the contract:

- `arecall()` for bounded current-query recall and documented relevance controls;
- `aretain_batch()` for one immutable admitted segment, with its persisted document ID,
  `update_mode="replace"`, and `retain_async=False`;
- `banks.get_bank_profile()`, `banks.get_bank_config()`, and
  `banks.update_bank_config()` with `BankConfigUpdate` for post-first-turn diagnostics and an
  explicit operator-only apply path;
- `acreate_bank()` and `banks.delete_bank()` only in guarded disposable setup/cleanup;
- `aclose()` on the runtime's owning event loop.

Normal runtime does not create, reset, delete, migrate, deduplicate, or rewrite a bank. It does not
inspect private client attributes or dynamically install/upgrade the SDK.

## `run_conversation()` caller inventory

This is the run_conversation() caller inventory used by the origin prerequisite.

Line references below are path-relative to the effective checkout at
`41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f`. A source path is evidence, not authorization: the
separate core prerequisite must carry an explicit typed origin from the real producer through
queues and result owners. Better automatic admission accepts only an eligible direct-user origin,
an authorized `primary` handle, and an authoritatively completed visible turn. Synthetic/automation
and `unknown` origin are ineligible.

### Supported direct-user producers

| Source evidence | Classification | Admission rule |
| --- | --- | --- |
| `cli.py:12086` | direct-user interactive CLI submit | Eligible only when the interactive input owner explicitly marks direct-user origin |
| `gateway/run.py:21026` | direct-user messaging-gateway inbound turn | Eligible only for a real user message; restart, completion, cron, and other injected envelopes remain synthetic/automation |
| `tui_gateway/server.py:10090` | direct-user TUI/Desktop foreground submit | Eligible only when the foreground RPC producer supplies direct-user origin |
| `gateway/platforms/api_server.py:4680` and `gateway/platforms/api_server.py:4979` | direct-user API run surfaces | Eligible only when the authenticated request boundary explicitly identifies the message as a direct user turn |
| `acp_adapter/server.py:1513` | direct-user ACP prompt | Eligible only when the ACP request boundary supplies direct-user origin |

### Synthetic, automation, mixed, and disabled producers

| Source evidence | Classification | Admission rule |
| --- | --- | --- |
| `cli.py:15522` | synthetic/automation Kanban goal-loop continuation | Ineligible |
| `gateway/run.py:14821` | synthetic/automation gateway background task | Ineligible |
| `tui_gateway/server.py:11117` and `tui_gateway/server.py:11229` | synthetic/automation background prompt and preview-restart agents | Ineligible |
| `hermes_cli/cli_commands_mixin.py:1698` | synthetic/automation CLI background task | Ineligible |
| `batch_runner.py:349` | synthetic/automation batch trajectory producer | Ineligible (and currently constructs the agent with memory skipped) |
| `tools/delegate_tool.py:2005` | synthetic/automation delegated subagent | Ineligible |
| `agent/curator.py:1955` and `agent/background_review.py:851` | synthetic/automation review agents | Ineligible |
| `tui_gateway/compute_host.py:315` and `scripts/tool_search_livetest.py:397` | synthetic test/spike producers | Ineligible |
| `cli.py:15979` and `hermes_cli/oneshot.py:442` | mixed non-interactive CLI surfaces used by humans, cron, SSH, subprocess, and workers | `unknown` and ineligible unless a future explicit producer contract distinguishes a direct user; Kanban/automation remains ineligible |
| `run_agent.py:6469` and `run_agent.py:6654` | generic convenience/example entry points with no proven ingress owner | `unknown` and ineligible |
| `plugins/platforms/feishu/feishu_comment.py:1089` | comment-triggered plugin-local agent | Automatic memory is explicitly disabled there; any future enablement needs its own reviewed origin proof |

Any unlisted caller, legacy raw string, lost queue envelope, or absent origin decodes to `unknown` and
is ineligible. Text markers, platform names, `agent_context`, and the mere fact that a call reaches a
foreground path must never be used to guess human origin.

## Lifecycle and preservation invariants

1. Current-query recall is the only remote or potentially long-running memory work before the first
   model call. It is bounded and fail-open.
2. After successful generation, the only inline memory work is short local SQLite admission. Every
   redacted immutable segment is committed before Hermes reports the turn complete; this path makes
   no network call.
3. One profile-wide POSIX advisory lock elects the sole remote sender across processes. All eligible
   processes may admit rows, but only the lock holder may recover, claim, retain, or complete them.
4. Every row carries a credential-free destination fingerprint over normalized URL, bank ID, and
   payload schema. A sender claims only matching rows; mismatches remain durable and blocked.
5. Replay reuses the stable document ID with `update_mode="replace"` until typed synchronous success.
   Append mode is not used, and disposable proof must establish idempotence before production writes.
6. Source documents are the preserved record. Facts, observations, embeddings, summaries, and other
   indexes are derived and may be rebuilt without deleting source documents.
7. There is no production auto-install, provider selection, service restart, or production bank
   mutation. Every production write requires separate authorization after the gates below.

The implementation order is **recall-first → separate generic core prerequisite → durable retain**.
Recall work must not claim retention; durable retention must not ship by violating the released
non-blocking provider contract.

## Production preservation checklist

Document and rehearse this sequence against a disposable clone; do not automate it:

- [ ] **Storage snapshot:** take the deployment's storage/VM/database snapshot and record the
      restore point.
- [ ] **Logical bank export:** export bank configuration plus the complete document-transfer
      artifact, including observations, and record its checksum.
- [ ] **Baseline counts and hashes:** record document/raw-unit/observation/operation counts,
      stable document IDs with source-content hashes, policy hashes, and export checksum.
- [ ] **Disposable restore proof:** restore/import into a disposable or cloned bank and reconcile
      counts, IDs, content hashes, and checksums before any Better write/replay test.
- [ ] **Replay and cutover proof:** on the clone, prove stable replace/replay, owner takeover,
      destination-mismatch blocking, and configuration-only rollback to bundled `hindsight`.
- [ ] **Rollback before any production write:** write and verify the exact provider/config/package
      rollback, require zero unreconciled old-destination rows, and obtain separate production-write
      authorization.

No step rebuilds, deduplicates, reconsolidates, deletes, or rewrites the existing production bank.

## Go/no-go decision

**GO — continue**, as of 2026-07-26. Released Hermes `v2026.7.20` plus bundled-provider
configuration still cannot satisfy the complete current-query relevance and durable replay contract.
The effective local fork adds synchronous current-query recall, but not inline durable SQLite
admission or destination-safe replay.

Hermes PRs [#61263](https://github.com/NousResearch/hermes-agent/pull/61263) (client 0.8.5),
[#64914](https://github.com/NousResearch/hermes-agent/pull/64914) (recall controls), and
[#70278](https://github.com/NousResearch/hermes-agent/pull/70278) (current-query recall) are still
open. They supply no durable SQLite admission/replay. Recheck released Hermes, current upstream,
and these PRs before each later gate; stop or delete redundant work if a released upstream contract
becomes equivalent.
