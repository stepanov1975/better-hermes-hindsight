# Operations

## Status

```bash
hermes --profile <profile> better_hindsight status
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
counts, maximum attempt count, next-retry bucket, installed package identity, and a point-in-time
sender-ownership probe. It reports `result: "degraded"` and exits 1 for a destination mismatch,
retrying or sending work, first-attempt work aged at least one hour, or due work when sender
ownership cannot be probed. `sender_ownership: "held"` is only a lock snapshot, not a sender
heartbeat.

A destination mismatch normally means the endpoint, bank, payload schema, tags, or observation scopes changed while rows remained queued. Restore the original configuration to let those rows drain, or preserve the outbox and perform a separately reviewed manual recovery. Better never re-targets or deletes them automatically.

## Delivery and retry

The Hermes `sync_turn()` callback performs redaction, deterministic segmentation, and one bounded SQLite admission. It makes no Hindsight request. Local durability starts when that transaction commits.

A process-shared sender claims only rows matching its destination fingerprint. It sends each persisted segment with its stable document ID, `update_mode="replace"`, and synchronous confirmation. Failed, timed-out, or unconfirmed attempts remain durable and are rescheduled with capped exponential backoff.

A timeout may mean the server committed after the caller deadline. Stable replace-mode identity makes replay safe for the source document, but delivery is not exactly once.

## Structured diagnostics

Recall, local retention admission, remote sender attempts, and sender-loop failures emit compact JSON
through normal Python logging. Events contain only fixed categories and bounded numeric metadata:
elapsed milliseconds, result/segment counts, byte counts, attempt count, and retry delay. They never
include queries, recalled text, turn content, document IDs, tags, bank names, endpoints, principal
identifiers, or credentials. The principal event names are `better_hindsight.recall`,
`better_hindsight.admission`, `better_hindsight.sender_attempt`, and
`better_hindsight.sender_loop`.

## External end-to-end canary

`python -m better_hermes_hindsight.canary` is an explicit synthetic write/read/delete check for a fixed canary bank.
It verifies Hindsight 0.8.5 health and version, synchronously retains one random synthetic document,
polls recall until the exact document ID, tag, and marker are visible, then validates exact-document
cleanup. Cleanup is attempted after every retain dispatch and has reserved time within one overall
deadline. Output is one bounded JSON object containing fixed error categories and numeric timing only.

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

`python -m better_hermes_hindsight.watchdog` accepts three bounded files: the latest status and
canary JSON objects plus JSONL containing only newly collected structured events. It alerts on degraded local
status, each new sender retention failure, a configurable rolling recall-timeout rate, or failed E2E
canary. State contains only fixed aggregate outcomes and the last active reason set, is written mode
0600, and is bounded. Healthy unchanged state produces no output; an alert transition produces one
JSON line and exit 1; unchanged alert state is silent; recovery produces one JSON line and exit 0.
Malformed evaluator input emits fixed `evaluation_failed` JSON and exits 2.

Example evaluator invocation after a caller has atomically produced bounded artifacts:

```bash
python -m better_hermes_hindsight.watchdog \
  --status-json /run/better-hindsight/status.json \
  --canary-json /run/better-hindsight/canary.json \
  --events-jsonl /run/better-hindsight/new-events.jsonl \
  --state /var/lib/better-hindsight/watchdog.json
```

The caller owns event collection/cursoring and must pass each event at most once. The evaluator is not
a log scraper and this repository does not install a timer or notification transport.

## Restart and shutdown

The elected sender resets stale `sending` rows after acquiring ownership and retries them. Provider finalization stops new work, settles the sender and async client within a bounded deadline, and closes the outbox only after settlement. If shutdown reports failure, preserve the process state and outbox for diagnosis rather than editing SQLite manually.

## Missions

```bash
hermes --profile <profile> better_hindsight missions check
hermes --profile <profile> better_hindsight missions apply --confirm
```

`check` performs a read and reports `equal`, `drift`, or `missing`. `apply --confirm` patches only configured drifted mission fields and requires an exact GET readback before reporting success. Mission commands use a client-only runtime and never start the retention sender.

There is no automatic mission application, retry-now, drain, arbitrary-row, row-deletion, or bank-selection command.

## Live validation

Use fake-service tests first. The opt-in procedure in [development-instance](development-instance.md) targets only the existing isolated Hermes/Hindsight environment and synthetic content. If cleanup fails, retain the reported generated bank identifier for manual cleanup; never infer or delete a resource in the existing production deployment.
