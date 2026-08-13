"""Strict standard-library E2E canary for an isolated Hindsight 0.8.5 bank."""

from __future__ import annotations

import json
import math
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Final, cast

_EXPECTED_VERSION: Final = "0.8.5"
_MAX_BODY_BYTES: Final = 64 * 1024
_MAX_OUTPUT_VALUE: Final = 2_147_483_647


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass(frozen=True, slots=True)
class CanaryConfig:
    """Explicit isolated destination and bounded canary timing."""

    api_url: str = field(repr=False)
    bank_id: str = field(repr=False)
    api_key: str | None = field(default=None, repr=False)
    expected_version: str = _EXPECTED_VERSION
    timeout_seconds: float = 15.0
    poll_interval_seconds: float = 0.5
    max_polls: int = 20
    cleanup_timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("invalid canary API URL")
        if not self.bank_id or self.expected_version != _EXPECTED_VERSION:
            raise ValueError("invalid canary destination")
        if (
            not math.isfinite(self.timeout_seconds)
            or not math.isfinite(self.poll_interval_seconds)
            or not math.isfinite(self.cleanup_timeout_seconds)
            or self.timeout_seconds <= 0
            or self.poll_interval_seconds < 0
            or self.max_polls <= 0
            or self.cleanup_timeout_seconds <= 0
            or self.cleanup_timeout_seconds >= self.timeout_seconds
        ):
            raise ValueError("invalid canary timing")


def _milliseconds(start: float, end: float) -> int:
    return max(0, min(_MAX_OUTPUT_VALUE, round((end - start) * 1_000)))


def _request_json(
    config: CanaryConfig,
    method: str,
    path: str,
    *,
    timeout: float,
    payload: object | None = None,
) -> tuple[int, object]:
    headers = {"accept": "application/json", "user-agent": "better-hindsight-canary/1"}
    if config.api_key:
        headers["authorization"] = f"Bearer {config.api_key}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(
        config.api_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with _OPENER.open(request, timeout=max(0.001, timeout)) as response:
            status = response.status
            body = response.read(_MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        body = error.read(_MAX_BODY_BYTES + 1)
    if len(body) > _MAX_BODY_BYTES:
        raise ValueError("response_too_large")
    try:
        return status, json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("response_invalid") from None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _bank_path(config: CanaryConfig) -> str:
    bank = urllib.parse.quote(config.bank_id, safe="")
    return f"/v1/default/banks/{bank}"


def _owned_recall_result(value: object, *, document_id: str, tag: str, marker: str) -> bool:
    if not isinstance(value, dict):
        return False
    text = value.get("text")
    result_document = value.get("document_id")
    tags = value.get("tags")
    return (
        type(text) is str
        and marker in text
        and result_document == document_id
        and isinstance(tags, list)
        and all(type(item) is str for item in tags)
        and set(cast(list[str], tags)) == {tag}
        and len(tags) == 1
    )


def _cleanup(config: CanaryConfig, *, document_id: str, deadline: float) -> tuple[bool, int]:
    started = time.monotonic()
    document = urllib.parse.quote(document_id, safe="")
    try:
        status, response = _request_json(
            config,
            "DELETE",
            f"{_bank_path(config)}/documents/{document}",
            timeout=min(
                config.cleanup_timeout_seconds,
                max(0.001, deadline - time.monotonic()),
            ),
        )
        valid = (
            status == 200
            and isinstance(response, dict)
            and response.get("success") is True
            and response.get("document_id") == document_id
            and type(response.get("memory_units_deleted")) is int
            and cast(int, response["memory_units_deleted"]) >= 0
        )
        return valid, _milliseconds(started, time.monotonic())
    except Exception:
        return False, _milliseconds(started, time.monotonic())


def run_canary(config: CanaryConfig) -> dict[str, object]:
    """Run one strict canary and return only fixed categories and bounded numeric metadata."""

    started = time.monotonic()
    deadline = started + config.timeout_seconds
    operation_deadline = deadline - config.cleanup_timeout_seconds
    document_id = f"better-hindsight-canary-{secrets.token_hex(16)}"
    marker = f"synthetic canary marker {secrets.token_hex(16)}"
    tag = f"better-hindsight-canary:{secrets.token_hex(16)}"
    result: dict[str, object] = {"result": "error", "error": "unexpected_failure"}
    retain_dispatched = False
    cleanup_ms = 0
    health_started = time.monotonic()
    try:
        status, health = _request_json(
            config, "GET", "/health", timeout=_remaining(operation_deadline)
        )
        if status != 200 or not isinstance(health, dict) or health.get("status") != "healthy":
            result = {"result": "error", "error": "health_invalid"}
            return result
        health_ms = _milliseconds(health_started, time.monotonic())

        status, version = _request_json(
            config, "GET", "/version", timeout=_remaining(operation_deadline)
        )
        if (
            status != 200
            or not isinstance(version, dict)
            or type(version.get("api_version")) is not str
            or version.get("api_version") != config.expected_version
        ):
            result = {"result": "error", "error": "version_invalid", "health_ms": health_ms}
            return result

        retain_started = time.monotonic()
        retain_dispatched = True
        status, retained = _request_json(
            config,
            "POST",
            f"{_bank_path(config)}/memories",
            timeout=_remaining(operation_deadline),
            payload={
                "items": [
                    {
                        "content": marker,
                        "document_id": document_id,
                        "tags": [tag],
                        "update_mode": "replace",
                    }
                ],
                "async": False,
            },
        )
        retain_ms = _milliseconds(retain_started, time.monotonic())
        if (
            status != 200
            or not isinstance(retained, dict)
            or retained.get("success") is not True
            or retained.get("bank_id") != config.bank_id
            or retained.get("items_count") != 1
            or retained.get("async") is not False
        ):
            result = {"result": "error", "error": "retain_unconfirmed", "health_ms": health_ms}
            return result

        recall_started = time.monotonic()
        poll_count = 0
        while poll_count < config.max_polls:
            poll_count += 1
            status, recalled = _request_json(
                config,
                "POST",
                f"{_bank_path(config)}/memories/recall",
                timeout=_remaining(operation_deadline),
                payload={"query": marker, "tags": [tag], "tags_match": "exact"},
            )
            if status != 200 or not isinstance(recalled, dict):
                result = {"result": "error", "error": "recall_failed", "poll_count": poll_count}
                return result
            values = recalled.get("results")
            if not isinstance(values, list):
                result = {"result": "error", "error": "recall_invalid", "poll_count": poll_count}
                return result
            if any(
                _owned_recall_result(item, document_id=document_id, tag=tag, marker=marker)
                for item in values
            ):
                result = {
                    "result": "ok",
                    "version": config.expected_version,
                    "health_ms": health_ms,
                    "retain_ms": retain_ms,
                    "recall_visible_ms": _milliseconds(recall_started, time.monotonic()),
                    "poll_count": poll_count,
                }
                return result
            if poll_count < config.max_polls:
                sleep_seconds = min(config.poll_interval_seconds, _remaining(operation_deadline))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
        result = {"result": "error", "error": "recall_timeout", "poll_count": poll_count}
        return result
    except TimeoutError:
        phase = "retain_unconfirmed" if retain_dispatched else "deadline_exceeded"
        result = {"result": "error", "error": phase}
        return result
    except Exception:
        phase = "retain_unconfirmed" if retain_dispatched else "request_failed"
        result = {"result": "error", "error": phase}
        return result
    finally:
        if retain_dispatched:
            cleanup_ok, cleanup_ms = _cleanup(config, document_id=document_id, deadline=deadline)
            result["cleanup_ms"] = cleanup_ms
            if not cleanup_ok:
                result["result"] = "error"
                result["error"] = "cleanup_failed"


def main() -> int:
    """Run only with explicit environment opt-in and print one compact JSON object."""

    if os.environ.get("BETTER_HINDSIGHT_CANARY_ENABLED") != "1":
        result: dict[str, object] = {"result": "error", "error": "not_enabled"}
    else:
        try:
            config = CanaryConfig(
                api_url=os.environ["BETTER_HINDSIGHT_CANARY_API_URL"],
                bank_id=os.environ["BETTER_HINDSIGHT_CANARY_BANK_ID"],
                api_key=os.environ.get("BETTER_HINDSIGHT_CANARY_API_KEY"),
                timeout_seconds=float(os.environ.get("BETTER_HINDSIGHT_CANARY_DEADLINE", "15")),
                poll_interval_seconds=float(
                    os.environ.get("BETTER_HINDSIGHT_CANARY_POLL_INTERVAL", "0.5")
                ),
                max_polls=int(os.environ.get("BETTER_HINDSIGHT_CANARY_MAX_POLLS", "20")),
            )
            result = run_canary(config)
        except Exception:
            result = {"result": "error", "error": "configuration_invalid"}
    rendered = json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    print(rendered[:4096])
    return 0 if result.get("result") == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
