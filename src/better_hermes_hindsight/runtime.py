"""One shared Better Hindsight process runtime and its owning async event loop."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from better_hermes_hindsight.client import (
    HindsightClientProtocol,
    create_hindsight_client,
)
from better_hermes_hindsight.config import BetterHindsightConfig

ASYNC_CANCELLATION_DRAIN_SECONDS = 0.05


class AsyncCallTimeoutError(TimeoutError):
    """A sync-to-async operation exceeded its total caller deadline."""


class AsyncRunnerReentrancyError(RuntimeError):
    """The sync bridge was called from its own event-loop thread."""


class AsyncRunnerClosedError(RuntimeError):
    """The async runner is no longer accepting work."""


class AsyncRunnerStartError(RuntimeError):
    """The owning event-loop thread could not start."""


class RuntimeConfigurationConflict(RuntimeError):
    """A process attempted to activate a second Better Hindsight configuration."""


class RuntimeFinalizedError(RuntimeError):
    """An operation was attempted after explicit process-runtime finalization."""


class RuntimeHandleClosedError(RuntimeError):
    """An operation was attempted through a closed lightweight handle."""


_T = TypeVar("_T")


@dataclass(slots=True)
class _ScheduledCall(Generic[_T]):
    result: concurrent.futures.Future[_T] = field(default_factory=concurrent.futures.Future)
    done: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[_T] | None = None


class AsyncRunner:
    """Run async operations on one dedicated owning event loop without an executor or queue."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_failed = False
        self._state_lock = threading.Lock()
        self._shutdown = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="better-hindsight-event-loop",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()
        if self._startup_failed or self._loop is None:
            raise AsyncRunnerStartError("Better Hindsight async runtime could not start.") from None

    def __repr__(self) -> str:
        return "AsyncRunner()"

    @property
    def in_owning_loop(self) -> bool:
        """Whether the caller is executing on this runner's event-loop thread."""

        return threading.get_ident() == self._thread.ident

    def run(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        timeout: float | None = None,
    ) -> _T:
        """Run one async operation under a total deadline and return its result synchronously."""

        if self.in_owning_loop:
            raise AsyncRunnerReentrancyError(
                "Better Hindsight async runner cannot be called from its owning event loop."
            )
        if timeout is not None and timeout <= 0:
            raise ValueError("Better Hindsight async deadline must be greater than zero.")

        deadline = None if timeout is None else time.monotonic() + timeout
        state: _ScheduledCall[_T] = _ScheduledCall()
        with self._state_lock:
            if self._shutdown or self._loop is None:
                raise AsyncRunnerClosedError("Better Hindsight async runner is closed.")
            loop = self._loop
            try:
                loop.call_soon_threadsafe(self._schedule, state, operation)
            except RuntimeError:
                raise AsyncRunnerClosedError("Better Hindsight async runner is closed.") from None

        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            return state.result.result(timeout=remaining)
        except concurrent.futures.TimeoutError:
            if state.result.done():
                return state.result.result()
            self._cancel_and_wait(loop, state)
            raise AsyncCallTimeoutError(
                "Better Hindsight operation exceeded its total deadline."
            ) from None
        except BaseException:
            if not state.done.is_set() and not state.result.done():
                self._cancel_and_wait(loop, state)
            raise

    def shutdown(self) -> bool:
        """Stop the event loop after cancelling any remaining owned tasks."""

        if self.in_owning_loop:
            raise AsyncRunnerReentrancyError(
                "Better Hindsight async runner cannot shut down from its owning event loop."
            )
        with self._state_lock:
            if self._shutdown:
                return False
            self._shutdown = True
            loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        self._thread.join()
        return True

    def _run_loop(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except BaseException:
            self._startup_failed = True
            self._ready.set()
            return

        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            asyncio.set_event_loop(None)
            loop.close()
            self._loop = None

    def _schedule(
        self,
        state: _ScheduledCall[_T],
        operation: Callable[[], Awaitable[_T]],
    ) -> None:
        loop = self._loop
        if loop is None:
            state.result.set_exception(
                AsyncRunnerClosedError("Better Hindsight async runner is closed.")
            )
            state.done.set()
            return

        async def invoke() -> _T:
            return await operation()

        task = loop.create_task(invoke())
        state.task = task
        task.add_done_callback(lambda completed: self._complete(state, completed))

    @staticmethod
    def _complete(state: _ScheduledCall[_T], task: asyncio.Task[_T]) -> None:
        try:
            value = task.result()
        except asyncio.CancelledError as error:
            state.result.set_exception(error)
        except BaseException as error:
            state.result.set_exception(error)
        else:
            state.result.set_result(value)
        finally:
            state.done.set()

    @staticmethod
    def _cancel(state: _ScheduledCall[_T]) -> None:
        if state.task is not None and not state.task.done():
            state.task.cancel()

    def _cancel_and_wait(
        self,
        loop: asyncio.AbstractEventLoop,
        state: _ScheduledCall[_T],
    ) -> None:
        try:
            loop.call_soon_threadsafe(self._cancel, state)
        except RuntimeError:
            return
        state.done.wait(timeout=ASYNC_CANCELLATION_DRAIN_SECONDS)


ClientFactory = Callable[[BetterHindsightConfig], HindsightClientProtocol]
_RuntimeOperation = Callable[[HindsightClientProtocol], Awaitable[_T]]


async def _construct_client(
    factory: ClientFactory,
    config: BetterHindsightConfig,
) -> HindsightClientProtocol:
    return factory(config)


class ProcessRuntime:
    """The sole active Better Hindsight client and event loop in this process."""

    __slots__ = (
        "__weakref__",
        "_active_calls",
        "_client",
        "_finalized",
        "_lifecycle",
        "_runner",
    )

    def __init__(
        self,
        config: BetterHindsightConfig,
        *,
        client_factory: ClientFactory = create_hindsight_client,
    ) -> None:
        self._runner = AsyncRunner()
        self._lifecycle = threading.Condition()
        self._active_calls = 0
        self._finalized = False
        try:
            self._client = self._runner.run(lambda: _construct_client(client_factory, config))
        except BaseException:
            self._runner.shutdown()
            raise

    def __repr__(self) -> str:
        return "ProcessRuntime()"

    def call(
        self,
        operation: _RuntimeOperation[_T],
        *,
        timeout: float | None,
    ) -> _T:
        """Run an operation against the shared client on its owning loop."""

        with self._lifecycle:
            if self._finalized:
                raise RuntimeFinalizedError("Better Hindsight process runtime is finalized.")
            self._active_calls += 1
        try:
            return self._runner.run(lambda: operation(self._client), timeout=timeout)
        finally:
            with self._lifecycle:
                self._active_calls -= 1
                if self._active_calls == 0:
                    self._lifecycle.notify_all()

    def recall(self, query: str, *, timeout: float) -> object:
        """Recall through the shared client under the caller's total deadline."""

        return self.call(lambda client: client.recall(query), timeout=timeout)

    def finalize(self) -> bool:
        """Close the client exactly once on its owning loop, then stop that loop."""

        if self._runner.in_owning_loop:
            raise AsyncRunnerReentrancyError(
                "Better Hindsight process runtime cannot finalize from its owning event loop."
            )
        with self._lifecycle:
            if self._finalized:
                return False
            self._finalized = True
            while self._active_calls:
                self._lifecycle.wait()
        try:
            self._runner.run(self._client.close)
        finally:
            self._runner.shutdown()
        return True


class ProcessRuntimeHandle:
    """A lightweight provider handle that never owns process resources."""

    __slots__ = ("_closed", "_runtime")

    def __init__(self, runtime: ProcessRuntime) -> None:
        self._runtime = runtime
        self._closed = False

    def __repr__(self) -> str:
        return "ProcessRuntimeHandle()"

    @property
    def runtime(self) -> ProcessRuntime:
        """Return the shared runtime while this lightweight handle remains active."""

        return self._require_runtime()

    def call(
        self,
        operation: _RuntimeOperation[_T],
        *,
        timeout: float | None,
    ) -> _T:
        """Run an explicit operation through the shared process runtime."""

        return self._require_runtime().call(operation, timeout=timeout)

    def recall(self, query: str, *, timeout: float) -> object:
        """Recall through the shared process runtime."""

        return self._require_runtime().recall(query, timeout=timeout)

    def close(self) -> None:
        """Drop this handle without closing or replacing process-owned resources."""

        self._closed = True

    def _require_runtime(self) -> ProcessRuntime:
        if self._closed:
            raise RuntimeHandleClosedError("Better Hindsight runtime handle is closed.")
        return self._runtime


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_CONFIG: BetterHindsightConfig | None = None
_ACTIVE_RUNTIME: ProcessRuntime | None = None
_ACTIVE_FINALIZING = False


def acquire_process_runtime(
    config: BetterHindsightConfig,
    *,
    client_factory: ClientFactory = create_hindsight_client,
) -> ProcessRuntimeHandle:
    """Acquire a lightweight handle to the one process runtime for an exact configuration."""

    global _ACTIVE_CONFIG, _ACTIVE_FINALIZING, _ACTIVE_RUNTIME
    with _ACTIVE_LOCK:
        if _ACTIVE_FINALIZING:
            raise RuntimeFinalizedError("Better Hindsight process runtime is finalizing.")
        if _ACTIVE_RUNTIME is not None:
            if config != _ACTIVE_CONFIG:
                raise RuntimeConfigurationConflict(
                    "Better Hindsight process runtime configuration conflict; restart required."
                ) from None
            return ProcessRuntimeHandle(_ACTIVE_RUNTIME)

        runtime = ProcessRuntime(config, client_factory=client_factory)
        _ACTIVE_CONFIG = config
        _ACTIVE_RUNTIME = runtime
        return ProcessRuntimeHandle(runtime)


def finalize_process_runtime() -> bool:
    """Explicitly close and clear the process runtime; repeated calls are inert."""

    global _ACTIVE_CONFIG, _ACTIVE_FINALIZING, _ACTIVE_RUNTIME
    with _ACTIVE_LOCK:
        runtime = _ACTIVE_RUNTIME
        if runtime is None or _ACTIVE_FINALIZING:
            return False
        _ACTIVE_FINALIZING = True

    try:
        finalized = runtime.finalize()
    except AsyncRunnerReentrancyError:
        with _ACTIVE_LOCK:
            _ACTIVE_FINALIZING = False
        raise
    except BaseException:
        with _ACTIVE_LOCK:
            _ACTIVE_CONFIG = None
            _ACTIVE_RUNTIME = None
            _ACTIVE_FINALIZING = False
        raise

    with _ACTIVE_LOCK:
        _ACTIVE_CONFIG = None
        _ACTIVE_RUNTIME = None
        _ACTIVE_FINALIZING = False
    return finalized


def reset_process_runtime_for_tests() -> bool:
    """Explicit test seam that performs the same close-once finalization as process shutdown."""

    return finalize_process_runtime()


def _atexit_finalize_process_runtime() -> None:
    # Interpreter shutdown is a fallback only. Explicit finalization owns observable failures.
    with contextlib.suppress(BaseException):
        finalize_process_runtime()


atexit.register(_atexit_finalize_process_runtime)


__all__ = [
    "AsyncCallTimeoutError",
    "AsyncRunner",
    "AsyncRunnerClosedError",
    "AsyncRunnerReentrancyError",
    "ProcessRuntime",
    "ProcessRuntimeHandle",
    "RuntimeConfigurationConflict",
    "RuntimeFinalizedError",
    "RuntimeHandleClosedError",
    "acquire_process_runtime",
    "finalize_process_runtime",
    "reset_process_runtime_for_tests",
]
