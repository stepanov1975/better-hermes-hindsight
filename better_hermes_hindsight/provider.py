"""Hermes ``MemoryProvider`` for bounded recall and local turn admission."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from agent.memory_provider import MemoryProvider, RecallStatus

from . import PROVIDER_ID
from .client import HindsightClientError, recall_request_parameters
from .client import is_available as is_hindsight_available
from .config import BetterHindsightConfig, load_config
from .diagnostics import enqueue_recall_capture, initialize_recall_capture
from .formatting import (
    RECALL_TRUST_LABEL,
    SYSTEM_PROMPT_BLOCK,
    format_recall_context_with_records,
    project_query,
)
from .management import status
from .outbox import AdmissionStatus
from .runtime import (
    AsyncCallTimeoutError,
    ProcessRuntimeHandle,
    RuntimeConfigurationConflict,
    acquire_process_runtime,
)
from .telemetry import elapsed_milliseconds, emit_event

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_SECTION_LOCK = threading.Lock()
_system_prompt_section_registration: object | None = None

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

_RETAIN_TOOL_NAME = "better_hindsight_retain"
_RETAIN_TOOL_INVALID_CONTENT = (
    "Better Hindsight retention requires non-empty text content and an optional non-empty text "
    "context."
)
_RETAIN_TOOL_UNAVAILABLE = "Better Hindsight retention is unavailable for this handle."
_RETAIN_TOOL_REJECTED = "Better Hindsight retention was not admitted."
_RETAIN_TOOL_MAX_CONTENT_CHARS = 8192
_RETAIN_TOOL_MAX_CONTEXT_CHARS = 256
_RETAIN_TOOL_MAX_SEGMENTS = 2000
_RETAIN_TOOL_SESSION_ID = "better-hindsight-model-retain-v1"
_RETAIN_TOOL_SOURCE_MARKER = (
    "This is an agent-selected durable memory record, not a direct user quotation."
)
_STATUS_TOOL_NAME = "better_hindsight_status"
_STATUS_TOOL_INVALID_ARGUMENTS = "Better Hindsight status does not accept arguments."
_STATUS_TOOL_UNAVAILABLE = "Better Hindsight status is unavailable for this handle."

_UNKNOWN_TOOL = "Unknown Better Hindsight tool."


def _ensure_system_prompt_section(
    registrar: Callable[[], object | None],
) -> bool:
    """Register one live process-global trust policy section."""

    global _system_prompt_section_registration
    with _SYSTEM_PROMPT_SECTION_LOCK:
        registration = _system_prompt_section_registration
        if registration is not None and getattr(registration, "active", True) is not False:
            return True
        registration = registrar()
        if registration is None:
            return False
        _system_prompt_section_registration = registration
        return True


class BetterHindsightMemoryProvider(MemoryProvider):  # type: ignore[misc]
    """A lightweight authorized handle over the shared process runtime."""

    __slots__ = (
        "_config",
        "_legacy_system_prompt_block",
        "_last_recall_count",
        "_recall_enabled",
        "_retain_enabled",
        "_runtime",
        "_system_prompt_section_registrar",
    )

    def __init__(
        self,
        *,
        system_prompt_section_registrar: Callable[[], object | None] | None = None,
    ) -> None:
        self._config: BetterHindsightConfig | None = None
        self._legacy_system_prompt_block = True
        self._last_recall_count = 0
        self._recall_enabled = False
        self._retain_enabled = False
        self._runtime: ProcessRuntimeHandle | None = None
        self._system_prompt_section_registrar = system_prompt_section_registrar

    @property
    def name(self) -> str:
        """Return the distinct provider identity used for configuration rollback."""

        return PROVIDER_ID

    def is_available(self) -> bool:
        """Check only local runtime dependencies; never read config or contact a service."""

        return is_hindsight_available()

    def system_prompt_block(self) -> str:
        """Return the policy only when the host lacks plugin prompt sections."""

        return SYSTEM_PROMPT_BLOCK if self._legacy_system_prompt_block else ""

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

        try:
            initialize_recall_capture(config)
        except Exception:
            emit_event(
                logger,
                "better_hindsight.recall_diagnostic",
                outcome="write_failed",
            )

        self._config = config
        self._recall_enabled = authorization.recall_enabled
        self._retain_enabled = authorization.retain_enabled
        self._runtime = runtime
        registrar = self._system_prompt_section_registrar
        if self._recall_enabled and registrar is not None:
            self._system_prompt_section_registrar = None
            try:
                if _ensure_system_prompt_section(registrar):
                    self._legacy_system_prompt_block = False
            except Exception:
                logger.warning("Better Hindsight recall trust policy registration failed.")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall exactly the current projected query under the configured total deadline."""

        del session_id  # Released Hermes supplies the documented default empty value.
        self._last_recall_count = 0
        config = self._config
        if not self._recall_enabled or config is None or self._runtime is None:
            return ""
        if not isinstance(query, str) or not query:
            return ""

        started_at = time.monotonic()
        deadline = started_at + config.recall.timeout_seconds
        try:
            projected = project_query(
                query,
                max_chars=config.recall.input_max_chars,
                max_tokens=config.recall.input_max_tokens,
            )
            if not projected.strip():
                return ""
        except Exception:
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return ""
        if time.monotonic() >= deadline:
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return ""
        recalled = self._recall_projected(
            projected,
            deadline=deadline,
            started_at=started_at,
            warn_on_format_failure=False,
        )
        if recalled is None:
            return ""
        context, records = recalled
        self._last_recall_count = len(records)
        return context

    def recall_status(self) -> RecallStatus | None:
        """Describe only the memories injected by the latest automatic prefetch."""

        if self._last_recall_count <= 0:
            return None
        return RecallStatus(
            provider_label="Better Hindsight",
            count=self._last_recall_count,
            glyph="👁️",
        )

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
        """Attempt short local admission from the Hermes background executor.

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
                segment_count_limit=config.outbox.max_pending_rows,
            )
            emit_event(
                logger,
                "better_hindsight.admission",
                duplicate_count=result.duplicate_count,
                inserted_count=result.inserted_count,
                outcome=result.status.value,
            )
            if result.accepted:
                return
        except Exception:
            emit_event(
                logger,
                "better_hindsight.admission",
                duplicate_count=0,
                inserted_count=0,
                outcome="local_failure",
            )
        logger.warning(RETENTION_ADMISSION_REJECTED_DIAGNOSTIC)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Advertise bounded recall, durable retention, and passive status tools."""

        return [_recall_tool_schema(), _retain_tool_schema(), _status_tool_schema()]

    def handle_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        **kwargs: object,
    ) -> str:
        """Dispatch model tools through the authorized provider handle."""

        del kwargs
        if tool_name == _RECALL_TOOL_NAME:
            return self._handle_recall_tool(args)
        if tool_name == _RETAIN_TOOL_NAME:
            return self._handle_retain_tool(args)
        if tool_name == _STATUS_TOOL_NAME:
            return self._handle_status_tool(args)
        return _tool_json(error=_UNKNOWN_TOOL)

    def _handle_recall_tool(self, args: dict[str, Any]) -> str:
        if not isinstance(args, dict) or set(args) != {"query"}:
            return _tool_json(error=_RECALL_TOOL_INVALID_QUERY)
        query = args["query"]
        if not isinstance(query, str) or not query.strip():
            return _tool_json(error=_RECALL_TOOL_INVALID_QUERY)

        config = self._config
        if not self._recall_enabled or config is None or self._runtime is None:
            return _tool_json(error=_RECALL_TOOL_UNAVAILABLE)
        started_at = time.monotonic()
        deadline = started_at + config.recall.timeout_seconds
        try:
            projected = project_query(
                query,
                max_chars=config.recall.input_max_chars,
                max_tokens=config.recall.input_max_tokens,
            )
        except Exception:
            return _tool_json(error=_RECALL_TOOL_INVALID_QUERY)
        if not projected.strip():
            return _tool_json(error=_RECALL_TOOL_INVALID_QUERY)
        if time.monotonic() >= deadline:
            return _tool_json(error=_RECALL_TOOL_UNAVAILABLE)

        recalled = self._recall_projected(
            projected,
            deadline=deadline,
            started_at=started_at,
            warn_on_format_failure=True,
        )
        if recalled is None:
            return _tool_json(error=_RECALL_TOOL_UNAVAILABLE)
        _context, records = recalled
        return _tool_json(
            memories=records,
            result="ok" if records else "empty",
            trust=RECALL_TRUST_LABEL,
        )

    def _handle_retain_tool(self, args: dict[str, Any]) -> str:
        if (
            not isinstance(args, dict)
            or "content" not in args
            or not set(args)
            <= {
                "content",
                "context",
            }
        ):
            return _tool_json(error=_RETAIN_TOOL_INVALID_CONTENT)
        content = args["content"]
        context = args.get("context")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > _RETAIN_TOOL_MAX_CONTENT_CHARS
        ):
            return _tool_json(error=_RETAIN_TOOL_INVALID_CONTENT)
        if context is not None and (
            not isinstance(context, str)
            or not context.strip()
            or len(context) > _RETAIN_TOOL_MAX_CONTEXT_CHARS
        ):
            return _tool_json(error=_RETAIN_TOOL_INVALID_CONTENT)

        config = self._config
        runtime = self._runtime
        if not self._retain_enabled or config is None or runtime is None:
            return _tool_json(error=_RETAIN_TOOL_UNAVAILABLE)
        assistant_content = content
        if context is not None:
            assistant_content = f"Context: {context}\n\n{content}"
        try:
            admission = runtime.admit_turn(
                session_id=_RETAIN_TOOL_SESSION_ID,
                user_content=_RETAIN_TOOL_SOURCE_MARKER,
                assistant_content=assistant_content,
                segment_count_limit=_RETAIN_TOOL_MAX_SEGMENTS,
            )
            emit_event(
                logger,
                "better_hindsight.admission",
                duplicate_count=admission.duplicate_count,
                inserted_count=admission.inserted_count,
                outcome=admission.status.value,
                source="model_tool",
            )
        except Exception:
            emit_event(
                logger,
                "better_hindsight.admission",
                duplicate_count=0,
                inserted_count=0,
                outcome="local_failure",
                source="model_tool",
            )
            return _tool_json(error=_RETAIN_TOOL_REJECTED, reason="local_failure")
        if not admission.accepted:
            return _tool_json(error=_RETAIN_TOOL_REJECTED, reason=admission.status.value)
        result = (
            "already_queued" if admission.status is AdmissionStatus.DUPLICATE else "queued_locally"
        )
        return _tool_json(result=result)

    def _handle_status_tool(self, args: dict[str, Any]) -> str:
        if not isinstance(args, dict) or args:
            return _tool_json(error=_STATUS_TOOL_INVALID_ARGUMENTS)
        config = self._config
        if config is None or self._runtime is None:
            return _tool_json(error=_STATUS_TOOL_UNAVAILABLE)

        try:
            status_result = status(config)
        except Exception:
            return _tool_json(error=_STATUS_TOOL_UNAVAILABLE)
        payload = _compact_status_payload(status_result.payload)
        if payload is None:
            return _tool_json(error=_STATUS_TOOL_UNAVAILABLE)
        return _tool_json(**payload)

    def _recall_projected(
        self,
        projected: str,
        *,
        deadline: float,
        started_at: float,
        warn_on_format_failure: bool,
    ) -> tuple[str, list[dict[str, object]]] | None:
        config = self._config
        runtime = self._runtime
        if not self._recall_enabled or config is None or runtime is None:
            return None
        request = recall_request_parameters(config.recall)

        def record(outcome: str, **fields: object) -> None:
            elapsed_ms = elapsed_milliseconds(started_at, time.monotonic())
            event_fields = dict(fields)
            try:
                diagnostic_id = enqueue_recall_capture(
                    config,
                    query=projected,
                    request=request,
                    elapsed_ms=elapsed_ms,
                    outcome=outcome,
                    result_count=cast(int | None, fields.get("result_count")),
                    formatted_bytes=cast(int | None, fields.get("formatted_bytes")),
                    reason=cast(str | None, fields.get("reason")),
                )
            except Exception:
                emit_event(
                    logger,
                    "better_hindsight.recall_diagnostic",
                    outcome="write_failed",
                )
            else:
                if diagnostic_id is not None:
                    event_fields["diagnostic_id"] = diagnostic_id
            emit_event(
                logger,
                "better_hindsight.recall",
                elapsed_ms=elapsed_ms,
                outcome=outcome,
                **event_fields,
            )

        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise AsyncCallTimeoutError
            response = runtime.recall(
                projected,
                timeout=remaining,
            )
        except AsyncCallTimeoutError:
            record("timeout")
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        except HindsightClientError as error:
            record("client_error", reason=error.reason)
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        except Exception:
            record("client_error")
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None

        try:
            results = getattr(response, "results", None)
            valid_results = isinstance(results, Sequence) and not isinstance(
                results, (str, bytes, bytearray)
            )
            if not valid_results:
                raise TypeError
            result_count = len(cast(Sequence[object], results))
        except Exception:
            record("response_invalid")
            if warn_on_format_failure:
                logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        if result_count == 0:
            if time.monotonic() >= deadline:
                record("timeout")
                return None
            record("empty", formatted_bytes=0, result_count=0)
            return "", []
        try:
            context, records = format_recall_context_with_records(
                response,
                max_bytes=config.recall.context_max_bytes,
                deadline=deadline,
            )
        except Exception:
            record("format_error")
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        if time.monotonic() >= deadline:
            record("timeout")
            logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        if not context:
            record("format_error")
            if warn_on_format_failure:
                logger.warning(RECALL_FAILED_DIAGNOSTIC)
            return None
        record(
            "success",
            formatted_bytes=len(context.encode("utf-8")),
            result_count=result_count,
        )
        return context, records

    def shutdown(self) -> None:
        """Drop this handle idempotently without finalizing process-owned sibling resources."""

        self._deactivate()

    def _deactivate(self) -> None:
        runtime = self._runtime
        self._config = None
        self._last_recall_count = 0
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


def _retain_tool_schema() -> dict[str, Any]:
    return {
        "name": _RETAIN_TOOL_NAME,
        "description": (
            "Durably queue one agent-selected fact, preference, decision, or convention for "
            "long-term memory. Use only for self-contained information that should remain useful "
            "across future sessions; do not store secrets or transient task progress. Acceptance "
            "confirms local durable admission, not remote delivery."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _RETAIN_TOOL_MAX_CONTENT_CHARS,
                    "description": "The self-contained durable information to store.",
                },
                "context": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _RETAIN_TOOL_MAX_CONTEXT_CHARS,
                    "description": (
                        "Optional short category, such as 'user preference', 'environment fact', "
                        "or 'project convention'."
                    ),
                },
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    }


def _status_tool_schema() -> dict[str, Any]:
    return {
        "name": _STATUS_TOOL_NAME,
        "description": (
            "Inspect compact passive health for the durable Better Hindsight retention queue. "
            "Makes no remote call and exposes extra detail only when the queue is degraded."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _compact_status_payload(payload: object) -> dict[str, object] | None:
    """Project operator status into the smallest model-actionable queue snapshot."""

    if not isinstance(payload, dict):
        return None
    outcome = payload.get("result")
    outbox = payload.get("outbox")
    counts = payload.get("counts")
    if outcome not in {"ok", "degraded"} or not isinstance(outbox, str) or not outbox:
        return None
    if not isinstance(counts, dict):
        return None

    queue_counts: dict[str, int] = {}
    for name in ("mismatch", "pending", "retry", "sending"):
        value = counts.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        queue_counts[name] = value

    compact: dict[str, object] = {
        "queued": sum(queue_counts.values()),
        "result": outcome,
        "retention_queue": outbox,
    }
    if outcome == "ok":
        return compact

    count_names = {
        "mismatch": "mismatched",
        "pending": "pending",
        "retry": "retrying",
        "sending": "sending",
    }
    for source, target in count_names.items():
        if queue_counts[source] > 0:
            compact[target] = queue_counts[source]
    age_bucket = payload.get("age_bucket")
    if age_bucket in {"1h_to_lt_24h", "gte_24h"}:
        compact["age_bucket"] = age_bucket
    for name in ("next_retry_bucket", "last_error_category"):
        value = payload.get(name)
        if isinstance(value, str) and value and value != "none":
            compact[name] = value
    if payload.get("sender_ownership") == "unavailable":
        compact["sender_ownership"] = "unavailable"
    return compact


def _tool_json(**payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def create_provider(
    *,
    system_prompt_section_registrar: Callable[[], object | None] | None = None,
) -> MemoryProvider:
    """Construct one zero-argument provider instance for the released Hermes loader."""

    return BetterHindsightMemoryProvider(
        system_prompt_section_registrar=system_prompt_section_registrar,
    )


__all__ = [
    "AUTHORIZATION_INACTIVE_DIAGNOSTIC",
    "CONFIG_INACTIVE_DIAGNOSTIC",
    "RECALL_FAILED_DIAGNOSTIC",
    "RETENTION_ADMISSION_REJECTED_DIAGNOSTIC",
    "RUNTIME_INACTIVE_DIAGNOSTIC",
    "BetterHindsightMemoryProvider",
    "create_provider",
]
