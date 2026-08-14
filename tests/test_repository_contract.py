"""Small repository-level smoke checks.

Runtime behavior belongs in unit, contract, and integration tests. These checks
only catch broken packaging metadata and documentation links.
"""

from __future__ import annotations

import re
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
        "tiktoken>=0.12,<0.13",
    ]


def test_required_operator_documentation_exists() -> None:
    required = {
        ROOT / "README.md",
        ROOT / "DESIGN.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "docs" / "configuration.md",
        ROOT / "docs" / "installation.md",
        ROOT / "docs" / "operations.md",
        ROOT / "docs" / "rollback.md",
        ROOT / "docs" / "compatibility.md",
        ROOT / "docs" / "development-instance.md",
    }
    assert all(path.is_file() for path in required)


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
