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

The result includes queue counts, logical queued bytes, oldest-item age bucket, last fixed error category, and a point-in-time sender-ownership probe. Destination-mismatched rows produce `result: "degraded"` and exit status 1 because the sender cannot safely deliver them under the current configuration.

A destination mismatch normally means the endpoint, bank, payload schema, tags, or observation scopes changed while rows remained queued. Restore the original configuration to let those rows drain, or preserve the outbox and perform a separately reviewed manual recovery. Better never re-targets or deletes them automatically.

## Delivery and retry

The Hermes `sync_turn()` callback performs redaction, deterministic segmentation, and one bounded SQLite admission. It makes no Hindsight request. Local durability starts when that transaction commits.

A process-shared sender claims only rows matching its destination fingerprint. It sends each persisted segment with its stable document ID, `update_mode="replace"`, and synchronous confirmation. Failed, timed-out, or unconfirmed attempts remain durable and are rescheduled with capped exponential backoff.

A timeout may mean the server committed after the caller deadline. Stable replace-mode identity makes replay safe for the source document, but delivery is not exactly once.

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
