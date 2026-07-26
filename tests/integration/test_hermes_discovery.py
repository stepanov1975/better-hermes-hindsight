"""Integration proof for the exact released Hermes filesystem memory loader."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import better_hermes_hindsight.hermes_plugin as packaged_plugin

RELEASE_COMMIT = "3ef6bbd201263d354fd83ec55b3c306ded2eb72a"
RELEASE_VERSION = "0.19.0"

_DISCOVERY_SCRIPT = r"""
import json
import sys
from importlib import metadata

release_commit = sys.argv[1]
release_version = sys.argv[2]

distribution = metadata.distribution("hermes-agent")
assert distribution.version == release_version
direct_url_text = distribution.read_text("direct_url.json")
assert direct_url_text is not None
direct_url = json.loads(direct_url_text)
assert direct_url["vcs_info"]["commit_id"] == release_commit

import plugins.memory as memory_loader

names = memory_loader.list_memory_provider_names()
assert names.count("better_hindsight") == 1

registrations = []
original_register = memory_loader._ProviderCollector.register_memory_provider

def recording_register(self, provider):
    registrations.append(provider.name)
    return original_register(self, provider)

memory_loader._ProviderCollector.register_memory_provider = recording_register
provider = memory_loader.load_memory_provider("better_hindsight")

assert provider is not None
assert provider.name == "better_hindsight"
assert provider.is_available() is True
assert provider.get_tool_schemas() == []
assert registrations == ["better_hindsight"]

print(json.dumps({
    "commit": direct_url["vcs_info"]["commit_id"],
    "discovered": names.count("better_hindsight"),
    "loaded": provider.name,
    "registrations": registrations,
    "version": distribution.version,
}, sort_keys=True))
"""


def _clean_subprocess_env(home: Path) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in (
            "LANG",
            "LC_ALL",
            "PATH",
            "PYTHONASYNCIODEBUG",
            "PYTHONTRACEMALLOC",
            "PYTHONWARNINGS",
            "TMPDIR",
            "TZ",
        )
        if name in os.environ
    }
    environment.update(
        {
            "HOME": str(home / "home"),
            "HERMES_HOME": str(home / "hermes-home"),
            "NO_PROXY": "*",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CACHE_HOME": str(home / "xdg-cache"),
            "XDG_CONFIG_HOME": str(home / "xdg-config"),
            "XDG_DATA_HOME": str(home / "xdg-data"),
            "XDG_STATE_HOME": str(home / "xdg-state"),
            "no_proxy": "*",
        }
    )
    return environment


def _materialize_packaged_shim(hermes_home: Path) -> Path:
    source = Path(packaged_plugin.__file__).resolve().parent
    destination = hermes_home / "plugins" / "better_hindsight"
    destination.mkdir(parents=True)
    for name in ("__init__.py", "plugin.yaml"):
        shutil.copy2(source / name, destination / name)
    return destination


def test_exact_released_loader_discovers_loads_and_registers_one_provider(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    shim = _materialize_packaged_shim(hermes_home)

    completed = subprocess.run(
        [sys.executable, "-c", _DISCOVERY_SCRIPT, RELEASE_COMMIT, RELEASE_VERSION],
        cwd=tmp_path,
        env=_clean_subprocess_env(tmp_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr[-4000:]
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload == {
        "commit": RELEASE_COMMIT,
        "discovered": 1,
        "loaded": "better_hindsight",
        "registrations": ["better_hindsight"],
        "version": RELEASE_VERSION,
    }
    assert sorted(path.name for path in shim.iterdir()) == ["__init__.py", "plugin.yaml"]
    assert not (hermes_home / "plugins" / "memory").exists()
