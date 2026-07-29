# Operations

Better Hermes Hindsight is still pre-alpha. Sender delivery is implemented and covered by deterministic
fake-service and released-Hermes tests, but retention remains disabled by default. Managed installation
and isolated live-write proof remain incomplete. Do not enable this repository in a production Hermes
profile.

<!-- better-hindsight-status-storage:start -->
## Status storage contract

- **Active WAL.** When WAL exists, status requires a pre-existing regular SHM file and uses
  SQLite `mode=ro&vfs=unix` with `PRAGMA query_only=ON` and one read transaction. SQLite may
  initialize, recover, resize, or otherwise change contents, size, atime, mtime, and ctime only
  on the same pre-existing regular SHM inode. Its inode, type, link count, mode, UID, GID, and
  xattrs/ACL xattrs remain unchanged.
- **Byte and lock effects.** Status issues no database, WAL, profile-lock, or row-byte writes.
  The point-in-time sender probe may acquire and release a transient kernel `flock` without
  changing lock-file bytes. An authorized writer may change database or WAL bytes and timestamps
  during the read; those external changes are not attributed to status.
- **Sidecar-free snapshot.** When WAL, SHM, and rollback journal are all absent, status uses
  `mode=ro&immutable=1&vfs=unix`, requires the main-file identity/size/mtime/ctime to remain
  unchanged, and requires all three sidecars to remain absent. Missing SHM is not an error in the
  all-sidecars-absent branch.
- **Malformed topology.** If WAL exists but SHM is missing, status fails before SQLite opens and
  creates nothing. A pre-existing rollback journal or SHM without WAL is unavailable. Active WAL
  never uses `immutable=1`.
- **Trusted topology.** Supported concurrency assumes stable file identities and journal mode.
  Observable same-principal races return `status_unavailable` when detected, but raced-path effects
  and undetectable ABA are not prevented; status is not safe against hostile same-UID replacement.
  This is not a zero-mutation claim because SQLite may change the derived SHM as described above.
<!-- better-hindsight-status-storage:end -->

## Delivery boundary

Released Hermes schedules the provider's `sync_turn()` callback on its serialized background executor.
That callback performs redaction, deterministic segmentation, and one bounded SQLite admission only; it
neither calls Hindsight nor waits for remote delivery. Local durability begins after the complete turn's
admission transaction commits. A callback that never runs or an admission that fails is outside that
guarantee.

A retain-enabled process eagerly starts one sender. A profile-wide POSIX advisory lock elects the only
process allowed to recover stale `sending` rows, claim work, complete confirmed rows, or reschedule
failures. Non-owner processes may still admit rows. Bounded cross-process polling lets the lock owner
observe those admissions without a cross-process event bus.

The owner claims only rows whose destination fingerprint and payload schema match its current
configuration. The fingerprint binds normalized endpoint and bank identity, the payload schema,
canonical redacted/sorted retain tags, and normalized observation scopes; it excludes credentials and
timing settings. A policy mismatch leaves the old row durable and unclaimed rather than replaying it
under new transport policy.

## Confirmation and retry

Each attempt sends the persisted stable document ID and exact segment content with `update_mode="replace"`
and synchronous retention. Deletion requires typed confirmation of the expected bank, exactly one item,
`success is True`, and a non-async response. This is replace mode with deterministic identity, not
exactly-once transport.

Unconfirmed work stays durable:

- `retain_timeout` means the caller deadline crossed; remote completion may be ambiguous;
- `retain_failed` means the SDK/HTTP operation or another remote attempt failed; and
- `retain_unconfirmed` means a well-formed response did not satisfy the exact confirmation predicate.

The sender increments `attempt_count` before network I/O and reschedules from completion wall time with
deterministic capped exponential backoff. A timeout may therefore replay a request that committed
remotely. Stable replace-mode IDs make that replay safe for the preserved source document, but they do
not provide a zero-loss or exactly-once guarantee.

## Recovery and shutdown

After acquiring the profile lock, a new owner atomically resets stale `sending` rows to immediately due
`pending` while preserving attempts and the prior fixed error category. This is safe only after exclusive
lock acquisition proves the former owner no longer holds sender ownership.

Finalization blocks new runtime work, signals the sender, and uses one code-owned deadline covering an
active attempt, bounded SQLite work, sender join, and cancellation-resistant SDK settlement. It closes
the outbox, async client, and runner only after both sender and shared runner are settled. If settlement
exceeds the deadline, finalization raises fixed `SenderStopError`, keeps the same runtime unavailable,
and closes none of those resources. A later finalization retries cleanup after settlement.

## Current operator boundary

`hermes better_hindsight status` passively reads only an existing schema-v1 outbox and probes only an
existing sender lock. It reports an exclusive queue partition, logical queued bytes, age bucket,
fixed current-row error category, and point-in-time lock state; it never initializes, recovers, claims,
reschedules, deletes, retries, or drains work. An absent outbox is reported as `uninitialized` and
creates nothing.

`hermes better_hindsight missions check` performs one typed remote read.
`hermes better_hindsight missions apply --confirm` changes only configured drifted allowlisted fields,
sends at most one PATCH, and requires exact PATCH response and fresh readback. Both require the
explicit `single_principal=true` assertion and use a client-only runtime that cannot start the
retention sender. There is still no retry-now, drain, arbitrary-row, row-deletion, or automatic mission
policy command. Do not edit the SQLite outbox manually: doing so can violate immutable identity,
capacity, and guarded-attempt invariants.

Use the repository test suite and a temporary `HERMES_HOME` with the loopback fake before any separately
authorized isolated deployment. Any future live proof must use a disposable development instance,
credential, and bank; production rollout remains a later reversible canary decision.
