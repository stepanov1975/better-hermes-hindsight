# Operations

## Model-facing tools

- `better_hindsight_recall(query)` performs bounded recall through the configured authorized path.
  It returns a structured `memories` list with a fixed untrusted-history label and without internal
  ranking scores or source-identifier counts.
- `better_hindsight_reflect(query)` performs one default-off, deadline-bounded synthesis through the
  configured authorized bank. The caller supplies only the query; bank, budget, final-answer target,
  tags, and tag mode remain local. It returns one redacted, byte-bounded reflection record inside the
  untrusted historical-evidence envelope, or one sanitized unavailable result. Reflection is
  read-only for bank memory but invokes Hindsight's configured LLM and may create audit/usage records.
  A local timeout does not guarantee backend model cancellation or refund work/cost already incurred.
- `better_hindsight_retain(content, context?)` durably admits one agent-selected memory to the local
  outbox when retention is enabled for an authorized primary handle. Its canonical acknowledgement is
  `queued_locally` or `already_queued`; neither claims remote delivery. Model input is capped before
  construction at 8,192 content characters, 256 context characters, and 2,000 canonical segments.
- `better_hindsight_status()` returns compact passive outbox health. Healthy output contains queue
  state and total queued work; degraded output adds only actionable nonzero counts and relevant state.
  It performs no remote call. Full diagnostics remain operator-only.

The reflect and retain tools accept no per-call bank, policy, timeout, or delivery controls beyond
their declared inputs. Exact diagnostic replay and mission changes remain operator-only.

## Status

```bash
hermes better_hindsight status
```

Status passively inspects an existing schema-v1 outbox. It does not initialize or migrate schemas,
perform application-owned queue recovery, write rows, claim, retry, drain, or delete work. An absent
outbox is reported as `uninitialized`; a non-regular database or malformed sidecar topology produces
the fixed `status_unavailable` error.

Inspection uses SQLite URI read-only mode rather than an ordinary writable database open. A
sidecar-free database is read as an immutable snapshot so inspection creates no SQLite files. When an
active WAL and SHM pair exists, status uses SQLite's read-only WAL path so committed WAL state remains
visible; SQLite may update the existing SHM coordination state during that read. The command assumes
the supported personal Linux/POSIX deployment: one trusted local operator and a stable outbox
pathname and sidecar topology for the short inspection. It is a passive operational snapshot, not a
forensic snapshot across concurrent pathname or topology replacement.

The result includes queue counts, logical queued bytes, oldest-item age bucket, per-category error
counts, maximum attempt count, next-retry bucket, plugin identity, and a point-in-time
sender-ownership probe. It reports `result: "degraded"` and exits 1 for a destination mismatch,
retrying or sending work, first-attempt work aged at least one hour, or due work when sender
ownership cannot be probed. `sender_ownership: "held"` is only a lock snapshot, not a sender
heartbeat.

A destination mismatch normally means the endpoint, bank, payload schema, tags, or observation scopes changed while rows remained queued. Restore the original configuration to let those rows drain, or preserve the outbox and perform a separately reviewed manual recovery. Better never re-targets or deletes them automatically.

## Delivery and retry

The Hermes `sync_turn()` callback performs redaction, deterministic segmentation, and one bounded SQLite admission. It makes no Hindsight request. Local durability starts when that transaction commits.

A process-shared sender claims current v2 destination rows plus the exact compatible legacy v1
schema/fingerprint pair. A pre-v2 sender cannot claim timestamp-bearing v2 rows. The upgraded sender
sends each persisted segment with its stable document ID, `update_mode="replace"`, and synchronous
confirmation; legacy v1 rows retain their null occurrence timestamp. Failed, timed-out, or unconfirmed
attempts remain durable and are rescheduled with capped exponential backoff.

A timeout may mean the server committed after the caller deadline. Stable replace-mode identity makes replay safe for the source document, but delivery is not exactly once.

## Structured diagnostics

Recall, reflection, local retention admission, each internal HTTP operation, client lifecycle, remote
sender attempts, and sender-loop failures emit compact JSON through normal Python logging. HTTP events record
only a fixed operation/outcome, elapsed milliseconds, response byte count, and numeric status when a
response existed. Fixed outcomes distinguish connection, TLS, DNS, timeout, redirect, authentication,
rate-limit, server-status, content-type, malformed-JSON, size-limit, schema, cancellation, and
closed-session failures. Sender and recall events preserve the same fixed reason when available.

Events never include queries, recalled text, turn content, document IDs, tags, bank names, endpoints,
principal identifiers, exception text, response bodies, headers, or credentials. The principal event
names are `better_hindsight.http_request`, `better_hindsight.client_lifecycle`,
`better_hindsight.recall`, `better_hindsight.reflect`, `better_hindsight.recall_diagnostic`,
`better_hindsight.admission`, `better_hindsight.sender_attempt`, and
`better_hindsight.sender_loop`.

### Replayable slow-recall capture

When `diagnostics.enabled=true`, Better additionally stores the exact bounded projected query and
credential-free recall request for successful recalls at or above the configured slow threshold and
for failed recalls. This private store is intentionally separate from structured logs: the directory
is mode 0700, records are mode 0600, ordinary list output contains only a query hash and timings, and
the oldest records are removed when the fixed record count is exceeded. Capture uses a bounded,
single-writer daemon queue so slow filesystem I/O cannot extend the recall deadline. If that queue is
full, the process exits before it drains, or a private record write fails, the diagnostic is
best-effort and may be lost; `better_hindsight.recall_diagnostic` reports only the fixed
`write_failed` outcome.

```bash
hermes better_hindsight diagnostics list
hermes better_hindsight diagnostics replay <record-id>
```

Replay sends the captured query and request to the currently configured endpoint and bank, but only
when the captured credential-free request still exactly matches the current typed recall policy; a
policy change rejects the replay before transport rather than broadening an old request. It forces
Hindsight's existing `trace=true` response and saves/prints only allowlisted numeric phase durations,
numeric or boolean phase details, collection counts, total duration, and result count. Candidate IDs,
candidate content, recalled text, and the query are excluded from command output. An unreadable or
corrupt diagnostic store is reported as unavailable rather than as an empty successful list. Replay is
read-only but creates normal recall load. A timeout can be captured exactly but cannot expose server
phases until a later trace-enabled replay completes; this is the unavoidable plugin-only limit.

## External end-to-end canary

`hermes better_hindsight canary` is an explicit synthetic write/read/delete check for a
fixed canary bank. It accepts only supported Hindsight API versions (`0.8.5`, `0.9.1`, and
`0.9.2`) and reports the exact observed version, then uses the installed
`HindsightClientAdapter` for synchronous retention and recall. That exercises the production
`aiohttp` transport, wire defaults, strict response decoding, and client lifecycle rather than a
parallel canary implementation. Raw bounded HTTP remains only for health/version preflight and exact
document cleanup, which are outside Better's narrow adapter surface. Cleanup is attempted after every
retain dispatch and has reserved time within one overall deadline. Output is one bounded JSON object
containing fixed error categories, fixed adapter reasons, and numeric timing only.

The command is inert unless `BETTER_HINDSIGHT_CANARY_ENABLED=1`. Configure only a reviewed
canary/dedicated bank; do not point it at an ordinary memory bank. Supported environment variables:

```text
BETTER_HINDSIGHT_CANARY_ENABLED=1
BETTER_HINDSIGHT_CANARY_API_URL=https://canary.example.invalid
BETTER_HINDSIGHT_CANARY_BANK_ID=dedicated-canary-bank
BETTER_HINDSIGHT_CANARY_API_KEY=<from secret environment, optional>
BETTER_HINDSIGHT_CANARY_DEADLINE=15
BETTER_HINDSIGHT_CANARY_POLL_INTERVAL=0.5
BETTER_HINDSIGHT_CANARY_MAX_POLLS=20
```

This repository does not install or schedule the canary. Production activation and its destination
remain separate operational changes requiring authorization.

## Low-noise alert evaluator

`hermes better_hindsight watchdog` accepts three bounded files: the latest status and
canary JSON objects plus JSONL containing only newly collected structured events. It alerts on degraded
local status, each new sender retention failure, adapter contract failure, sender-loop failure, or
client lifecycle failure; configurable rolling recall timeout and non-timeout error rates; or failed
E2E canary. Ordinary transient HTTP outcomes remain discoverable in structured logs and become alerts
through recall, retention, or canary failure instead of paging independently on every request. State
contains only fixed aggregate recall outcomes and the last active persistent reason set, is written
mode 0600, and is bounded. Healthy unchanged state produces no output. Persistent alert transitions
produce one JSON line and exit 1; unchanged state is silent; recovery produces one JSON line and exits
0. If a persistent condition recovers during a new edge alert, the same line includes fixed
`resolved_reasons`. Malformed evaluator input emits fixed `evaluation_failed` JSON and exits 2.

Example evaluator invocation after a caller has atomically produced bounded artifacts:

```bash
hermes better_hindsight watchdog \
  --status-json /run/better-hindsight/status.json \
  --canary-json /run/better-hindsight/canary.json \
  --events-jsonl /run/better-hindsight/new-events.jsonl \
  --state /var/lib/better-hindsight/watchdog.json \
  --recall-error-rate 0.2 \
  --recall-timeout-rate 0.2
```

The caller owns event collection/cursoring and must pass each event at most once. The evaluator is not
a log scraper and this repository does not install a timer or notification transport.

For production, collect new JSON events by a persistent journal cursor and evaluate them with status
at least every five minutes. Run the adapter-backed full canary daily in a dedicated bank. Treat any
reported Hindsight version change as a compatibility review trigger: compare the four used OpenAPI
operations, rerun fake-service contracts, then run the isolated live proof before accepting the new
wire contract. Recording every fixed failure does not imply paging on every transient timeout.

## Restart and shutdown

The elected sender resets stale `sending` rows after acquiring ownership and retries them. Provider finalization stops new work, settles the sender and async client within a bounded deadline, and closes the outbox only after settlement. If shutdown reports failure, preserve the process state and outbox for diagnosis rather than editing SQLite manually.

## Missions

```bash
hermes better_hindsight missions check
hermes better_hindsight missions apply --confirm
```

`check` performs a read and reports `equal`, `drift`, or `missing`. `apply --confirm` patches only configured drifted mission fields and requires an exact GET readback before reporting success. Mission commands use a client-only runtime and never start the retention sender.

There is no automatic mission application, retry-now, drain, arbitrary-row, row-deletion, or bank-selection command.

## Live validation

Use fake-service tests first. The opt-in procedure in [development-instance](development-instance.md) targets only an isolated Hindsight environment and synthetic content. If cleanup fails, retain the reported generated bank identifier for manual cleanup; never infer or delete a resource in the existing production deployment.
