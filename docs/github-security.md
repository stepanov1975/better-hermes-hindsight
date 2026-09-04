# GitHub security and repository hygiene

Better Hermes Hindsight uses lightweight controls for a public, Linux-only Hermes memory plugin that handles credentials, private recalled content, network requests, and a durable SQLite outbox.

## Automated checks

| File or GitHub feature | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | Tests Python 3.11–3.13 against one reviewed Hermes commit and runs a scheduled/manual Hermes `main` compatibility canary. |
| `.github/workflows/security.yml` | Runs Gitleaks, actionlint, Semgrep, zizmor, and dependency audits. |
| `.github/workflows/release.yml` | Publishes an optional source-only snapshot after verified `main` CI when a new synchronized version has no existing tag. |
| `.github/dependabot.yml` | Maintains weekly Python and GitHub Actions pins with grouped updates and a cooldown. |
| `requirements-audit.txt` | Pins the minimal audit toolchain used under each supported Python version. |
| `requirements-ci.txt` | Pins the CI-only `uv` and Twine toolchain so Dependabot can maintain it. |
| `requirements-security.txt` | Pins the Python static-security scanners executed in CI. |
| `.gitleaks.toml` and `.semgrepignore` | Keep secret/static scans focused on tracked project content. |
| GitHub CodeQL default setup | Scans Python and GitHub Actions weekly without a duplicate checked-in workflow. |

All checked-in Actions use full commit SHAs, checkout credentials are not persisted, and default workflow permissions are read-only. Only the post-test source-snapshot job receives `contents: write`.

## Repository settings

The repository should keep these settings enabled:

- dependency graph, Dependabot alerts, and Dependabot security updates;
- secret scanning and push protection;
- private vulnerability reporting;
- CodeQL default setup for Python and GitHub Actions;
- required full-length SHA pinning for Actions;
- read-only default workflow permissions;
- automatic head-branch deletion after merge; and
- protected `main` with strict required checks, conversation resolution, admin enforcement, and no force-push or deletion.

Required checks should match the emitted jobs after their first successful run:

- `Python 3.11 / Hermes pinned`;
- `Python 3.12 / Hermes pinned`;
- `Python 3.13 / Hermes pinned`;
- `Gitleaks`;
- `actionlint`;
- `Python static/security checks`;
- `Runtime dependency audit / Python 3.11`;
- `Runtime dependency audit / Python 3.12`;
- `Runtime dependency audit / Python 3.13`;
- `Analyze (python)`; and
- `Analyze (actions)`.

The moving Hermes `main` canary is intentionally scheduled/manual rather than a pull-request requirement. This preserves early compatibility detection without allowing unrelated upstream changes to make an unchanged pull request nondeterministic.

## Optional source snapshots

The ordinary installation path remains `hermes plugins install --enable stepanov1975/better-hermes-hindsight`; PyPI and uploaded wheel/sdist assets are not publication targets.

After required tests pass on a `main` push, the release workflow:

1. validates synchronized version metadata and a nonempty matching changelog section;
2. exits successfully when that version already has a published release;
3. otherwise creates a draft tag/release at the exact verified commit;
4. verifies the tag target, final/prerelease state, and changelog-derived notes; and
5. publishes and reads the release back.

A tag without a release or a draft tag targeting another commit fails closed for operator review. Enable release immutability only after this workflow is merged and a no-op run against the current version succeeds.

## Local validation

```bash
uv lock --check
.venv/bin/python -m ruff check better_hermes_hindsight tests scripts __init__.py cli.py
.venv/bin/python -m ruff format --check better_hermes_hindsight tests scripts __init__.py cli.py
.venv/bin/python -m mypy
.venv/bin/python -m pytest -p no:cacheprovider

docker run --rm -v "$PWD:/repo" -w /repo \
  docker.io/rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 \
  -color

python -m pip install --requirement requirements-security.txt
zizmor .github/workflows
semgrep scan --config p/ci --config p/secrets --error --metrics=off .
```
