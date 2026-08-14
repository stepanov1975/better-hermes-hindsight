# Installation

Install a tagged GitHub prerelease into the existing Hermes interpreter and a selected profile. The
release wheel supplies the Python implementation; the exact tagged checkout supplies the
thin Hermes plugin bridge. The installer verifies both identities and writes a launcher
bound to that interpreter.

## Prerequisites

- Hermes installed with the official per-user installer;
- an external Hindsight 0.8.5 service and isolated bank;
- `git`, `curl`, and `uv` on `PATH`.

Better's internal HTTP adapter does not import the Hindsight Python SDK. The normal Hermes
interpreter can therefore retain bundled `hindsight-client==0.6.1` while also running Better.

The published `v0.1.0a3` release predates the internal adapter and still requires its original
isolated-interpreter procedure. Use the shared-interpreter commands below with `v0.1.0a5` or
newer. `v0.1.0a4` contains the internal client but its installer can reject an unrelated
dependency mismatch already present in Hermes.

## Install a tagged release

Replace `vX.Y.Z` below with `v0.1.0a5` or a newer published tag.
Do not install from a moving branch.

```bash
set -eu
RELEASE=vX.Y.Z
VERSION="${RELEASE#v}"
PROFILE=better-hindsight
SOURCE_DIR="$HOME/src/better-hermes-hindsight-$RELEASE"
ASSET_DIR="$HOME/.cache/better-hermes-hindsight/$RELEASE"
HERMES_SOURCE="$HOME/.hermes/hermes-agent"
ASSET_BASE="https://github.com/stepanov1975/better-hermes-hindsight/releases/download/$RELEASE"

test -x "$HERMES_SOURCE/venv/bin/python"
mkdir -p "$ASSET_DIR"
git clone --depth 1 --branch "$RELEASE" \
  https://github.com/stepanov1975/better-hermes-hindsight.git "$SOURCE_DIR"

curl -fL "$ASSET_BASE/better_hermes_hindsight-$VERSION-py3-none-any.whl" \
  -o "$ASSET_DIR/better_hermes_hindsight-$VERSION-py3-none-any.whl"
curl -fL "$ASSET_BASE/better_hermes_hindsight-$VERSION.tar.gz" \
  -o "$ASSET_DIR/better_hermes_hindsight-$VERSION.tar.gz"
curl -fL "$ASSET_BASE/SHA256SUMS" -o "$ASSET_DIR/SHA256SUMS"
(cd "$ASSET_DIR" && sha256sum --check SHA256SUMS)

python3 "$SOURCE_DIR/scripts/install_release.py" \
  --profile "$PROFILE" \
  --hermes-python "$HERMES_SOURCE/venv/bin/python" \
  --wheel "$ASSET_DIR/better_hermes_hindsight-$VERSION-py3-none-any.whl" \
  --sha256sums "$ASSET_DIR/SHA256SUMS"
```

The installer rejects a dirty or untagged source checkout, a mismatched wheel name, or a
bad checksum before changing the interpreter or profile. It then verifies package
compatibility, installed package version, exact bridge-file identity, selected provider,
and the interpreter-bound launcher at `~/.local/bin/$PROFILE`. It does not create a bank, write a
credential, enable retention, start a gateway, or schedule operational checks.

The compatibility check rejects dependency conflicts introduced by Better. If the selected
Hermes interpreter already has unrelated `uv pip check` findings, installation may continue only
when the exact bounded issue set does not grow; the installer reports the pre-existing count
without printing those dependency details.

The release checkout can be removed after installation. Hermes installs a private copy of
the verified bridge into the profile, and the Python implementation is installed
non-editably in the existing Hermes interpreter.

Configure the endpoint, bank, API-key environment variable, principal, and policy under the selected profile. See [configuration](configuration.md). Keep `retain.enabled=false` initially.

## Verify before retention

```bash
"$HOME/.local/bin/$PROFILE" plugins list
"$HOME/.local/bin/$PROFILE" config get memory.provider
"$HOME/.local/bin/$PROFILE" better_hindsight status
"$HOME/.local/bin/$PROFILE" better_hindsight missions check
```

Start the selected profile and verify a synthetic recall against the isolated Hindsight bank. Then explicitly enable retention and verify one synthetic completed turn reaches that bank.

An absent outbox is reported as `uninitialized`; that is normal before the first admitted retained turn. A non-zero status caused by destination-mismatched rows is degraded and must be inspected rather than ignored.

## Update

Stop processes using the Hermes interpreter. Clone the new exact release tag into a
fresh directory, download that release's wheel, sdist, and `SHA256SUMS`, and run its
`scripts/install_release.py` with the same profile and interpreter arguments. The installer
replaces the wheel and plugin bridge, then repeats all post-install checks.

```bash
"$HOME/.local/bin/$PROFILE" better_hindsight status
```

Run status and a synthetic recall before restarting normal use. The status payload reports
the installed release version and plugin commit without requiring an environment variable.

## Failure boundary

Installation and updates do not migrate or delete Hindsight banks and do not drain or delete Better's outbox. If package installation, discovery, status, or recall fails, keep the selected profile stopped and follow [rollback](rollback.md).
