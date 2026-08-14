"""Deterministic fake-server tests for the strict Hindsight 0.8.5 E2E canary."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import math
import threading
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from better_hermes_hindsight import canary as canary_module
from better_hermes_hindsight.canary import CanaryConfig, run_canary
from better_hermes_hindsight.client import (
    HindsightClientError,
    RecallResponse,
    RecallResult,
    RetainConfirmation,
    RetainSegment,
)
from better_hermes_hindsight.config import BetterHindsightConfig

_PRIVATE = "private-api-key-sentinel"


class _Handler(BaseHTTPRequestHandler):
    paths: ClassVar[list[tuple[str, str]]] = []
    bodies: ClassVar[list[object]] = []
    recalls: ClassVar[int] = 0
    visible_after: ClassVar[int] = 1
    document_id: ClassVar[str] = ""
    marker: ClassVar[str] = ""
    tags: ClassVar[list[str]] = []
    health: ClassVar[object] = {"status": "healthy"}
    version: ClassVar[object] = {"api_version": "0.8.5"}
    retain_override: ClassVar[object | None] = None
    recall_override: ClassVar[object | None] = None
    cleanup_override: ClassVar[object | None] = None

    def log_message(self, _format: str, *args: object) -> None:
        del args

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).paths.append(("GET", self.path))
        if self.path == "/health":
            self._json(200, type(self).health)
        elif self.path == "/version":
            self._json(200, type(self).version)
        else:
            self._json(404, {})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).paths.append(("POST", self.path))
        type(self).bodies.append(body)
        if self.path.endswith("/memories/recall"):
            type(self).recalls += 1
            if type(self).recall_override is not None:
                self._json(200, type(self).recall_override)
                return
            visible = type(self).recalls >= type(self).visible_after
            results = []
            if visible:
                results = [
                    {
                        "id": "canary-result",
                        "text": "Hindsight synthesized memory unit",
                        "document_id": type(self).document_id,
                        "tags": type(self).tags,
                    }
                ]
            self._json(200, {"results": results})
            return
        item = body["items"][0]
        type(self).document_id = item["document_id"]
        type(self).marker = item["content"]
        type(self).tags = item["tags"]
        response = type(self).retain_override
        if response is None:
            response = {
                "success": True,
                "bank_id": "isolated-canary-bank",
                "items_count": 1,
                "async": False,
            }
        self._json(200, response)

    def do_DELETE(self) -> None:
        type(self).paths.append(("DELETE", self.path))
        response = type(self).cleanup_override
        if response is None:
            response = {
                "success": True,
                "message": "deleted",
                "document_id": type(self).document_id,
                "memory_units_deleted": 1,
            }
        elif isinstance(response, dict) and response.get("document_id") is None:
            response = {**response, "document_id": type(self).document_id}
        self._json(200, response)


@contextmanager
def _server(**overrides: object) -> Iterator[str]:
    _Handler.paths = []
    _Handler.bodies = []
    _Handler.recalls = 0
    _Handler.visible_after = 1
    _Handler.document_id = ""
    _Handler.marker = ""
    _Handler.tags = []
    _Handler.health = {"status": "healthy"}
    _Handler.version = {"api_version": "0.8.5"}
    _Handler.retain_override = None
    _Handler.recall_override = None
    _Handler.cleanup_override = None
    for name, value in overrides.items():
        setattr(_Handler, name, value)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _config(api_url: str, *, max_polls: int = 3, timeout: float = 1.0) -> CanaryConfig:
    return CanaryConfig(
        api_url=api_url,
        bank_id="isolated-canary-bank",
        api_key=_PRIVATE,
        timeout_seconds=timeout,
        cleanup_timeout_seconds=min(0.2, timeout / 2),
        poll_interval_seconds=0.0,
        max_polls=max_polls,
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize(
    "field",
    ["timeout_seconds", "poll_interval_seconds", "cleanup_timeout_seconds"],
)
def test_canary_rejects_non_finite_timing(field: str, value: float) -> None:
    values: dict[str, object] = {
        "api_url": "http://127.0.0.1:9",
        "bank_id": "isolated-canary-bank",
        "timeout_seconds": 1.0,
        "poll_interval_seconds": 0.0,
        "cleanup_timeout_seconds": 0.2,
    }
    values[field] = value
    with pytest.raises(ValueError, match="invalid canary timing"):
        CanaryConfig(**values)  # type: ignore[arg-type]


def test_canary_rejects_redirect_without_forwarding_authorization() -> None:
    observed_authorization: list[str | None] = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            observed_authorization.append(self.headers.get("authorization"))
            self.send_response(200)
            self.end_headers()

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("location", f"http://127.0.0.1:{target.server_port}/captured")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=target.serve_forever),
        threading.Thread(target=redirect.serve_forever),
    ]
    for thread in threads:
        thread.start()
    try:
        result = run_canary(_config(f"http://127.0.0.1:{redirect.server_port}"))
    finally:
        redirect.shutdown()
        target.shutdown()
        for thread in threads:
            thread.join()
        redirect.server_close()
        target.server_close()

    assert result == {"error": "request_failed", "result": "error"}
    assert observed_authorization == []


def _assert_private_absent(result: dict[str, object]) -> None:
    rendered = json.dumps(result)
    assert _PRIVATE not in rendered
    if _Handler.marker:
        assert _Handler.marker not in rendered
    if _Handler.document_id:
        assert _Handler.document_id not in rendered
    assert not any(tag in rendered for tag in _Handler.tags)


def test_canary_uses_exact_protocol_proves_owned_recall_and_validates_cleanup() -> None:
    with _server(visible_after=2) as api_url:
        result = run_canary(_config(api_url))

    assert result["result"] == "ok"
    assert result["version"] == "0.8.5"
    assert result["poll_count"] == 2
    assert all(
        type(result[field]) is int
        for field in ("health_ms", "retain_ms", "recall_visible_ms", "cleanup_ms")
    )
    retain = _Handler.bodies[0]
    recall = _Handler.bodies[1]
    assert retain == {
        "items": [
            {
                "content": _Handler.marker,
                "timestamp": None,
                "context": None,
                "document_id": _Handler.document_id,
                "entities": None,
                "tags": _Handler.tags,
                "metadata": {
                    "better_hindsight_payload_schema": "better-hindsight-canary-v1",
                    "better_hindsight_segment_count": "1",
                    "better_hindsight_segment_index": "0",
                    "better_hindsight_source_sha256": hashlib.sha256(
                        _Handler.marker.encode("utf-8")
                    ).hexdigest(),
                },
                "observation_scopes": None,
                "strategy": None,
                "update_mode": "replace",
            }
        ],
        "async": False,
        "document_tags": None,
    }
    assert recall == {
        "query": _Handler.marker,
        "types": None,
        "prefer_observations": False,
        "budget": "mid",
        "max_tokens": 4096,
        "trace": False,
        "query_timestamp": None,
        "include": {"entities": None, "chunks": None, "source_facts": None},
        "tags": _Handler.tags,
        "tags_match": "exact",
        "tag_groups": None,
        "min_scores": None,
    }
    assert _Handler.paths[-1] == (
        "DELETE",
        "/v1/default/banks/isolated-canary-bank/documents/" + _Handler.document_id,
    )
    _assert_private_absent(result)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            RecallResult(
                id="result",
                text="Hindsight synthesized memory unit",
                document_id="owned-document",
                tags=["owned-tag"],
            ),
            True,
        ),
        (
            RecallResult(id="result", text="marker", document_id="wrong", tags=["owned-tag"]),
            False,
        ),
        (
            RecallResult(
                id="result",
                text="marker",
                document_id="owned-document",
                tags=["wrong"],
            ),
            False,
        ),
        (
            RecallResult(
                id="result",
                text="marker",
                document_id="owned-document",
                tags=["owned-tag", "extra"],
            ),
            False,
        ),
    ],
)
def test_recall_ownership_requires_exact_document_and_singleton_tag(
    value: RecallResult,
    expected: bool,
) -> None:
    assert (
        canary_module._owned_recall_result(
            value,
            document_id="owned-document",
            tag="owned-tag",
        )
        is expected
    )


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"success": True}, "retain_failed"),
        (
            {
                "success": True,
                "bank_id": "wrong",
                "items_count": 1,
                "async": False,
            },
            "retain_unconfirmed",
        ),
    ],
)
def test_unconfirmed_retain_is_error_and_always_attempts_exact_cleanup(
    override: object, expected: str
) -> None:
    with _server(retain_override=override) as api_url:
        result = run_canary(_config(api_url))
    assert result["error"] == expected
    if expected == "retain_failed":
        assert result["adapter_error"] == "schema_invalid"
    assert _Handler.paths[-1][0] == "DELETE"
    _assert_private_absent(result)


def test_canary_preserves_adapter_initialization_failure_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_initialization(_config: BetterHindsightConfig) -> None:
        raise HindsightClientError(
            "client_initialization_failed",
            "Better Hindsight client initialization failed.",
        )

    monkeypatch.setattr(canary_module, "create_hindsight_client", fail_initialization)
    with _server() as url:
        result = run_canary(_config(url))
        paths = list(_Handler.paths)

    assert result == {
        "adapter_error": "client_initialization_failed",
        "error": "adapter_initialization_failed",
        "health_ms": result["health_ms"],
        "result": "error",
    }
    assert paths == [("GET", "/health"), ("GET", "/version")]


def test_canary_preserves_cleanup_reserve_when_adapter_close_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remaining_cleanup: list[float] = []

    class SlowCloseClient:
        def __init__(self, config: BetterHindsightConfig) -> None:
            self.config = config
            self.segment: RetainSegment | None = None

        async def retain_segment(self, segment: RetainSegment) -> RetainConfirmation:
            self.segment = segment
            return RetainConfirmation(confirmed=True)

        async def recall(self, _query: str) -> RecallResponse:
            assert self.segment is not None
            return RecallResponse(
                results=[
                    RecallResult(
                        id="result",
                        text="marker",
                        document_id=self.segment.document_id,
                        tags=list(self.config.recall.tags or ()),
                    )
                ]
            )

        async def close(self) -> None:
            await asyncio.sleep(1.0)

    def cleanup(
        _config: CanaryConfig,
        *,
        document_id: str,
        deadline: float,
    ) -> tuple[bool, int]:
        assert document_id.startswith("better-hindsight-canary-")
        remaining_cleanup.append(deadline - time.monotonic())
        return True, 0

    monkeypatch.setattr(canary_module, "create_hindsight_client", SlowCloseClient)
    monkeypatch.setattr(canary_module, "_cleanup", cleanup)
    with _server() as url:
        result = run_canary(
            CanaryConfig(
                api_url=url,
                bank_id="dedicated-canary",
                timeout_seconds=0.3,
                cleanup_timeout_seconds=0.1,
                poll_interval_seconds=0.0,
            )
        )

    assert result["error"] == "adapter_close_failed"
    assert len(remaining_cleanup) == 1
    assert 0.05 <= remaining_cleanup[0] <= 0.1


def test_canary_does_not_mark_retain_dispatched_before_deadline_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls: list[str] = []
    remaining_calls = 0

    class PreDispatchClient:
        retain_called = False
        close_called = False

        async def retain_segment(self, _segment: RetainSegment) -> RetainConfirmation:
            self.retain_called = True
            return RetainConfirmation(confirmed=True)

        async def recall(self, _query: str) -> RecallResponse:
            raise AssertionError("recall must not run")

        async def close(self) -> None:
            self.close_called = True

    client = PreDispatchClient()

    def remaining(_deadline: float) -> float:
        nonlocal remaining_calls
        remaining_calls += 1
        if remaining_calls == 3:
            raise TimeoutError
        return 1.0

    def cleanup(
        _config: CanaryConfig,
        *,
        document_id: str,
        deadline: float,
    ) -> tuple[bool, int]:
        del deadline
        cleanup_calls.append(document_id)
        return False, 0

    monkeypatch.setattr(canary_module, "create_hindsight_client", lambda _config: client)
    monkeypatch.setattr(canary_module, "_remaining", remaining)
    monkeypatch.setattr(canary_module, "_cleanup", cleanup)
    with warnings.catch_warnings(record=True) as caught, _server() as url:
        warnings.simplefilter("always")
        result = run_canary(_config(url))
        gc.collect()

    assert result["error"] == "retain_timeout"
    assert client.retain_called is False
    assert client.close_called is True
    assert cleanup_calls == []
    assert not [warning for warning in caught if "was never awaited" in str(warning.message)]


def test_canary_checks_deadline_before_creating_recall_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls: list[str] = []
    remaining_calls = 0

    class PreRecallClient:
        retain_called = False
        recall_called = False
        close_called = False

        async def retain_segment(self, _segment: RetainSegment) -> RetainConfirmation:
            self.retain_called = True
            return RetainConfirmation(confirmed=True)

        async def recall(self, _query: str) -> RecallResponse:
            self.recall_called = True
            return RecallResponse(results=[])

        async def close(self) -> None:
            self.close_called = True

    client = PreRecallClient()

    def remaining(_deadline: float) -> float:
        nonlocal remaining_calls
        remaining_calls += 1
        if remaining_calls == 4:
            raise TimeoutError
        return 1.0

    def cleanup(
        _config: CanaryConfig,
        *,
        document_id: str,
        deadline: float,
    ) -> tuple[bool, int]:
        del deadline
        cleanup_calls.append(document_id)
        return True, 0

    monkeypatch.setattr(canary_module, "create_hindsight_client", lambda _config: client)
    monkeypatch.setattr(canary_module, "_remaining", remaining)
    monkeypatch.setattr(canary_module, "_cleanup", cleanup)
    with warnings.catch_warnings(record=True) as caught, _server() as url:
        warnings.simplefilter("always")
        result = run_canary(_config(url))
        gc.collect()

    assert result["error"] == "recall_timeout"
    assert client.retain_called is True
    assert client.recall_called is False
    assert client.close_called is True
    assert len(cleanup_calls) == 1
    assert not [warning for warning in caught if "was never awaited" in str(warning.message)]


@pytest.mark.parametrize(
    "recall_override",
    [
        {
            "results": [
                {
                    "id": "result",
                    "text": "marker",
                    "document_id": "wrong",
                    "tags": ["wrong-owner-tag"],
                }
            ]
        },
        {
            "results": [
                {
                    "id": "result",
                    "text": "wrong",
                    "document_id": "wrong",
                    "tags": ["wrong"],
                }
            ]
        },
    ],
)
def test_mismatched_recall_ownership_never_passes(recall_override: object) -> None:
    with _server(recall_override=recall_override) as api_url:
        result = run_canary(_config(api_url, max_polls=2))
    assert result["error"] == "recall_timeout"
    assert result["poll_count"] == 2
    _assert_private_absent(result)


def test_malformed_recall_is_distinct_and_cleanup_still_runs() -> None:
    with _server(recall_override={"not_results": []}) as api_url:
        result = run_canary(_config(api_url))
    assert result["error"] == "recall_failed"
    assert result["adapter_error"] == "schema_invalid"
    assert _Handler.paths[-1][0] == "DELETE"


@pytest.mark.parametrize(
    ("failure", "expected", "adapter_error"),
    [
        (TimeoutError(), "recall_timeout", None),
        (
            HindsightClientError(
                "recall_failed",
                "fixed error",
                reason="schema_invalid",
            ),
            "recall_failed",
            "schema_invalid",
        ),
        (
            HindsightClientError(
                "recall_failed",
                "fixed error",
                reason="transport_error",
            ),
            "recall_failed",
            "transport_error",
        ),
    ],
)
def test_post_retain_recall_exceptions_use_recall_phase_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: str,
    adapter_error: str | None,
) -> None:
    cleanup_calls: list[str] = []

    class FailingRecallClient:
        async def retain_segment(self, segment: RetainSegment) -> RetainConfirmation:
            del segment
            return RetainConfirmation(confirmed=True)

        async def recall(self, query: str) -> RecallResponse:
            del query
            raise failure

        async def close(self) -> None:
            return None

    def create_client(config: BetterHindsightConfig) -> FailingRecallClient:
        del config
        return FailingRecallClient()

    def cleanup(
        config: CanaryConfig,
        document_id: str,
        *,
        deadline: float,
    ) -> tuple[bool, int]:
        del config, deadline
        cleanup_calls.append(document_id)
        return True, 0

    monkeypatch.setattr(canary_module, "create_hindsight_client", create_client)
    monkeypatch.setattr(canary_module, "_cleanup", cleanup)
    with _server() as api_url:
        result = run_canary(_config(api_url))

    assert result["error"] == expected
    if adapter_error is not None:
        assert result["adapter_error"] == adapter_error
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("better-hindsight-canary-")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"health": {"status": "starting"}}, "health_invalid"),
        ({"health": []}, "health_invalid"),
        ({"version": {"api_version": "0.8.4"}}, "version_invalid"),
        ({"version": {}}, "version_invalid"),
    ],
)
def test_health_and_version_validation_fail_closed_without_retain(
    overrides: dict[str, object], expected: str
) -> None:
    with _server(**overrides) as api_url:
        result = run_canary(_config(api_url))
    assert result["error"] == expected
    assert not any(method == "DELETE" for method, _ in _Handler.paths)
    _assert_private_absent(result)


@pytest.mark.parametrize(
    "cleanup_override",
    [
        {
            "success": True,
            "document_id": None,
            "memory_units_deleted": 0,
        },
        {"success": False, "document_id": "wrong", "memory_units_deleted": 0},
        {"success": True, "document_id": "wrong", "memory_units_deleted": 1},
        {"success": True, "document_id": "ignored", "memory_units_deleted": -1},
    ],
)
def test_cleanup_failure_overrides_otherwise_successful_result(cleanup_override: object) -> None:
    with _server(cleanup_override=cleanup_override) as api_url:
        result = run_canary(_config(api_url))
    assert result["result"] == "error"
    assert result["error"] == "cleanup_failed"
    _assert_private_absent(result)


def test_polling_exhaustion_is_bounded_and_cleans_only_exact_document() -> None:
    with _server(visible_after=99) as api_url:
        result = run_canary(_config(api_url, max_polls=3))
    assert result["error"] == "recall_timeout"
    assert result["poll_count"] == 3
    assert sum(path.endswith("/memories/recall") for _, path in _Handler.paths) == 3
    deletes = [path for method, path in _Handler.paths if method == "DELETE"]
    assert deletes == ["/v1/default/banks/isolated-canary-bank/documents/" + _Handler.document_id]
    _assert_private_absent(result)
