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
    packaged_manifest = _manifest(
        ROOT / "src" / "better_hermes_hindsight" / "hermes_plugin" / "plugin.yaml"
    )

    assert project["name"] == "better-hermes-hindsight"
    assert better_hermes_hindsight.PROVIDER_ID == "better_hindsight"
    assert better_hermes_hindsight.__version__ == project["version"]
    assert root_manifest == packaged_manifest
    assert root_manifest["name"] == better_hermes_hindsight.PROVIDER_ID
    assert root_manifest["kind"] == "exclusive"
    assert root_manifest["version"] == project["version"]


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


def test_public_install_guide_fetches_the_complete_immutable_release_set() -> None:
    text = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")

    assert 'VERSION="${RELEASE#v}"' in text
    assert "better_hermes_hindsight-$VERSION-py3-none-any.whl" in text
    assert "better_hermes_hindsight-$VERSION.tar.gz" in text
    assert '(cd "$ASSET_DIR" && sha256sum --check SHA256SUMS)' in text
    assert "Do not install from a moving branch" in text
    assert 'uv pip install --python "$APP_DIR/venv/bin/python" -e "$SOURCE_DIR"' not in text


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
