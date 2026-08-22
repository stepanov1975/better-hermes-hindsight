from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.release_snapshot import prepare, read_version, release_notes

ROOT = Path(__file__).resolve().parents[1]


def test_prepare_current_source_snapshot(tmp_path: Path) -> None:
    notes_path = tmp_path / "notes.md"

    metadata = prepare(ROOT, notes_path)

    version = read_version(ROOT)
    expected_prerelease = re.search(r"(?:a|b|rc)\d+$", version) is not None
    assert metadata == {
        "version": version,
        "tag": f"v{version}",
        "prerelease": expected_prerelease,
    }
    notes = notes_path.read_text(encoding="utf-8")
    assert notes.strip()
    assert "\n## " not in notes


def test_release_notes_requires_a_nonempty_exact_section() -> None:
    changelog = (
        "# Changelog\n\n## 1.2.3 - 2026-08-22\n\n### Fixed\n\n- One.\n\n## 1.2.2\n\n- Older.\n"
    )

    assert release_notes(changelog, "1.2.3") == "### Fixed\n\n- One.\n"

    with pytest.raises(ValueError, match="no section"):
        release_notes(changelog, "1.2.4")
    with pytest.raises(ValueError, match="is empty"):
        release_notes("## 1.2.3\n\n## 1.2.2\n\n- Older.\n", "1.2.3")


def test_read_version_rejects_unsynchronized_metadata(tmp_path: Path) -> None:
    (tmp_path / "better_hermes_hindsight").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "plugin.yaml").write_text("version: 1.2.4\n", encoding="utf-8")
    (tmp_path / "better_hermes_hindsight" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="not synchronized"):
        read_version(tmp_path)


def test_prepare_marks_prerelease_versions(tmp_path: Path) -> None:
    (tmp_path / "better_hermes_hindsight").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1.2.3rc1"\n', encoding="utf-8"
    )
    (tmp_path / "plugin.yaml").write_text("version: 1.2.3rc1\n", encoding="utf-8")
    (tmp_path / "better_hermes_hindsight" / "__init__.py").write_text(
        '__version__ = "1.2.3rc1"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.2.3rc1\n\n- Candidate.\n", encoding="utf-8"
    )

    metadata = prepare(tmp_path, tmp_path / "notes.md")

    assert metadata == {"version": "1.2.3rc1", "tag": "v1.2.3rc1", "prerelease": True}
