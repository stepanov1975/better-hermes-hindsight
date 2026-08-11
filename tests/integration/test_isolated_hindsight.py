"""Opt-in smoke test against the isolated Hindsight 0.8.5 environment.

The test intentionally proves only the deployment's useful path: current Hermes
discovers the provider, pending rows survive a provider restart, delivery reaches
a disposable bank, replay keeps stable document identities, and current-query
recall is useful. Unit and fake-service tests cover adversarial failure detail.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.outbox import OutboxRow, ProfileLockOwner, SQLiteOutbox
from better_hermes_hindsight.retention import RetainedSegment, build_retained_segments
from better_hermes_hindsight.runtime import finalize_process_runtime

ROOT = Path(__file__).resolve().parents[2]
_HINDSIGHT_VERSION = "0.8.5"
_RETAIN_TAGS = ("better-hindsight-live",)
_SEGMENT_MAX_BYTES = 384
_DRAIN_TIMEOUT_SECONDS = 45.0
_CHILD_TIMEOUT_SECONDS = 150.0
_ROOT_PLUGIN_FILES = ("__init__.py", "cli.py", "plugin.yaml")
_SHORT_TURN = (
    "better-hindsight-live-short",
    "The synthetic Northstar rehearsal uses the recovery phrase cobalt lantern.",
    "The synthetic recovery phrase has been recorded.",
)
_LONG_TURN = (
    "better-hindsight-live-long",
    "Synthetic segmented source: " + ("northstar-界-" * 160),
    "Synthetic segmented acknowledgement: " + ("lantern-界-" * 100),
)


@dataclass(frozen=True, slots=True)
class DevelopmentInputs:
    api_url: str
    api_key: str
    hermes_python: Path
    allowed_endpoints: tuple[str, ...]
    bank_id: str = ""
    ownership_name: str = ""


def _development_inputs(environ: Mapping[str, str]) -> DevelopmentInputs | None:
    names = (
        "BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF",
        "BETTER_HINDSIGHT_ALLOW_DEV_WRITES",
        "BETTER_HINDSIGHT_DEV_API_URL",
        "BETTER_HINDSIGHT_DEV_API_KEY",
        "BETTER_HINDSIGHT_DEV_HERMES_PYTHON",
    )
    values = {name: environ.get(name, "") for name in names}
    if not any(values.values()):
        return None
    if values["BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF"] != "1":
        raise RuntimeError("live proof requires BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF=1")
    if values["BETTER_HINDSIGHT_ALLOW_DEV_WRITES"] != "1":
        raise RuntimeError("live proof requires BETTER_HINDSIGHT_ALLOW_DEV_WRITES=1")
    missing = [name for name in names[2:] if not values[name]]
    if missing:
        raise RuntimeError("live proof configuration is incomplete")

    api_url = values["BETTER_HINDSIGHT_DEV_API_URL"].rstrip("/")
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("live proof endpoint must be an absolute HTTP(S) URL")
    allowed = tuple(
        item.strip().rstrip("/")
        for item in environ.get("BETTER_HINDSIGHT_DEV_ALLOWED_ENDPOINTS", "").split(",")
        if item.strip()
    )
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and api_url not in allowed:
        raise RuntimeError("non-loopback live endpoint must be explicitly allowlisted")

    hermes_python = Path(values["BETTER_HINDSIGHT_DEV_HERMES_PYTHON"]).expanduser()
    if not hermes_python.is_file() or not os.access(hermes_python, os.X_OK):
        raise RuntimeError("live proof Hermes interpreter is not executable")
    return DevelopmentInputs(
        api_url=api_url,
        api_key=values["BETTER_HINDSIGHT_DEV_API_KEY"],
        hermes_python=hermes_python.resolve(),
        allowed_endpoints=allowed,
    )


def _with_disposable_bank(inputs: DevelopmentInputs) -> DevelopmentInputs:
    token = uuid.uuid4().hex
    return DevelopmentInputs(
        api_url=inputs.api_url,
        api_key=inputs.api_key,
        hermes_python=inputs.hermes_python,
        allowed_endpoints=inputs.allowed_endpoints,
        bank_id=f"better-hindsight-live-{token[:16]}",
        ownership_name=f"Better Hindsight live test {token}",
    )


def _payload(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        loaded = dump(mode="json")
        if isinstance(loaded, dict):
            return cast(dict[str, Any], loaded)
    raise AssertionError("public SDK response was not an object")


def _new_sdk(inputs: DevelopmentInputs) -> Any:
    from hindsight_client import Hindsight

    return Hindsight(
        base_url=inputs.api_url,
        api_key=inputs.api_key,
        timeout=30.0,
        user_agent="better-hermes-hindsight-live-test",
    )


def _api_status(exception: Exception) -> int | None:
    value = getattr(exception, "status", None)
    return value if type(value) is int else None


def _create_disposable_bank(inputs: DevelopmentInputs) -> None:
    from hindsight_client_api.exceptions import ApiException

    async def create() -> None:
        sdk = _new_sdk(inputs)
        try:
            version = _payload(await sdk.aget_version())
            if version.get("api_version") != _HINDSIGHT_VERSION:
                raise AssertionError("isolated Hindsight server is not version 0.8.5")
            try:
                await sdk.banks.get_bank_profile(bank_id=inputs.bank_id)
            except ApiException as exception:
                if _api_status(exception) != 404:
                    raise
            else:
                raise AssertionError("generated disposable bank already exists")
            created = _payload(
                await sdk.acreate_bank(bank_id=inputs.bank_id, name=inputs.ownership_name)
            )
            if created.get("bank_id") != inputs.bank_id:
                raise AssertionError("Hindsight created an unexpected bank")
        finally:
            await sdk.aclose()

    asyncio.run(create())


def _delete_disposable_bank(inputs: DevelopmentInputs) -> None:
    from hindsight_client_api.exceptions import ApiException

    async def delete() -> None:
        sdk = _new_sdk(inputs)
        try:
            try:
                profile = _payload(await sdk.banks.get_bank_profile(bank_id=inputs.bank_id))
            except ApiException as exception:
                if _api_status(exception) == 404:
                    return
                raise
            if (
                profile.get("bank_id") != inputs.bank_id
                or profile.get("name") != inputs.ownership_name
            ):
                raise AssertionError(
                    "refusing to delete a bank without the live-test ownership marker"
                )
            try:
                await sdk.adelete_bank(bank_id=inputs.bank_id)
            except ApiException as exception:
                if _api_status(exception) != 404:
                    raise
        finally:
            await sdk.aclose()

    try:
        asyncio.run(delete())
    except Exception as exception:
        bank = inputs.bank_id
        raise AssertionError(
            f"cleanup failed; remove disposable bank {bank!r} manually after verifying its name"
        ) from exception


def _profile_document(inputs: DevelopmentInputs) -> dict[str, Any]:
    return {
        "api_url": inputs.api_url,
        "bank_id": inputs.bank_id,
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
            "enabled": True,
            "timeout_seconds": 30.0,
            "segment_max_bytes": _SEGMENT_MAX_BYTES,
            "tags": list(_RETAIN_TAGS),
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


def _prepare_home(inputs: DevelopmentInputs) -> tuple[Path, BetterHindsightConfig]:
    home = Path(os.environ["HERMES_HOME"])
    config_dir = home / "better_hindsight"
    plugin_dir = home / "plugins" / "better_hindsight"
    config_dir.mkdir(parents=True, mode=0o700)
    plugin_dir.mkdir(parents=True, mode=0o700)
    (config_dir / "config.json").write_text(
        json.dumps(_profile_document(inputs), sort_keys=True), encoding="utf-8"
    )
    (home / "config.yaml").write_text("memory:\n  provider: better_hindsight\n", encoding="utf-8")
    for name in _ROOT_PLUGIN_FILES:
        shutil.copy2(ROOT / name, plugin_dir / name)
    config = load_config(home, environ={"HINDSIGHT_API_KEY": inputs.api_key})
    return home, config


def _start_manager(home: Path) -> tuple[Any, Any]:
    from agent.memory_manager import MemoryManager
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("better_hindsight")
    if provider is None or provider.name != "better_hindsight":
        raise AssertionError("Hermes did not discover Better Hindsight")
    manager = MemoryManager()
    try:
        manager.add_provider(provider)
        manager.initialize_all(
            "better-hindsight-live-initial",
            hermes_home=os.fspath(home),
            platform="cli",
            agent_context="primary",
        )
        return manager, provider
    except BaseException:
        _stop_manager(manager)
        raise


def _stop_manager(manager: Any) -> None:
    failure: BaseException | None = None
    try:
        manager.shutdown_all()
    except BaseException as exception:
        failure = exception
    try:
        if finalize_process_runtime() is not True and failure is None:
            failure = AssertionError("Better Hindsight runtime did not finalize")
    except BaseException as exception:
        if failure is None:
            failure = exception
    if failure is not None:
        raise AssertionError("Better Hindsight manager did not stop cleanly") from failure


def _sync_turn(manager: Any, turn: tuple[str, str, str]) -> None:
    session_id, user_content, assistant_content = turn
    manager.sync_all(user_content, assistant_content, session_id=session_id)
    if manager.flush_pending(timeout=5.0) is not True:
        raise AssertionError("Hermes did not finish retention admission")


def _read_rows(config: BetterHindsightConfig) -> tuple[OutboxRow, ...]:
    outbox = SQLiteOutbox.open(config)
    try:
        return outbox.read_unconfirmed()
    finally:
        outbox.close()


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
    outbox = SQLiteOutbox.open(config)
    try:
        acquisition = outbox.try_acquire_profile_lock()
        if not acquisition.acquired or acquisition.owner is None:
            raise AssertionError("could not reserve the disposable sender lock")
        return acquisition.owner
    finally:
        outbox.close()


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
    expected: Sequence[RetainedSegment],
) -> dict[str, dict[str, Any]]:
    expected_ids = {segment.document_id for segment in expected}

    async def read() -> dict[str, dict[str, Any]]:
        sdk = _new_sdk(inputs)
        try:
            listed = _payload(
                await sdk.documents.list_documents(bank_id=inputs.bank_id, limit=100, offset=0)
            )
            items = listed.get("items")
            if not isinstance(items, list):
                raise AssertionError("Hindsight did not return a document list")
            listed_ids = {
                item.get("id")
                for item in items
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if listed_ids != expected_ids:
                raise AssertionError("remote document identities did not converge")
            return {
                document_id: _payload(
                    await sdk.documents.get_document(
                        bank_id=inputs.bank_id, document_id=document_id
                    )
                )
                for document_id in sorted(expected_ids)
            }
        finally:
            await sdk.aclose()

    return asyncio.run(read())


def _assert_long_source_reconstructs(
    expected: Sequence[RetainedSegment],
    documents: Mapping[str, Mapping[str, Any]],
) -> None:
    long_source = _expected_segments((_LONG_TURN,))[0].source_sha256
    long_segments = [segment for segment in expected if segment.source_sha256 == long_source]
    if len(long_segments) < 2:
        raise AssertionError("long synthetic turn was not segmented")
    long_segments.sort(key=lambda segment: segment.segment_index)
    reconstructed = "".join(
        cast(str, documents[segment.document_id].get("original_text")) for segment in long_segments
    )
    digest = hashlib.sha256(reconstructed.encode("utf-8")).hexdigest()
    if digest != long_segments[0].source_sha256:
        raise AssertionError("segmented source did not reconstruct")


def _wait_for_useful_recall(provider: Any) -> None:
    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    while True:
        context = provider.prefetch(
            "What recovery phrase is used by the synthetic Northstar rehearsal?"
        )
        if "cobalt lantern" in context.casefold() and '"type":' in context:
            return
        if time.monotonic() >= deadline:
            raise AssertionError("live current-query recall was not useful")
        time.sleep(0.25)


def _run_live_child() -> int:
    inputs = DevelopmentInputs(
        api_url=os.environ["BETTER_HINDSIGHT_CHILD_API_URL"],
        api_key=os.environ["HINDSIGHT_API_KEY"],
        hermes_python=Path(os.environ["BETTER_HINDSIGHT_CHILD_HERMES_PYTHON"]),
        allowed_endpoints=(),
        bank_id=os.environ["BETTER_HINDSIGHT_CHILD_BANK_ID"],
        ownership_name=os.environ["BETTER_HINDSIGHT_CHILD_OWNERSHIP_NAME"],
    )
    manager: Any | None = None
    barrier: ProfileLockOwner | None = None
    try:
        if metadata.version("hindsight-client") != _HINDSIGHT_VERSION:
            raise AssertionError("dedicated interpreter does not use hindsight-client 0.8.5")
        home, config = _prepare_home(inputs)
        expected = _expected_segments((_SHORT_TURN, _LONG_TURN))

        barrier = _acquire_sender_barrier(config)
        manager, _ = _start_manager(home)
        _sync_turn(manager, _SHORT_TURN)
        _sync_turn(manager, _LONG_TURN)
        pending = _wait_for_rows(config, lambda rows: len(rows) == len(expected))
        if any(row.state != "pending" for row in pending):
            raise AssertionError("retained rows were not durably pending")
        _stop_manager(manager)
        manager = None
        barrier.release()
        barrier = None

        manager, provider = _start_manager(home)
        _wait_for_rows(config, lambda rows: not rows)
        documents = _remote_documents(inputs, expected)
        _assert_long_source_reconstructs(expected, documents)
        _wait_for_useful_recall(provider)

        _sync_turn(manager, _SHORT_TURN)
        _wait_for_rows(config, lambda rows: not rows)
        replayed = _remote_documents(inputs, expected)
        if set(replayed) != set(documents):
            raise AssertionError("stable replay changed remote document identities")

        result = {
            "better": metadata.version("better-hermes-hindsight"),
            "documents": len(documents),
            "hermes": metadata.version("hermes-agent"),
            "hindsight_client": metadata.version("hindsight-client"),
            "segments": len(expected),
            "status": "ok",
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    except BaseException as exception:
        print(json.dumps({"failure": type(exception).__name__, "status": "failed"}, sort_keys=True))
        return 2
    finally:
        if manager is not None:
            _stop_manager(manager)
        if barrier is not None:
            barrier.release()


def _child_environment(inputs: DevelopmentInputs, home: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith("HINDSIGHT_") or name.startswith("BETTER_HINDSIGHT_CHILD_"):
            environment.pop(name)
    environment.update(
        {
            "HERMES_HOME": os.fspath(home),
            "HINDSIGHT_API_KEY": inputs.api_key,
            "BETTER_HINDSIGHT_CHILD_API_URL": inputs.api_url,
            "BETTER_HINDSIGHT_CHILD_BANK_ID": inputs.bank_id,
            "BETTER_HINDSIGHT_CHILD_HERMES_PYTHON": os.fspath(inputs.hermes_python),
            "BETTER_HINDSIGHT_CHILD_OWNERSHIP_NAME": inputs.ownership_name,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def test_live_proof_is_explicitly_opt_in() -> None:
    assert _development_inputs({}) is None
    with pytest.raises(RuntimeError, match="REQUIRE_LIVE_PROOF"):
        _development_inputs({"BETTER_HINDSIGHT_ALLOW_DEV_WRITES": "1"})


def test_live_smoke_attempts_cleanup_when_create_result_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = DevelopmentInputs(
        api_url="http://127.0.0.1:8888",
        api_key="synthetic-live-key",
        hermes_python=Path("/synthetic/hermes/python"),
        allowed_endpoints=(),
        bank_id="better-hindsight-live-synthetic",
        ownership_name="Better Hindsight live test synthetic",
    )
    cleanup_calls: list[DevelopmentInputs] = []

    def uncertain_create(_inputs: DevelopmentInputs) -> None:
        raise RuntimeError("create result uncertain")

    monkeypatch.setattr(
        "tests.integration.test_isolated_hindsight._development_inputs",
        lambda _environ: inputs,
    )
    monkeypatch.setattr(
        "tests.integration.test_isolated_hindsight._with_disposable_bank",
        lambda _inputs: inputs,
    )
    monkeypatch.setattr(
        "tests.integration.test_isolated_hindsight._create_disposable_bank",
        uncertain_create,
    )
    monkeypatch.setattr(
        "tests.integration.test_isolated_hindsight._delete_disposable_bank",
        cleanup_calls.append,
    )

    with pytest.raises(RuntimeError, match="create result uncertain"):
        test_isolated_hindsight_smoke(tmp_path)
    assert cleanup_calls == [inputs]


@pytest.mark.isolated_hindsight_live
def test_isolated_hindsight_smoke(tmp_path: Path) -> None:
    base_inputs = _development_inputs(os.environ)
    if base_inputs is None:
        pytest.skip("isolated Hindsight live proof was not requested")
    inputs = _with_disposable_bank(base_inputs)

    try:
        _create_disposable_bank(inputs)
        completed = subprocess.run(
            [
                os.fspath(inputs.hermes_python),
                "-c",
                (
                    "from tests.integration.test_isolated_hindsight import _run_live_child; "
                    "raise SystemExit(_run_live_child())"
                ),
            ],
            cwd=ROOT,
            env=_child_environment(inputs, tmp_path / "hermes-home"),
            check=False,
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_SECONDS,
        )
        if inputs.api_key in completed.stdout or inputs.api_key in completed.stderr:
            raise AssertionError("live child exposed its API credential")
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            raise AssertionError("live child returned no structured result") from None
        assert completed.returncode == 0, payload
        assert payload["status"] == "ok"
        assert payload["hindsight_client"] == _HINDSIGHT_VERSION
        assert payload["documents"] == payload["segments"]
    finally:
        _delete_disposable_bank(inputs)
