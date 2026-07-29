"""Small deterministic aiohttp fake for the pinned Hindsight 0.8.5 wire contract."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import socket
from dataclasses import dataclass
from typing import Literal, TypeAlias

from aiohttp import web

MAX_REQUEST_BYTES = 64 * 1024
MAX_REQUEST_RECORDS = 64

RecallFault: TypeAlias = Literal[
    "malformed_json",
    "malformed_schema",
    "http_503",
    "delay",
]
RetainFault: TypeAlias = Literal[
    "false_success",
    "wrong_bank",
    "wrong_count",
    "asynchronous",
    "malformed_json",
    "malformed_schema",
    "http_503",
    "success_integer",
    "items_count_boolean",
    "async_integer",
    "delay",
]
AuthorizationState: TypeAlias = Literal[
    "absent",
    "valid_bearer",
    "invalid_bearer",
    "unexpected_bearer",
    "unsupported",
]


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """One bounded request record with no raw body or authorization value."""

    method: str
    path: str
    query: str
    json_body: object | None
    accept: str | None
    content_type: str | None
    user_agent: str | None
    authorization: AuthorizationState


@dataclass(frozen=True, slots=True)
class FakeServerReport:
    """Bounded operational metadata that excludes payloads and error bodies."""

    request_count: int
    recorded_count: int
    routes: tuple[str, ...]


class FakeHindsightServer:
    """Serve only the exact Task 3 Hindsight routes on a loopback socket."""

    def __init__(
        self,
        *,
        bank_id: str,
        disposable_bank_id: str,
        error_sentinel: str,
        expected_api_key: str | None,
    ) -> None:
        self._bank_id = bank_id
        self._disposable_bank_id = disposable_bank_id
        self._error_sentinel = error_sentinel
        self._expected_api_key = expected_api_key
        self._records: list[RequestRecord] = []
        self._request_count = 0
        self._next_recall_fault: RecallFault | None = None
        self._next_retain_fault: RetainFault | None = None
        self._delay_entered = asyncio.Event()
        self._delay_release = asyncio.Event()
        self._delay_finished = asyncio.Event()
        self._retain_delay_entered = asyncio.Event()
        self._retain_delay_release = asyncio.Event()
        self._retain_delay_finished = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._site: web.SockSite | None = None
        self._socket: socket.socket | None = None
        self._base_url: str | None = None
        self._closed = False

        app = web.Application(client_max_size=MAX_REQUEST_BYTES)
        bank_path = f"/v1/default/banks/{bank_id}"
        disposable_path = f"/v1/default/banks/{disposable_bank_id}"
        app.router.add_get("/version", self._version, allow_head=False)
        app.router.add_get(f"{bank_path}/profile", self._profile, allow_head=False)
        app.router.add_post(f"{bank_path}/memories/recall", self._recall)
        app.router.add_post(f"{bank_path}/memories", self._retain)
        app.router.add_get(f"{bank_path}/config", self._get_config, allow_head=False)
        app.router.add_patch(f"{bank_path}/config", self._patch_config)
        app.router.add_put(disposable_path, self._create_disposable)
        app.router.add_delete(disposable_path, self._delete_disposable)
        self._app = app

    def __repr__(self) -> str:
        return "FakeHindsightServer()"

    @property
    def base_url(self) -> str:
        """Return the started loopback URL without inspecting aiohttp internals."""

        if self._base_url is None:
            raise RuntimeError("Fake Hindsight server is not started.")
        return self._base_url

    @property
    def records(self) -> tuple[RequestRecord, ...]:
        """Return the bounded request records as an immutable container."""

        return tuple(self._records)

    @property
    def closed(self) -> bool:
        """Whether cleanup has completed."""

        return self._closed

    async def start(self, *, bound_socket: socket.socket | None = None) -> None:
        """Bind or adopt one loopback socket and pass it to public ``web.SockSite``.

        ``bound_socket`` exists only so connection-refusal recovery can keep the selected port
        reserved before this fake starts listening. The fake takes ownership of that socket.
        """

        if self._runner is not None or self._closed:
            raise RuntimeError("Fake Hindsight server cannot be started twice.")

        sock = bound_socket or socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if bound_socket is None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", 0))
            elif sock.getsockname()[0] != "127.0.0.1":
                raise ValueError("Fake Hindsight requires a bound IPv4 loopback socket.")
            sock.listen(socket.SOMAXCONN)
            sock.setblocking(False)
            port = sock.getsockname()[1]

            runner = web.AppRunner(self._app, access_log=None)
            await runner.setup()
            site = web.SockSite(runner, sock)
            await site.start()
        except BaseException:
            sock.close()
            raise

        self._socket = sock
        self._runner = runner
        self._site = site
        self._base_url = f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        """Release delayed handlers, stop aiohttp, and close the reserved socket."""

        if self._closed:
            return
        self._closed = True
        self.release_delay()
        self.release_retain_delay()
        runner = self._runner
        sock = self._socket
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
            self._site = None
            self._runner = None
            self._socket = None

    def arm_recall_fault(self, fault: RecallFault) -> None:
        """Apply one fault to the next recall only."""

        if self._next_recall_fault is not None:
            raise RuntimeError("Fake Hindsight recall fault is already armed.")
        if self._delay_entered.is_set() and not self._delay_finished.is_set():
            raise RuntimeError("Fake Hindsight delayed handler is still active.")
        if fault == "delay":
            self._delay_entered.clear()
            self._delay_release.clear()
            self._delay_finished.clear()
        self._next_recall_fault = fault

    async def wait_for_delay_entered(self) -> None:
        """Wait a bounded interval for the one delayed recall handler to start."""

        await asyncio.wait_for(self._delay_entered.wait(), timeout=1.0)

    def release_delay(self) -> None:
        """Cooperatively release any delayed recall handler."""

        self._delay_release.set()

    async def wait_for_delay_finished(self) -> None:
        """Wait a bounded interval for delayed-handler cleanup."""

        if not self._delay_entered.is_set():
            return
        await asyncio.wait_for(self._delay_finished.wait(), timeout=1.0)

    def arm_retain_fault(self, fault: RetainFault) -> None:
        """Apply one bounded fault to the next retain request only."""

        if self._next_retain_fault is not None:
            raise RuntimeError("Fake Hindsight retain fault is already armed.")
        if self._retain_delay_entered.is_set() and not self._retain_delay_finished.is_set():
            raise RuntimeError("Fake Hindsight delayed retain handler is still active.")
        if fault == "delay":
            self._retain_delay_entered.clear()
            self._retain_delay_release.clear()
            self._retain_delay_finished.clear()
        self._next_retain_fault = fault

    async def wait_for_retain_delay_entered(self) -> None:
        """Wait a bounded interval for the one delayed retain handler to start."""

        await asyncio.wait_for(self._retain_delay_entered.wait(), timeout=1.0)

    def release_retain_delay(self) -> None:
        """Cooperatively release any delayed retain handler."""

        self._retain_delay_release.set()

    async def wait_for_retain_delay_finished(self) -> None:
        """Wait a bounded interval for delayed retain-handler cleanup."""

        if not self._retain_delay_entered.is_set():
            return
        await asyncio.wait_for(self._retain_delay_finished.wait(), timeout=1.0)

    def safe_report(self) -> FakeServerReport:
        """Return only bounded route metadata, never bodies or authorization values."""

        return FakeServerReport(
            request_count=self._request_count,
            recorded_count=len(self._records),
            routes=tuple(f"{record.method} {record.path}" for record in self._records),
        )

    async def _record(self, request: web.Request) -> object | None:
        json_body: object | None = None
        if request.can_read_body:
            try:
                json_body = await request.json()
            except (ValueError, UnicodeError):
                raise web.HTTPBadRequest(
                    text="Fake Hindsight expected a JSON request body."
                ) from None

        self._request_count += 1
        if len(self._records) >= MAX_REQUEST_RECORDS:
            raise web.HTTPTooManyRequests(text="Fake Hindsight request record limit reached.")

        authorization = self._authorization_state(request.headers.get("Authorization"))
        self._records.append(
            RequestRecord(
                method=request.method,
                path=request.path,
                query=request.query_string,
                json_body=json_body,
                accept=request.headers.get("Accept"),
                content_type=request.headers.get("Content-Type"),
                user_agent=request.headers.get("User-Agent"),
                authorization=authorization,
            )
        )
        if request.query_string:
            raise web.HTTPBadRequest(text="Fake Hindsight does not accept query parameters.")
        return json_body

    def _authorization_state(self, header: str | None) -> AuthorizationState:
        if header is None:
            return "absent"
        scheme, separator, value = header.partition(" ")
        if scheme != "Bearer" or not separator or not value:
            return "unsupported"
        if self._expected_api_key is None:
            return "unexpected_bearer"
        if hmac.compare_digest(value, self._expected_api_key):
            return "valid_bearer"
        return "invalid_bearer"

    async def _version(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response(
            {
                "api_version": "0.8.5",
                "features": {
                    "observations": True,
                    "mcp": False,
                    "worker": False,
                    "bank_config_api": True,
                    "bank_llm_health": False,
                    "file_upload_api": False,
                    "document_export_api": False,
                    "document_import_api": False,
                    "audit_log": False,
                    "llm_trace": False,
                    "store_document_text": True,
                },
            }
        )

    async def _profile(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response(self._profile_response(self._bank_id, "Fixture bank"))

    async def _recall(self, request: web.Request) -> web.Response:
        await self._record(request)
        fault = self._next_recall_fault
        self._next_recall_fault = None
        if fault == "malformed_json":
            return web.Response(
                text=f"{self._error_sentinel}{{",
                content_type="application/json",
            )
        if fault == "malformed_schema":
            return web.json_response(
                {
                    "results": [
                        {
                            "id": "invalid-text",
                            "text": {"raw_error": self._error_sentinel},
                        }
                    ]
                }
            )
        if fault == "http_503":
            return web.Response(status=503, text=self._error_sentinel)
        if fault == "delay":
            self._delay_entered.set()
            try:
                await self._delay_release.wait()
            finally:
                self._delay_finished.set()
        return web.json_response(
            {
                "results": [
                    {
                        "id": "observation-1",
                        "text": "fixture observation",
                        "type": "observation",
                        "tags": ["scope-a"],
                        "source_fact_ids": ["source-fact-1"],
                        "scores": {
                            "final": 0.9,
                            "semantic": 0.8,
                            "keyword": 0.2,
                            "reranker": 0.7,
                        },
                    }
                ],
                "source_facts": {
                    "source-fact-1": {
                        "id": "source-fact-1",
                        "text": "fixture source fact",
                        "type": "world",
                        "tags": ["scope-a"],
                    }
                },
            }
        )

    async def _retain(self, request: web.Request) -> web.Response:
        await self._record(request)
        fault = self._next_retain_fault
        self._next_retain_fault = None
        if fault == "malformed_json":
            return web.Response(
                text=f"{self._error_sentinel}{{",
                content_type="application/json",
            )
        if fault == "malformed_schema":
            return web.json_response(
                {
                    "success": True,
                    "bank_id": self._bank_id,
                    "items_count": {"raw_error": self._error_sentinel},
                    "async": False,
                }
            )
        if fault == "http_503":
            return web.Response(status=503, text=self._error_sentinel)
        if fault == "delay":
            self._retain_delay_entered.set()
            try:
                await self._retain_delay_release.wait()
            finally:
                self._retain_delay_finished.set()

        response: dict[str, object] = {
            "success": True,
            "bank_id": self._bank_id,
            "items_count": 1,
            "async": False,
        }
        if fault == "false_success":
            response["success"] = False
        elif fault == "wrong_bank":
            response["bank_id"] = "different-fixture-bank"
        elif fault == "wrong_count":
            response["items_count"] = 2
        elif fault == "asynchronous":
            response["async"] = True
        elif fault == "success_integer":
            response["success"] = 1
        elif fault == "items_count_boolean":
            response["items_count"] = True
        elif fault == "async_integer":
            response["async"] = 0
        return web.json_response(response)

    async def _get_config(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response(self._config_response({}))

    async def _patch_config(self, request: web.Request) -> web.Response:
        json_body = await self._record(request)
        updates: object | None = None
        if isinstance(json_body, dict):
            updates = json_body.get("updates")
        if (
            not isinstance(updates, dict)
            or len(updates) != 1
            or not set(updates).issubset({"retain_mission", "observations_mission"})
            or any(not isinstance(value, str) or not value for value in updates.values())
        ):
            raise web.HTTPBadRequest(text="Fake Hindsight expected one changed mission key.")
        return web.json_response(self._config_response(updates))

    async def _create_disposable(self, request: web.Request) -> web.Response:
        json_body = await self._record(request)
        if not isinstance(json_body, dict):
            raise web.HTTPBadRequest(text="Fake Hindsight expected a JSON object.")
        return web.json_response(
            self._profile_response(self._disposable_bank_id, "Disposable fixture bank")
        )

    async def _delete_disposable(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"success": True})

    @staticmethod
    def _profile_response(bank_id: str, name: str) -> dict[str, object]:
        return {
            "bank_id": bank_id,
            "name": name,
            "disposition": {
                "skepticism": 3,
                "literalism": 3,
                "empathy": 3,
            },
            "mission": "Fixture mission",
        }

    def _config_response(self, updates: dict[object, object]) -> dict[str, object]:
        config: dict[object, object] = {
            "retain_mission": "retain-old",
            "observations_mission": "observe-old",
        }
        config.update(updates)
        return {
            "bank_id": self._bank_id,
            "config": config,
            "overrides": dict(config),
        }


__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_REQUEST_RECORDS",
    "FakeHindsightServer",
    "FakeServerReport",
    "RecallFault",
    "RequestRecord",
    "RetainFault",
]
