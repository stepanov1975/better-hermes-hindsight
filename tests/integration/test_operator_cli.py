"""Exact released-Hermes integration proofs for the operator CLI."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import better_hermes_hindsight.hermes_plugin as packaged_plugin
from tests.hermes_compat import EXPECTED_HERMES_COMMIT, EXPECTED_HERMES_VERSION
from tests.integration.helpers import (
    clean_subprocess_env,
    materialize_packaged_shim,
    write_host_selection,
)

SHIM_FILES = ("__init__.py", "cli.py", "plugin.yaml")
FIXTURE_BANK_ID = "operator-cli-fixture-bank"
FIXTURE_API_KEY = "synthetic-operator-cli-api-key"
DESIRED_RETAIN_MISSION = "Retain exact synthetic operator preferences."
DESIRED_OBSERVATIONS_MISSION = "Observe exact synthetic operator patterns."

EXPECTED_UNINITIALIZED_STATUS = (
    '{"age_bucket":"none","command":"status","counts":{"mismatch":0,"pending":0,'
    '"retry":0,"sending":0},"deployed":{"commit":"unknown","version":"0.1.0a1"},'
    '"error_counts":{"retain_failed":0,"retain_timeout":0,"retain_unconfirmed":0},'
    '"last_error_category":"none","logical_queued_bytes":0,"max_attempt_count":0,'
    '"next_retry_bucket":"none","outbox":"uninitialized","result":"ok",'
    '"sender_ownership":"free"}\n'
)
EXPECTED_READY_STATUS = EXPECTED_UNINITIALIZED_STATUS.replace(
    '"outbox":"uninitialized"',
    '"outbox":"ready"',
)

_RELEASED_CLI_SCRIPT = r"""
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
mode = sys.argv[3]
expected_home = Path(sys.argv[4]).resolve()
expected_outbox = (expected_home / "better_hindsight" / "outbox.sqlite3").resolve()
expect_discovery = sys.argv[5] == "1"
command_argv = sys.argv[6:]
sys.argv = ["hermes", *command_argv]

fixture_bank_id = "operator-cli-fixture-bank"
desired_retain = "Retain exact synthetic operator preferences."
desired_observations = "Observe exact synthetic operator patterns."

release = metadata.distribution("hermes-agent")
assert release.version == release_version
direct_url_text = release.read_text("direct_url.json")
if release_commit:
    assert direct_url_text is not None
    assert json.loads(direct_url_text).get("vcs_info", {}).get("commit_id") == release_commit


def release_path(relative_path):
    files = release.files or ()
    entry = next((item for item in files if str(item) == relative_path), None)
    if entry is not None:
        return Path(str(release.locate_file(entry))).resolve()
    assert direct_url_text is not None
    direct_url = json.loads(direct_url_text)
    assert direct_url.get("dir_info", {}).get("editable") is True
    source_root = Path(unquote(urlsplit(direct_url["url"]).path))
    return (source_root / relative_path).resolve()


def forbidden_network(*_args, **_kwargs):
    raise AssertionError("released operator CLI attempted a real socket operation")


socket.socket.connect = forbidden_network
socket.socket.connect_ex = forbidden_network
socket.create_connection = forbidden_network

plugin_config_path = expected_home / "better_hindsight" / "config.json"
config_forbidden_modes = {"help"}


def audit_plugin_config(event, args):
    if event != "open" or mode not in config_forbidden_modes or not args:
        return
    candidate = args[0]
    if not isinstance(candidate, (str, bytes, os.PathLike)):
        return
    try:
        opened = Path(candidate).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return
    if opened == plugin_config_path:
        raise AssertionError("CLI import/help/argparse read Better Hindsight configuration")


sys.addaudithook(audit_plugin_config)

import hermes_cli.main as released_main

main_source = inspect.getsourcefile(released_main)
assert main_source is not None
assert Path(main_source).resolve() == release_path("hermes_cli/main.py")
assert Path(os.environ["HERMES_HOME"]).resolve() == expected_home


def forbidden(label):
    def fail(*_args, **_kwargs):
        raise AssertionError(label)

    return fail


subprocess.Popen = forbidden("operator CLI attempted to start a subprocess or service")
original_sqlite_connect = sqlite3.connect
sqlite_calls = []


def guarded_sqlite_connect(database, *args, **kwargs):
    sqlite_calls.append(database)
    if mode != "status_ready":
        raise AssertionError("operator CLI opened SQLite outside the passive status seam")
    assert type(database) is str
    assert database == f"{expected_outbox.as_uri()}?mode=ro&immutable=1&vfs=unix"
    assert kwargs.get("uri") is True
    return original_sqlite_connect(database, *args, **kwargs)


sqlite3.connect = guarded_sqlite_connect

original_flock = fcntl.flock
flock_calls = []


def guarded_flock(descriptor, operation):
    if mode != "status_ready":
        raise AssertionError("operator CLI acquired or probed a lock on a forbidden path")
    assert operation in {fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN}
    flock_calls.append(operation)
    return original_flock(descriptor, operation)


fcntl.flock = guarded_flock

import better_hermes_hindsight.client as client_module
import better_hermes_hindsight.config as config_module
import better_hermes_hindsight.outbox as outbox_module
import better_hermes_hindsight.runtime as runtime_module

runtime_module.acquire_process_runtime = forbidden(
    "operator CLI used the provider process singleton runtime"
)
outbox_module.SQLiteOutbox.open = classmethod(
    forbidden("operator CLI opened the read-write provider outbox")
)
runtime_module.OutboxSender.start = forbidden("operator CLI started a sender")

no_client_modes = {
    "help",
    "status_ready",
    "status_uninitialized",
}
if mode in no_client_modes:
    import hindsight_client

    client_module.create_hindsight_client = forbidden(
        "local/import/help CLI path constructed a Hindsight client"
    )
    hindsight_client.Hindsight = forbidden(
        "local/import/help CLI path constructed the pinned SDK client"
    )

if mode in config_forbidden_modes:
    config_module.load_config = forbidden(
        "CLI import/help/argparse validated Better Hindsight configuration"
    )

original_thread_start = threading.Thread.start
started_threads = []


def guarded_thread_start(thread):
    if "outbox-sender" in thread.name:
        raise AssertionError("operator CLI started a sender thread")
    started_threads.append(thread.name)
    return original_thread_start(thread)


threading.Thread.start = guarded_thread_start

sdk_instances = []
sdk_modes = {"apply_verified", "check_equal"}

if mode in sdk_modes:
    import hindsight_client
    from hindsight_client_api.models.bank_config_response import BankConfigResponse

    class FakeBanks:
        def __init__(self):
            retain_value = desired_retain if mode == "check_equal" else "remote retain mission"
            self.state = {
                "observations_mission": desired_observations,
                "retain_mission": retain_value,
            }
            self.calls = []

        def response(self):
            return BankConfigResponse(
                bank_id=fixture_bank_id,
                config=dict(self.state),
                overrides={},
            )

        async def get_bank_config(self, **kwargs):
            assert kwargs == {"bank_id": fixture_bank_id}
            self.calls.append("get")
            return self.response()

        async def update_bank_config(self, **kwargs):
            assert kwargs["bank_id"] == fixture_bank_id
            request = kwargs["bank_config_update"]
            updates = request.updates
            assert type(updates) is dict
            self.calls.append("patch")
            self.state.update(updates)
            return self.response()

    class FakeHindsight:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://127.0.0.1:9"
            assert kwargs["api_key"] == "synthetic-operator-cli-api-key"
            self.banks = FakeBanks()
            self.close_calls = 0
            sdk_instances.append(self)

        async def aclose(self):
            self.close_calls += 1

    hindsight_client.Hindsight = FakeHindsight

caught = None
try:
    released_main.main()
except SystemExit as error:
    caught = error

cli_module_name = "_hermes_user_memory.better_hindsight.cli"
assert (cli_module_name in sys.modules) is expect_discovery
if expect_discovery:
    discovered_module = sys.modules[cli_module_name]
    assert Path(discovered_module.__file__).resolve() == (
        expected_home / "plugins" / "better_hindsight" / "cli.py"
    ).resolve()
    assert inspect.iscoroutinefunction(discovered_module.register_cli) is False
    assert inspect.iscoroutinefunction(discovered_module.better_hindsight_command) is False

expected_calls = {
    "apply_verified": ["get", "patch", "get"],
    "check_equal": ["get"],
}
if mode in expected_calls:
    assert len(sdk_instances) == 1
    assert sdk_instances[0].banks.calls == expected_calls[mode]
    assert sdk_instances[0].close_calls == 1
else:
    assert sdk_instances == []

if mode in sdk_modes:
    assert started_threads == ["better-hindsight-event-loop"]
    assert not any(
        thread.name.startswith("better-hindsight-") and thread.is_alive()
        for thread in threading.enumerate()
    )
else:
    assert started_threads == []

if mode == "status_ready":
    assert len(sqlite_calls) == 1, sqlite_calls
    assert flock_calls
    assert flock_calls[0] == fcntl.LOCK_EX | fcntl.LOCK_NB
else:
    assert sqlite_calls == []
    assert flock_calls == []

if caught is not None:
    raise SystemExit(caught.code)
"""


def _write_plugin_config(
    hermes_home: Path,
    *,
    single_principal: bool = True,
    missions: Mapping[str, str] | None = None,
    malformed: bool = False,
) -> Path:
    config_dir = hermes_home / "better_hindsight"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    if malformed:
        config_path.write_text('{"single_principal":', encoding="utf-8")
        return config_path

    document: dict[str, object] = {
        "api_url": "http://127.0.0.1:9",
        "bank_id": FIXTURE_BANK_ID,
        "recall": {"enabled": False},
        "retain": {"enabled": False, "timeout_seconds": 0.5},
        "single_principal": single_principal,
    }
    if missions is not None:
        document["missions"] = dict(missions)
    config_path.write_text(
        json.dumps(document, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return config_path


def _snapshot_tree(root: Path) -> dict[str, tuple[str, int, int, int, str | None]]:
    if not root.exists():
        return {}
    paths = [root, *sorted(root.rglob("*"))]
    snapshot: dict[str, tuple[str, int, int, int, str | None]] = {}
    for path in paths:
        details = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_file():
            kind = "file"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            kind = "directory"
            digest = None
        elif path.is_symlink():
            kind = "symlink"
            digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
        else:
            kind = "other"
            digest = None
        snapshot[relative] = (
            kind,
            stat.S_IMODE(details.st_mode),
            details.st_size,
            details.st_mtime_ns,
            digest,
        )
    return snapshot


def _run_released_cli(
    root: Path,
    *,
    expected_home: Path,
    argv: Sequence[str],
    mode: str,
    expect_discovery: bool,
    exported_hermes_home: Path | None,
    extra_environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _RELEASED_CLI_SCRIPT,
            EXPECTED_HERMES_COMMIT,
            EXPECTED_HERMES_VERSION,
            mode,
            str(expected_home),
            "1" if expect_discovery else "0",
            *argv,
        ],
        cwd=root,
        env=clean_subprocess_env(
            root,
            hermes_home=exported_hermes_home,
            no_proxy="*",
            extra={
                "HERMES_DISABLE_UPDATE_CHECK": "1",
                "HERMES_QUIET": "1",
                **(extra_environment or {}),
            },
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_handler_output(
    completed: subprocess.CompletedProcess[str],
    *,
    exit_code: int,
    stdout: str,
) -> None:
    assert completed.returncode == exit_code, completed.stderr[-4000:]
    assert completed.stdout == stdout
    assert completed.stderr == ""
    assert len(completed.stdout.encode("utf-8")) <= 1024
    decoded = json.loads(completed.stdout)
    assert (
        json.dumps(decoded, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        == completed.stdout
    )


def _prepare_explicit_home(root: Path) -> tuple[Path, Path]:
    hermes_home = root / "hermes-home"
    write_host_selection(hermes_home)
    shim = materialize_packaged_shim(
        source=Path(packaged_plugin.__file__).resolve().parent,
        hermes_home=hermes_home,
        names=SHIM_FILES,
    )
    return hermes_home, shim


def _initialize_empty_outbox(hermes_home: Path) -> None:
    from better_hermes_hindsight.config import load_config
    from better_hermes_hindsight.outbox import SQLiteOutbox

    config = load_config(
        hermes_home=hermes_home,
        environ={"HINDSIGHT_API_KEY": FIXTURE_API_KEY},
    )
    outbox = SQLiteOutbox.open(config)
    outbox.close()


def test_top_level_help_skips_plugin_discovery_and_better_side_effects(tmp_path: Path) -> None:
    hermes_home, shim = _prepare_explicit_home(tmp_path)
    _write_plugin_config(hermes_home, malformed=True)
    before_shim = _snapshot_tree(shim)
    before_profile = _snapshot_tree(hermes_home / "better_hindsight")

    completed = _run_released_cli(
        tmp_path,
        expected_home=hermes_home,
        argv=["--help"],
        mode="help",
        expect_discovery=False,
        exported_hermes_home=hermes_home,
    )

    assert completed.returncode == 0, completed.stderr[-4000:]
    assert completed.stderr == ""
    assert "Hermes Agent - AI assistant" in completed.stdout
    assert "better_hindsight" not in completed.stdout
    assert _snapshot_tree(shim) == before_shim
    assert _snapshot_tree(hermes_home / "better_hindsight") == before_profile


def test_released_plugin_help_discovers_only_the_underscore_command_without_side_effects(
    tmp_path: Path,
) -> None:
    hermes_home, shim = _prepare_explicit_home(tmp_path)
    _write_plugin_config(hermes_home, malformed=True)
    before_shim = _snapshot_tree(shim)
    before_profile = _snapshot_tree(hermes_home / "better_hindsight")

    completed = _run_released_cli(
        tmp_path,
        expected_home=hermes_home,
        argv=["better_hindsight", "--help"],
        mode="help",
        expect_discovery=True,
        exported_hermes_home=hermes_home,
    )

    assert completed.returncode == 0, completed.stderr[-4000:]
    assert completed.stderr == ""
    assert "usage: hermes better_hindsight" in completed.stdout
    assert "better-hindsight" not in completed.stdout
    assert _snapshot_tree(shim) == before_shim
    assert _snapshot_tree(hermes_home / "better_hindsight") == before_profile


def test_named_profile_selection_drives_discovery_and_absent_status_without_creating_state(
    tmp_path: Path,
) -> None:
    default_home = tmp_path / "home" / ".hermes"
    write_host_selection(default_home, provider="hindsight")
    selected_home = default_home / "profiles" / "operator_fixture"
    write_host_selection(selected_home)
    shim = materialize_packaged_shim(
        source=Path(packaged_plugin.__file__).resolve().parent,
        hermes_home=selected_home,
        names=SHIM_FILES,
    )
    _write_plugin_config(selected_home)
    before_shim = _snapshot_tree(shim)
    before_profile = _snapshot_tree(selected_home / "better_hindsight")

    completed = _run_released_cli(
        tmp_path,
        expected_home=selected_home,
        argv=["--profile", "operator_fixture", "better_hindsight", "status"],
        mode="status_uninitialized",
        expect_discovery=True,
        exported_hermes_home=None,
    )

    _assert_handler_output(completed, exit_code=0, stdout=EXPECTED_UNINITIALIZED_STATUS)
    assert _snapshot_tree(shim) == before_shim
    assert _snapshot_tree(selected_home / "better_hindsight") == before_profile
    assert not (selected_home / "better_hindsight" / "outbox.sqlite3").exists()
    assert not (selected_home / "better_hindsight" / "outbox.sqlite3.lock").exists()


def test_existing_status_uses_only_read_only_sqlite_and_existing_lock_probe_without_mutation(
    tmp_path: Path,
) -> None:
    hermes_home, shim = _prepare_explicit_home(tmp_path)
    _write_plugin_config(hermes_home)
    _initialize_empty_outbox(hermes_home)
    before_shim = _snapshot_tree(shim)
    before_profile = _snapshot_tree(hermes_home / "better_hindsight")

    completed = _run_released_cli(
        tmp_path,
        expected_home=hermes_home,
        argv=["better_hindsight", "status"],
        mode="status_ready",
        expect_discovery=True,
        exported_hermes_home=hermes_home,
    )

    _assert_handler_output(completed, exit_code=0, stdout=EXPECTED_READY_STATUS)
    assert _snapshot_tree(shim) == before_shim
    assert _snapshot_tree(hermes_home / "better_hindsight") == before_profile


def test_sync_check_handler_preserves_released_host_success(tmp_path: Path) -> None:
    hermes_home, _shim = _prepare_explicit_home(tmp_path)
    _write_plugin_config(
        hermes_home,
        missions={
            "observations_mission": DESIRED_OBSERVATIONS_MISSION,
            "retain_mission": DESIRED_RETAIN_MISSION,
        },
    )

    completed = _run_released_cli(
        tmp_path,
        expected_home=hermes_home,
        argv=["better_hindsight", "missions", "check"],
        mode="check_equal",
        expect_discovery=True,
        exported_hermes_home=hermes_home,
        extra_environment={"HINDSIGHT_API_KEY": FIXTURE_API_KEY},
    )

    _assert_handler_output(
        completed,
        exit_code=0,
        stdout=(
            '{"command":"missions_check","observations_mission":"equal",'
            '"result":"equal","retain_mission":"equal"}\n'
        ),
    )


def test_sync_apply_handler_preserves_verified_success(tmp_path: Path) -> None:
    hermes_home, _shim = _prepare_explicit_home(tmp_path)
    _write_plugin_config(
        hermes_home,
        missions={"retain_mission": DESIRED_RETAIN_MISSION},
    )

    completed = _run_released_cli(
        tmp_path,
        expected_home=hermes_home,
        argv=["better_hindsight", "missions", "apply", "--confirm"],
        mode="apply_verified",
        expect_discovery=True,
        exported_hermes_home=hermes_home,
        extra_environment={"HINDSIGHT_API_KEY": FIXTURE_API_KEY},
    )

    _assert_handler_output(
        completed,
        exit_code=0,
        stdout='{"command":"missions_apply","outcome":"verified_success","result":"ok"}\n',
    )
