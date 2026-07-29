# Operations

Better Hermes Hindsight is still pre-alpha. Sender delivery is implemented and covered by deterministic
fake-service and released-Hermes tests, but retention remains disabled by default. Managed installation
and isolated live-write proof remain incomplete. Do not enable this repository in a production Hermes
profile.

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

Operator-visible queue counts and management commands remain Task 4. This checkpoint intentionally
adds no queue CLI, retry-now command, row deletion command, mission command, or automatic policy
migration. Do not edit the SQLite outbox manually: doing so can violate immutable identity, capacity,
and guarded-attempt invariants.

Use the repository test suite and a temporary `HERMES_HOME` with the loopback fake before any separately
authorized isolated deployment. Any future live proof must use a disposable development instance,
credential, and bank; production rollout remains a later reversible canary decision.
