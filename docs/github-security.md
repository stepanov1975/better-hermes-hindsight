# GitHub security and repository hygiene

Better Hermes Hindsight uses lightweight controls for a public, Linux-only Hermes memory plugin that handles credentials, private recalled content, network requests, and a durable SQLite outbox.

## Automated checks

| File or GitHub feature | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | Tests Python 3.11–3.13 against the older reviewed Hermes commit, Python 3.14 against reviewed Hermes v0.23.0, and a scheduled/manual Hermes `main` compatibility canary. |
| `.github/workflows/security.yml` | Runs Gitleaks, actionlint, Semgrep, zizmor, and dependency audits. |
| `.github/workflows/release.yml` | Publishes an optional source-only snapshot after verified `main` CI when a new synchronized version has no existing tag. |
| `.github/dependabot.yml` | Maintains weekly Python and GitHub Actions pins with grouped updates and a cooldown. |
| `requirements-audit.txt` | Pins the minimal audit toolchain used under each supported Python version. |
| `requirements-ci.txt` | Pins the CI-only `uv` and Twine toolchain so Dependabot can maintain it. |
| `requirements-security.txt` | Pins the Python static-security scanners executed in CI. |
| `.gitleaks.toml` and `.semgrepignore` | Keep secret/static scans focused on tracked project content. |
| GitHub CodeQL default setup | Scans Python and GitHub Actions weekly without a duplicate checked-in workflow. |

All checked-in Actions use full commit SHAs, checkout credentials are not persisted, and default workflow permissions are read-only. Only the post-test source-snapshot job receives `contents: write`.

## Security domains

- **Source and workflow checks:** `Python static/security checks` runs Semgrep (`p/ci` and
  `p/secrets`) and zizmor. It remains required, alongside Gitleaks, actionlint, and CodeQL.
- **Application dependencies:** `Runtime dependency audit / Python 3.11`, `3.12`, `3.13`, and `3.14`
  audit the frozen `uv.lock` runtime export, without development dependencies or the project itself.
  These audit the application rather than the ambient scanner environment. The existing 3.11–3.13
  checks remain required; add the new 3.14 check to branch protection after its first hosted run.
- **CI/scanner/auditor dependencies:** `Toolchain dependency audit / <manifest>` separately audits
  `requirements-ci.txt`, `requirements-security.txt`, and `requirements-audit.txt`, including their
  resolved transitive dependencies. The Python 3.13 matrix disables fail-fast so a finding in one
  manifest does not cancel the others. These checks are intentionally nonblocking for merges via
  branch protection, not via suppressed failures: unexcluded vulnerabilities and audit errors still
  produce red jobs and a failed Security scans workflow (including its badge).

A scanner-only advisory is not evidence that the application runtime is vulnerable, but it still
requires maintainer triage and a toolchain update when available. Conversely, a runtime finding
remains blocking even when source scans pass. Only the five explicitly approved scanner-manifest
exceptions below are ignored. All other advisories, the CI/auditor manifests, and every frozen
runtime export remain strict. No audit uses `continue-on-error` or masks its exit status; scanner
installation or execution failures still fail the required source job.

### Approved scanner-only exceptions

The `requirements-security.txt` audit alone excludes **five advisories in two unpatched transitive
packages**: `mcp==1.29.0` (four) and resolved `opentelemetry-api==1.37.0` (one). These are exclusions,
not dependency fixes, and a passing audit with five ignored findings is not a vulnerability-free
scanner environment. Runtime requirements, runtime lock/export, and installed Hermes are unchanged.

Semgrep 1.181.0 requires `mcp==1.29.0` and `opentelemetry-api~=1.37.0`; the latter excludes 1.45.1.
The reviewed newer Semgrep 1.185.0 retains both constraints, so that update alone cannot provide a
compatible patched graph. See the published [1.181.0 metadata](https://pypi.org/pypi/semgrep/1.181.0/json)
and [1.185.0 metadata](https://pypi.org/pypi/semgrep/1.185.0/json). Do not force conflicting versions
or install without dependencies to make the audit appear clean.

| Excluded advisory | Affected package / patched version | Rationale limited to this workflow |
| --- | --- | --- |
| [CVE-2026-59951](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-84m7-p3x7-pcfv) | `mcp` 1.29.0 / 1.30.0 | Stateful Streamable HTTP server session exhaustion; this command does not start an MCP server. |
| [CVE-2026-116684](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-w4fh-qvv9-3v23) | `mcp` 1.29.0 / 1.30.0 | Server bearer-token audience validation; no MCP HTTP/auth server is started. The upstream fix also requires server configuration. |
| [CVE-2026-116685](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-5h93-6whr-6q8j) | `mcp` 1.29.0 / 1.30.0 | MCP HTTP/SSE client redirect disclosure; the scan does not connect through MCP client transports or OAuth providers. |
| [CVE-2026-116686](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-rwrf-2pqf-9j8j) | `mcp` 1.29.0 / 1.30.0 | MCP client tool-result schema reference fetching; the scan does not call tools through `ClientSession`. |
| [CVE-2026-114603](https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-xcq7-p3pf-p88h) | `opentelemetry-api` 1.37.0 / 1.45.1 | Untrusted inbound `tracestate` parsing through `TraceState.from_header`; the ordinary CLI scan has tracing disabled by default and serves no instrumented inbound requests. |

This decision covers exactly `semgrep scan --config p/ci --config p/secrets --error --metrics=off .`
and zizmor 1.31.1 in the checked-in source-scanner job. Semgrep's
[scan entry point](https://github.com/semgrep/semgrep/blob/v1.181.0/cli/src/semgrep/commands/scan.py)
keeps tracing behind `--trace`; the separate
[MCP entry point](https://github.com/semgrep/semgrep/blob/v1.181.0/cli/src/semgrep/commands/mcp.py)
starts the MCP server. `--metrics=off` is not itself a tracing-disable guarantee. Imports or installed
packages alone do not establish advisory reachability, and these exceptions are not a safety claim
for `semgrep mcp`, MCP clients, tracing-enabled/custom-instrumented runs, or other environments.

Remove each exception when a compatible scanner release resolves the affected dependency to its
patched version, then rerun installation, source scans, and the scanner audit without that ignore.
Reassess the source/advisory trace whenever the scanner version, command, tracing configuration,
transports, or exposure changes; withdraw the relevant exception if its non-applicability no longer
holds. New advisories require separate triage and approval, never a blanket scanner waiver.

`tests/test_security_audit_exceptions.py` executes the actual YAML audit recipes in Bash with a
recording audit executable. It checks the five exact scanner-only arguments and nonzero shell
status for unknown scanner findings, each excluded advisory in every other audit, and audit errors.
The runtime recipe is exercised with a test exporter, not a claim of fresh hosted CI or a real
vulnerability-database audit.

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

Keep the three `Toolchain dependency audit / <manifest>` checks out of the required-check list;
require the individual source and runtime contexts above, not an aggregate Security scans gate.
Workflow YAML does not configure branch protection: verify the emitted names in Settings → Branches
(or Rules → Rulesets) after rollout. Renaming a required source/runtime job requires a coordinated
settings change; the toolchain split preserves those names and requires no settings change when
protection already uses the list above. Review red toolchain jobs independently rather than treating
a mergeable pull request as an all-green security result.

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
