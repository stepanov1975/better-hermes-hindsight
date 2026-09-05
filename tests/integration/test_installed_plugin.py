"""Focused proof of released Hermes host-managed plugin installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROOT_PLUGIN_FILES = ("__init__.py", "after-install.md", "cli.py", "plugin.yaml")
NO_RUNTIME_DEPENDENCY_DISCOVERY_PROBE = """
import sys


class BlockRuntimeDependencies:
    def find_spec(self, fullname, path=None, target=None):
        roots = ("aiohttp", "tiktoken")
        if any(fullname == root or fullname.startswith(root + ".") for root in roots):
            raise ModuleNotFoundError(fullname)
        return None


sys.meta_path.insert(0, BlockRuntimeDependencies())
from plugins.memory import load_memory_provider

provider = load_memory_provider("better_hindsight")
assert provider is not None
assert provider.name == "better_hindsight"
assert provider.is_available() is False
assert "aiohttp" not in sys.modules
assert "tiktoken" not in sys.modules
print(type(provider).__module__)
"""


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


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
    shutil.copytree(
        ROOT / "better_hermes_hindsight",
        source / "better_hermes_hindsight",
    )
    _run(["git", "init", "--quiet"], cwd=source)
    _run(["git", "config", "user.name", "Task 5 fixture"], cwd=source)
    _run(["git", "config", "user.email", "fixture@example.invalid"], cwd=source)
    _run(["git", "add", "."], cwd=source)
    _run(["git", "commit", "--quiet", "-m", "fixture plugin"], cwd=source)
    return source


def _hermes_environment(home: Path) -> dict[str, str]:
    environ = os.environ.copy()
    environ["HERMES_HOME"] = str(home)
    environ["PYTHONDONTWRITEBYTECODE"] = "1"
    return environ


def test_root_plugin_surface_is_self_contained_and_version_aligned() -> None:
    root_manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert root_manifest["name"] == "better_hindsight"
    assert root_manifest["kind"] == "standalone"
    assert root_manifest["version"] == project["version"]
    assert root_manifest["manifest_version"] == 1
    assert root_manifest["pip_dependencies"] == [
        "aiohttp>=3.14.1,<4",
        "tiktoken>=0.12,<0.14",
    ]
    assert "scripts" not in project
    assert all((ROOT / name).is_file() for name in ROOT_PLUGIN_FILES)
    assert (ROOT / "better_hermes_hindsight" / "provider.py").is_file()
    assert (ROOT / "better_hermes_hindsight" / "data" / "cl100k_base.tiktoken").is_file()
    assert not (ROOT / "scripts" / "install_release.py").exists()


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
            "--enable",
            source.as_uri(),
        ],
        cwd=tmp_path,
        environ=environ,
    )

    discovery_without_dependency = _run(
        [sys.executable, "-c", NO_RUNTIME_DEPENDENCY_DISCOVERY_PROBE],
        cwd=tmp_path,
        environ=environ,
    )
    assert discovery_without_dependency.stdout.strip().endswith(
        "_hermes_user_memory.better_hindsight.better_hermes_hindsight.provider"
    )

    _run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "memory",
            "setup",
            "better_hindsight",
        ],
        cwd=tmp_path,
        environ=environ,
    )

    installed = home / "plugins/better_hindsight"
    assert installed.is_dir()
    assert all((installed / name).is_file() for name in ROOT_PLUGIN_FILES)
    assert (installed / "better_hermes_hindsight" / "provider.py").is_file()
    assert (installed / ".git").is_dir()
    installed_commit = _run(["git", "rev-parse", "HEAD"], cwd=installed).stdout.strip()
    install_metadata = json.loads(
        (home / "plugins/.install-metadata.json").read_text(encoding="utf-8")
    )
    assert install_metadata["better_hindsight"]["revision"] == installed_commit

    installed_config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert installed_config["memory"]["provider"] == "better_hindsight"
    assert "better_hindsight" in installed_config["plugins"]["enabled"]

    probe = _run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "from plugins.memory import discover_plugin_cli_commands, load_memory_provider; "
                "provider = load_memory_provider('better_hindsight'); "
                "commands = discover_plugin_cli_commands(); "
                "print(json.dumps({'provider_name': provider.name, "
                "'provider_type': type(provider).__name__, "
                "'provider_module': type(provider).__module__, "
                "'top_level_loaded': 'better_hermes_hindsight' in sys.modules, "
                "'commands': [item['name'] for item in commands]}, sort_keys=True))"
            ),
        ],
        cwd=tmp_path,
        environ=environ,
    )
    payload = json.loads(probe.stdout.strip().splitlines()[-1])
    assert payload == {
        "commands": ["better_hindsight"],
        "provider_module": (
            "_hermes_user_memory.better_hindsight.better_hermes_hindsight.provider"
        ),
        "provider_name": "better_hindsight",
        "provider_type": "BetterHindsightMemoryProvider",
        "top_level_loaded": False,
    }

    (source / "update-marker.txt").write_text("updated\n", encoding="utf-8")
    _git("add", "update-marker.txt", cwd=source)
    _git("commit", "-m", "update fixture", cwd=source)
    _run(
        [sys.executable, "-m", "hermes_cli.main", "plugins", "update", "better_hindsight"],
        cwd=tmp_path,
        environ=environ,
    )
    assert (installed / "update-marker.txt").read_text(encoding="utf-8") == "updated\n"

    help_result = _run(
        [sys.executable, "-m", "hermes_cli.main", "better_hindsight", "--help"],
        cwd=tmp_path,
        environ=environ,
    )
    assert "{status,canary,watchdog,diagnostics,missions}" in help_result.stdout
    assert "status" in help_result.stdout
    assert "missions" in help_result.stdout

    canary_result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "better_hindsight", "canary"],
        cwd=tmp_path,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert canary_result.returncode == 1
    assert canary_result.stdout.strip() == '{"error":"not_enabled","result":"error"}'

    _run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "config",
            "set",
            "memory.provider",
            "holographic",
        ],
        cwd=tmp_path,
        environ=environ,
    )
    _run(
        [sys.executable, "-m", "hermes_cli.main", "plugins", "remove", "better_hindsight"],
        cwd=tmp_path,
        environ=environ,
    )
    assert not installed.exists()
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["memory"]["provider"] == "holographic"
