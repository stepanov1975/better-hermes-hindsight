"""Integration proof for current-Hermes memory and command discovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.hermes_compat import EXPECTED_HERMES_COMMIT, EXPECTED_HERMES_VERSION
from tests.integration.helpers import (
    clean_subprocess_env,
    materialize_standard_plugin,
    write_host_selection,
)

ROOT = Path(__file__).resolve().parents[2]

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
assert [schema["name"] for schema in provider.get_tool_schemas()] == [
    "better_hindsight_recall",
    "better_hindsight_retain",
    "better_hindsight_status",
]
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
expected_cli_module = (
    "_hermes_user_memory.better_hindsight.better_hermes_hindsight.operator_cli"
)
assert command["setup_fn"].__module__ == expected_cli_module
assert command["handler_fn"].__module__ == expected_cli_module

parser = argparse.ArgumentParser(prog="hermes better_hindsight")
command["setup_fn"](parser)
assert parser.parse_args(["status"]) is not None
assert parser.parse_args(["canary"]) is not None
assert parser.parse_args([
    "watchdog",
    "--status-json", "/tmp/status.json",
    "--canary-json", "/tmp/canary.json",
    "--events-jsonl", "/tmp/events.jsonl",
    "--state", "/tmp/state.json",
]) is not None
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


def test_current_loader_discovers_active_standard_plugin_cli_and_recall_tool(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    write_host_selection(hermes_home)
    plugin = materialize_standard_plugin(
        source=ROOT,
        hermes_home=hermes_home,
    )
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
        env=clean_subprocess_env(
            tmp_path,
            hermes_home=hermes_home,
            no_proxy="*",
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr[-4000:]
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload == {
        "cli_commands": ["better_hindsight"],
        "cli_module": ("_hermes_user_memory.better_hindsight.better_hermes_hindsight.operator_cli"),
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
            },
            {
                "description": (
                    "Durably queue one agent-selected fact, preference, decision, or convention "
                    "for long-term memory. Use only for self-contained information that should "
                    "remain useful across future sessions; do not store secrets or transient task "
                    "progress. Acceptance confirms local durable admission, not remote delivery."
                ),
                "name": "better_hindsight_retain",
                "parameters": {
                    "additionalProperties": False,
                    "properties": {
                        "content": {
                            "description": "The self-contained durable information to store.",
                            "maxLength": 8192,
                            "minLength": 1,
                            "type": "string",
                        },
                        "context": {
                            "description": (
                                "Optional short category, such as 'user preference', 'environment "
                                "fact', or 'project convention'."
                            ),
                            "maxLength": 256,
                            "minLength": 1,
                            "type": "string",
                        },
                    },
                    "required": ["content"],
                    "type": "object",
                },
            },
            {
                "description": (
                    "Inspect compact passive health for the durable Better Hindsight retention "
                    "queue. Makes no remote call and exposes extra detail only when the queue is "
                    "degraded."
                ),
                "name": "better_hindsight_status",
                "parameters": {
                    "additionalProperties": False,
                    "properties": {},
                    "type": "object",
                },
            },
        ],
        "registrations": ["better_hindsight"],
        "version": EXPECTED_HERMES_VERSION,
    }
    assert {path.name for path in plugin.iterdir()} == {
        "__init__.py",
        "after-install.md",
        "better_hermes_hindsight",
        "cli.py",
        "plugin.yaml",
    }
    assert not (hermes_home / "plugins" / "memory").exists()
    assert not (config_dir / "outbox.sqlite3").exists()
    assert not (config_dir / "outbox.sqlite3.lock").exists()


def test_current_inactive_provider_discovery_returns_no_better_command_directly(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    write_host_selection(hermes_home, "hindsight")
    plugin = materialize_standard_plugin(
        source=ROOT,
        hermes_home=hermes_home,
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _INACTIVE_DISCOVERY_SCRIPT,
            EXPECTED_HERMES_COMMIT,
            EXPECTED_HERMES_VERSION,
        ],
        cwd=tmp_path,
        env=clean_subprocess_env(
            tmp_path,
            hermes_home=hermes_home,
            no_proxy="*",
        ),
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
    assert (plugin / "better_hermes_hindsight" / "provider.py").is_file()
