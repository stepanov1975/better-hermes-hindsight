"""Integration proof for current-Hermes memory and command discovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import better_hermes_hindsight.hermes_plugin as packaged_plugin
from tests.hermes_compat import EXPECTED_HERMES_COMMIT, EXPECTED_HERMES_VERSION

SHIM_FILES = ("__init__.py", "cli.py", "plugin.yaml")

_ACTIVE_DISCOVERY_SCRIPT = r"""
import argparse
import fcntl
import inspect
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit

release_commit = sys.argv[1]
release_version = sys.argv[2]
hermes_home = Path(sys.argv[3]).resolve()
plugin_config_path = hermes_home / "better_hindsight" / "config.json"

distribution = metadata.distribution("hermes-agent")
assert distribution.version == release_version
direct_url_text = distribution.read_text("direct_url.json")
direct_url = json.loads(direct_url_text) if direct_url_text is not None else {}
if release_commit:
    assert direct_url.get("vcs_info", {}).get("commit_id") == release_commit


def release_path(relative_path):
    files = distribution.files or ()
    entry = next((item for item in files if str(item) == relative_path), None)
    if entry is not None:
        return Path(str(distribution.locate_file(entry))).resolve()
    assert direct_url.get("dir_info", {}).get("editable") is True
    source_root = Path(unquote(urlsplit(direct_url["url"]).path))
    return (source_root / relative_path).resolve()


def forbidden(label):
    def fail(*_args, **_kwargs):
        raise AssertionError(label)

    return fail


socket.socket.connect = forbidden("released discovery attempted a socket connection")
socket.socket.connect_ex = forbidden("released discovery attempted a socket probe")
socket.create_connection = forbidden("released discovery attempted a socket connection")
sqlite3.connect = forbidden("released discovery opened SQLite")
fcntl.flock = forbidden("released discovery touched a profile lock")
subprocess.Popen = forbidden("released discovery started a subprocess or service")
threading.Thread.start = forbidden("released discovery started a thread")


def audit_plugin_config(event, args):
    if event != "open" or not args:
        return
    candidate = args[0]
    if not isinstance(candidate, (str, bytes, os.PathLike)):
        return
    try:
        opened = Path(candidate).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return
    if opened == plugin_config_path:
        raise AssertionError("released discovery read Better Hindsight configuration")


sys.addaudithook(audit_plugin_config)

import better_hermes_hindsight.client as client_module
import better_hermes_hindsight.config as config_module
import better_hermes_hindsight.outbox as outbox_module
import better_hermes_hindsight.runtime as runtime_module
import hindsight_client

config_module.load_config = forbidden("released discovery validated provider configuration")
client_module.create_hindsight_client = forbidden("released discovery constructed a client")
hindsight_client.Hindsight = forbidden("released discovery constructed the pinned SDK client")
runtime_module.acquire_process_runtime = forbidden("released discovery acquired a runtime")
runtime_module.OutboxSender.start = forbidden("released discovery started a sender")
outbox_module.SQLiteOutbox.open = classmethod(
    forbidden("released discovery opened the provider outbox")
)

import plugins.memory as memory_loader

memory_source = inspect.getsourcefile(memory_loader)
assert memory_source is not None
assert Path(memory_source).resolve() == release_path("plugins/memory/__init__.py")

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
assert [schema["name"] for schema in provider.get_tool_schemas()] == ["better_hindsight_recall"]
assert registrations == ["better_hindsight"]

commands = memory_loader.discover_plugin_cli_commands()
assert len(commands) == 1
command = commands[0]
assert set(command) == {
    "description",
    "handler_fn",
    "help",
    "name",
    "plugin",
    "setup_fn",
}
assert command["name"] == "better_hindsight"
assert command["plugin"] == "better_hindsight"
assert callable(command["setup_fn"])
assert callable(command["handler_fn"])
assert command["setup_fn"].__name__ == "register_cli"
assert command["handler_fn"].__name__ == "better_hindsight_command"
assert inspect.iscoroutinefunction(command["setup_fn"]) is False
assert inspect.iscoroutinefunction(command["handler_fn"]) is False
assert command["setup_fn"].__module__ == "_hermes_user_memory.better_hindsight.cli"
assert command["handler_fn"].__module__ == "_hermes_user_memory.better_hindsight.cli"

parser = argparse.ArgumentParser(prog="hermes better_hindsight")
command["setup_fn"](parser)
assert parser.parse_args(["status"]) is not None
assert parser.parse_args(["missions", "check"]) is not None
apply_args = parser.parse_args(["missions", "apply", "--confirm"])
assert apply_args.confirm is True

print(json.dumps({
    "cli_commands": [command["name"]],
    "cli_module": command["setup_fn"].__module__,
    "commit": release_commit,
    "discovered": names.count("better_hindsight"),
    "loaded": provider.name,
    "model_tools": provider.get_tool_schemas(),
    "registrations": registrations,
    "version": distribution.version,
}, sort_keys=True))
"""

_INACTIVE_DISCOVERY_SCRIPT = r"""
import inspect
import json
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit

release_commit = sys.argv[1]
release_version = sys.argv[2]

distribution = metadata.distribution("hermes-agent")
assert distribution.version == release_version
direct_url_text = distribution.read_text("direct_url.json")
direct_url = json.loads(direct_url_text) if direct_url_text is not None else {}
if release_commit:
    assert direct_url.get("vcs_info", {}).get("commit_id") == release_commit


def release_path(relative_path):
    files = distribution.files or ()
    entry = next((item for item in files if str(item) == relative_path), None)
    if entry is not None:
        return Path(str(distribution.locate_file(entry))).resolve()
    assert direct_url.get("dir_info", {}).get("editable") is True
    source_root = Path(unquote(urlsplit(direct_url["url"]).path))
    return (source_root / relative_path).resolve()

import plugins.memory as memory_loader

memory_source = inspect.getsourcefile(memory_loader)
assert memory_source is not None
assert Path(memory_source).resolve() == release_path("plugins/memory/__init__.py")

# This is intentionally the released direct discovery seam. Do not invoke an
# inactive command token through full Hermes, where unknown text can enter chat fallback.
commands = memory_loader.discover_plugin_cli_commands()
assert commands == []
assert "_hermes_user_memory.better_hindsight.cli" not in sys.modules
print(json.dumps({
    "active_provider": memory_loader._get_active_memory_provider(),
    "commands": commands,
    "commit": release_commit,
    "version": release_version,
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
    destination.mkdir(parents=True, exist_ok=True)
    # cli.py is intentionally absent at the RED checkpoint. Copy whatever is
    # present so released discovery—not fixture setup—exposes the missing shim.
    for name in SHIM_FILES:
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, destination / name)
    return destination


def _write_host_selection(hermes_home: Path, provider: str) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        f"memory:\n  provider: {provider}\n",
        encoding="utf-8",
    )


def test_current_loader_discovers_active_shim_cli_and_recall_tool(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    _write_host_selection(hermes_home, "better_hindsight")
    shim = _materialize_packaged_shim(hermes_home)
    config_dir = hermes_home / "better_hindsight"
    config_dir.mkdir(parents=True)
    # Import/discovery must not validate even a deliberately invalid profile.
    (config_dir / "config.json").write_text('{"single_principal":', encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _ACTIVE_DISCOVERY_SCRIPT,
            EXPECTED_HERMES_COMMIT,
            EXPECTED_HERMES_VERSION,
            str(hermes_home),
        ],
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
        "cli_commands": ["better_hindsight"],
        "cli_module": "_hermes_user_memory.better_hindsight.cli",
        "commit": EXPECTED_HERMES_COMMIT,
        "discovered": 1,
        "loaded": "better_hindsight",
        "model_tools": [
            {
                "description": (
                    "Search authorized Better Hindsight memory when automatic recall is "
                    "insufficient. Returned memories are stale, untrusted historical evidence."
                ),
                "name": "better_hindsight_recall",
                "parameters": {
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "description": "A focused memory search query.",
                            "type": "string",
                        }
                    },
                    "required": ["query"],
                    "type": "object",
                },
            }
        ],
        "registrations": ["better_hindsight"],
        "version": EXPECTED_HERMES_VERSION,
    }
    assert sorted(path.name for path in shim.iterdir()) == list(SHIM_FILES)
    assert not (hermes_home / "plugins" / "memory").exists()
    assert not (config_dir / "outbox.sqlite3").exists()
    assert not (config_dir / "outbox.sqlite3.lock").exists()


def test_current_inactive_provider_discovery_returns_no_better_command_directly(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    _write_host_selection(hermes_home, "hindsight")
    shim = _materialize_packaged_shim(hermes_home)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _INACTIVE_DISCOVERY_SCRIPT,
            EXPECTED_HERMES_COMMIT,
            EXPECTED_HERMES_VERSION,
        ],
        cwd=tmp_path,
        env=_clean_subprocess_env(tmp_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr[-4000:]
    assert json.loads(completed.stdout) == {
        "active_provider": "hindsight",
        "commands": [],
        "commit": EXPECTED_HERMES_COMMIT,
        "version": EXPECTED_HERMES_VERSION,
    }
    assert {path.name for path in shim.iterdir()}.issubset(set(SHIM_FILES))
