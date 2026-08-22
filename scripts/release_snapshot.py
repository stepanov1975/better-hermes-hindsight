#!/usr/bin/env python3
"""Prepare metadata and notes for an optional source snapshot release."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path
from typing import TypedDict

_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?P<prerelease>(?:a|b|rc)[0-9]+)?$")


class SnapshotMetadata(TypedDict):
    version: str
    tag: str
    prerelease: bool


def _match_version(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    if match is None:
        raise ValueError(f"Could not read version metadata from {path.name}")
    return match.group(1)


def read_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    project_version = project["version"]
    if not isinstance(project_version, str):
        raise ValueError("pyproject.toml project.version must be a string")
    plugin_version = _match_version(
        root / "plugin.yaml",
        r"^version:\s*['\"]?([^\s'\"]+)",
    )
    package_version = _match_version(
        root / "better_hermes_hindsight" / "__init__.py",
        r"^__version__\s*=\s*['\"]([^'\"]+)",
    )
    versions = {project_version, plugin_version, package_version}
    if len(versions) != 1:
        raise ValueError(f"Version metadata is not synchronized: {sorted(versions)}")
    if not _VERSION.fullmatch(project_version):
        raise ValueError(f"Version is not safe for a release tag: {project_version!r}")
    return project_version


def release_notes(changelog: str, version: str) -> str:
    heading = re.compile(rf"^## {re.escape(version)}(?:[ \t]+-[ \t]+[^\n]+)?[ \t]*$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    next_heading = re.search(r"^## ", changelog[match.end() :], flags=re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(changelog)
    notes = changelog[match.end() : end].strip()
    if not notes:
        raise ValueError(f"CHANGELOG.md section for {version} is empty")
    return notes + "\n"


def prepare(root: Path, notes_output: Path) -> SnapshotMetadata:
    version = read_version(root)
    notes = release_notes((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    notes_output.write_text(notes, encoding="utf-8")
    version_match = _VERSION.fullmatch(version)
    assert version_match is not None
    return {
        "version": version,
        "tag": f"v{version}",
        "prerelease": version_match.group("prerelease") is not None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--notes-output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root.resolve(), args.notes_output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
