"""Deterministic fake-server tests for the strict Hindsight 0.8.5 E2E canary."""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from better_hermes_hindsight import canary as canary_module
from better_hermes_hindsight.canary import CanaryConfig, run_canary

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
                "document_id": _Handler.document_id,
                "tags": _Handler.tags,
                "update_mode": "replace",
            }
        ],
        "async": False,
    }
    assert recall == {"query": _Handler.marker, "tags": _Handler.tags, "tags_match": "exact"}
    assert _Handler.paths[-1] == (
        "DELETE",
        "/v1/default/banks/isolated-canary-bank/documents/" + _Handler.document_id,
    )
    _assert_private_absent(result)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {
                "text": "Hindsight synthesized memory unit",
                "document_id": "owned-document",
                "tags": ["owned-tag"],
            },
            True,
        ),
        (
            {"text": "marker", "document_id": "wrong", "tags": ["owned-tag"]},
            False,
        ),
        (
            {"text": "marker", "document_id": "owned-document", "tags": ["wrong"]},
            False,
        ),
        (
            {
                "text": "marker",
                "document_id": "owned-document",
                "tags": ["owned-tag", "extra"],
            },
            False,
        ),
    ],
)
def test_recall_ownership_requires_exact_document_and_singleton_tag(
    value: object,
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
        ({"success": True}, "retain_unconfirmed"),
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
    assert _Handler.paths[-1][0] == "DELETE"
    _assert_private_absent(result)


@pytest.mark.parametrize(
    "recall_override",
    [
        {"results": [{"text": "marker", "document_id": "wrong", "tags": ["wrong-owner-tag"]}]},
        {"results": [{"text": "wrong", "document_id": "wrong", "tags": ["wrong"]}]},
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
    assert result["error"] == "recall_invalid"
    assert _Handler.paths[-1][0] == "DELETE"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError(), "recall_timeout"),
        (ValueError("private malformed-response sentinel"), "recall_invalid"),
        (OSError("private transport sentinel"), "recall_failed"),
    ],
)
def test_post_retain_recall_exceptions_use_recall_phase_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: str
) -> None:
    original_request = canary_module._request_json

    def fail_recall(
        config: CanaryConfig,
        method: str,
        path: str,
        *,
        timeout: float,
        payload: object | None = None,
    ) -> tuple[int, object]:
        if path.endswith("/memories/recall"):
            raise failure
        return original_request(config, method, path, timeout=timeout, payload=payload)

    monkeypatch.setattr(canary_module, "_request_json", fail_recall)
    with _server() as api_url:
        result = run_canary(_config(api_url))

    assert result["error"] == expected
    assert _Handler.paths[-1][0] == "DELETE"
    rendered = json.dumps(result)
    assert "private malformed-response sentinel" not in rendered
    assert "private transport sentinel" not in rendered


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
