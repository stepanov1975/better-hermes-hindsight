"""One shared Better Hindsight process runtime and its owning async event loop."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, NoReturn, Protocol, TypeVar

from better_hermes_hindsight.client import (
    HindsightClientError,
    HindsightClientProtocol,
    RetainConfirmation,
    RetainSegment,
    create_hindsight_client,
)
from better_hermes_hindsight.config import BetterHindsightConfig
from better_hermes_hindsight.outbox import (
    AdmissionResult,
    AdmissionStatus,
    OutboxClaimResult,
    OutboxClaimStatus,
    OutboxFailureCategory,
    OutboxRow,
    OutboxTransitionResult,
    OutboxTransitionStatus,
    ProfileLockAcquisitionResult,
    ProfileLockOwner,
    ProfileLockStatus,
    SQLiteOutbox,
)
from better_hermes_hindsight.retention import RetainedSegment, build_retained_segments

ASYNC_CANCELLATION_DRAIN_SECONDS = 0.05


class AsyncCallTimeoutError(TimeoutError):
    """A sync-to-async operation exceeded its total caller deadline."""


class AsyncRunnerReentrancyError(RuntimeError):
    """The sync bridge was called from its own event-loop thread."""


class AsyncRunnerClosedError(RuntimeError):
    """The async runner is no longer accepting work."""


class AsyncRunnerStartError(RuntimeError):
    """The owning event-loop thread could not start."""


class AsyncRunnerUnsettledError(RuntimeError):
    """A prior cancellation-resistant operation still owns the shared async client."""


class SenderStopError(RuntimeError):
    """The sender or shared runner did not settle before the one shutdown deadline."""


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
    unsettled_token: object = field(default_factory=object)


class AsyncRunner:
    """Run async operations on one dedicated owning event loop without an executor or queue."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_failed = False
        self._condition = threading.Condition()
        self._unsettled_tokens: set[object] = set()
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
        with self._condition:
            if self._shutdown or self._loop is None:
                raise AsyncRunnerClosedError("Better Hindsight async runner is closed.")
            if self._unsettled_tokens:
                raise AsyncRunnerUnsettledError(
                    "Better Hindsight async runner is waiting for prior work to settle."
                ) from None
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
            self._publish_unsettled(state)
            raise AsyncCallTimeoutError(
                "Better Hindsight operation exceeded its total deadline."
            ) from None
        except BaseException:
            if not state.done.is_set() and not state.result.done():
                self._cancel_and_wait(loop, state)
                self._publish_unsettled(state)
            raise

    def wait_for_settlement(self, *, timeout: float) -> bool:
        """Wait within one caller-supplied bound until every published live task settles."""

        if timeout < 0:
            raise ValueError("Better Hindsight settlement timeout must not be negative.")
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._unsettled_tokens:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def shutdown(self) -> bool:
        """Stop the event loop after cancelling any remaining owned tasks."""

        if self.in_owning_loop:
            raise AsyncRunnerReentrancyError(
                "Better Hindsight async runner cannot shut down from its owning event loop."
            )
        with self._condition:
            if self._shutdown:
                return False
            if self._unsettled_tokens:
                raise AsyncRunnerUnsettledError(
                    "Better Hindsight async runner is waiting for prior work to settle."
                ) from None
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

    def _complete(self, state: _ScheduledCall[_T], task: asyncio.Task[_T]) -> None:
        with self._condition:
            try:
                value = task.result()
            except asyncio.CancelledError as error:
                state.result.set_exception(error)
            except BaseException as error:
                state.result.set_exception(error)
            else:
                state.result.set_result(value)
            state.done.set()
            self._unsettled_tokens.discard(state.unsettled_token)
            self._condition.notify_all()

    def _publish_unsettled(self, state: _ScheduledCall[_T]) -> None:
        with self._condition:
            if not state.done.is_set():
                self._unsettled_tokens.add(state.unsettled_token)

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


class OutboxProtocol(Protocol):
    """The complete local admission and owner-fenced sender surface."""

    def admit(self, segments: Sequence[RetainedSegment]) -> AdmissionResult:
        """Atomically admit one complete retained turn."""
        ...

    def try_acquire_profile_lock(self) -> ProfileLockAcquisitionResult:
        """Try one nonblocking profile-wide sender ownership acquisition."""
        ...

    def recover_sending(
        self,
        owner: ProfileLockOwner,
        *,
        now: float,
    ) -> OutboxTransitionResult:
        """Recover every stale sending row under exclusive ownership."""
        ...

    def claim_due(self, owner: ProfileLockOwner, *, now: float) -> OutboxClaimResult:
        """Claim one matching due row under exclusive ownership."""
        ...

    def complete_claim(
        self,
        owner: ProfileLockOwner,
        *,
        document_id: str,
        attempt_count: int,
    ) -> OutboxTransitionResult:
        """Delete one exactly guarded confirmed claim."""
        ...

    def reschedule_claim(
        self,
        owner: ProfileLockOwner,
        *,
        document_id: str,
        attempt_count: int,
        category: OutboxFailureCategory,
        completed_at: float,
    ) -> OutboxTransitionResult:
        """Guardedly persist one retryable failure."""
        ...

    def next_matching_retry_deadline(self) -> float | None:
        """Return the earliest pending deadline for this runtime identity."""
        ...

    def close(self) -> None:
        """Close the sole process-owned outbox connection."""
        ...


class SenderProtocol(Protocol):
    """Lifecycle seam for the one runtime-owned sender thread."""

    def start(self) -> None: ...

    def wake(self) -> None: ...

    def request_stop(self) -> None: ...

    def join(self, timeout: float | None = None) -> bool: ...


ClientFactory = Callable[[BetterHindsightConfig], HindsightClientProtocol]
OutboxFactory = Callable[[BetterHindsightConfig], OutboxProtocol]
SenderFactory = Callable[
    [BetterHindsightConfig, OutboxProtocol, HindsightClientProtocol, AsyncRunner],
    SenderProtocol,
]
_RuntimeOperation = Callable[[HindsightClientProtocol], Awaitable[_T]]


class OutboxSender:
    """One eager bounded sender elected by the profile's POSIX advisory lock."""

    def __init__(
        self,
        *,
        config: BetterHindsightConfig,
        outbox: OutboxProtocol,
        client: HindsightClientProtocol,
        runner: AsyncRunner,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self._outbox = outbox
        self._client = client
        self._runner = runner
        self._wall_time = wall_time
        self._poll_interval = config.outbox.poll_interval_seconds
        self._retain_timeout = config.retain.timeout_seconds
        self._state_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._started = False
        self._wake = threading.Event()
        self._wake.set()
        self._thread = threading.Thread(
            target=self._run,
            name="better-hindsight-outbox-sender",
            daemon=True,
        )

    def __repr__(self) -> str:
        return "OutboxSender()"

    def start(self) -> None:
        """Start this sender exactly once; startup immediately inspects persisted work."""

        with self._state_lock:
            if self._started:
                return
            self._thread.start()
            self._started = True

    def wake(self) -> None:
        """Wake startup, admission, retry, ownership, or shutdown polling."""

        self._wake.set()

    def request_stop(self) -> None:
        """Block future claims and wake the thread so active work can finish."""

        self._stop_requested.set()
        self._wake.set()

    def join(self, timeout: float | None = None) -> bool:
        """Join within the caller's remaining absolute-deadline budget."""

        with self._state_lock:
            started = self._started
        if not started:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stopping():
            acquisition = self._outbox.try_acquire_profile_lock()
            if acquisition.status is not ProfileLockStatus.ACQUIRED or acquisition.owner is None:
                self._wake.clear()
                if not self._stopping():
                    self._wake.wait(timeout=self._poll_interval)
                continue

            owner = acquisition.owner
            ownership_lost = False
            try:
                recovery = self._outbox.recover_sending(owner, now=self._wall_time())
                if recovery.status is not OutboxTransitionStatus.APPLIED:
                    ownership_lost = True
                else:
                    ownership_lost = self._run_as_owner(owner)
            finally:
                owner.release()

            if ownership_lost:
                self._wake.clear()
                if not self._stopping():
                    self._wake.wait(timeout=self._poll_interval)

    def _run_as_owner(self, owner: ProfileLockOwner) -> bool:
        while not self._stopping():
            self._wake.clear()
            if self._stopping():
                return False
            if not self._runner.wait_for_settlement(timeout=0.0):
                if not self._wait_for_runner_settlement(stop_sensitive=True):
                    return False
                continue

            claim = self._claim_if_running(owner)
            if claim is None:
                return False
            if claim.status is OutboxClaimStatus.LOCAL_FAILURE:
                return True
            if claim.status is OutboxClaimStatus.EMPTY or claim.row is None:
                try:
                    retry_deadline = self._outbox.next_matching_retry_deadline()
                except Exception:
                    return True
                timeout = self._poll_interval
                if retry_deadline is not None:
                    timeout = min(timeout, max(0.0, retry_deadline - self._wall_time()))
                self._wake.wait(timeout=timeout)
                continue

            row = claim.row
            self._before_submit(row)
            transition = self._deliver_claim(owner, row)
            if transition.status is not OutboxTransitionStatus.APPLIED:
                return True
        return False

    def _claim_if_running(self, owner: ProfileLockOwner) -> OutboxClaimResult | None:
        if self._stopping():
            return None
        return self._outbox.claim_due(owner, now=self._wall_time())

    def _before_submit(self, row: OutboxRow) -> None:
        """Deterministic test barrier immediately after claim and before SDK submission."""

        del row

    def _deliver_claim(
        self,
        owner: ProfileLockOwner,
        row: OutboxRow,
    ) -> OutboxTransitionResult:
        segment = RetainSegment(content=row.content, document_id=row.document_id)
        category: OutboxFailureCategory | None = None
        try:
            confirmation = self._runner.run(
                lambda: self._client.retain_segment(segment),
                timeout=self._retain_timeout,
            )
        except AsyncRunnerUnsettledError:
            self._wait_for_runner_settlement(stop_sensitive=False)
            category = OutboxFailureCategory.RETAIN_FAILED
        except AsyncCallTimeoutError:
            self._wait_for_runner_settlement(stop_sensitive=False)
            category = OutboxFailureCategory.RETAIN_TIMEOUT
        except HindsightClientError:
            category = OutboxFailureCategory.RETAIN_FAILED
        except asyncio.CancelledError:
            category = OutboxFailureCategory.RETAIN_FAILED
        except Exception:
            category = OutboxFailureCategory.RETAIN_FAILED
        else:
            if type(confirmation) is RetainConfirmation and confirmation.confirmed is True:
                return self._outbox.complete_claim(
                    owner,
                    document_id=row.document_id,
                    attempt_count=row.attempt_count,
                )
            category = OutboxFailureCategory.RETAIN_UNCONFIRMED

        return self._outbox.reschedule_claim(
            owner,
            document_id=row.document_id,
            attempt_count=row.attempt_count,
            category=category,
            completed_at=self._wall_time(),
        )

    def _wait_for_runner_settlement(self, *, stop_sensitive: bool) -> bool:
        wait_slice = min(self._poll_interval, ASYNC_CANCELLATION_DRAIN_SECONDS)
        while not self._runner.wait_for_settlement(timeout=wait_slice):
            if stop_sensitive and self._stopping():
                return False
        return True

    def _stopping(self) -> bool:
        return self._stop_requested.is_set()


def _open_outbox(config: BetterHindsightConfig) -> OutboxProtocol:
    return SQLiteOutbox.open(config)


def _create_sender(
    config: BetterHindsightConfig,
    outbox: OutboxProtocol,
    client: HindsightClientProtocol,
    runner: AsyncRunner,
) -> SenderProtocol:
    return OutboxSender(config=config, outbox=outbox, client=client, runner=runner)


async def _construct_client(
    factory: ClientFactory,
    config: BetterHindsightConfig,
) -> HindsightClientProtocol:
    return factory(config)


def _monotonic_now() -> float:
    return time.monotonic()


def _remaining_until(deadline: float) -> float:
    return max(0.0, deadline - _monotonic_now())


class ProcessRuntime:
    """The sole active Better Hindsight client and event loop in this process."""

    __slots__ = (
        "__weakref__",
        "_active_calls",
        "_client",
        "_closed",
        "_finalizing",
        "_lifecycle",
        "_outbox",
        "_retain_segment_max_bytes",
        "_retain_tags",
        "_runner",
        "_sender",
        "_shutdown_timeout",
    )

    def __init__(
        self,
        config: BetterHindsightConfig,
        *,
        client_factory: ClientFactory = create_hindsight_client,
        outbox_factory: OutboxFactory = _open_outbox,
        sender_factory: SenderFactory = _create_sender,
    ) -> None:
        self._runner = AsyncRunner()
        self._lifecycle = threading.Condition()
        self._active_calls = 0
        self._closed = False
        self._finalizing = False
        self._outbox: OutboxProtocol | None = None
        self._sender: SenderProtocol | None = None
        self._retain_segment_max_bytes = config.retain.segment_max_bytes
        self._retain_tags = config.retain.tags
        self._shutdown_timeout = (
            config.retain.timeout_seconds
            + config.outbox.busy_timeout_seconds
            + ASYNC_CANCELLATION_DRAIN_SECONDS
            + 1.0
        )
        client: HindsightClientProtocol | None = None
        try:
            client = self._runner.run(lambda: _construct_client(client_factory, config))
            self._client = client
            if config.retain.enabled:
                self._outbox = outbox_factory(config)
                self._sender = sender_factory(config, self._outbox, client, self._runner)
                self._sender.start()
        except BaseException:
            self._cleanup_failed_construction(client)
            raise

    def __repr__(self) -> str:
        return "ProcessRuntime()"

    @property
    def accepting_operations(self) -> bool:
        """Return whether this runtime can still admit provider work."""

        with self._lifecycle:
            return not self._finalizing and not self._closed

    def _begin_operation(self) -> None:
        with self._lifecycle:
            if self._finalizing or self._closed:
                raise RuntimeFinalizedError("Better Hindsight process runtime is finalized.")
            self._active_calls += 1

    def _finish_operation(self) -> None:
        with self._lifecycle:
            self._active_calls -= 1
            if self._active_calls == 0:
                self._lifecycle.notify_all()

    def call(
        self,
        operation: _RuntimeOperation[_T],
        *,
        timeout: float | None,
    ) -> _T:
        """Run an operation against the shared client on its owning loop."""

        self._begin_operation()
        try:
            return self._runner.run(lambda: operation(self._client), timeout=timeout)
        finally:
            self._finish_operation()

    def admit_turn(
        self,
        *,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> AdmissionResult:
        """Construct and atomically admit one turn without client or network work."""

        self._begin_operation()
        try:
            outbox = self._outbox
            if outbox is None:
                return AdmissionResult(AdmissionStatus.INVALID)
            segments = build_retained_segments(
                session_id=session_id,
                user_content=user_content,
                assistant_content=assistant_content,
                tags=self._retain_tags,
                segment_max_bytes=self._retain_segment_max_bytes,
            )
            result = outbox.admit(segments)
            sender = self._sender
            if result.accepted and sender is not None:
                sender.wake()
            return result
        finally:
            self._finish_operation()

    def recall(self, query: str, *, timeout: float) -> object:
        """Recall through the shared client under the caller's total deadline."""

        return self.call(lambda client: client.recall(query), timeout=timeout)

    def finalize(self) -> bool:
        """Stop all async work before exact outbox, client, then runner closure."""

        if self._runner.in_owning_loop:
            raise AsyncRunnerReentrancyError(
                "Better Hindsight process runtime cannot finalize from its owning event loop."
            )
        with self._lifecycle:
            if self._closed:
                return False
            self._finalizing = True

        deadline = _monotonic_now() + self._shutdown_timeout
        sender = self._sender
        if sender is not None:
            sender.request_stop()
            sender.wake()

        with self._lifecycle:
            while self._active_calls:
                remaining = _remaining_until(deadline)
                if remaining <= 0:
                    self._raise_sender_stop()
                self._lifecycle.wait(timeout=remaining)

        if sender is not None and not sender.join(timeout=_remaining_until(deadline)):
            self._raise_sender_stop()
        if not self._runner.wait_for_settlement(timeout=_remaining_until(deadline)):
            self._raise_sender_stop()

        failure: BaseException | None = None
        outbox = self._outbox
        if outbox is not None:
            try:
                outbox.close()
            except BaseException as error:
                failure = error
        try:
            self._runner.run(self._client.close)
        except BaseException as error:
            if failure is None:
                failure = error
        try:
            self._runner.shutdown()
        except BaseException as error:
            if failure is None:
                failure = error
        with self._lifecycle:
            self._closed = True
            self._lifecycle.notify_all()
        if failure is not None:
            raise failure
        return True

    @staticmethod
    def _raise_sender_stop() -> NoReturn:
        raise SenderStopError(
            "Better Hindsight sender could not stop before shutdown deadline."
        ) from None

    def _cleanup_failed_construction(
        self,
        client: HindsightClientProtocol | None,
    ) -> None:
        sender = self._sender
        if sender is not None:
            with contextlib.suppress(BaseException):
                sender.request_stop()
            with contextlib.suppress(BaseException):
                sender.wake()
            try:
                stopped = sender.join(timeout=self._shutdown_timeout)
            except BaseException:
                stopped = False
            if not stopped:
                self._raise_sender_stop()

        outbox = self._outbox
        if outbox is not None:
            with contextlib.suppress(BaseException):
                outbox.close()
        if client is not None:
            with contextlib.suppress(BaseException):
                self._runner.run(client.close)
        with contextlib.suppress(BaseException):
            self._runner.shutdown()


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

    def admit_turn(
        self,
        *,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> AdmissionResult:
        """Request one local admission through the shared process runtime."""

        return self._require_runtime().admit_turn(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
        )

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
    outbox_factory: OutboxFactory = _open_outbox,
    sender_factory: SenderFactory = _create_sender,
) -> ProcessRuntimeHandle:
    """Acquire a lightweight handle to the one process runtime for an exact configuration."""

    global _ACTIVE_CONFIG, _ACTIVE_FINALIZING, _ACTIVE_RUNTIME
    with _ACTIVE_LOCK:
        if _ACTIVE_FINALIZING:
            raise RuntimeFinalizedError("Better Hindsight process runtime is finalizing.")
        if _ACTIVE_RUNTIME is not None:
            if not _ACTIVE_RUNTIME.accepting_operations:
                raise RuntimeFinalizedError("Better Hindsight process runtime is finalizing.")
            if config != _ACTIVE_CONFIG:
                raise RuntimeConfigurationConflict(
                    "Better Hindsight process runtime configuration conflict; restart required."
                ) from None
            return ProcessRuntimeHandle(_ACTIVE_RUNTIME)

        runtime = ProcessRuntime(
            config,
            client_factory=client_factory,
            outbox_factory=outbox_factory,
            sender_factory=sender_factory,
        )
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
    except SenderStopError:
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
    "AsyncRunnerUnsettledError",
    "OutboxSender",
    "ProcessRuntime",
    "ProcessRuntimeHandle",
    "RuntimeConfigurationConflict",
    "RuntimeFinalizedError",
    "RuntimeHandleClosedError",
    "SenderStopError",
    "acquire_process_runtime",
    "finalize_process_runtime",
    "reset_process_runtime_for_tests",
]
