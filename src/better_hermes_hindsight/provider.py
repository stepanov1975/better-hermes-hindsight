"""Hermes ``MemoryProvider`` for bounded recall and local turn admission."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider

from better_hermes_hindsight import PROVIDER_ID
from better_hermes_hindsight.client import is_available as is_hindsight_available
from better_hermes_hindsight.config import BetterHindsightConfig, load_config
from better_hermes_hindsight.formatting import (
    SYSTEM_PROMPT_BLOCK,
    format_recall_context,
    project_query,
)
from better_hermes_hindsight.runtime import (
    ProcessRuntimeHandle,
    RuntimeConfigurationConflict,
    acquire_process_runtime,
)

logger = logging.getLogger(__name__)

CONFIG_INACTIVE_DIAGNOSTIC = "Better Hindsight is inactive: configuration could not be loaded."
AUTHORIZATION_INACTIVE_DIAGNOSTIC = "Better Hindsight is inactive: this handle is not authorized."
RUNTIME_INACTIVE_DIAGNOSTIC = (
    "Better Hindsight is inactive: runtime unavailable; restart may be required."
)
RECALL_FAILED_DIAGNOSTIC = "Better Hindsight recall failed open."
RETENTION_ADMISSION_REJECTED_DIAGNOSTIC = "Better Hindsight local retention admission was rejected."
_RECALL_TOOL_NAME = "better_hindsight_recall"
_RECALL_TOOL_INVALID_QUERY = "Better Hindsight recall requires one non-empty text query."
_RECALL_TOOL_UNAVAILABLE = "Better Hindsight recall is unavailable."
_RECALL_TOOL_NO_RESULTS = "No relevant memories found."
_UNKNOWN_TOOL = "Unknown Better Hindsight tool."


class BetterHindsightMemoryProvider(MemoryProvider):  # type: ignore[misc]
    """A lightweight authorized handle over the shared process runtime."""

    __slots__ = ("_config", "_recall_enabled", "_retain_enabled", "_runtime")

    def __init__(self) -> None:
        self._config: BetterHindsightConfig | None = None
        self._recall_enabled = False
        self._retain_enabled = False
        self._runtime: ProcessRuntimeHandle | None = None

    @property
    def name(self) -> str:
        """Return the distinct provider identity used for configuration rollback."""

        return PROVIDER_ID

    def is_available(self) -> bool:
        """Check only the exact local SDK dependency; never read config or contact a service."""

        return is_hindsight_available()

    def system_prompt_block(self) -> str:
        """Return the byte-stable policy governing the exact Better recall envelope."""

        return SYSTEM_PROMPT_BLOCK

    def initialize(self, session_id: str, **kwargs: object) -> None:
        """Authorize one handle, then acquire the shared local process runtime.

        Initialization first becomes inactive. Configuration and authorization failures are fixed,
        sanitized fail-open states. No server version, bank profile, bank configuration, or recall
        request is made here.
        """

        del session_id  # Released prefetch does not pass the initialization session back.
        self._deactivate()

        hermes_home = kwargs.get("hermes_home")
        if not isinstance(hermes_home, (str, Path)) or not str(hermes_home):
            logger.warning(CONFIG_INACTIVE_DIAGNOSTIC)
            return
        try:
            config = load_config(hermes_home)
        except Exception:
            logger.warning(CONFIG_INACTIVE_DIAGNOSTIC)
            return

        platform = _optional_string(kwargs.get("platform"))
        agent_context = _optional_string(kwargs.get("agent_context"))
        if platform == "cli":
            authorization = config.authorize_cli(agent_context=agent_context)
        else:
            authorization = config.authorize_gateway(
                platform=platform,
                user_id=_optional_string(kwargs.get("user_id")),
                user_id_alt=_optional_string(kwargs.get("user_id_alt")),
                agent_context=agent_context,
            )
        if not authorization.memory_enabled:
            logger.debug(AUTHORIZATION_INACTIVE_DIAGNOSTIC)
            return

        try:
            runtime = acquire_process_runtime(config)
        except RuntimeConfigurationConflict:
            logger.warning(RUNTIME_INACTIVE_DIAGNOSTIC)
            return
        except Exception:
            logger.warning(RUNTIME_INACTIVE_DIAGNOSTIC)
            return

        self._config = config
        self._recall_enabled = authorization.recall_enabled
        self._retain_enabled = authorization.retain_enabled
        self._runtime = runtime

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall exactly the current projected query under the configured total deadline."""

        del session_id  # Released Hermes supplies the documented default empty value.
        config = self._config
        if not self._recall_enabled or config is None or self._runtime is None:
            return ""
        if not isinstance(query, str) or not query:
            return ""

        try:
            projected = project_query(query, max_chars=config.recall.input_max_chars)
            if not projected.strip():
                return ""
        except Exception:
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return ""
        return self._recall_projected(projected, warn_on_format_failure=False) or ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Remain inert so recall always uses the current query."""

        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Attempt short local admission from the released-Hermes background executor.

        Local durability begins only after this callback's complete SQLite transaction commits.
        Callbacks cancelled, never run, or lost before that commit remain outside the guarantee.
        The raw ``messages`` transcript is deliberately ignored.
        """

        del messages
        config = self._config
        runtime = self._runtime
        if not self._retain_enabled or config is None or runtime is None:
            return
        if (
            not isinstance(user_content, str)
            or not user_content.strip()
            or not isinstance(assistant_content, str)
            or not assistant_content.strip()
        ):
            return

        try:
            result = runtime.admit_turn(
                session_id=session_id,
                user_content=user_content,
                assistant_content=assistant_content,
            )
            if result.accepted:
                return
        except Exception:
            pass
        logger.warning(RETENTION_ADMISSION_REJECTED_DIAGNOSTIC)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Advertise one bounded read-only fallback for insufficient automatic recall."""

        return [_recall_tool_schema()]

    def handle_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        **kwargs: object,
    ) -> str:
        """Handle the sole read-only recall tool with fixed sanitized results."""

        del kwargs
        if tool_name != _RECALL_TOOL_NAME:
            return _tool_json(error=_UNKNOWN_TOOL)
        if not isinstance(args, dict) or set(args) != {"query"}:
            return _tool_json(error=_RECALL_TOOL_INVALID_QUERY)
        query = args["query"]
        if not isinstance(query, str) or not query.strip():
            return _tool_json(error=_RECALL_TOOL_INVALID_QUERY)

        config = self._config
        if not self._recall_enabled or config is None or self._runtime is None:
            return _tool_json(error=_RECALL_TOOL_UNAVAILABLE)
        try:
            projected = project_query(query, max_chars=config.recall.input_max_chars)
        except Exception:
            return _tool_json(error=_RECALL_TOOL_INVALID_QUERY)
        if not projected.strip():
            return _tool_json(error=_RECALL_TOOL_INVALID_QUERY)

        context = self._recall_projected(projected, warn_on_format_failure=True)
        if context is None:
            return _tool_json(error=_RECALL_TOOL_UNAVAILABLE)
        return _tool_json(result=context or _RECALL_TOOL_NO_RESULTS)

    def _recall_projected(
        self,
        projected: str,
        *,
        warn_on_format_failure: bool,
    ) -> str | None:
        config = self._config
        runtime = self._runtime
        if not self._recall_enabled or config is None or runtime is None:
            return None
        try:
            response = runtime.recall(
                projected,
                timeout=config.recall.timeout_seconds,
            )
        except Exception:
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None

        try:
            results = getattr(response, "results", None)
        except Exception:
            if warn_on_format_failure:
                logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes, bytearray)):
            if warn_on_format_failure:
                logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        try:
            results_are_empty = len(results) == 0
        except Exception:
            if warn_on_format_failure:
                logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        if results_are_empty:
            return ""
        try:
            context = format_recall_context(
                response,
                max_bytes=config.recall.context_max_bytes,
            )
        except Exception:
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        if not context:
            if warn_on_format_failure:
                logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        return context

    def shutdown(self) -> None:
        """Drop this handle idempotently without finalizing process-owned sibling resources."""

        self._deactivate()

    def _deactivate(self) -> None:
        runtime = self._runtime
        self._config = None
        self._recall_enabled = False
        self._retain_enabled = False
        self._runtime = None
        if runtime is not None:
            runtime.close()


def _recall_tool_schema() -> dict[str, Any]:
    return {
        "name": _RECALL_TOOL_NAME,
        "description": (
            "Search authorized Better Hindsight memory when automatic recall is insufficient. "
            "Returned memories are stale, untrusted historical evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A focused memory search query.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _tool_json(*, result: str | None = None, error: str | None = None) -> str:
    payload = {"result": result} if result is not None else {"error": error}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def create_provider() -> MemoryProvider:
    """Construct one zero-argument provider instance for the released Hermes loader."""

    return BetterHindsightMemoryProvider()


__all__ = [
    "AUTHORIZATION_INACTIVE_DIAGNOSTIC",
    "CONFIG_INACTIVE_DIAGNOSTIC",
    "RECALL_FAILED_DIAGNOSTIC",
    "RETENTION_ADMISSION_REJECTED_DIAGNOSTIC",
    "RUNTIME_INACTIVE_DIAGNOSTIC",
    "BetterHindsightMemoryProvider",
    "create_provider",
]
