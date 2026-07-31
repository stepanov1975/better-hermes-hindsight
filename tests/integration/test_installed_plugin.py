"""Focused proof of released Hermes host-managed plugin installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROOT_PLUGIN_FILES = ("__init__.py", "cli.py", "plugin.yaml")
PACKAGED_PLUGIN = ROOT / "src/better_hermes_hindsight/hermes_plugin"


def _run(
    command: list[str],
    *,
    cwd: Path,
    environ: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"command failed: {command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _plugin_repository(tmp_path: Path) -> Path:
    source = tmp_path / "plugin-source"
    source.mkdir()
    for name in ROOT_PLUGIN_FILES:
        shutil.copy2(ROOT / name, source / name)
    _run(["git", "init", "--quiet"], cwd=source)
    _run(["git", "config", "user.name", "Task 5 fixture"], cwd=source)
    _run(["git", "config", "user.email", "fixture@example.invalid"], cwd=source)
    _run(["git", "add", *ROOT_PLUGIN_FILES], cwd=source)
    _run(["git", "commit", "--quiet", "-m", "fixture plugin"], cwd=source)
    return source


def _hermes_environment(home: Path) -> dict[str, str]:
    environ = os.environ.copy()
    environ["HERMES_HOME"] = str(home)
    environ["PYTHONDONTWRITEBYTECODE"] = "1"
    return environ


def _flat_manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        assert separator and key and value.strip()
        values[key] = value.strip()
    return values


def test_root_plugin_surface_is_thin_and_version_aligned() -> None:
    root_manifest = _flat_manifest(ROOT / "plugin.yaml")
    packaged_manifest = _flat_manifest(PACKAGED_PLUGIN / "plugin.yaml")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert root_manifest == packaged_manifest
    assert root_manifest["name"] == "better_hindsight"
    assert root_manifest["kind"] == "exclusive"
    assert root_manifest["version"] == project["version"]
    assert set(root_manifest) == {"name", "kind", "version", "description"}
    assert "scripts" not in project
    assert all((ROOT / name).is_file() for name in ROOT_PLUGIN_FILES)


def test_released_hermes_installs_loads_discovers_cli_and_removes_plugin(
    tmp_path: Path,
) -> None:
    source = _plugin_repository(tmp_path)
    home = tmp_path / "hermes-home"
    home.mkdir()
    environ = _hermes_environment(home)

    _run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "plugins",
            "install",
            source.as_uri(),
            "--no-enable",
        ],
        cwd=ROOT,
        environ=environ,
    )

    installed = home / "plugins/better_hindsight"
    assert installed.is_dir()
    assert all((installed / name).is_file() for name in ROOT_PLUGIN_FILES)
    assert (installed / ".git").is_dir()

    (home / "config.yaml").write_text(
        "memory:\n  provider: better_hindsight\n",
        encoding="utf-8",
    )
    probe = _run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from plugins.memory import discover_plugin_cli_commands, load_memory_provider; "
                "provider = load_memory_provider('better_hindsight'); "
                "commands = discover_plugin_cli_commands(); "
                "print(json.dumps({'provider_name': provider.name, "
                "'provider_type': type(provider).__name__, "
                "'commands': [item['name'] for item in commands]}, sort_keys=True))"
            ),
        ],
        cwd=ROOT,
        environ=environ,
    )
    payload = json.loads(probe.stdout.strip().splitlines()[-1])
    assert payload == {
        "commands": ["better_hindsight"],
        "provider_name": "better_hindsight",
        "provider_type": "BetterHindsightMemoryProvider",
    }

    help_result = _run(
        [sys.executable, "-m", "hermes_cli.main", "better_hindsight", "--help"],
        cwd=ROOT,
        environ=environ,
    )
    assert "{status,missions}" in help_result.stdout
    assert "status" in help_result.stdout
    assert "missions" in help_result.stdout

    _run(
        [sys.executable, "-m", "hermes_cli.main", "plugins", "remove", "better_hindsight"],
        cwd=ROOT,
        environ=environ,
    )
    assert not installed.exists()
    assert (home / "config.yaml").read_text(encoding="utf-8") == (
        "memory:\n  provider: better_hindsight\n"
    )
