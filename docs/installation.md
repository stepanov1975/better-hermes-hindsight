# Installation

Better Hermes Hindsight targets the current stable Hermes Agent release selected by the rolling
compatibility policy (`v2026.8.3`, package metadata 0.20.0) and an external/self-hosted Hindsight
0.8.5 service. This is a development-only prerelease workflow. The isolated Task 6 proof is complete
at checkpoint `3f542d4`, but production use and public release remain blocked by the current
supported-host security audit documented in [audit-findings.md](audit-findings.md).

## Ownership and isolation model

Two existing managers own separate artifacts:

- Hermes owns the profile-local Git plugin directory through `hermes plugins install|update|remove`.
- `uv` owns the Better wheel and exact `hindsight-client==0.8.5` in Hermes's interpreter.

No custom installer runs inside the provider. The root `plugin.yaml`, `__init__.py`, and `cli.py` are
thin bridges to the installed wheel.

A Hermes profile scopes configuration, the outbox, and plugin checkout. It does **not** isolate Python
packages: every profile and process using the same Hermes interpreter sees the SDK replacement. For
an actually isolated development canary, use a dedicated Hermes installation/interpreter as well as a
separate profile. Otherwise all processes sharing the interpreter must be stopped, and profiles that
need an incompatible provider/SDK must remain stopped.

## Prerequisites

- standard current stable Hermes Agent installation;
- `uv` available on `PATH`;
- a built Better Hermes Hindsight wheel from the reviewed commit;
- the Git URL for that same reviewed commit;
- an existing named Hermes profile plus an isolated Hindsight 0.8.5 development instance; and
- credentials supplied through the documented profile configuration path, never the repository.

For the standard Hermes installation layout:

```bash
PROFILE=better-hindsight-dev
HERMES_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python3"
command -v uv
test -x "$HERMES_PYTHON"
```

If Hermes uses another interpreter, set `HERMES_PYTHON` to the interpreter named by the installed
`hermes` launcher. Do not install into whichever unrelated `python` happens to be on `PATH`.

## Stop every process sharing the interpreter

Build the reviewed wheel first. Then list gateways and stop every profile backed by this Hermes
installation, not only `$PROFILE`:

```bash
# From the reviewed Better Hermes Hindsight checkout:
uv build

hermes gateway list
# Repeat for every listed profile using HERMES_PYTHON:
hermes --profile <profile> gateway stop
```

Close Hermes TUI/CLI sessions and any standalone worker using the same installation. Do not mutate the
interpreter until no process uses it. One practical Linux check is:

```bash
if pgrep -af "$HERMES_PYTHON"; then
  echo "Hermes interpreter is still in use; stop those processes first" >&2
  exit 1
fi
```

## Install into the selected profile

```bash
uv pip install --python "$HERMES_PYTHON" --upgrade \
  dist/better_hermes_hindsight-0.0.0-py3-none-any.whl \
  'hindsight-client==0.8.5'

hermes --profile "$PROFILE" plugins install <reviewed-git-url> --enable
hermes --profile "$PROFILE" config set memory.provider better_hindsight
```

Configure endpoint, bank, credential, and other Better settings in `$PROFILE`. Keep
`retain.enabled=false` initially. Then perform profile-scoped, stopped-process checks:

```bash
hermes --profile "$PROFILE" plugins list
hermes --profile "$PROFILE" config get memory.provider
hermes --profile "$PROFILE" better_hindsight status
hermes --profile "$PROFILE" better_hindsight missions check
```

`status` may report an uninitialized outbox before the first admitted retained turn; that is not an
installation failure. Start only profiles compatible with `hindsight-client==0.8.5`:

```bash
hermes --profile "$PROFILE" gateway start
```

Verify current-query recall against a synthetic fact before separately enabling retention for the
isolated canary. Leave incompatible profiles stopped or give them a separate Hermes interpreter.

## Update

Build the reviewed replacement first, then repeat the all-process stop and interpreter-use check.
Upgrade the wheel and exact SDK with `uv pip --python`, and update only the selected profile's Git
checkout:

```bash
uv pip install --python "$HERMES_PYTHON" --upgrade \
  dist/better_hermes_hindsight-0.0.0-py3-none-any.whl \
  'hindsight-client==0.8.5'
hermes --profile "$PROFILE" plugins update better_hindsight
```

The Hermes command does not rewrite the Python environment. Re-run profile-scoped discovery, status,
and recall checks before restarting compatible profiles.

## Failure boundary

If any package, install, discovery, or status step fails, keep every process sharing the interpreter
stopped and follow [rollback](rollback.md). The workflow does not migrate or delete a Hindsight bank
and does not drain or delete Better's outbox.
