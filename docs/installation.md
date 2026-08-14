# Installation

Better Hermes Hindsight is a standard, self-contained Hermes memory plugin. It installs into the
currently active Hermes configuration and uses Hermes's normal plugin lifecycle. It does not need a
separate profile, Python environment, package installation, launcher, or custom gateway startup
procedure.

## Prerequisites

- Hermes installed and working;
- an external Hindsight 0.8.5 or 0.9.1 service;
- `git` available for Hermes's Git-plugin installer.

The plugin declares `aiohttp>=3.14.1,<4` and `tiktoken>=0.12,<0.13` in `plugin.yaml`. Hermes checks
and installs declared memory-plugin dependencies through its normal memory setup command. Better
uses `tiktoken` only to match supported Hindsight servers' recall input limit; it does not import or
replace Hermes's bundled Hindsight client.

The official `cl100k_base` encoding table is packaged with Better and verified by SHA-256 before
use. Tokenizer initialization therefore does not depend on a first-use download from an external
encoding service.

## Install

Run the standard Hermes plugin commands:

```bash
hermes plugins install stepanov1975/better-hermes-hindsight
hermes memory setup better_hindsight
```

The first command clones the complete plugin into the current Hermes plugin directory. The second
selects `better_hindsight` as the active memory provider and checks its declared dependency. No
custom install script or additional runtime is involved.

The plugin does not create a Hindsight bank, store a credential, enable retention, start or restart
a gateway, or schedule its optional canary and watchdog.

## Configure

Create the Better Hindsight configuration under the same Hermes home used by the rest of the
current installation. Configure the endpoint, bank, API-key environment variable, principal, and
policy as described in [configuration](configuration.md). Keep `retain.enabled=false` initially.

## Verify before retention

```bash
hermes plugins list
hermes config get memory.provider
hermes better_hindsight status
hermes better_hindsight missions check
```

The plugin should appear as installed at version `0.2.3`, and `memory.provider` should be
`better_hindsight`. General-plugin enablement is not required for a selected memory provider. An
absent outbox is reported as `uninitialized`; that is normal before the first admitted retained
turn.

Verify one synthetic recall against the configured Hindsight bank. Then explicitly enable
retention and verify one synthetic completed turn reaches that bank. A non-zero status caused by
destination-mismatched rows is degraded and must be inspected rather than ignored.

## Update

Use Hermes's standard plugin update command:

```bash
hermes plugins update better_hindsight
hermes memory setup better_hindsight
hermes better_hindsight status
```

If upgrading from a pre-0.2.0 installation that copied only bridge files, replace it once with the
standard Git plugin:

```bash
hermes plugins install stepanov1975/better-hermes-hindsight --force
hermes memory setup better_hindsight
```

Updating the plugin does not migrate or delete Hindsight banks, drain or delete the local outbox,
or restart Hermes. Verify status and one synthetic recall before restarting normal use.

## Remove

Select another memory provider before removing Better:

```bash
hermes memory setup hindsight
hermes plugins remove better_hindsight
```

Removing the plugin does not delete its configuration, outbox, or remote memories. See
[rollback](rollback.md) before deleting any preserved state.
