"""Best-effort observational work: one daemon call per process, no waiting queue."""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Callable
from contextlib import suppress
from contextvars import copy_context
from typing import cast

# Hermes can load this package under multiple names. The bound must survive that boundary.
_SHARED_MODULE = "_better_hermes_hindsight_shadow_rewrite_v1"
_candidate = types.ModuleType(_SHARED_MODULE)
_candidate.__dict__["slot"] = threading.BoundedSemaphore(1)
_slot = cast(
    threading.BoundedSemaphore,
    sys.modules.setdefault(_SHARED_MODULE, _candidate).__dict__["slot"],
)


def submit_shadow(work: Callable[[], object]) -> str:
    """Never wait for a model, queue space, or an earlier hung call.

    A timeout cannot kill a synchronous host auxiliary call. Keep its slot occupied
    until it actually exits, even if its deadline passed; never replace stuck workers.
    """
    if not _slot.acquire(blocking=False):
        return "busy"
    try:
        context = copy_context()

        def run() -> None:
            try:
                with suppress(Exception):
                    context.run(work)
            finally:
                _slot.release()

        threading.Thread(target=run, name="better-hindsight-shadow-rewrite", daemon=True).start()
    except Exception:
        _slot.release()
        return "unavailable"
    return "submitted"


def drain_shadow_for_tests(*, timeout: float = 5.0) -> bool:
    """Test-only synchronization; neither routing nor shutdown waits for shadow work."""
    if not _slot.acquire(timeout=timeout):
        return False
    _slot.release()
    return True
