"""Deterministic high-confidence redaction shared by recall and retention boundaries."""

from __future__ import annotations

import re

REDACTION_MARKER = "[REDACTED]"

_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9][A-Z0-9 -]* )?PRIVATE KEY)-----"
    r".*?"
    r"-----END (?P=label)-----",
    flags=re.DOTALL,
)
_AUTHORIZATION_HEADER_PATTERN = re.compile(
    r"(?P<prefix>\bauthorization[ \t]*:[ \t]*)[^\r\n]+",
    flags=re.IGNORECASE,
)
_BEARER_TOKEN_PATTERN = re.compile(
    r"(?P<prefix>\bbearer[ \t]+)[A-Za-z0-9._~+/=-]{8,}",
    flags=re.IGNORECASE,
)
_API_KEY_PATTERN = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9])api[-_ ]?key\b[\"']?[ \t]*(?:=|:)[ \t]*)"
    r"(?P<quote>[\"']?)"
    r"[^\s\"'`,;}\]]{4,}"
    r"(?P=quote)",
    flags=re.IGNORECASE,
)
_URL_USERINFO_PATTERN = re.compile(
    r"(?P<scheme>\bhttps?://)[^\s/@]+(?::[^\s/@]*)?@",
    flags=re.IGNORECASE,
)


def redact_sensitive_text(text: str) -> str:
    """Replace narrow, high-confidence credential forms with one fixed marker.

    This intentionally avoids broad token-like heuristics. Pattern matching reduces common
    accidental egress but is not a universal secret detector.
    """

    if not isinstance(text, str):
        raise TypeError("redaction input must be text")

    redacted = _PRIVATE_KEY_PATTERN.sub(REDACTION_MARKER, text)
    redacted = _AUTHORIZATION_HEADER_PATTERN.sub(
        lambda match: match.group("prefix") + REDACTION_MARKER,
        redacted,
    )
    redacted = _BEARER_TOKEN_PATTERN.sub(
        lambda match: match.group("prefix") + REDACTION_MARKER,
        redacted,
    )
    redacted = _API_KEY_PATTERN.sub(
        lambda match: (
            match.group("prefix") + match.group("quote") + REDACTION_MARKER + match.group("quote")
        ),
        redacted,
    )
    return _URL_USERINFO_PATTERN.sub(
        lambda match: match.group("scheme") + REDACTION_MARKER + "@",
        redacted,
    )


__all__ = ["REDACTION_MARKER", "redact_sensitive_text"]
