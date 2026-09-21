# Rollback

Better Hermes Hindsight uses Hermes's normal memory-provider selection and plugin lifecycle. Roll
back by selecting another provider; no Python environment or package repair is required.

## Preserve

Keep:

- the Better release/tag used;
- the Better SQLite outbox;
- the Better Hindsight bank; and
- the original bundled-Hindsight bank and deployment.

Do not copy credentials, private bank names, principal identifiers, memories, or transcripts into
rollback notes.

## Select the bundled provider

```bash
hermes memory setup hindsight
```

Verify one bundled-Hindsight recall before resuming normal use. Do not migrate, delete, or
reconstruct either bank during rollback. Better does not replace Hermes's bundled Hindsight SDK.

## Optional plugin removal

After another provider is selected:

```bash
hermes plugins remove better_hindsight
```

The normal rollback can leave Better installed but inactive. Removing the plugin checkout does not
delete `~/.hermes/better_hindsight/`, its outbox, or any remote memory.

## Restore the prior Better snapshot

Use the immutable prior snapshot `v0.6.2`, not moving `main`. Its peeled Git commit is
`267493f93104a8199f58ed8d9078625b55ce66f1`; it predates the Jev-only planner and Hindsight 0.10.0
support. Restore the matching saved configuration and a Hindsight version supported by that snapshot
(0.8.5, 0.9.1, or 0.9.2). Do not assume the new planner keys are backward compatible. Keep planning,
reflection, and retention off until synthetic verification succeeds.

Stop profile users through your normal, separately authorized process controls before replacing
loaded code. Preserve the configuration, outbox, and both banks. Then install the exact commit:

```bash
hermes plugins install --enable stepanov1975/better-hermes-hindsight \
  --ref 267493f93104a8199f58ed8d9078625b55ce66f1 --force
hermes memory setup better_hindsight
hermes better_hindsight status
```

Confirm status reports version `0.6.2` and the commit above, then verify a synthetic recall before
resuming use. Plugin replacement does not restart an already-running gateway, undo remote writes,
or delete queued records; preserve blocked rows for separately authorized recovery.

## Return to current Better

The following intentionally selects moving development `main`, **not** the rollback snapshot.
For a controlled deployment, substitute a separately verified immutable release commit:

Install with the standard Hermes commands:

```bash
hermes plugins install --enable stepanov1975/better-hermes-hindsight --force
hermes memory setup better_hindsight
hermes better_hindsight status
```

Verify recall before re-enabling retention. Neither rollback direction owns remote-memory deletion.
Preserve mismatched or failed rows for diagnosis unless a separate recovery explicitly authorizes
otherwise.
