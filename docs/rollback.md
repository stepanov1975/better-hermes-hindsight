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

Stop and verify every process sharing the interpreter again. Use `uv pip --python` to install the
reviewed Better wheel and exact `hindsight-client==0.8.5`, install the reviewed Git plugin into the
named profile, and select Better:

```bash
uv pip install --python "$HERMES_PYTHON" --upgrade \
  dist/better_hermes_hindsight-0.0.0-py3-none-any.whl \
  'hindsight-client==0.8.5'
hermes --profile "$PROFILE" plugins install <reviewed-git-url> --enable
hermes --profile "$PROFILE" config set memory.provider better_hindsight
hermes --profile "$PROFILE" better_hindsight status
```

Run profile-scoped discovery/status checks before restart and verify recall before enabling retention.
Neither direction changes remote data ownership. Better's outbox and both banks remain available for
inspection or a later separately authorized migration.

## Scope

`hermes --profile "$PROFILE" plugins remove better_hindsight` removes that profile's host-owned Git
plugin directory. It does not own profile configuration, Python packages, Better's outbox, or
Hindsight data. `uv` owns only the interpreter packages explicitly named above.
