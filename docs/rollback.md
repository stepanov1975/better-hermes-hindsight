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

## Return to Better

Install or update the plugin with the standard Hermes commands:

```bash
hermes plugins install stepanov1975/better-hermes-hindsight --force
hermes memory setup better_hindsight
hermes better_hindsight status
```

Verify recall before re-enabling retention. Neither rollback direction owns remote-memory deletion.
Preserve mismatched or failed rows for diagnosis unless a separate recovery explicitly authorizes
otherwise.
