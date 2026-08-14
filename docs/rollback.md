# Rollback

The normal rollback is to stop the dedicated Better Hindsight profile and return traffic to the unchanged Hermes/Hindsight deployment. Do not migrate, delete, or reconstruct either bank during rollback.

## Preserve

Keep:

- the Better release tag and reported Git commit;
- the Better SQLite outbox;
- the isolated Better Hindsight bank; and
- the original bundled-Hindsight bank and deployment.

Do not copy credentials, private bank names, principal identifiers, memories, or transcripts into rollback notes.

## Stop Better

```bash
PROFILE=better-hindsight
"$HOME/.local/bin/$PROFILE" gateway stop
```

Select and start the unchanged old deployment using its existing procedure. Verify one bundled-Hindsight recall. Better does not replace Hermes's bundled Hindsight SDK, so ordinary rollback requires no Python package changes.

## Optional package removal

Provider rollback can leave the inert Better package installed. To remove it completely, stop every process sharing the Hermes interpreter first:

```bash
HERMES_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python"
hermes --profile "$PROFILE" config set memory.provider hindsight
hermes --profile "$PROFILE" plugins remove better_hindsight
uv pip uninstall --python "$HERMES_PYTHON" better-hermes-hindsight
uv pip check --python "$HERMES_PYTHON"
```

Restart only after provider discovery and an actual bundled recall succeed. Do not uninstall or replace Hermes's bundled `hindsight-client` as part of Better rollback.

## Return to Better

Stop the dedicated Better profile and repeat the tagged-release installation procedure with
the known-good release tag, wheel, and `SHA256SUMS`. The release installer replaces and
verifies both the package and plugin bridge. Then run:

```bash
"$HOME/.local/bin/$PROFILE" better_hindsight status
```

Verify recall before re-enabling retention. Neither rollback direction owns remote-memory deletion. Preserve mismatched or failed rows for diagnosis unless a separate recovery explicitly authorizes otherwise.
