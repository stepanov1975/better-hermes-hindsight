"""Exercise the checked-in audit recipes, including their real shell exit status."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/security.yml"
EXCEPTIONS = (
    "CVE-2026-59951",
    "CVE-2026-116684",
    "CVE-2026-116685",
    "CVE-2026-116686",
    "CVE-2026-114603",
)
STRICT_MANIFESTS = ("requirements-audit.txt", "requirements-ci.txt", "runtime-requirements.txt")


@pytest.fixture
def audit_shell(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Record argv and simulate findings/errors; no network or production access."""
    executable = tmp_path / "pip-audit"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(os.environ['AUDIT_RECORD']).write_text(json.dumps(args))\n"
        "ignored = {args[i + 1] for i, arg in enumerate(args) if arg == '--ignore-vuln'}\n"
        "findings = set(filter(None, os.environ['AUDIT_FINDINGS'].split(',')))\n"
        "sys.exit(2 if os.environ['AUDIT_ERROR'] else int(bool(findings - ignored)))\n"
    )
    executable.chmod(0o700)
    exporter = tmp_path / "uv"
    exporter.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-file') + 1]).write_text('example==1\\n')\n"
    )
    exporter.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "RUNNER_TEMP": str(tmp_path),
        "AUDIT_RECORD": str(tmp_path / "argv.json"),
        "AUDIT_FINDINGS": "",
        "AUDIT_ERROR": "",
    }
    env.pop("PYTHONPATH", None)
    return tmp_path, env


def run_audit(
    audit_shell: tuple[Path, dict[str, str]],
    manifest: str,
    findings: tuple[str, ...] = (),
    *,
    error: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    directory, env = audit_shell
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"][
        "runtime-audit" if manifest == "runtime-requirements.txt" else "toolchain-audit"
    ]
    step = next(step for step in job["steps"] if step["name"].startswith("Audit "))
    assert not job.get("continue-on-error", False)
    assert not step.get("continue-on-error", False)
    script = step["run"].replace("${{ matrix.python-version }}", "3.13")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=directory,
        env={
            **env,
            "MANIFEST": manifest,
            "AUDIT_FINDINGS": ",".join(findings),
            "AUDIT_ERROR": "1" if error else "",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    args: list[str] = json.loads(Path(env["AUDIT_RECORD"]).read_text())
    return result, args


@pytest.mark.parametrize("manifest", ("requirements-security.txt", *STRICT_MANIFESTS))
def test_only_scanner_manifest_receives_exact_exceptions(
    audit_shell: tuple[Path, dict[str, str]], manifest: str
) -> None:
    result, args = run_audit(audit_shell, manifest)
    expected = ["--progress-spinner", "off"]
    if manifest == "requirements-security.txt":
        for advisory in EXCEPTIONS:
            expected.extend(("--ignore-vuln", advisory))
    target = str(audit_shell[0] / manifest) if manifest == "runtime-requirements.txt" else manifest
    assert args == [*expected, "-r", target]
    assert result.returncode == 0, result.stderr
    if manifest == "runtime-requirements.txt":
        assert Path(target).read_text() == "example==1\n"


def test_scanner_accepts_only_the_approved_findings(
    audit_shell: tuple[Path, dict[str, str]],
) -> None:
    result, _ = run_audit(audit_shell, "requirements-security.txt", EXCEPTIONS)
    assert result.returncode == 0, result.stderr
    result, _ = run_audit(
        audit_shell, "requirements-security.txt", (*EXCEPTIONS, "UNKNOWN-ADVISORY")
    )
    assert result.returncode == 1, result.stderr


@pytest.mark.parametrize("manifest", STRICT_MANIFESTS)
@pytest.mark.parametrize("advisory", EXCEPTIONS)
def test_each_scanner_exception_still_fails_other_audits(
    audit_shell: tuple[Path, dict[str, str]], manifest: str, advisory: str
) -> None:
    result, _ = run_audit(audit_shell, manifest, (advisory,))
    assert result.returncode == 1, result.stderr


@pytest.mark.parametrize("manifest", ("requirements-security.txt", *STRICT_MANIFESTS))
def test_audit_errors_remain_failures(
    audit_shell: tuple[Path, dict[str, str]], manifest: str
) -> None:
    result, _ = run_audit(audit_shell, manifest, error=True)
    assert result.returncode == 2, result.stderr
