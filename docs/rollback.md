# Rollback

Rollback is stopped-process operator work. It uses released Hermes plugin commands and `uv`; Better
Hermes Hindsight has no independent rollback engine. Returning to bundled Hindsight must restore
exact `hindsight-client==0.6.1` before restart.

## Preserve before changing provider

Keep Better's outbox and both banks. Do not drain, migrate, reconstruct, prune, or delete them as part
of rollback. Record the reviewed Better commit and package version, but do not copy credentials into
logs or the repository.

A profile scopes configuration and local data, not interpreter packages. Every Hermes gateway, TUI,
CLI, and worker sharing the interpreter must be stopped before replacing the SDK. Afterward, restart
only profiles whose selected provider is compatible with the restored SDK.

## Stop the shared interpreter

For the standard Hermes installation layout:

```bash
PROFILE=better-hindsight-dev
HERMES_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python3"
command -v uv
test -x "$HERMES_PYTHON"

hermes gateway list
# Repeat for every listed profile using HERMES_PYTHON:
hermes --profile <profile> gateway stop
```

Close Hermes TUI/CLI sessions and standalone workers. Do not continue while the interpreter is in
use:

```bash
if pgrep -af "$HERMES_PYTHON"; then
  echo "Hermes interpreter is still in use; stop those processes first" >&2
  exit 1
fi
```

## Return the selected profile to bundled Hindsight

While every process sharing the interpreter remains stopped:

```bash
hermes --profile "$PROFILE" config set memory.provider hindsight
hermes --profile "$PROFILE" plugins remove better_hindsight
uv pip uninstall --python "$HERMES_PYTHON" better-hermes-hindsight hindsight-client
uv pip install --python "$HERMES_PYTHON" 'hindsight-client==0.6.1'
uv pip check --python "$HERMES_PYTHON"
"$HERMES_PYTHON" -c \
  'from importlib.metadata import version; assert version("hindsight-client") == "0.6.1"'
```

Check the target profile, then restart only profiles compatible with `hindsight-client==0.6.1`:

```bash
hermes --profile "$PROFILE" config get memory.provider
hermes --profile "$PROFILE" gateway start
```

Verify the first bundled recall without any lazy package installation. Importing the bundled provider
while 0.8.5 is still installed is not rollback proof; the exact 0.6.1 state must be exercised. Leave
profiles that still select Better stopped or give them a separate Hermes interpreter.

## Return to Better

Stop and verify every process sharing the interpreter again. Acquire and verify the release assets
and exact tag clone using the
[immutable installation procedure](releases/0.1.0a1.md#immutable-installation-inputs),
then set these paths:

```bash
BETTER_WHEEL=/path/to/downloaded/better_hermes_hindsight-0.1.0a1-py3-none-any.whl
SOURCE_DIR=/path/to/exact-v0.1.0a1-clone
EXPECTED_COMMIT=3404516e69d4d9861b04ff9299a2c30a76566158
HERMES_BASE="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_BASE/profiles/$PROFILE/plugins/better_hindsight"

test "$(sha256sum "$BETTER_WHEEL" | cut -d' ' -f1)" = \
  ba94a798c34043bca02fb80eb51200ae62e82bde85973c6314da7812de5e7ce4
test "$(git -C "$SOURCE_DIR" rev-parse HEAD)" = "$EXPECTED_COMMIT"
test -z "$(git -C "$SOURCE_DIR" status --porcelain)"

uv pip install --python "$HERMES_PYTHON" \
  "$BETTER_WHEEL" \
  'hindsight-client==0.8.5'
uv pip check --python "$HERMES_PYTHON"
hermes --profile "$PROFILE" plugins install "file://$SOURCE_DIR" --force --enable
test "$(git -C "$PLUGIN_DIR" rev-parse HEAD)" = "$EXPECTED_COMMIT"
test -z "$(git -C "$PLUGIN_DIR" status --porcelain)"
hermes --profile "$PROFILE" config set memory.provider better_hindsight
hermes --profile "$PROFILE" better_hindsight status
```

Run profile-scoped discovery/status checks before restart and verify recall before enabling retention.
Neither direction changes remote data ownership. Better's outbox and both banks remain available for
inspection or a later separately authorized migration.

## Development proof and future canary

The opt-in procedure in [development-instance.md](development-instance.md) owns only its generated,
preflight-absent development bank and disposable temporary profile. Hindsight 0.8.5 has no conditional
create-only primitive, so the separate development key and datastore must remain single-writer for the
full proof; a host-local nonblocking lock rejects accidental concurrent local runs. A second process
already holding the same key and random target ID is outside the safe contract. After the absence
guard, the harness records a random local cleanup token before create and stores the matching witness
in the bank's public display name. The marker is completely written, synced, and reread; partial or
unverifiable writes fail before create. The child never deletes; both success and failure leave the
marker for the parent. Public mission-command results `runtime_cleanup_failed` and
`write_attempted_outcome_unknown` fail the proof because they do not prove operator-runtime quiescence.
The parent treats the complete interval from before launch through process-tree absence as
exception-total. After timeout or normal leader exit, it detects and terminates surviving descendants.
An interrupt before the absence proof either settles the known tree before propagation or becomes an
unsettled-tree result; an unknown launch outcome or failed liveness proof always suppresses cleanup.
After proven absence, propagated Python interrupts still run ownership-gated cleanup before being
re-raised. Parent cleanup treats authenticated HTTP 404 as confirmed absence without DELETE and deletes
a present bank only when its ID and remote ownership witness both match. Existing-bank rejection and
timeout before create therefore send no mutation, including timeout after the local marker is written.
If cleanup fails, use the reported sanitized identifier only to correlate the generated resource in the
development instance; never infer or delete a bank in the existing deployment from that value.

A future production canary is a separate Hermes interpreter/profile plus another isolated Better
Hindsight instance and bank. Activating it is not part of Task 6. To stop a failed canary, stop only its
dedicated Hermes processes and select the unchanged old profile/deployment for traffic. Preserve the
canary bank and Better outbox for diagnosis unless deletion receives separate authorization. The old
deployment remains running throughout; canary rollback needs no migration, reconstruction,
deduplication, reconsolidation, pruning, or deletion.

## Scope

`hermes --profile "$PROFILE" plugins remove better_hindsight` removes that profile's host-owned Git
plugin directory. It does not own profile configuration, Python packages, Better's outbox, or
Hindsight data. `uv` owns only the interpreter packages explicitly named above.
