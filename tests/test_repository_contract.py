"""Small repository-level smoke checks.

Runtime behavior belongs in unit, contract, and integration tests. These checks
only catch broken packaging metadata and documentation links.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

import yaml

import better_hermes_hindsight

ROOT = Path(__file__).resolve().parents[1]
_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _project() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert isinstance(project, dict)
    return cast(dict[str, object], project)


def _manifest(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def test_package_and_plugin_metadata_are_consistent() -> None:
    project = _project()
    root_manifest = _manifest(ROOT / "plugin.yaml")

    assert project["name"] == "better-hermes-hindsight"
    assert better_hermes_hindsight.PROVIDER_ID == "better_hindsight"
    assert better_hermes_hindsight.__version__ == project["version"]
    assert root_manifest["name"] == better_hermes_hindsight.PROVIDER_ID
    assert root_manifest["kind"] == "exclusive"
    assert root_manifest["version"] == project["version"]
    assert root_manifest["manifest_version"] == 1
    assert root_manifest["pip_dependencies"] == [
        "aiohttp>=3.14.1,<4",
        "tiktoken>=0.12,<0.14",
    ]


def test_current_hermes_security_scanner_accepts_the_tracked_plugin_tree(
    tmp_path: Path,
) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    staged = tmp_path / "plugin"
    for raw_path in listed.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(raw_path.decode("utf-8"))
        source = ROOT / relative
        if not source.is_file():
            continue
        destination = staged / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    scanner_root = ROOT / ".compat" / "hermes-current"
    scanner = scanner_root / "tools" / "plugin_guard.py"
    assert scanner.is_file(), "install the selected Hermes checkout under .compat/hermes-current"
    scan_script = """
import sys
from pathlib import Path

from tools.plugin_guard import format_scan_report, scan_plugin

result = scan_plugin(Path(sys.argv[1]), source="stepanov1975/better-hermes-hindsight")
if result.verdict != "safe":
    print(format_scan_report(result), file=sys.stderr)
    raise SystemExit(1)
"""
    completed = subprocess.run(
        [sys.executable, "-c", scan_script, str(staged)],
        cwd=tmp_path,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(scanner_root)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_required_operator_documentation_exists() -> None:
    required = {
        ROOT / "README.md",
        ROOT / "DESIGN.md",
        ROOT / "DEVELOPMENT.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "docs" / "configuration.md",
        ROOT / "docs" / "installation.md",
        ROOT / "docs" / "operations.md",
        ROOT / "docs" / "rollback.md",
        ROOT / "docs" / "compatibility.md",
        ROOT / "docs" / "development-instance.md",
    }
    assert all(path.is_file() for path in required)


def test_retention_documentation_describes_occurrence_records() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    assert "independently decodable event records" in readme
    assert "per-admission event ID and occurrence time" in readme
    assert "better-hindsight-retained-event-v2" in configuration
    assert "separately\nadmitted identical turn receives a new event ID" in configuration
    assert "concatenate exactly to that canonical source" not in configuration
    assert "Identical rows\nare admission no-ops" not in configuration


def test_vendored_tokenizer_attribution_is_packaged() -> None:
    project = _project()
    assert project["license-files"] == ["LICENSE", "THIRD_PARTY_NOTICES.md"]

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "cl100k_base.tiktoken" in notices
    assert "Copyright (c) 2022 OpenAI, Shantanu Jain" in notices
    assert "The above copyright notice and this permission notice" in notices


def test_public_install_guide_uses_only_standard_hermes_plugin_commands() -> None:
    text = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")

    assert "hermes plugins install stepanov1975/better-hermes-hindsight" in text
    assert "hermes memory setup better_hindsight" in text
    for forbidden in (
        "--profile",
        "install_release.py",
        "uv pip install",
        "python -m venv",
        "~/.local/bin/better-hindsight",
    ):
        assert forbidden not in text


def test_local_markdown_links_resolve() -> None:
    failures: list[str] = []
    for document in (ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "DESIGN.md"):
        text = document.read_text(encoding="utf-8")
        for raw_target in _LINK.findall(text):
            target = raw_target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / target).resolve()
            if ROOT not in resolved.parents and resolved != ROOT:
                failures.append(
                    f"{document.relative_to(ROOT)}: link escapes repository: {raw_target}"
                )
            elif not resolved.exists():
                failures.append(f"{document.relative_to(ROOT)}: missing link target: {raw_target}")
    assert failures == []
