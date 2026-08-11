# Installation

Better Hermes Hindsight is normally installed from a Git checkout. The checkout commit identifies the deployed code; no wheel checksum, GitHub release, or PyPI publication is required.

## Prerequisites

- a dedicated Hermes installation/interpreter and profile;
- an external Hindsight 0.8.5 service and isolated bank;
- `uv` on `PATH`; and
- the Better Hindsight source checkout.

The dedicated interpreter matters because Better requires `hindsight-client==0.8.5`, while bundled Hermes Hindsight currently requires an incompatible client version.

## Install

Stop processes using the selected dedicated interpreter, then install the package editable from the same checkout used as the Hermes plugin:

```bash
PROFILE=better-hindsight-dev
SOURCE_DIR=/path/to/better-hermes-hindsight
HERMES_PYTHON=/path/to/dedicated/hermes/python

uv pip install --python "$HERMES_PYTHON" -e "$SOURCE_DIR" 'hindsight-client==0.8.5'
uv pip check --python "$HERMES_PYTHON"

hermes --profile "$PROFILE" plugins install "file://$SOURCE_DIR" --force --enable
hermes --profile "$PROFILE" config set memory.provider better_hindsight
```

The root `plugin.yaml`, `__init__.py`, and `cli.py` are thin Hermes bridges; the editable package supplies the implementation.

Configure the endpoint, bank, API-key environment variable, principal, and policy under the selected profile. See [configuration](configuration.md). Keep `retain.enabled=false` initially.

## Verify before retention

```bash
hermes --profile "$PROFILE" plugins list
hermes --profile "$PROFILE" config get memory.provider
hermes --profile "$PROFILE" better_hindsight status
hermes --profile "$PROFILE" better_hindsight missions check
```

Start the dedicated profile and verify a synthetic recall against the isolated Hindsight bank. Then explicitly enable retention and verify one synthetic completed turn reaches that bank.

An absent outbox is reported as `uninitialized`; that is normal before the first admitted retained turn. A non-zero status caused by destination-mismatched rows is degraded and must be inspected rather than ignored.

## Update

Stop the dedicated Hermes process, update the Git checkout, and refresh the editable install:

```bash
git -C "$SOURCE_DIR" pull --ff-only
uv pip install --python "$HERMES_PYTHON" -e "$SOURCE_DIR" 'hindsight-client==0.8.5'
uv pip check --python "$HERMES_PYTHON"
hermes --profile "$PROFILE" plugins install "file://$SOURCE_DIR" --force --enable
```

Record `git -C "$SOURCE_DIR" rev-parse HEAD` with the validation result. Run status and a recall smoke test before restarting normal use.

## Failure boundary

Installation and updates do not migrate or delete Hindsight banks and do not drain or delete Better's outbox. If package installation, discovery, status, or recall fails, keep the dedicated profile stopped and follow [rollback](rollback.md).
