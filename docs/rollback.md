# Rollback

The normal rollback is to stop the dedicated Better Hindsight profile and return traffic to the unchanged Hermes/Hindsight deployment. Do not migrate, delete, or reconstruct either bank during rollback.

## Preserve

Keep:

- the Better Git commit used;
- the Better SQLite outbox;
- the isolated Better Hindsight bank; and
- the original bundled-Hindsight bank and deployment.

Do not copy credentials, private bank names, principal identifiers, memories, or transcripts into rollback notes.

## Stop Better

```bash
PROFILE=better-hindsight-dev
hermes --profile "$PROFILE" gateway stop
```

Select and start the unchanged old deployment using its existing procedure. Verify one bundled-Hindsight recall. Because the Better canary uses a dedicated interpreter/profile, ordinary rollback does not require replacing its Python packages or deleting its plugin checkout.

## If the same interpreter must return to bundled Hindsight

Only if a dedicated interpreter was not used, stop every process sharing that interpreter before changing packages:

```bash
hermes --profile "$PROFILE" config set memory.provider hindsight
hermes --profile "$PROFILE" plugins remove better_hindsight
uv pip uninstall --python "$HERMES_PYTHON" better-hermes-hindsight hindsight-client
uv pip install --python "$HERMES_PYTHON" 'hindsight-client==0.6.1'
uv pip check --python "$HERMES_PYTHON"
```

Restart only after provider discovery and an actual bundled recall succeed. This fallback is more disruptive and is why the dedicated interpreter is the supported deployment.

## Return to Better

Stop the dedicated Better profile, select the known-good Git commit, refresh the editable package, and reinstall the local plugin bridge:

```bash
git -C "$SOURCE_DIR" checkout <known-good-commit>
uv pip install --python "$HERMES_PYTHON" -e "$SOURCE_DIR" 'hindsight-client==0.8.5'
uv pip check --python "$HERMES_PYTHON"
hermes --profile "$PROFILE" plugins install "file://$SOURCE_DIR" --force --enable
hermes --profile "$PROFILE" config set memory.provider better_hindsight
hermes --profile "$PROFILE" better_hindsight status
```

Verify recall before re-enabling retention. Neither rollback direction owns remote-memory deletion. Preserve mismatched or failed rows for diagnosis unless a separate recovery explicitly authorizes otherwise.
