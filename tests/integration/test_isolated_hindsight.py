"""Opt-in proof against an operator-supplied isolated Hindsight development instance.

Deterministic tests exercise every mutation guard with fakes. The live proof runs only in an
operator-selected dedicated Hermes interpreter and receives no inherited generic ``HINDSIGHT_*``
configuration. It is intentionally one bounded proof, not a provisioning or evaluation framework.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import unquote, urlsplit

import pytest

from better_hermes_hindsight.config import (
    BetterHindsightConfig,
    derive_destination_fingerprint,
    load_config,
    normalize_api_url,
)
from better_hermes_hindsight.management import ManagementResult, apply_missions, check_missions
from better_hermes_hindsight.outbox import OutboxRow, ProfileLockOwner, SQLiteOutbox
from better_hermes_hindsight.retention import RetainedSegment, build_retained_segments
from better_hermes_hindsight.runtime import finalize_process_runtime
from tests.fakes.hindsight_server import FakeHindsightServer, ProfileFault

ROOT = Path(__file__).resolve().parents[2]
_RELEASED_HERMES_VERSION = "0.19.0"
_RELEASED_HERMES_COMMIT = "3ef6bbd201263d354fd83ec55b3c306ded2eb72a"
_HINDSIGHT_VERSION = "0.8.5"
_REQUIRED_ACK = "dedicated-interpreter-and-datastore"
_BANK_PATTERN = re.compile(r"better-hindsight-dev-[0-9a-f]{32}\Z")
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_RETAIN_TAGS = ("kind:isolated-proof", "source:task6")
_SEGMENT_MAX_BYTES = 512
_CHILD_TIMEOUT_SECONDS = 240.0
_DRAIN_TIMEOUT_SECONDS = 45.0
_OWNERSHIP_MARKER_ENV = "BETTER_HINDSIGHT_INTERNAL_CLEANUP_OWNERSHIP"
_OWNERSHIP_MARKER_NAME = "bank-cleanup-owned"
_REQUIRE_LIVE_PROOF_ENV = "BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF"
_ROOT_PLUGIN_FILES = ("__init__.py", "cli.py", "plugin.yaml")
_EXPLICIT_DEVELOPMENT_VARIABLES = (
    "BETTER_HINDSIGHT_ALLOW_DEV_WRITES",
    "BETTER_HINDSIGHT_DEV_API_KEY",
    "BETTER_HINDSIGHT_DEV_API_URL",
    "BETTER_HINDSIGHT_DEV_BANK_ID",
    "BETTER_HINDSIGHT_DEV_DESTINATION_FINGERPRINT",
    "BETTER_HINDSIGHT_DEV_ENDPOINT_ALLOWLIST",
    "BETTER_HINDSIGHT_DEV_HERMES_PYTHON",
    "BETTER_HINDSIGHT_DEV_ISOLATION_ACK",
    "BETTER_HINDSIGHT_DEV_WHEEL_SHA256",
)
_SAFE_INHERITED_VARIABLES = ("LANG", "LC_ALL", "PATH")
_SYNTHETIC_TURNS = (
    (
        "synthetic-task6-session-alpha",
        "For the synthetic Northstar rehearsal, the recovery phrase is cobalt lantern.",
        "Recorded the synthetic Northstar recovery phrase as cobalt lantern.",
    ),
    (
        "synthetic-task6-session-long",
        "Synthetic long-source rehearsal notes: " + "amber-orbit checkpoint " * 80,
        "The synthetic long-source rehearsal uses amber-orbit checkpoints only.",
    ),
)
_RESTART_TURN = (
    "synthetic-task6-session-restart",
    "Synthetic restart rehearsal queues the silver compass note before process replacement.",
    "Recorded the synthetic silver compass restart note.",
)
_RETAIN_MISSION = "Retain only synthetic Task 6 rehearsal material for this disposable bank."
_OBSERVATIONS_MISSION = "Observe only synthetic Task 6 rehearsal relationships."
T = TypeVar("T")


class DevelopmentGuardError(RuntimeError):
    """A fixed, non-secret development-isolation guard failure."""


class RuntimeSettlementFailure(RuntimeError):
    """A runtime could not be settled safely before remote cleanup."""


class ProcessTreeSettlementFailure(RuntimeError):
    """The bounded child process tree could not be proven absent."""


class ChildProcessTreeProtocolFailure(RuntimeError):
    """A child left a contained descendant running after its leader exited."""


@dataclass(frozen=True, slots=True)
class DevelopmentInputs:
    """Explicit operator inputs; repr omits destination, credential, and bank values."""

    hermes_python: Path = field(repr=False)
    api_url: str = field(repr=False)
    api_key: str = field(repr=False)
    bank_id: str = field(repr=False)
    destination_fingerprint: str = field(repr=False)
    wheel_sha256: str = field(repr=False)
    endpoint_allowlist: tuple[str, ...] = field(default=(), repr=False)


def _development_environment(**overrides: str) -> dict[str, str]:
    api_url = overrides.get("BETTER_HINDSIGHT_DEV_API_URL", "http://127.0.0.1:8000")
    bank_id = overrides.get(
        "BETTER_HINDSIGHT_DEV_BANK_ID",
        "better-hindsight-dev-0123456789abcdef0123456789abcdef",
    )
    values = {
        "BETTER_HINDSIGHT_ALLOW_DEV_WRITES": "1",
        "BETTER_HINDSIGHT_DEV_API_KEY": "synthetic-development-key",
        "BETTER_HINDSIGHT_DEV_API_URL": api_url,
        "BETTER_HINDSIGHT_DEV_BANK_ID": bank_id,
        "BETTER_HINDSIGHT_DEV_DESTINATION_FINGERPRINT": derive_destination_fingerprint(
            api_url=api_url,
            bank_id=bank_id,
            retain_tags=_RETAIN_TAGS,
        ),
        "BETTER_HINDSIGHT_DEV_ENDPOINT_ALLOWLIST": "[]",
        "BETTER_HINDSIGHT_DEV_HERMES_PYTHON": os.fspath(Path(os.devnull)),
        "BETTER_HINDSIGHT_DEV_ISOLATION_ACK": _REQUIRED_ACK,
        "BETTER_HINDSIGHT_DEV_WHEEL_SHA256": "a" * 64,
    }
    values.update(overrides)
    return values


def _development_inputs_from_environment(
    environ: Mapping[str, str],
) -> DevelopmentInputs | None:
    """Return inputs only when every explicit live-write gate is present and exact."""

    if environ.get("BETTER_HINDSIGHT_ALLOW_DEV_WRITES") != "1":
        return None
    if environ.get("BETTER_HINDSIGHT_DEV_ISOLATION_ACK") != _REQUIRED_ACK:
        return None
    if any(not environ.get(name) for name in _EXPLICIT_DEVELOPMENT_VARIABLES):
        return None

    raw_allowlist = environ["BETTER_HINDSIGHT_DEV_ENDPOINT_ALLOWLIST"]
    try:
        decoded_allowlist = json.loads(raw_allowlist)
    except (TypeError, ValueError):
        raise DevelopmentGuardError("development endpoint allowlist is invalid") from None
    if not isinstance(decoded_allowlist, list) or not all(
        isinstance(item, str) and item for item in decoded_allowlist
    ):
        raise DevelopmentGuardError("development endpoint allowlist is invalid")

    return DevelopmentInputs(
        hermes_python=Path(environ["BETTER_HINDSIGHT_DEV_HERMES_PYTHON"]),
        api_url=environ["BETTER_HINDSIGHT_DEV_API_URL"],
        api_key=environ["BETTER_HINDSIGHT_DEV_API_KEY"],
        bank_id=environ["BETTER_HINDSIGHT_DEV_BANK_ID"],
        destination_fingerprint=environ["BETTER_HINDSIGHT_DEV_DESTINATION_FINGERPRINT"],
        wheel_sha256=environ["BETTER_HINDSIGHT_DEV_WHEEL_SHA256"],
        endpoint_allowlist=tuple(decoded_allowlist),
    )


def _normalize_allowlist(values: Sequence[str]) -> tuple[str, ...]:
    try:
        return tuple(normalize_api_url(value) for value in values)
    except Exception:
        raise DevelopmentGuardError("development endpoint allowlist is invalid") from None


def _is_literal_loopback(api_url: str) -> bool:
    hostname = urlsplit(api_url).hostname
    if hostname is None:
        return False
    normalized_hostname = hostname.rstrip(".").casefold()
    if normalized_hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized_hostname).is_loopback
    except ValueError:
        return False


def _validate_static_development_guards(inputs: DevelopmentInputs) -> None:
    try:
        normalized_url = normalize_api_url(inputs.api_url)
    except Exception:
        raise DevelopmentGuardError("development endpoint is invalid") from None
    if not _is_literal_loopback(normalized_url) and normalized_url not in _normalize_allowlist(
        inputs.endpoint_allowlist
    ):
        raise DevelopmentGuardError("development endpoint is not authorized")
    if _BANK_PATTERN.fullmatch(inputs.bank_id) is None:
        raise DevelopmentGuardError("development bank identifier is not generated")
    if _FINGERPRINT_PATTERN.fullmatch(inputs.destination_fingerprint) is None:
        raise DevelopmentGuardError("development destination fingerprint is invalid")
    if _FINGERPRINT_PATTERN.fullmatch(inputs.wheel_sha256) is None:
        raise DevelopmentGuardError("reviewed wheel digest is invalid")

    expected = derive_destination_fingerprint(
        api_url=normalized_url,
        bank_id=inputs.bank_id,
        retain_tags=_RETAIN_TAGS,
    )
    if not hmac.compare_digest(inputs.destination_fingerprint, expected):
        raise DevelopmentGuardError("development destination fingerprint does not match")


def _guard_then_create_bank(
    inputs: DevelopmentInputs,
    *,
    bank_exists: Callable[[], bool],
    create_bank: Callable[[], T],
) -> T:
    """Fail closed through static guards and an exact absence read before one create."""

    _validate_static_development_guards(inputs)
    if bank_exists():
        raise DevelopmentGuardError("generated development bank already exists")
    return create_bank()


@contextmanager
def _exclusive_live_proof_lock(lock_path: Path | None = None) -> Iterator[None]:
    """Serialize local proof writers; remote instances still require an exclusive API key."""

    path = lock_path or Path(f"/tmp/better-hindsight-task6-{os.geteuid()}.lock")
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise DevelopmentGuardError("local proof writer lock could not be opened") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise DevelopmentGuardError("local proof writer lock is not trusted")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DevelopmentGuardError("another local proof writer is active") from None
        except OSError:
            raise DevelopmentGuardError("local proof writer lock could not be acquired") from None
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _sanitized_generated_id(bank_id: str) -> str:
    return "dev-" + hashlib.sha256(bank_id.encode("utf-8")).hexdigest()[:12]


def _ownership_marker_path(temporary_home: Path) -> Path:
    return temporary_home / _OWNERSHIP_MARKER_NAME


def _remote_ownership_name(ownership_token: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", ownership_token) is None:
        raise AssertionError("cleanup ownership token is invalid")
    return "better-hindsight-task6-owned-" + ownership_token


def _claim_cleanup_ownership(marker: Path, inputs: DevelopmentInputs) -> str:
    ownership_token = secrets.token_hex(16)
    payload = (f"v1:{_sanitized_generated_id(inputs.bank_id)}:{ownership_token}\n").encode("ascii")
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("cleanup ownership marker write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        if _read_cleanup_ownership(marker, inputs) != ownership_token:
            raise OSError("cleanup ownership marker verification failed")
    except BaseException:
        with suppress(OSError):
            marker.unlink(missing_ok=True)
        raise
    return ownership_token


def _read_cleanup_ownership(marker: Path, inputs: DevelopmentInputs) -> str | None:
    try:
        payload = marker.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    match = re.fullmatch(r"v1:(dev-[0-9a-f]{12}):([0-9a-f]{32})\n", payload)
    if match is None or not hmac.compare_digest(
        match.group(1), _sanitized_generated_id(inputs.bank_id)
    ):
        return None
    return match.group(2)


def _cleanup_ownership_is_valid(marker: Path, inputs: DevelopmentInputs) -> bool:
    return _read_cleanup_ownership(marker, inputs) is not None


def _clear_cleanup_ownership(
    marker: Path,
    inputs: DevelopmentInputs,
    ownership_token: str,
) -> None:
    stored_token = _read_cleanup_ownership(marker, inputs)
    if stored_token is None or not hmac.compare_digest(stored_token, ownership_token):
        raise AssertionError("cleanup ownership marker is invalid")
    marker.unlink()


def _child_ownership_marker(home: Path) -> Path:
    expected = _ownership_marker_path(home.parent).resolve()
    supplied = os.environ.get(_OWNERSHIP_MARKER_ENV)
    if supplied is None or Path(supplied).resolve() != expected:
        raise DevelopmentGuardError("cleanup ownership marker path is invalid")
    return expected


def _build_sanitized_child_environment(
    inputs: DevelopmentInputs,
    *,
    inherited: Mapping[str, str],
    temporary_home: Path,
) -> dict[str, str]:
    """Admit a tiny non-secret base plus explicit development values.

    Never admit generic Hindsight state.
    """

    child = {name: inherited[name] for name in _SAFE_INHERITED_VARIABLES if inherited.get(name)}
    child.update(
        {
            "HOME": os.fspath(temporary_home / "process-home"),
            "HERMES_HOME": os.fspath(temporary_home / "hermes-home"),
            "NO_PROXY": "*",
            _OWNERSHIP_MARKER_ENV: os.fspath(_ownership_marker_path(temporary_home)),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.fspath(ROOT),
            "TMPDIR": os.fspath(temporary_home / "tmp"),
            "no_proxy": "*",
        }
    )
    for name in _EXPLICIT_DEVELOPMENT_VARIABLES:
        value = {
            "BETTER_HINDSIGHT_ALLOW_DEV_WRITES": "1",
            "BETTER_HINDSIGHT_DEV_API_KEY": inputs.api_key,
            "BETTER_HINDSIGHT_DEV_API_URL": inputs.api_url,
            "BETTER_HINDSIGHT_DEV_BANK_ID": inputs.bank_id,
            "BETTER_HINDSIGHT_DEV_DESTINATION_FINGERPRINT": inputs.destination_fingerprint,
            "BETTER_HINDSIGHT_DEV_ENDPOINT_ALLOWLIST": json.dumps(
                list(inputs.endpoint_allowlist), separators=(",", ":")
            ),
            "BETTER_HINDSIGHT_DEV_HERMES_PYTHON": os.fspath(inputs.hermes_python),
            "BETTER_HINDSIGHT_DEV_ISOLATION_ACK": _REQUIRED_ACK,
            "BETTER_HINDSIGHT_DEV_WHEEL_SHA256": inputs.wheel_sha256,
        }[name]
        child[name] = value
    return child


def _prepare_temporary_home(temporary_home: Path) -> None:
    if _ownership_marker_path(temporary_home).exists():
        raise AssertionError("cleanup ownership marker already exists")
    for path in (
        temporary_home,
        temporary_home / "process-home",
        temporary_home / "tmp",
        temporary_home / "hermes-home",
    ):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    plugin_dir = temporary_home / "hermes-home/plugins/better_hindsight"
    plugin_dir.mkdir(parents=True, mode=0o700)
    for name in _ROOT_PLUGIN_FILES:
        shutil.copy2(ROOT / name, plugin_dir / name)


def _assert_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        raise DevelopmentGuardError("dedicated Hermes interpreter is unavailable") from None
    if not stat.S_ISREG(mode) or mode & 0o111 == 0:
        raise DevelopmentGuardError("dedicated Hermes interpreter is unavailable")


def _model_payload(value: object) -> dict[str, Any]:
    model = cast(Any, value)
    payload = model.model_dump(mode="json")
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise AssertionError("public SDK response did not produce an object")
    return cast(dict[str, Any], payload)


def _run_async(operation: Callable[[], Any]) -> Any:
    async def invoke() -> Any:
        result = operation()
        if not hasattr(result, "__await__"):
            raise AssertionError("public SDK operation was not asynchronous")
        return await result

    return asyncio.run(invoke())


def _new_sdk(inputs: DevelopmentInputs) -> Any:
    from hindsight_client import Hindsight

    return Hindsight(
        base_url=inputs.api_url,
        api_key=inputs.api_key,
        timeout=30.0,
        user_agent="better-hermes-hindsight-isolated-proof",
    )


def _api_status(exception: Exception) -> int | None:
    status = getattr(exception, "status", None)
    return status if type(status) is int else None


def _bank_exists(inputs: DevelopmentInputs) -> bool:
    from hindsight_client_api.exceptions import ApiException

    async def probe() -> bool:
        sdk = _new_sdk(inputs)
        try:
            try:
                await sdk.banks.get_bank_profile(bank_id=inputs.bank_id)
            except ApiException as exception:
                if _api_status(exception) == 404:
                    return False
                raise
            return True
        finally:
            await sdk.aclose()

    return asyncio.run(probe())


def _create_bank(inputs: DevelopmentInputs, ownership_token: str) -> None:
    remote_name = _remote_ownership_name(ownership_token)

    async def create() -> None:
        sdk = _new_sdk(inputs)
        try:
            response = await sdk.acreate_bank(
                bank_id=inputs.bank_id,
                name=remote_name,
            )
            payload = _model_payload(response)
            if payload.get("bank_id") != inputs.bank_id or payload.get("name") != remote_name:
                raise AssertionError("bank create ownership confirmation did not match")
        finally:
            await sdk.aclose()

    asyncio.run(create())


def _delete_bank_and_confirm_absent(
    inputs: DevelopmentInputs,
    ownership_token: str,
) -> None:
    from hindsight_client_api.exceptions import ApiException

    remote_name = _remote_ownership_name(ownership_token)

    async def delete() -> bool:
        sdk = _new_sdk(inputs)
        try:
            try:
                profile = await sdk.banks.get_bank_profile(bank_id=inputs.bank_id)
            except ApiException as exception:
                if _api_status(exception) == 404:
                    return False
                raise
            payload = _model_payload(profile)
            if payload.get("bank_id") != inputs.bank_id or payload.get("name") != remote_name:
                raise AssertionError("remote bank cleanup ownership did not match")
            try:
                await sdk.adelete_bank(bank_id=inputs.bank_id)
            except ApiException as exception:
                if _api_status(exception) != 404:
                    raise
            return True
        finally:
            await sdk.aclose()

    deleted = asyncio.run(delete())
    if deleted and _bank_exists(inputs):
        raise AssertionError("disposable bank still exists after cleanup")


def _assert_release_identities(inputs: DevelopmentInputs) -> None:
    from importlib import metadata

    if metadata.version("hermes-agent") != _RELEASED_HERMES_VERSION:
        raise AssertionError("dedicated interpreter has the wrong Hermes release")
    direct_url_text = metadata.distribution("hermes-agent").read_text("direct_url.json")
    if direct_url_text is None:
        raise AssertionError("dedicated interpreter lacks Hermes release provenance")
    direct_url = json.loads(direct_url_text)
    if direct_url.get("vcs_info", {}).get("commit_id") != _RELEASED_HERMES_COMMIT:
        raise AssertionError("dedicated interpreter has the wrong Hermes source commit")
    if metadata.version("hindsight-client") != _HINDSIGHT_VERSION:
        raise AssertionError("dedicated interpreter has the wrong Hindsight SDK")

    better_distribution = metadata.distribution("better-hermes-hindsight")
    better_direct_url_text = better_distribution.read_text("direct_url.json")
    if better_direct_url_text is None:
        raise AssertionError("dedicated interpreter lacks reviewed wheel provenance")
    better_direct_url = json.loads(better_direct_url_text)
    archive_info = better_direct_url.get("archive_info")
    if not isinstance(archive_info, dict):
        raise AssertionError("dedicated interpreter did not install Better from a wheel archive")
    hashes = archive_info.get("hashes")
    installed_wheel_sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
    if installed_wheel_sha256 is None:
        legacy_hash = archive_info.get("hash")
        if isinstance(legacy_hash, str) and legacy_hash.startswith("sha256="):
            installed_wheel_sha256 = legacy_hash.removeprefix("sha256=")
    if installed_wheel_sha256 is None:
        direct_url = better_direct_url.get("url")
        parsed_direct_url = urlsplit(direct_url) if isinstance(direct_url, str) else None
        if (
            parsed_direct_url is None
            or parsed_direct_url.scheme != "file"
            or parsed_direct_url.netloc not in {"", "localhost"}
        ):
            raise AssertionError("installed Better wheel has no verifiable archive provenance")
        archive_path = Path(unquote(parsed_direct_url.path))
        if not archive_path.is_file():
            raise AssertionError("installed Better wheel archive is unavailable for attestation")
        with archive_path.open("rb") as archive_file:
            installed_wheel_sha256 = hashlib.file_digest(
                archive_file,
                "sha256",
            ).hexdigest()
    if not hmac.compare_digest(
        installed_wheel_sha256,
        inputs.wheel_sha256,
    ):
        raise AssertionError("dedicated interpreter has the wrong Better wheel")

    import better_hermes_hindsight
    from better_hermes_hindsight import provider as better_provider

    package_root = Path(str(better_distribution.locate_file("better_hermes_hindsight"))).resolve()
    imported_paths = (
        Path(better_hermes_hindsight.__file__).resolve(),
        Path(better_provider.__file__).resolve(),
    )
    if any(not imported.is_relative_to(package_root) for imported in imported_paths):
        raise AssertionError("Better modules do not resolve inside the reviewed wheel")

    async def version() -> None:
        sdk = _new_sdk(inputs)
        try:
            payload = _model_payload(await sdk.aget_version())
            if payload.get("api_version") != _HINDSIGHT_VERSION:
                raise AssertionError("development Hindsight server has the wrong version")
        finally:
            await sdk.aclose()

    asyncio.run(version())


def _profile_document(*, retention_enabled: bool, api_url: str, bank_id: str) -> dict[str, Any]:
    return {
        "api_url": api_url,
        "bank_id": bank_id,
        "single_principal": True,
        "recall": {
            "enabled": True,
            "timeout_seconds": 8.0,
            "input_max_chars": 2048,
            "context_max_bytes": 16384,
            "types": ["world", "experience", "observation"],
            "tags": list(_RETAIN_TAGS),
            "tag_mode": "all_strict",
            "include_source_facts": True,
            "max_source_facts_tokens": 4096,
        },
        "retain": {
            "enabled": retention_enabled,
            "timeout_seconds": 30.0,
            "segment_max_bytes": _SEGMENT_MAX_BYTES,
            "tags": list(_RETAIN_TAGS),
        },
        "missions": {
            "retain_mission": _RETAIN_MISSION,
            "observations_mission": _OBSERVATIONS_MISSION,
        },
        "outbox": {
            "max_pending_rows": 100,
            "max_pending_bytes": 1_000_000,
            "busy_timeout_seconds": 0.2,
            "poll_interval_seconds": 0.1,
            "retry_initial_seconds": 0.2,
            "retry_max_seconds": 0.5,
        },
    }


def _write_profile(
    home: Path,
    inputs: DevelopmentInputs,
    *,
    retention_enabled: bool,
    api_url: str | None = None,
) -> BetterHindsightConfig:
    config_dir = home / "better_hindsight"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = _profile_document(
        retention_enabled=retention_enabled,
        api_url=inputs.api_url if api_url is None else api_url,
        bank_id=inputs.bank_id,
    )
    (config_dir / "config.json").write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    (home / "config.yaml").write_text("memory:\n  provider: better_hindsight\n", encoding="utf-8")
    return load_config(home, environ={"HINDSIGHT_API_KEY": inputs.api_key})


def _install_temporary_bridge(home: Path) -> None:
    plugin_dir = home / "plugins/better_hindsight"
    plugin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in _ROOT_PLUGIN_FILES:
        shutil.copy2(ROOT / name, plugin_dir / name)


def _start_released_manager(home: Path) -> tuple[Any, Any]:
    from agent.memory_manager import MemoryManager  # type: ignore[import-untyped]
    from plugins.memory import load_memory_provider  # type: ignore[import-untyped]

    os.environ["HERMES_HOME"] = os.fspath(home)
    provider = load_memory_provider("better_hindsight")
    if provider is None or provider.name != "better_hindsight":
        raise AssertionError("released Hermes did not discover Better Hindsight")
    manager = MemoryManager()
    try:
        manager.add_provider(provider)
        manager.initialize_all(
            "synthetic-task6-initial-session",
            hermes_home=os.fspath(home),
            platform="cli",
            agent_context="primary",
        )
        if [registered.name for registered in manager.providers] != ["better_hindsight"]:
            raise AssertionError("released Hermes did not select Better Hindsight")
        return manager, provider
    except BaseException:
        _stop_released_manager(manager)
        raise


def _stop_released_manager(
    manager: Any,
    *,
    finalizer: Callable[[], bool] = finalize_process_runtime,
) -> None:
    settlement_error: Exception | None = None
    try:
        manager.shutdown_all()
    except Exception as exc:
        settlement_error = exc
    try:
        if finalizer() is not True and settlement_error is None:
            settlement_error = AssertionError("Better Hindsight process runtime did not finalize")
    except Exception as exc:
        if settlement_error is None:
            settlement_error = exc
    if settlement_error is not None:
        raise RuntimeSettlementFailure(
            "Better Hindsight runtime did not settle"
        ) from settlement_error


def _sync_turn(manager: Any, turn: tuple[str, str, str]) -> None:
    session_id, user_content, assistant_content = turn
    manager.sync_all(user_content, assistant_content, session_id=session_id)
    if manager.flush_pending(timeout=5.0) is not True:
        raise AssertionError("released Hermes did not finish the local retention callback")


def _read_rows(config: BetterHindsightConfig) -> tuple[OutboxRow, ...]:
    inspector = SQLiteOutbox.open(config)
    try:
        return inspector.read_unconfirmed()
    finally:
        inspector.close()


def _wait_for_rows(
    config: BetterHindsightConfig,
    predicate: Callable[[tuple[OutboxRow, ...]], bool],
) -> tuple[OutboxRow, ...]:
    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    while True:
        rows = _read_rows(config)
        if predicate(rows):
            return rows
        if time.monotonic() >= deadline:
            raise AssertionError("bounded outbox wait expired")
        time.sleep(0.05)


def _acquire_sender_barrier(config: BetterHindsightConfig) -> ProfileLockOwner:
    bootstrap = SQLiteOutbox.open(config)
    try:
        acquisition = bootstrap.try_acquire_profile_lock()
        if not acquisition.acquired or acquisition.owner is None:
            raise AssertionError("could not reserve the disposable profile sender lock")
        return acquisition.owner
    finally:
        bootstrap.close()


def _expected_segments(turns: Sequence[tuple[str, str, str]]) -> tuple[RetainedSegment, ...]:
    return tuple(
        segment
        for session_id, user_content, assistant_content in turns
        for segment in build_retained_segments(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
            tags=_RETAIN_TAGS,
            segment_max_bytes=_SEGMENT_MAX_BYTES,
        )
    )


def _remote_documents(
    inputs: DevelopmentInputs,
    expected_segments: Sequence[RetainedSegment],
) -> dict[str, dict[str, Any]]:
    expected_ids = {segment.document_id for segment in expected_segments}

    async def read() -> dict[str, dict[str, Any]]:
        sdk = _new_sdk(inputs)
        try:
            listed = _model_payload(
                await sdk.documents.list_documents(bank_id=inputs.bank_id, limit=100, offset=0)
            )
            items = listed.get("items")
            if not isinstance(items, list) or listed.get("total") != len(expected_ids):
                raise AssertionError("unexpected document count in disposable bank")
            listed_ids = {
                item.get("id")
                for item in items
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if listed_ids != expected_ids:
                raise AssertionError("disposable bank document identities did not converge")
            documents: dict[str, dict[str, Any]] = {}
            for document_id in sorted(expected_ids):
                documents[document_id] = _model_payload(
                    await sdk.documents.get_document(
                        bank_id=inputs.bank_id, document_id=document_id
                    )
                )
            return documents
        finally:
            await sdk.aclose()

    return asyncio.run(read())


def _stable_document_projection(
    documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep only replace-stable source identity, content, and provenance fields."""

    fields = (
        "content_hash",
        "document_metadata",
        "id",
        "observation_scopes",
        "original_text",
        "tags",
    )
    return {
        document_id: {field: document.get(field) for field in fields}
        for document_id, document in documents.items()
    }


def _assert_long_source_reconstructs(
    expected_segments: Sequence[RetainedSegment],
    documents: Mapping[str, Mapping[str, Any]],
) -> None:
    grouped = [segment for segment in expected_segments if segment.segment_count > 1]
    if not grouped:
        raise AssertionError("synthetic long turn did not exceed the segment limit")
    source_digest = grouped[0].source_sha256
    source_segments = sorted(
        (segment for segment in grouped if segment.source_sha256 == source_digest),
        key=lambda segment: segment.segment_index,
    )
    reconstructed: list[str] = []
    for segment in source_segments:
        payload = documents[segment.document_id]
        metadata = payload.get("document_metadata")
        if not isinstance(metadata, dict):
            raise AssertionError("public document metadata is unavailable")
        expected_metadata = {
            "better_hindsight_payload_schema": segment.payload_schema,
            "better_hindsight_segment_count": str(segment.segment_count),
            "better_hindsight_segment_index": str(segment.segment_index),
            "better_hindsight_source_sha256": segment.source_sha256,
        }
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            raise AssertionError("remote reconstruction metadata did not match")
        original_text = payload.get("original_text")
        if not isinstance(original_text, str):
            raise AssertionError("public document text is unavailable")
        reconstructed.append(original_text)
    digest = hashlib.sha256("".join(reconstructed).encode("utf-8")).hexdigest()
    if digest != source_digest:
        raise AssertionError("reconstructed long source digest did not match")


def _bank_config_payload(inputs: DevelopmentInputs) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        sdk = _new_sdk(inputs)
        try:
            response = _model_payload(await sdk.banks.get_bank_config(bank_id=inputs.bank_id))
            config = response.get("config")
            if not isinstance(config, dict):
                raise AssertionError("public bank configuration is unavailable")
            return cast(dict[str, Any], config)
        finally:
            await sdk.aclose()

    return asyncio.run(read())


def _require_mission_runtime_settlement(result: ManagementResult) -> None:
    """Defer cleanup when a public mission result cannot prove runtime quiescence."""

    if (
        result.payload.get("error") == "runtime_cleanup_failed"
        or result.payload.get("outcome") == "write_attempted_outcome_unknown"
    ):
        raise RuntimeSettlementFailure("mission command did not prove operator-runtime settlement")


def _assert_mission_update_is_narrow(
    config: BetterHindsightConfig,
    inputs: DevelopmentInputs,
) -> None:
    from better_hermes_hindsight.client import create_hindsight_client

    async def establish_drift() -> None:
        client = create_hindsight_client(config)
        try:
            await client.update_bank_missions(
                {
                    "retain_mission": "Synthetic drifted retain mission for Task 6.",
                    "observations_mission": "Synthetic drifted observation mission for Task 6.",
                }
            )
        finally:
            await client.close()

    asyncio.run(establish_drift())
    before = _bank_config_payload(inputs)
    if (
        before.get("retain_mission") == _RETAIN_MISSION
        or before.get("observations_mission") == _OBSERVATIONS_MISSION
    ):
        raise AssertionError("synthetic mission fixture was not drifted in both intended fields")
    checked = check_missions(config)
    _require_mission_runtime_settlement(checked)
    if checked.exit_code != 1 or checked.payload != {
        "command": "missions_check",
        "observations_mission": "drift",
        "result": "drift",
        "retain_mission": "drift",
    }:
        raise AssertionError("synthetic mission check did not report exactly two drifted fields")
    applied = apply_missions(config, confirmed=True)
    _require_mission_runtime_settlement(applied)
    if applied.exit_code != 0 or applied.payload != {
        "command": "missions_apply",
        "outcome": "verified_success",
        "result": "ok",
    }:
        raise AssertionError("synthetic mission apply did not report a verified patch")
    after = _bank_config_payload(inputs)
    expected = dict(before)
    expected["retain_mission"] = _RETAIN_MISSION
    expected["observations_mission"] = _OBSERVATIONS_MISSION
    if after != expected:
        raise AssertionError("mission apply changed fields outside the intended pair")
    checked_again = check_missions(config)
    _require_mission_runtime_settlement(checked_again)
    if checked_again.exit_code != 0:
        raise AssertionError("synthetic missions did not verify after apply")


def _assert_recall_useful_and_provenanced(provider: Any) -> None:
    provider.queue_prefetch("stale synthetic query")
    context = provider.prefetch(
        "What recovery phrase is used by the synthetic Northstar rehearsal?"
    )
    normalized = context.casefold()
    if "cobalt lantern" not in normalized:
        raise AssertionError("fixed synthetic recall was not useful")
    if '"type":' not in context or '"source_fact_count":' not in context:
        raise AssertionError("fixed synthetic recall lacked provenance")


def _assert_bounded_first_call_fail_open(inputs: DevelopmentInputs, root_home: Path) -> None:
    fail_home = root_home / "fail-open-profile"
    fail_home.mkdir(parents=True, mode=0o700)
    _install_temporary_bridge(fail_home)
    _write_profile(
        fail_home,
        inputs,
        retention_enabled=False,
        api_url="http://127.0.0.1:9",
    )
    manager, provider = _start_released_manager(fail_home)
    try:
        started = time.monotonic()
        context = provider.prefetch("synthetic current query must fail open")
        elapsed = time.monotonic() - started
        if context != "" or elapsed > 2.0:
            raise AssertionError("first-call recall did not fail open within its bound")
    finally:
        _stop_released_manager(manager)


def _run_restart_convergence_child() -> int:
    """Drain pending work in a fresh interpreter using the existing disposable profile."""

    inputs = _development_inputs_from_environment(os.environ)
    if inputs is None:
        print(json.dumps({"phase": "gates", "status": "failed"}, sort_keys=True))
        return 2
    home = Path(os.environ["HERMES_HOME"])
    manager: Any | None = None
    try:
        _validate_static_development_guards(inputs)
        _assert_release_identities(inputs)
        os.environ["HINDSIGHT_API_KEY"] = inputs.api_key
        config = load_config(home, environ={"HINDSIGHT_API_KEY": inputs.api_key})
        manager, _ = _start_released_manager(home)
        _wait_for_rows(config, lambda rows: not rows)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "failure": type(exc).__name__,
                    "phase": "restart-convergence",
                    "status": "failed",
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        if manager is not None:
            _stop_released_manager(manager)
    print(json.dumps({"status": "ok"}, sort_keys=True))
    return 0


def _run_restart_convergence_process(inputs: DevelopmentInputs, home: Path) -> None:
    environment = _build_sanitized_child_environment(
        inputs,
        inherited=os.environ,
        temporary_home=home.parent,
    )
    result = subprocess.run(
        [
            os.fspath(inputs.hermes_python),
            "-c",
            (
                "from tests.integration.test_isolated_hindsight import "
                "_run_restart_convergence_child; "
                "raise SystemExit(_run_restart_convergence_child())"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=_DRAIN_TIMEOUT_SECONDS + 30.0,
    )
    if any(
        value in result.stdout or value in result.stderr
        for value in (inputs.api_url, inputs.api_key, inputs.bank_id)
    ):
        raise AssertionError("restart child emitted a private development value")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        raise AssertionError("restart child returned no sanitized result") from None
    if result.returncode != 0 or payload != {"status": "ok"}:
        raise AssertionError("fresh restart child did not converge pending work")


def _execute_live_proof(
    inputs: DevelopmentInputs,
    home: Path,
    report_phase: Callable[[str], None],
) -> dict[str, int | str]:
    report_phase("identity")
    _assert_release_identities(inputs)
    os.environ["HINDSIGHT_API_KEY"] = inputs.api_key
    report_phase("profile")
    config = _write_profile(home, inputs, retention_enabled=True)
    expected_initial = _expected_segments(_SYNTHETIC_TURNS)
    expected_all = _expected_segments((*_SYNTHETIC_TURNS, _RESTART_TURN))

    report_phase("sender-barrier")
    barrier = _acquire_sender_barrier(config)
    barrier_released = False
    manager: Any | None = None
    try:
        report_phase("discovery")
        manager, provider = _start_released_manager(home)
        report_phase("admission")
        for turn in _SYNTHETIC_TURNS:
            _sync_turn(manager, turn)
        admitted = _wait_for_rows(config, lambda rows: len(rows) == len(expected_initial))
        if any(row.state != "pending" or row.attempt_count != 0 for row in admitted):
            raise AssertionError("local callback admission was not observed before sender work")
        barrier.release()
        barrier_released = True

        report_phase("initial-delivery")
        _wait_for_rows(config, lambda rows: not rows)
        report_phase("documents")
        first_documents = _remote_documents(inputs, expected_initial)
        _assert_long_source_reconstructs(expected_initial, first_documents)
        report_phase("recall")
        _assert_recall_useful_and_provenanced(provider)

        report_phase("replay")
        _sync_turn(manager, _SYNTHETIC_TURNS[0])
        _wait_for_rows(config, lambda rows: not rows)
        replay_documents = _remote_documents(inputs, expected_initial)
        if _stable_document_projection(replay_documents) != _stable_document_projection(
            first_documents
        ):
            raise AssertionError("byte-identical callback did not replace safely")
        replay_ids = {segment.document_id for segment in _expected_segments((_SYNTHETIC_TURNS[0],))}
        if any(
            replay_documents[document_id].get("updated_at")
            == first_documents[document_id].get("updated_at")
            for document_id in replay_ids
        ):
            raise AssertionError("byte-identical callback did not reach remote replace")
    finally:
        try:
            if manager is not None:
                _stop_released_manager(manager)
        finally:
            if not barrier_released:
                barrier.release()

    report_phase("restart-admission")
    restart_barrier = _acquire_sender_barrier(config)
    pending_manager: Any | None = None
    try:
        pending_manager, _ = _start_released_manager(home)
        _sync_turn(pending_manager, _RESTART_TURN)
        pending = _wait_for_rows(
            config,
            lambda rows: len(rows) == len(_expected_segments((_RESTART_TURN,))),
        )
        if any(row.state != "pending" or row.attempt_count != 0 for row in pending):
            raise AssertionError("restart fixture was not durably pending")
    finally:
        try:
            if pending_manager is not None:
                _stop_released_manager(pending_manager)
        finally:
            restart_barrier.release()

    report_phase("restart-convergence")
    _run_restart_convergence_process(inputs, home)
    converged_documents = _remote_documents(inputs, expected_all)
    _assert_long_source_reconstructs(expected_all, converged_documents)

    report_phase("missions")
    _assert_mission_update_is_narrow(config, inputs)

    report_phase("retention-disable")
    disabled_config = _write_profile(home, inputs, retention_enabled=False)
    if _read_rows(disabled_config):
        raise AssertionError("outbox was not drained before retention disablement")
    disabled_manager, _ = _start_released_manager(home)
    try:
        _sync_turn(
            disabled_manager,
            (
                "synthetic-task6-session-disabled",
                "Synthetic retention is disabled for this later callback.",
                "This synthetic callback must not write.",
            ),
        )
    finally:
        _stop_released_manager(disabled_manager)
    if _read_rows(disabled_config):
        raise AssertionError("disabled retention admitted local work")
    if _stable_document_projection(
        _remote_documents(inputs, expected_all)
    ) != _stable_document_projection(converged_documents):
        raise AssertionError("disabled retention changed remote documents")

    report_phase("fail-open")
    _assert_bounded_first_call_fail_open(inputs, home.parent)
    return {
        "documents": len(converged_documents),
        "segments": len(expected_all),
        "status": "ok",
    }


def _run_live_child() -> int:
    inputs = _development_inputs_from_environment(os.environ)
    if inputs is None:
        print(
            json.dumps(
                {
                    "failure": "DevelopmentGuardError",
                    "phase": "gates",
                    "status": "failed",
                },
                sort_keys=True,
            )
        )
        return 2
    home = Path(os.environ["HERMES_HOME"])
    ownership_marker = _child_ownership_marker(home)
    phase = "preflight"
    proof_result: dict[str, int | str] | None = None
    proof_failed = False
    failure_phase: str | None = None
    failure_kind: str | None = None
    try:
        _validate_static_development_guards(inputs)
        _assert_executable(inputs.hermes_python)
        phase = "bank-create"

        def create() -> None:
            ownership_token = _claim_cleanup_ownership(ownership_marker, inputs)
            _create_bank(inputs, ownership_token)

        _guard_then_create_bank(
            inputs,
            bank_exists=lambda: _bank_exists(inputs),
            create_bank=create,
        )
        phase = "proof"

        def report_proof_phase(stage: str) -> None:
            nonlocal phase
            phase = f"proof-{stage}"

        proof_result = _execute_live_proof(inputs, home, report_proof_phase)
    except RuntimeSettlementFailure as exc:
        proof_failed = True
        failure_phase = phase
        failure_kind = type(exc).__name__
    except Exception as exc:
        proof_failed = True
        failure_phase = phase
        failure_kind = type(exc).__name__

    if proof_failed or proof_result is None:
        print(
            json.dumps(
                {
                    "failure": failure_kind or "unknown",
                    "phase": failure_phase or phase,
                    "status": "failed",
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(proof_result, sort_keys=True))
    return 0


def test_live_inputs_are_absent_unless_every_explicit_gate_is_present() -> None:
    assert _development_inputs_from_environment({}) is None
    partial = _development_environment()
    partial.pop("BETTER_HINDSIGHT_DEV_API_KEY")
    assert _development_inputs_from_environment(partial) is None


def test_fake_server_static_guard_failure_sends_zero_requests() -> None:
    async def scenario() -> None:
        bank_id = "better-hindsight-dev-0123456789abcdef0123456789abcdef"
        server = FakeHindsightServer(
            bank_id=bank_id,
            disposable_bank_id=bank_id,
            error_sentinel="synthetic-profile-error",
            expected_api_key="synthetic-development-key",
        )
        await server.start()
        try:
            inputs = _development_inputs_from_environment(
                _development_environment(
                    BETTER_HINDSIGHT_DEV_API_URL=server.base_url,
                    BETTER_HINDSIGHT_DEV_DESTINATION_FINGERPRINT="f" * 64,
                )
            )
            assert inputs is not None
            with pytest.raises(DevelopmentGuardError, match="does not match"):
                await asyncio.to_thread(
                    _guard_then_create_bank,
                    inputs,
                    bank_exists=lambda: _bank_exists(inputs),
                    create_bank=lambda: _create_bank(inputs, "0" * 32),
                )
            assert server.safe_report().request_count == 0
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    ["http_401", "http_403", "http_500", "malformed_json", "malformed_schema"],
)
def test_fake_server_failed_absence_probe_sends_no_mutation(fault: ProfileFault) -> None:
    from hindsight_client_api.exceptions import ApiException, ApiTypeError, ApiValueError
    from pydantic import ValidationError

    async def scenario() -> None:
        bank_id = "better-hindsight-dev-0123456789abcdef0123456789abcdef"
        server = FakeHindsightServer(
            bank_id=bank_id,
            disposable_bank_id=bank_id,
            error_sentinel="synthetic-profile-error",
            expected_api_key="synthetic-development-key",
        )
        await server.start()
        try:
            inputs = _development_inputs_from_environment(
                _development_environment(BETTER_HINDSIGHT_DEV_API_URL=server.base_url)
            )
            assert inputs is not None
            server.arm_profile_fault(fault)
            with pytest.raises(
                (ApiException, ApiTypeError, ApiValueError, ValidationError, json.JSONDecodeError)
            ):
                await asyncio.to_thread(
                    _guard_then_create_bank,
                    inputs,
                    bank_exists=lambda: _bank_exists(inputs),
                    create_bank=lambda: _create_bank(inputs, "0" * 32),
                )
            assert server.safe_report().routes == (f"GET /v1/default/banks/{bank_id}/profile",)
        finally:
            await server.close()

    asyncio.run(scenario())


def test_fake_server_existing_bank_guard_reads_once_without_mutation() -> None:
    async def scenario() -> None:
        bank_id = "better-hindsight-dev-0123456789abcdef0123456789abcdef"
        server = FakeHindsightServer(
            bank_id=bank_id,
            disposable_bank_id=bank_id,
            error_sentinel="synthetic-profile-error",
            expected_api_key="synthetic-development-key",
        )
        await server.start()
        try:
            inputs = _development_inputs_from_environment(
                _development_environment(BETTER_HINDSIGHT_DEV_API_URL=server.base_url)
            )
            assert inputs is not None
            with pytest.raises(DevelopmentGuardError, match="already exists"):
                await asyncio.to_thread(
                    _guard_then_create_bank,
                    inputs,
                    bank_exists=lambda: _bank_exists(inputs),
                    create_bank=lambda: _create_bank(inputs, "0" * 32),
                )
            assert server.safe_report().routes == (f"GET /v1/default/banks/{bank_id}/profile",)
        finally:
            await server.close()

    asyncio.run(scenario())


def test_fake_server_absent_bank_allows_one_guarded_create() -> None:
    async def scenario() -> None:
        bank_id = "better-hindsight-dev-0123456789abcdef0123456789abcdef"
        server = FakeHindsightServer(
            bank_id=bank_id,
            disposable_bank_id=bank_id,
            error_sentinel="synthetic-profile-error",
            expected_api_key="synthetic-development-key",
        )
        await server.start()
        try:
            inputs = _development_inputs_from_environment(
                _development_environment(BETTER_HINDSIGHT_DEV_API_URL=server.base_url)
            )
            assert inputs is not None
            server.arm_profile_fault("not_found")
            await asyncio.to_thread(
                _guard_then_create_bank,
                inputs,
                bank_exists=lambda: _bank_exists(inputs),
                create_bank=lambda: _create_bank(inputs, "0" * 32),
            )
            assert server.safe_report().routes == (
                f"GET /v1/default/banks/{bank_id}/profile",
                f"PUT /v1/default/banks/{bank_id}",
            )
        finally:
            await server.close()

    asyncio.run(scenario())


def test_post_marker_pre_create_timeout_sends_no_mutation(tmp_path: Path) -> None:
    async def scenario() -> None:
        bank_id = "better-hindsight-dev-0123456789abcdef0123456789abcdef"
        server = FakeHindsightServer(
            bank_id=bank_id,
            disposable_bank_id=bank_id,
            error_sentinel="synthetic-profile-error",
            expected_api_key="synthetic-development-key",
        )
        await server.start()
        try:
            inputs = _development_inputs_from_environment(
                _development_environment(BETTER_HINDSIGHT_DEV_API_URL=server.base_url)
            )
            assert inputs is not None
            _claim_cleanup_ownership(_ownership_marker_path(tmp_path), inputs)
            server.arm_profile_fault("not_found")

            def time_out(
                _command: Sequence[str],
                _cwd: Path,
                _environment: Mapping[str, str],
                _timeout: float,
            ) -> subprocess.CompletedProcess[str]:
                raise subprocess.TimeoutExpired(cmd="synthetic-child", timeout=1.0)

            with pytest.raises(AssertionError, match="fixed child-process bound"):
                await asyncio.to_thread(
                    _run_bounded_live_child,
                    inputs,
                    tmp_path,
                    process_runner=time_out,
                )
            report = server.safe_report()
            assert report.routes == (f"GET /v1/default/banks/{bank_id}/profile",)
            assert not _ownership_marker_path(tmp_path).exists()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_cleanup_requires_matching_remote_ownership_witness(tmp_path: Path) -> None:
    async def scenario() -> None:
        bank_id = "better-hindsight-dev-0123456789abcdef0123456789abcdef"
        server = FakeHindsightServer(
            bank_id=bank_id,
            disposable_bank_id=bank_id,
            error_sentinel="synthetic-profile-error",
            expected_api_key="synthetic-development-key",
        )
        await server.start()
        try:
            inputs = _development_inputs_from_environment(
                _development_environment(BETTER_HINDSIGHT_DEV_API_URL=server.base_url)
            )
            assert inputs is not None
            marker = _ownership_marker_path(tmp_path)
            ownership_token = _claim_cleanup_ownership(marker, inputs)
            with pytest.raises(AssertionError, match="parent cleanup failed"):
                await asyncio.to_thread(_parent_cleanup_if_owned, inputs, tmp_path)
            assert server.safe_report().routes == (f"GET /v1/default/banks/{bank_id}/profile",)
            assert marker.exists()
            _clear_cleanup_ownership(marker, inputs, ownership_token)
        finally:
            await server.close()

    asyncio.run(scenario())


def test_cleanup_deletes_only_matching_remote_ownership_witness() -> None:
    async def scenario() -> None:
        bank_id = "better-hindsight-dev-0123456789abcdef0123456789abcdef"
        server = FakeHindsightServer(
            bank_id=bank_id,
            disposable_bank_id=bank_id,
            error_sentinel="synthetic-profile-error",
            expected_api_key="synthetic-development-key",
        )
        await server.start()
        try:
            inputs = _development_inputs_from_environment(
                _development_environment(BETTER_HINDSIGHT_DEV_API_URL=server.base_url)
            )
            assert inputs is not None
            ownership_token = "a" * 32
            server.arm_profile_fault("not_found")
            await asyncio.to_thread(
                _guard_then_create_bank,
                inputs,
                bank_exists=lambda: _bank_exists(inputs),
                create_bank=lambda: _create_bank(inputs, ownership_token),
            )
            await asyncio.to_thread(
                _delete_bank_and_confirm_absent,
                inputs,
                ownership_token,
            )
            assert server.safe_report().routes == (
                f"GET /v1/default/banks/{bank_id}/profile",
                f"PUT /v1/default/banks/{bank_id}",
                f"GET /v1/default/banks/{bank_id}/profile",
                f"DELETE /v1/default/banks/{bank_id}",
                f"GET /v1/default/banks/{bank_id}/profile",
            )
        finally:
            await server.close()

    asyncio.run(scenario())


def test_non_loopback_endpoint_requires_exact_development_allowlist() -> None:
    inputs = _development_inputs_from_environment(
        _development_environment(
            BETTER_HINDSIGHT_DEV_API_URL="https://dev-memory.example.invalid",
            BETTER_HINDSIGHT_DEV_ENDPOINT_ALLOWLIST="[]",
        )
    )
    assert inputs is not None
    mutations: list[str] = []

    with pytest.raises(DevelopmentGuardError, match="not authorized"):
        _guard_then_create_bank(
            inputs,
            bank_exists=lambda: False,
            create_bank=lambda: mutations.append("create"),
        )

    assert mutations == []


def test_destination_fingerprint_mismatch_fails_before_read_or_mutation() -> None:
    inputs = _development_inputs_from_environment(
        _development_environment(BETTER_HINDSIGHT_DEV_DESTINATION_FINGERPRINT="f" * 64)
    )
    assert inputs is not None
    events: list[str] = []

    def bank_exists() -> bool:
        events.append("read")
        return False

    with pytest.raises(DevelopmentGuardError, match="does not match"):
        _guard_then_create_bank(
            inputs,
            bank_exists=bank_exists,
            create_bank=lambda: events.append("create"),
        )

    assert events == []


def test_existing_generated_bank_fails_before_mutation() -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    events: list[str] = []

    def bank_exists() -> bool:
        events.append("read")
        return True

    with pytest.raises(DevelopmentGuardError, match="already exists"):
        _guard_then_create_bank(
            inputs,
            bank_exists=bank_exists,
            create_bank=lambda: events.append("create"),
        )

    assert events == ["read"]


def test_concurrent_local_proof_writer_fails_before_mutation(tmp_path: Path) -> None:
    lock_path = tmp_path / "proof-writer.lock"
    mutations: list[str] = []

    with (
        _exclusive_live_proof_lock(lock_path),
        pytest.raises(DevelopmentGuardError, match="another local proof writer"),
        _exclusive_live_proof_lock(lock_path),
    ):
        mutations.append("create")

    assert mutations == []


def test_cleanup_ownership_claim_completes_short_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    marker = _ownership_marker_path(tmp_path)
    real_write = os.write
    writes = 0

    def short_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        return real_write(descriptor, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(os, "write", short_write)
    ownership_token = _claim_cleanup_ownership(marker, inputs)

    assert writes > 1
    assert _read_cleanup_ownership(marker, inputs) == ownership_token


def test_incomplete_cleanup_ownership_blocks_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    marker = _ownership_marker_path(tmp_path)
    real_write = os.write
    write_calls = 0
    mutations: list[str] = []

    def partial_then_stall(descriptor: int, payload: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return real_write(descriptor, payload[:1])
        return 0

    def claim_then_create() -> None:
        _claim_cleanup_ownership(marker, inputs)
        mutations.append("create")

    monkeypatch.setattr(os, "write", partial_then_stall)
    with pytest.raises(OSError, match="made no progress"):
        claim_then_create()

    assert mutations == []
    assert not marker.exists()


def test_exact_allowlisted_development_endpoint_can_reach_guarded_create() -> None:
    endpoint = "https://dev-memory.example.invalid/"
    inputs = _development_inputs_from_environment(
        _development_environment(
            BETTER_HINDSIGHT_DEV_API_URL=endpoint,
            BETTER_HINDSIGHT_DEV_ENDPOINT_ALLOWLIST=json.dumps(
                ["https://DEV-memory.example.invalid"]
            ),
        )
    )
    assert inputs is not None
    events: list[str] = []

    def bank_exists() -> bool:
        events.append("read")
        return False

    _guard_then_create_bank(
        inputs,
        bank_exists=bank_exists,
        create_bank=lambda: events.append("create"),
    )

    assert events == ["read", "create"]


def test_live_subprocess_environment_drops_inherited_generic_hindsight_values(
    tmp_path: Path,
) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    inherited = {
        "PATH": "/synthetic/bin",
        "LANG": "C.UTF-8",
        "HINDSIGHT_API_KEY": "must-not-cross",
        "HINDSIGHT_API_URL": "https://production.example.invalid",
        "HINDSIGHT_BANK_ID": "production-bank",
        "UNRELATED_SECRET": "must-not-cross",
    }

    child = _build_sanitized_child_environment(inputs, inherited=inherited, temporary_home=tmp_path)

    assert child["PATH"] == inherited["PATH"]
    assert child["BETTER_HINDSIGHT_DEV_API_KEY"] == "synthetic-development-key"
    assert all(not name.startswith("HINDSIGHT_") for name in child)
    assert "UNRELATED_SECRET" not in child


def test_stable_document_projection_ignores_server_timestamps_but_not_source() -> None:
    original = {
        "synthetic-doc": {
            "content_hash": "synthetic-hash",
            "created_at": "first",
            "document_metadata": {"source_sha256": "synthetic-digest"},
            "id": "synthetic-doc",
            "observation_scopes": [],
            "original_text": "synthetic source",
            "tags": list(_RETAIN_TAGS),
            "updated_at": "first",
        }
    }
    replaced = {key: dict(value) for key, value in original.items()}
    replaced["synthetic-doc"]["updated_at"] = "second"
    assert _stable_document_projection(replaced) == _stable_document_projection(original)

    replaced["synthetic-doc"]["original_text"] = "different synthetic source"
    assert _stable_document_projection(replaced) != _stable_document_projection(original)


def test_cleanup_identifier_is_fixed_length_and_does_not_reveal_bank() -> None:
    bank_id = "better-hindsight-dev-0123456789abcdef0123456789abcdef"
    rendered = _sanitized_generated_id(bank_id)

    assert rendered.startswith("dev-")
    assert len(rendered) == 16
    assert bank_id not in rendered


def test_manager_finalizer_runs_when_shutdown_fails() -> None:
    events: list[str] = []

    class FailingManager:
        def shutdown_all(self) -> None:
            events.append("shutdown")
            raise RuntimeError("synthetic shutdown failure")

    def finalize() -> bool:
        events.append("finalize")
        return True

    with pytest.raises(RuntimeSettlementFailure, match="runtime did not settle"):
        _stop_released_manager(FailingManager(), finalizer=finalize)
    assert events == ["shutdown", "finalize"]


def test_raw_process_tree_error_is_typed_as_unsettled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group containment is POSIX-only")

    class SyntheticProcess:
        pid = 12345

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            del timeout
            return "", ""

    def deny_signal(_process_group: int, _signal: int) -> None:
        raise PermissionError("synthetic process-group failure")

    monkeypatch.setattr(os, "killpg", deny_signal)
    with pytest.raises(ProcessTreeSettlementFailure, match="could not be settled") as raised:
        _terminate_owned_process_tree(cast(Any, SyntheticProcess()))
    assert isinstance(raised.value.__cause__, PermissionError)


def _parent_cleanup_or_raise(inputs: DevelopmentInputs, ownership_token: str) -> None:
    try:
        _delete_bank_and_confirm_absent(inputs, ownership_token)
    except Exception:
        raise AssertionError(
            "parent cleanup failed; resource=" + _sanitized_generated_id(inputs.bank_id)
        ) from None


def _parent_cleanup_if_owned(
    inputs: DevelopmentInputs,
    temporary_home: Path,
    *,
    cleanup: Callable[[DevelopmentInputs, str], None] = _parent_cleanup_or_raise,
) -> bool:
    marker = _ownership_marker_path(temporary_home)
    ownership_token = _read_cleanup_ownership(marker, inputs)
    if ownership_token is None:
        return False
    cleanup(inputs, ownership_token)
    _clear_cleanup_ownership(marker, inputs, ownership_token)
    return True


def _owned_process_group_is_absent(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    return False


def _terminate_owned_process_tree_impl(process: subprocess.Popen[str]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
    grace_deadline = time.monotonic() + 1.0
    while not _owned_process_group_is_absent(process.pid):
        if time.monotonic() >= grace_deadline:
            break
        time.sleep(0.01)
    if _owned_process_group_is_absent(process.pid):
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while not _owned_process_group_is_absent(process.pid):
        if time.monotonic() >= deadline:
            raise ProcessTreeSettlementFailure("isolated proof process tree did not terminate")
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        time.sleep(0.01)


def _terminate_owned_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        _terminate_owned_process_tree_impl(process)
    except ProcessTreeSettlementFailure:
        raise
    except BaseException as exc:
        raise ProcessTreeSettlementFailure(
            "isolated proof process tree could not be settled"
        ) from exc


def _run_owned_process_tree(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    if os.name != "posix":
        raise DevelopmentGuardError("isolated proof process-tree containment requires POSIX")
    process: subprocess.Popen[str] | None = None
    tree_absent = False
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        if process.returncode is None:
            raise AssertionError("isolated proof child was not reaped")
        try:
            group_absent = _owned_process_group_is_absent(process.pid)
        except BaseException as exc:
            _terminate_owned_process_tree(process)
            tree_absent = True
            raise ProcessTreeSettlementFailure(
                "isolated proof process-group absence could not be established"
            ) from exc
        if not group_absent:
            _terminate_owned_process_tree(process)
            tree_absent = True
            raise ChildProcessTreeProtocolFailure(
                "isolated proof descendant outlived its child-process leader"
            )
        tree_absent = True
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except BaseException as exc:
        if tree_absent:
            raise
        if process is None:
            raise ProcessTreeSettlementFailure(
                "isolated proof process launch outcome could not be settled"
            ) from exc
        _terminate_owned_process_tree(process)
        raise


def _run_bounded_live_child(
    inputs: DevelopmentInputs,
    temporary_home: Path,
    *,
    cleanup: Callable[[DevelopmentInputs, str], None] = _parent_cleanup_or_raise,
    process_runner: Callable[
        [Sequence[str], Path, Mapping[str, str], float],
        subprocess.CompletedProcess[str],
    ] = _run_owned_process_tree,
) -> subprocess.CompletedProcess[str]:
    child_environment = _build_sanitized_child_environment(
        inputs,
        inherited=os.environ,
        temporary_home=temporary_home,
    )
    command = [
        os.fspath(inputs.hermes_python),
        "-c",
        (
            "from tests.integration.test_isolated_hindsight import _run_live_child; "
            "raise SystemExit(_run_live_child())"
        ),
    ]
    try:
        return process_runner(command, ROOT, child_environment, _CHILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _parent_cleanup_if_owned(inputs, temporary_home, cleanup=cleanup)
        raise AssertionError(
            "isolated Hindsight proof exceeded its fixed child-process bound"
        ) from None


def _validated_live_payload(
    inputs: DevelopmentInputs,
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    sensitive_values = tuple(
        value
        for value in (
            inputs.api_url,
            inputs.api_key,
            inputs.bank_id,
            inputs.destination_fingerprint,
            inputs.wheel_sha256,
            os.fspath(inputs.hermes_python),
            *inputs.endpoint_allowlist,
        )
        if value
    )
    if any(value in result.stdout or value in result.stderr for value in sensitive_values):
        raise AssertionError("isolated Hindsight proof emitted a private development value")
    if result.stderr.strip():
        raise AssertionError("isolated Hindsight proof returned unsanitized stderr")
    stdout_lines = result.stdout.strip().splitlines()
    if len(stdout_lines) != 1:
        raise AssertionError("isolated Hindsight proof returned an invalid sanitized result")
    try:
        payload = json.loads(stdout_lines[0])
    except json.JSONDecodeError:
        raise AssertionError("isolated Hindsight proof returned no sanitized result") from None
    if not isinstance(payload, dict):
        raise AssertionError("isolated Hindsight proof returned an invalid sanitized result")

    if result.returncode == 0:
        if (
            set(payload) != {"documents", "segments", "status"}
            or payload.get("status") != "ok"
            or type(payload.get("documents")) is not int
            or type(payload.get("segments")) is not int
            or payload["documents"] <= 0
            or payload["documents"] != payload["segments"]
        ):
            raise AssertionError("isolated Hindsight proof returned an invalid success result")
        return cast(dict[str, Any], payload)

    if result.returncode == 2:
        if set(payload) != {"failure", "phase", "status"} or payload.get("status") != "failed":
            raise AssertionError("isolated Hindsight proof returned an invalid failure result")
        phase = payload.get("phase")
        failure = payload.get("failure")
        if not isinstance(phase, str) or re.fullmatch(r"[a-z-]{1,32}", phase) is None:
            phase = "unknown"
        if not isinstance(failure, str) or re.fullmatch(r"[A-Za-z]{1,64}", failure) is None:
            failure = "unknown"
        raise AssertionError(f"isolated Hindsight proof failed; phase={phase}; failure={failure}")

    raise AssertionError("isolated Hindsight proof returned an invalid return code")


def _run_parent_live_proof(
    inputs: DevelopmentInputs,
    temporary_home: Path,
    *,
    child_runner: Callable[
        [DevelopmentInputs, Path], subprocess.CompletedProcess[str]
    ] = _run_bounded_live_child,
    cleanup: Callable[[DevelopmentInputs, str], None] = _parent_cleanup_or_raise,
) -> dict[str, Any]:
    try:
        result = child_runner(inputs, temporary_home)
        payload = _validated_live_payload(inputs, result)
        marker = _ownership_marker_path(temporary_home)
        if _cleanup_ownership_is_valid(marker, inputs):
            _parent_cleanup_if_owned(inputs, temporary_home, cleanup=cleanup)
        elif marker.exists():
            raise AssertionError("successful child left an invalid cleanup marker")
        else:
            raise AssertionError("successful child left no cleanup ownership")
        return payload
    except ProcessTreeSettlementFailure:
        raise
    except BaseException:
        _parent_cleanup_if_owned(inputs, temporary_home, cleanup=cleanup)
        raise


def test_parent_rejects_inconsistent_or_unsanitized_child_results() -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None

    invalid_successes = (
        {"status": "ok"},
        {"documents": 1, "extra": True, "segments": 1, "status": "ok"},
        {"documents": True, "segments": True, "status": "ok"},
        {"documents": 1, "segments": 2, "status": "ok"},
    )
    for payload in invalid_successes:
        result = subprocess.CompletedProcess(
            args=["synthetic-child"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with pytest.raises(AssertionError, match="invalid success result"):
            _validated_live_payload(inputs, result)

    preceding_output = subprocess.CompletedProcess(
        args=["synthetic-child"],
        returncode=0,
        stdout="unexpected output\n" + json.dumps({"documents": 1, "segments": 1, "status": "ok"}),
        stderr="",
    )
    with pytest.raises(AssertionError, match="invalid sanitized result"):
        _validated_live_payload(inputs, preceding_output)

    unsanitized_stderr = subprocess.CompletedProcess(
        args=["synthetic-child"],
        returncode=0,
        stdout=json.dumps({"documents": 1, "segments": 1, "status": "ok"}),
        stderr="unexpected stderr",
    )
    with pytest.raises(AssertionError, match="unsanitized stderr"):
        _validated_live_payload(inputs, unsanitized_stderr)

    unsanitized = subprocess.CompletedProcess(
        args=["synthetic-child"],
        returncode=3,
        stdout=json.dumps(
            {
                "cleanup_failed": True,
                "resource": "raw-resource",
                "status": "failed",
            }
        ),
        stderr="",
    )
    with pytest.raises(AssertionError) as raised:
        _validated_live_payload(inputs, unsanitized)
    assert "raw-resource" not in str(raised.value)


def test_parent_does_not_cleanup_existing_bank_rejection(tmp_path: Path) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    cleanups: list[str] = []
    failed = subprocess.CompletedProcess(
        args=["synthetic-child"],
        returncode=2,
        stdout=json.dumps(
            {
                "failure": "DevelopmentGuardError",
                "phase": "bank-create",
                "status": "failed",
            }
        ),
        stderr="",
    )

    with pytest.raises(AssertionError, match="DevelopmentGuardError"):
        _run_parent_live_proof(
            inputs,
            tmp_path,
            child_runner=lambda _inputs, _home: failed,
            cleanup=lambda _inputs, _token: cleanups.append("cleanup"),
        )

    assert cleanups == []


def test_parent_cleans_owned_bank_after_success(tmp_path: Path) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    _claim_cleanup_ownership(_ownership_marker_path(tmp_path), inputs)
    cleanups: list[str] = []
    success = subprocess.CompletedProcess(
        args=["synthetic-child"],
        returncode=0,
        stdout=json.dumps({"documents": 1, "segments": 1, "status": "ok"}),
        stderr="",
    )

    payload = _run_parent_live_proof(
        inputs,
        tmp_path,
        child_runner=lambda _inputs, _home: success,
        cleanup=lambda _inputs, _token: cleanups.append("cleanup"),
    )

    assert payload == {"documents": 1, "segments": 1, "status": "ok"}
    assert cleanups == ["cleanup"]
    assert not _ownership_marker_path(tmp_path).exists()


@pytest.mark.parametrize(
    "interruption",
    (KeyboardInterrupt(), SystemExit(130)),
    ids=("keyboard-interrupt", "system-exit"),
)
def test_settled_parent_interrupt_still_cleans_owned_bank(
    interruption: BaseException,
    tmp_path: Path,
) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    _claim_cleanup_ownership(_ownership_marker_path(tmp_path), inputs)
    cleanups: list[str] = []

    def interrupt_after_settlement(
        _inputs: DevelopmentInputs,
        _home: Path,
    ) -> subprocess.CompletedProcess[str]:
        raise interruption

    with pytest.raises(type(interruption)):
        _run_parent_live_proof(
            inputs,
            tmp_path,
            child_runner=interrupt_after_settlement,
            cleanup=lambda cleanup_inputs, _token: cleanups.append(cleanup_inputs.bank_id),
        )

    assert cleanups == [inputs.bank_id]
    assert not _ownership_marker_path(tmp_path).exists()


def test_unsettled_interrupt_suppresses_parent_cleanup(tmp_path: Path) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    marker = _ownership_marker_path(tmp_path)
    _claim_cleanup_ownership(marker, inputs)
    cleanups: list[str] = []
    settlement_failure = ProcessTreeSettlementFailure("synthetic unsettled interrupt")
    settlement_failure.__cause__ = KeyboardInterrupt()

    def fail_to_settle(
        _inputs: DevelopmentInputs,
        _home: Path,
    ) -> subprocess.CompletedProcess[str]:
        raise settlement_failure

    with pytest.raises(ProcessTreeSettlementFailure, match="unsettled interrupt"):
        _run_parent_live_proof(
            inputs,
            tmp_path,
            child_runner=fail_to_settle,
            cleanup=lambda cleanup_inputs, _token: cleanups.append(cleanup_inputs.bank_id),
        )

    assert cleanups == []
    assert marker.exists()


def test_parent_rejects_success_without_cleanup_ownership(tmp_path: Path) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    success = subprocess.CompletedProcess(
        args=["synthetic-child"],
        returncode=0,
        stdout=json.dumps({"documents": 1, "segments": 1, "status": "ok"}),
        stderr="",
    )

    with pytest.raises(AssertionError, match="no cleanup ownership"):
        _run_parent_live_proof(
            inputs,
            tmp_path,
            child_runner=lambda _inputs, _home: success,
        )


def test_child_leaves_cleanup_for_reaped_parent(tmp_path: Path) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    marker = _ownership_marker_path(tmp_path)
    _claim_cleanup_ownership(marker, inputs)

    assert _cleanup_ownership_is_valid(marker, inputs)
    parent_cleanups: list[str] = []
    assert _parent_cleanup_if_owned(
        inputs,
        tmp_path,
        cleanup=lambda _inputs, _token: parent_cleanups.append("parent"),
    )
    assert parent_cleanups == ["parent"]
    assert not marker.exists()


@pytest.mark.parametrize(
    ("payload", "exit_code"),
    (
        (
            {
                "command": "missions_check",
                "error": "runtime_cleanup_failed",
                "result": "error",
            },
            3,
        ),
        (
            {
                "command": "missions_apply",
                "error": "runtime_cleanup_failed",
                "result": "error",
            },
            3,
        ),
        (
            {
                "command": "missions_apply",
                "outcome": "write_attempted_outcome_unknown",
                "result": "error",
            },
            4,
        ),
    ),
)
def test_unsettled_mission_result_leaves_cleanup_for_reaped_parent(
    tmp_path: Path,
    payload: dict[str, object],
    exit_code: int,
) -> None:
    """Mission results that may hide resistant async work must wait for parent reaping."""

    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    marker = _ownership_marker_path(tmp_path)
    _claim_cleanup_ownership(marker, inputs)
    with pytest.raises(RuntimeSettlementFailure, match="mission command"):
        _require_mission_runtime_settlement(ManagementResult(payload=payload, exit_code=exit_code))

    assert marker.exists()
    parent_cleanups: list[str] = []
    assert _parent_cleanup_if_owned(
        inputs,
        tmp_path,
        cleanup=lambda _inputs, _token: parent_cleanups.append("parent"),
    )
    assert parent_cleanups == ["parent"]
    assert not marker.exists()


def test_unsettled_process_tree_suppresses_parent_cleanup(tmp_path: Path) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    marker = _ownership_marker_path(tmp_path)
    ownership_token = _claim_cleanup_ownership(marker, inputs)
    cleanups: list[str] = []

    def child_runner(
        _inputs: DevelopmentInputs,
        _home: Path,
    ) -> subprocess.CompletedProcess[str]:
        raise ProcessTreeSettlementFailure("synthetic unsettled process tree")

    with pytest.raises(ProcessTreeSettlementFailure):
        _run_parent_live_proof(
            inputs,
            tmp_path,
            child_runner=child_runner,
            cleanup=lambda _inputs, _token: cleanups.append("cleanup"),
        )

    assert cleanups == []
    assert _cleanup_ownership_is_valid(marker, inputs)
    _clear_cleanup_ownership(marker, inputs, ownership_token)


def test_parent_timeout_cleanup_requires_ownership(tmp_path: Path) -> None:
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    cleanups: list[str] = []

    def time_out(
        _command: Sequence[str],
        _cwd: Path,
        _environment: Mapping[str, str],
        _timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="synthetic-child", timeout=1.0)

    with pytest.raises(AssertionError, match="fixed child-process bound"):
        _run_bounded_live_child(
            inputs,
            tmp_path,
            cleanup=lambda _inputs, _token: cleanups.append("cleanup"),
            process_runner=time_out,
        )
    assert cleanups == []

    _claim_cleanup_ownership(_ownership_marker_path(tmp_path), inputs)
    with pytest.raises(AssertionError, match="fixed child-process bound"):
        _run_bounded_live_child(
            inputs,
            tmp_path,
            cleanup=lambda _inputs, _token: cleanups.append("cleanup"),
            process_runner=time_out,
        )
    assert cleanups == ["cleanup"]
    assert not _ownership_marker_path(tmp_path).exists()


def test_interrupt_during_process_launch_suppresses_parent_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group containment is POSIX-only")
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    marker = _ownership_marker_path(tmp_path)
    ownership_token = _claim_cleanup_ownership(marker, inputs)
    cleanups: list[str] = []
    launched: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def launch_then_interrupt(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        process = real_popen(*args, **kwargs)
        launched.append(process)
        raise KeyboardInterrupt

    def child_runner(
        _inputs: DevelopmentInputs,
        _home: Path,
    ) -> subprocess.CompletedProcess[str]:
        return _run_owned_process_tree(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            ROOT,
            dict(os.environ),
            5.0,
        )

    monkeypatch.setattr(subprocess, "Popen", launch_then_interrupt)
    try:
        with pytest.raises(ProcessTreeSettlementFailure, match="launch outcome"):
            _run_parent_live_proof(
                inputs,
                tmp_path,
                child_runner=child_runner,
                cleanup=lambda _inputs, _token: cleanups.append("cleanup"),
            )

        assert len(launched) == 1
        assert not _owned_process_group_is_absent(launched[0].pid)
        assert cleanups == []
        assert _cleanup_ownership_is_valid(marker, inputs)
    finally:
        if launched:
            _terminate_owned_process_tree(launched[0])
        _clear_cleanup_ownership(marker, inputs, ownership_token)


def test_interrupt_before_group_absence_proof_suppresses_parent_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group containment is POSIX-only")
    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    marker = _ownership_marker_path(tmp_path)
    ownership_token = _claim_cleanup_ownership(marker, inputs)
    cleanups: list[str] = []
    absence_probes = 0
    real_absence_probe = _owned_process_group_is_absent

    def interrupt_first_absence_probe(process_group: int) -> bool:
        nonlocal absence_probes
        absence_probes += 1
        if absence_probes == 1:
            raise KeyboardInterrupt
        return real_absence_probe(process_group)

    def child_runner(
        _inputs: DevelopmentInputs,
        _home: Path,
    ) -> subprocess.CompletedProcess[str]:
        return _run_owned_process_tree(
            [sys.executable, "-c", "raise SystemExit(0)"],
            ROOT,
            dict(os.environ),
            5.0,
        )

    monkeypatch.setattr(
        sys.modules[__name__],
        "_owned_process_group_is_absent",
        interrupt_first_absence_probe,
    )
    with pytest.raises(ProcessTreeSettlementFailure, match="absence could not be established"):
        _run_parent_live_proof(
            inputs,
            tmp_path,
            child_runner=child_runner,
            cleanup=lambda _inputs, _token: cleanups.append("cleanup"),
        )

    assert absence_probes >= 2
    assert cleanups == []
    assert _cleanup_ownership_is_valid(marker, inputs)
    _clear_cleanup_ownership(marker, inputs, ownership_token)


def test_timeout_terminates_and_reaps_restart_descendant(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("process-group containment is POSIX-only")
    started = tmp_path / "descendant-started"
    terminated = tmp_path / "descendant-terminated"
    grandchild_code = f"""
import signal
import time
from pathlib import Path
started = Path({os.fspath(started)!r})
terminated = Path({os.fspath(terminated)!r})
def stop(_signum, _frame):
    terminated.write_text('yes', encoding='ascii')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
started.write_text('yes', encoding='ascii')
while True:
    time.sleep(1)
"""
    parent_code = f"""
import subprocess
import sys
import time
from pathlib import Path
started = Path({os.fspath(started)!r})
subprocess.Popen([sys.executable, '-c', {grandchild_code!r}])
deadline = time.monotonic() + 5
while not started.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
time.sleep(30)
"""

    with pytest.raises(subprocess.TimeoutExpired):
        _run_owned_process_tree(
            [sys.executable, "-c", parent_code],
            ROOT,
            dict(os.environ),
            1.0,
        )

    deadline = time.monotonic() + 2.0
    while not terminated.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert terminated.exists()


def test_normal_leader_exit_terminates_contained_descendant(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("process-group containment is POSIX-only")
    started = tmp_path / "normal-exit-descendant-started"
    terminated = tmp_path / "normal-exit-descendant-terminated"
    grandchild_code = f"""
import signal
import time
from pathlib import Path
started = Path({os.fspath(started)!r})
terminated = Path({os.fspath(terminated)!r})
def stop(_signum, _frame):
    terminated.write_text('yes', encoding='ascii')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
started.write_text('yes', encoding='ascii')
while True:
    time.sleep(1)
"""
    parent_code = f"""
import subprocess
import sys
import time
from pathlib import Path
started = Path({os.fspath(started)!r})
subprocess.Popen(
    [sys.executable, '-c', {grandchild_code!r}],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not started.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
"""

    inputs = _development_inputs_from_environment(_development_environment())
    assert inputs is not None
    _claim_cleanup_ownership(_ownership_marker_path(tmp_path), inputs)
    cleanups: list[str] = []

    def child_runner(
        _inputs: DevelopmentInputs,
        _home: Path,
    ) -> subprocess.CompletedProcess[str]:
        return _run_owned_process_tree(
            [sys.executable, "-c", parent_code],
            ROOT,
            dict(os.environ),
            5.0,
        )

    def cleanup(_inputs: DevelopmentInputs, _token: str) -> None:
        assert terminated.exists()
        cleanups.append("parent")

    with pytest.raises(ChildProcessTreeProtocolFailure, match="outlived"):
        _run_parent_live_proof(
            inputs,
            tmp_path,
            child_runner=child_runner,
            cleanup=cleanup,
        )

    assert cleanups == ["parent"]
    assert not _ownership_marker_path(tmp_path).exists()


def test_required_live_proof_cannot_report_a_skip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in _EXPLICIT_DEVELOPMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(_REQUIRE_LIVE_PROOF_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match="live-proof inputs are absent"):
        test_isolated_hindsight_released_host_proof(tmp_path)


@pytest.mark.isolated_hindsight_live
def test_isolated_hindsight_released_host_proof(tmp_path: Path) -> None:
    """Run exactly one live proof only after every operator gate is explicit."""

    inputs = _development_inputs_from_environment(os.environ)
    if inputs is None:
        if os.environ.get(_REQUIRE_LIVE_PROOF_ENV) == "1":
            pytest.fail("required isolated Hindsight live-proof inputs are absent")
        pytest.skip("isolated Hindsight development writes were not explicitly enabled")
    _validate_static_development_guards(inputs)
    _assert_executable(inputs.hermes_python)
    _prepare_temporary_home(tmp_path)
    with _exclusive_live_proof_lock():
        payload = _run_parent_live_proof(inputs, tmp_path)
    assert payload["status"] == "ok"
    assert payload["documents"] == payload["segments"]
