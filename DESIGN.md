# Design and proof contract

## Status

Pre-alpha repository scaffold, created 2026-07-25. The repository is private during the proof and
becomes public only after the release checklist passes. The compatibility snapshot and go/no-go
record are in [docs/compatibility.md](docs/compatibility.md).

## Primary goal

Provide a reliable Hermes memory-provider boundary for an external/self-hosted Hindsight service
without requiring Hindsight-specific patches to be reapplied after every Hermes update.

The implementation should reduce recurring integration maintenance, not recreate every feature of
the bundled Hindsight provider. Better is narrower and is not universally better than official
Hermes or Hindsight.

## Architecture boundary

The project owns:

- provider-specific configuration and validation;
- the `hindsight-client==0.8.5` adapter for Hindsight server 0.8.5;
- bounded current-query recall and compact historical-evidence formatting;
- redacted immutable retain-job construction;
- profile-scoped SQLite admission, destination ownership, remote write ordering, and recovery;
- provider tools and truthful operation status; and
- compatibility tests against supported public Hermes and Hindsight contracts.

Hermes core continues to own turn classification, external-memory trust framing, provider lifecycle
invocation, and the one-active-provider policy. Generic fixes in those areas require a separate,
focused upstream-compatible patch and review; this package must not monkey-patch or vendor private
Hermes core code.

## Stable identities and scope

- Repository/distribution: `better-hermes-hindsight`
- Python package: `better_hermes_hindsight`
- Hermes provider ID: `better_hindsight`
- Initial deployment: external/self-hosted only

The provider ID intentionally differs from bundled `hindsight`, allowing configuration-only
rollback. It also avoids relying on same-name user-plugin override behavior, which differs between
Hermes releases and documentation revisions.

There is no production auto-install, provider selection, or service restart. There is no
production bank mutation. The package does not implement cloud account setup or manage an embedded
Hindsight daemon.

## Implementation order

Implementation is deliberately split into three ownership boundaries:

1. **Recall-first.** Build the typed configuration, public Hindsight 0.8.5 adapter, fake-server
   contract, and read-only current-query provider. This stage has no automatic-retain placeholder
   and makes no retention claim.
2. **Separate generic core prerequisite.** In a separately authorized Hermes branch, add generic,
   backward-compatible opt-ins for stale/untrusted historical-memory framing, typed turn origin,
   inline local durable admission, current-query prefetch scheduling, and bounded local session
   hooks. Legacy providers keep today's scheduling and `sync_turn()` behavior.
3. **Durable retain.** Only against the exact independently reviewed core prerequisite, add inline
   SQLite admission, the profile-wide sender election/outbox, stable destination-matched replay,
   and retention tooling.

In short: **recall-first → separate generic core prerequisite → durable retain**. This order prevents
provider code from silently violating released Hermes's requirement that legacy `sync_turn()` remain
non-blocking.

## Simplified lifecycle

1. Discovery and initialization are local. `is_available()` is network-free; initialization does
   not install packages, create banks, read mission configuration, or mutate remote state.
2. Before the first and every later model call, `prefetch()` recalls against the current projected
   query. Current-query recall is the only remote or potentially long-running memory work before the
   first model call. It is bounded and fail-open; failure returns no memory rather than blocking the
   turn. Better does not queue a redundant previous-query prefetch after the turn.
3. Automatic retention is eligible only for an authorized `primary` handle, explicit direct-user
   origin, completed visible user/final-assistant text, and an authoritative successful result
   owner. Synthetic/automation and `unknown` origin remain ineligible without text heuristics.
4. After successful generation, inline local SQLite admission redacts, deterministically segments,
   fingerprints, and atomically commits the immutable job before Hermes reports the turn complete.
   It performs no network call. A failed commit is reported as not admitted without changing the
   generated response.
5. SQLite is the durable FIFO shared by processes in one profile. A profile-wide POSIX advisory lock
   elects exactly one remote retention sender; other processes remain admission-only.
6. Each row has a credential-free destination fingerprint over normalized API URL, bank ID, and
   payload schema. The sender claims only matching rows. Mismatched rows remain durable and blocked
   until an operator restores or explicitly reconciles the matching destination.
7. The sender uses the persisted stable document ID with `update_mode="replace"` and synchronous
   Hindsight confirmation. Timeout, response loss, process death, or status-update failure leaves
   the same row replayable; append mode is absent.
8. Source documents are the preserved record. Facts, observations, embeddings, summaries, and other
   Hindsight indexes are derived and may be rebuilt without deleting source documents.
9. Shutdown stops new remote work, releases ownership, closes the client on its owning loop where
   possible, and leaves all unconfirmed rows for takeover or restart. It never drains remote work on
   the user-facing response path.

## Core-prerequisite boundary

Released Hermes invokes `prefetch()` before model calls, `queue_prefetch()` after turns, and
`sync_turn()` after completed turns, while documenting `sync_turn()` as non-blocking. Durable inline
admission therefore needs explicit provider timing capabilities in core rather than an overloaded
legacy hook.

The separate prerequisite owns exactly these generic changes:

- stale/untrusted historical-evidence framing instead of authoritative-instruction framing;
- an optional typed origin propagated from real `AIAgent.run_conversation()` producers through
  queues, normal finalization, and the Codex result owner, with legacy raw strings decoded as
  `unknown`;
- an opt-in inline-local admission capability after successful generation and before result return;
- an opt-in skip for obsolete after-turn prefetch when the provider recalls the current query; and
- opt-in bounded local/no-op session-boundary handling without a host executor.

Direct-user CLI, messaging gateway, TUI/Desktop, API, and ACP producers require explicit eligible
origin. Cron, batch, Kanban, delegated subagents, background/process completions, curator/review
agents, and other synthetic producers are ineligible. The frozen source inventory and exact line
references are in [docs/compatibility.md](docs/compatibility.md).

## Proof acceptance gates

Promotion from `spike/local-external-provider` requires all of the following:

- A fresh temporary `HERMES_HOME` discovers and selects only `better_hindsight`.
- The exact released Hermes, effective local revision, current upstream observation, and Hindsight
  0.8.5 baseline remain documented and rechecked.
- The first turn recalls against the current query, not a previous query.
- Recall is the sole potentially long-running pre-LLM memory operation and fails open within a
  documented bounded deadline.
- Automatic context is compact, bounded, provenance-aware historical evidence and never an
  instruction source.
- Explicit origin reaches normal and Codex result paths from real direct-user and synthetic
  producers; `unknown`, synthetic, incomplete, failed, and interrupted turns are not admitted.
- Inline local admission commits every eligible redacted segment before turn completion and makes no
  network call.
- Concurrent processes may admit, while only the profile advisory-lock owner sends or recovers rows.
- Destination mismatch blocks rather than misroutes; replay keeps the same document ID and replace
  mode until typed success.
- Disposable Hindsight 0.8.5 proof shows repeated/crash replay leaves one source document and no
  extra active derived units.
- `shared` is present in retain requests only after single-principal/exclusive-bank proof.
- Retain, reflect, and observation missions reach distinct API fields under read-only-by-default
  policy.
- Score floors affect automatic and explicit recall consistently.
- Session switches and reused session IDs cannot claim or append stale automatic memory; unsupported
  same-ID rewind fails closed and requires `/new`.
- Existing bank/document formats need no migration for read compatibility, and source documents are
  never automatically rebuilt, deduplicated, deleted, or rewritten.
- Switching back to bundled `hindsight` requires only package/configuration rollback and preserves
  bank/outbox data.
- Package, lint, typing, unit, integration, disposable, fresh-install, security, and independent
  review gates pass.
- Public-facing files contain no private paths, endpoints, bank names, principal identifiers, memory
  content, transcripts, databases, or logs.

## Preservation and production-write gate

No production write is part of this proof. Before separate authorization, operators must complete
the storage snapshot, full logical bank export, baseline counts/hashes, disposable restore and
reconciliation, stable replace/replay, destination-mismatch, and configuration rollback checklist in
[docs/compatibility.md](docs/compatibility.md). The project must not rebuild, deduplicate,
reconsolidate, delete, or rewrite a legacy bank to make the proof pass.

## Stop and retirement conditions

Stop the custom-provider approach if:

- released bundled Hermes plus configuration satisfies the complete current-query relevance and
  durable replay contract;
- the proof starts reproducing most cloud, embedded, installer, setup-wizard, or bank-control
  surfaces of the bundled provider; or
- the pinned public API cannot prove destination-matched stable replace/replay without source loss.

At that point, upstream only the generic core fixes or reduce this project to a thin compatibility
shell. The current decision is to continue; the dated rationale and still-open named Hermes PRs are
recorded in [docs/compatibility.md](docs/compatibility.md).

## Runtime isolation

Proof work uses disposable resources:

- a temporary virtual environment;
- a temporary `HERMES_HOME` and profile;
- a deterministic fake Hindsight HTTP service; and
- a separately named disposable live test bank only after fake-server tests pass.

The active Hermes configuration, gateway, bundled provider, live Hindsight service, and production
bank are outside this repository's proof workflow.

## Publication policy

No automated release workflow is present during the proof. Before publication, complete
[docs/public-release-checklist.md](docs/public-release-checklist.md), select a supported version
matrix, add verified install instructions, complete preservation rehearsal, and cut an explicit
pre-release rather than publishing from every main-branch commit.
