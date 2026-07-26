"""Pure, typed configuration for Better Hermes Hindsight.

Configuration is loaded only when :func:`load_config` is called.  Importing this module does not
read the environment or filesystem and does not import Hermes or Hindsight.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias, cast
from urllib.parse import urlsplit, urlunsplit

DEFAULT_API_URL = "http://localhost:8888"
DEFAULT_BANK_ID = "hermes"
PAYLOAD_SCHEMA_VERSION = "better-hindsight-turn-v1"
DEFAULT_RECALL_TIMEOUT_SECONDS = 3.5
DEFAULT_RECALL_INPUT_MAX_CHARS = 4096
DEFAULT_RECALL_CONTEXT_MAX_BYTES = 8192
DEFAULT_RETAIN_SEGMENT_MAX_BYTES = 65536
DEFAULT_OUTBOX_BUSY_TIMEOUT_SECONDS = 1.0

MAX_RECALL_TIMEOUT_SECONDS = 30.0
MAX_RECALL_INPUT_CHARS = 65536
MAX_RECALL_CONTEXT_BYTES = 1_048_576
MAX_RECALL_TOKENS = 1_048_576
MAX_RETAIN_SEGMENT_BYTES = 16_777_216
MAX_OUTBOX_BUSY_TIMEOUT_SECONDS = 5.0
MAX_TAG_COUNT = 64
MAX_TAG_CHARS = 256

IdentifierKind: TypeAlias = Literal["user_id", "user_id_alt"]
IntegrationMode: TypeAlias = Literal["hybrid", "context"]
QueryProjection: TypeAlias = Literal["head_tail"]
RecallBudget: TypeAlias = Literal["low", "mid", "high"]
RecallType: TypeAlias = Literal["world", "experience", "observation"]
RecallTagMode: TypeAlias = Literal["any", "all", "any_strict", "all_strict", "exact"]
MissionPolicy: TypeAlias = Literal["off", "check"]
ObservationScopes: TypeAlias = Literal["combined"] | tuple[tuple[str, ...], ...] | None

_ROOT_KEYS = {
    "api_url",
    "api_key",
    "bank_id",
    "single_principal",
    "allowed_principals",
    "integration_mode",
    "recall",
    "retain",
    "missions",
    "outbox",
}
_RECALL_KEYS = {
    "enabled",
    "query_projection",
    "timeout_seconds",
    "input_max_chars",
    "context_max_bytes",
    "budget",
    "max_tokens",
    "types",
    "tags",
    "tag_mode",
    "prefer_observations",
    "min_scores",
    "include_source_facts",
    "max_source_facts_tokens",
}
_RETAIN_KEYS = {
    "enabled",
    "segment_max_bytes",
    "observation_scopes",
    "tags",
}
_MISSION_KEYS = {"policy", "retain_mission", "observations_mission"}
_OUTBOX_KEYS = {"path", "busy_timeout_seconds"}
_PRINCIPAL_KEYS = {"platform", "identifier_kind", "identifier"}
_SCORE_KEYS = {"semantic", "keyword", "reranker", "final"}

_SECRET_KEY_SUFFIXES = {
    "apikey",
    "accesstoken",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "privatekey",
    "password",
    "passwd",
    "authorization",
    "credential",
    "credentials",
}
_SECRET_KEY_EXACT = {"token", "secret"}


class ConfigError(ValueError):
    """A sanitized, actionable Better Hindsight configuration error."""


def _error(message: str) -> ConfigError:
    return ConfigError(f"Better Hindsight configuration error: {message}")


@dataclass(frozen=True, slots=True)
class RecallMinScores:
    """Optional Hindsight 0.8.5 score floors; omitted stages impose no floor."""

    semantic: float | None = None
    keyword: float | None = None
    reranker: float | None = None
    final: float | None = None

    def as_dict(self) -> dict[str, float]:
        """Return only explicitly configured score floors."""
        values = {
            "semantic": self.semantic,
            "keyword": self.keyword,
            "reranker": self.reranker,
            "final": self.final,
        }
        return {name: value for name, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class AllowedPrincipal:
    """One exact gateway identity allowed to use the static single-principal bank."""

    platform: str
    identifier_kind: IdentifierKind
    identifier: str = field(repr=False)

    def as_tuple(self) -> tuple[str, IdentifierKind, str]:
        """Return the exact tuple used for gateway authorization."""
        return (self.platform, self.identifier_kind, self.identifier)


@dataclass(frozen=True, slots=True)
class RecallConfig:
    """Bounded local recall policy plus optional Hindsight 0.8.5 controls."""

    enabled: bool = True
    query_projection: QueryProjection = "head_tail"
    timeout_seconds: float = DEFAULT_RECALL_TIMEOUT_SECONDS
    input_max_chars: int = DEFAULT_RECALL_INPUT_MAX_CHARS
    context_max_bytes: int = DEFAULT_RECALL_CONTEXT_MAX_BYTES
    budget: RecallBudget | None = None
    max_tokens: int | None = None
    types: tuple[RecallType, ...] | None = None
    tags: tuple[str, ...] | None = None
    tag_mode: RecallTagMode | None = None
    prefer_observations: bool | None = None
    min_scores: RecallMinScores | None = None
    include_source_facts: bool | None = None
    max_source_facts_tokens: int | None = None

    @property
    def tags_match(self) -> RecallTagMode | None:
        """Return the Hindsight 0.8.5 request name for ``tag_mode``."""
        return self.tag_mode


@dataclass(frozen=True, slots=True)
class RetainConfig:
    """Typed retain inputs; this module performs no retention work."""

    enabled: bool = True
    segment_max_bytes: int = DEFAULT_RETAIN_SEGMENT_MAX_BYTES
    observation_scopes: ObservationScopes = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MissionConfig:
    """Distinct retain/observation mission text and background check policy."""

    policy: MissionPolicy = "check"
    retain_mission: str | None = field(default=None, repr=False)
    observations_mission: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class OutboxConfig:
    """Profile-scoped outbox identity and SQLite wait bound."""

    path: Path = field(repr=False)
    payload_schema: str = field(default=PAYLOAD_SCHEMA_VERSION, init=False)
    busy_timeout_seconds: float = DEFAULT_OUTBOX_BUSY_TIMEOUT_SECONDS

    @property
    def busy_timeout_ms(self) -> int:
        """Return the configured SQLite busy timeout in whole milliseconds."""
        return round(self.busy_timeout_seconds * 1000)


@dataclass(frozen=True, slots=True)
class MemoryAuthorization:
    """A pre-client authorization result for one Hermes provider handle."""

    identity_authorized: bool
    recall_enabled: bool
    retain_enabled: bool
    agent_context: str | None

    @property
    def memory_enabled(self) -> bool:
        """Whether this handle may perform any memory operation."""
        return self.recall_enabled or self.retain_enabled


@dataclass(frozen=True, slots=True)
class BetterHindsightConfig:
    """Complete immutable Better Hindsight configuration."""

    hermes_home: Path = field(repr=False)
    api_url: str = field(repr=False)
    api_key: str | None = field(default=None, repr=False)
    bank_id: str = field(default=DEFAULT_BANK_ID, repr=False)
    single_principal: bool = False
    allowed_principals: tuple[AllowedPrincipal, ...] = field(default=(), repr=False)
    integration_mode: IntegrationMode = "hybrid"
    recall: RecallConfig = field(default_factory=RecallConfig)
    retain: RetainConfig = field(default_factory=RetainConfig)
    missions: MissionConfig = field(default_factory=MissionConfig)
    outbox: OutboxConfig = field(
        default_factory=lambda: OutboxConfig(Path("better_hindsight/outbox.sqlite3"))
    )

    @property
    def destination_fingerprint(self) -> str:
        """Return the code-derived, credential-free destination identity."""
        return derive_destination_fingerprint(api_url=self.api_url, bank_id=self.bank_id)

    def authorize_gateway(
        self,
        *,
        platform: str | None,
        user_id: str | None = None,
        user_id_alt: str | None = None,
        agent_context: str | None = None,
    ) -> MemoryAuthorization:
        """Authorize only exact tuples built from real gateway initialization values."""
        identity_authorized = False
        if self.single_principal and platform:
            allowed = {principal.as_tuple() for principal in self.allowed_principals}
            candidates: list[tuple[str, IdentifierKind, str]] = []
            if user_id:
                candidates.append((platform, "user_id", user_id))
            if user_id_alt:
                candidates.append((platform, "user_id_alt", user_id_alt))
            identity_authorized = any(candidate in allowed for candidate in candidates)
        return self._authorization(identity_authorized, agent_context)

    def authorize_cli(self, *, agent_context: str | None = None) -> MemoryAuthorization:
        """Authorize CLI memory only under the explicit single-principal assertion."""
        return self._authorization(self.single_principal, agent_context)

    def _authorization(
        self, identity_authorized: bool, agent_context: str | None
    ) -> MemoryAuthorization:
        return MemoryAuthorization(
            identity_authorized=identity_authorized,
            recall_enabled=identity_authorized and self.recall.enabled,
            retain_enabled=(
                identity_authorized and self.retain.enabled and agent_context == "primary"
            ),
            agent_context=agent_context,
        )


def load_config(
    hermes_home: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    injected: Mapping[str, object] | None = None,
) -> BetterHindsightConfig:
    """Load and validate configuration with deterministic, explicit precedence.

    Precedence, highest first, is ``injected`` values, standard ``HINDSIGHT_*`` process
    variables, ``$HERMES_HOME/better_hindsight/config.json``, then documented defaults.  The
    explicit ``hermes_home`` argument is the only profile root used; no cwd or dotenv discovery
    occurs. ``api_key`` is accepted only through ``HINDSIGHT_API_KEY``.
    """
    home = _parse_hermes_home(hermes_home)
    profile_values = _load_profile_json(home)
    environment_values = _environment_values(os.environ if environ is None else environ)
    injected_mapping = _expect_mapping({} if injected is None else injected, "injected values")
    if "api_key" in injected_mapping:
        raise _error("api_key must come from HINDSIGHT_API_KEY")
    injected_values = dict(injected_mapping)

    merged = _merge_mappings(profile_values, environment_values)
    merged = _merge_mappings(merged, injected_values)
    _check_unknown_keys(merged, _ROOT_KEYS, "")

    api_url = normalize_api_url(merged.get("api_url", DEFAULT_API_URL))
    api_key = _parse_api_key(merged.get("api_key"))
    bank_id = _parse_bank_id(merged.get("bank_id", DEFAULT_BANK_ID))
    single_principal = _parse_bool(merged.get("single_principal", False), "single_principal")
    integration_mode = cast(
        IntegrationMode,
        _parse_literal(
            merged.get("integration_mode", "hybrid"),
            "integration_mode",
            ("hybrid", "context"),
        ),
    )
    principals = _parse_principals(merged.get("allowed_principals", ()))
    recall = _parse_recall(merged.get("recall", {}))
    retain = _parse_retain(merged.get("retain", {}))
    missions = _parse_missions(merged.get("missions", {}))
    outbox = _parse_outbox(home, merged.get("outbox", {}))

    if retain.observation_scopes == ((),) and not single_principal:
        raise _error("retain.observation_scopes='shared' requires explicit single_principal=true")

    return BetterHindsightConfig(
        hermes_home=home,
        api_url=api_url,
        api_key=api_key,
        bank_id=bank_id,
        single_principal=single_principal,
        allowed_principals=principals,
        integration_mode=integration_mode,
        recall=recall,
        retain=retain,
        missions=missions,
        outbox=outbox,
    )


def normalize_api_url(value: object) -> str:
    """Validate and canonicalize an HTTP(S) API URL without exposing its value."""
    if not isinstance(value, str) or not value.strip():
        raise _error("api_url must be a non-empty HTTP or HTTPS URL")
    candidate = value.strip()
    if (
        _contains_control(candidate)
        or "\\" in candidate
        or any(character.isspace() for character in candidate)
    ):
        raise _error("api_url contains unsupported characters")
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError:
        raise _error("api_url is malformed") from None

    if scheme not in {"http", "https"}:
        raise _error("api_url must use http or https")
    if not hostname or not parsed.netloc:
        raise _error("api_url must include a host")
    if username is not None or password is not None:
        raise _error("api_url must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise _error("api_url must not contain a query string or fragment")

    host = _normalize_hostname(hostname)
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    path = parsed.path.rstrip("/")
    if path == "/":
        path = ""
    return urlunsplit((scheme, netloc, path, "", ""))


def derive_destination_fingerprint(*, api_url: object, bank_id: object) -> str:
    """Hash only normalized non-secret destination identity."""
    normalized_url = normalize_api_url(api_url)
    normalized_bank = _parse_bank_id(bank_id)
    payload = json.dumps(
        {
            "api_url": normalized_url,
            "bank_id": normalized_bank,
            "payload_schema": PAYLOAD_SCHEMA_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_hermes_home(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _error("hermes_home must be an explicit absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise _error("hermes_home must be absolute; cwd discovery is disabled")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise _error("hermes_home must be a valid absolute path") from None


def _load_profile_json(home: Path) -> dict[str, object]:
    path = home / "better_hindsight" / "config.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise _error("profile config.json could not be read as UTF-8") from None

    try:
        loaded = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except _DuplicateJsonKey:
        raise _error("profile config.json contains a duplicate JSON key") from None
    except json.JSONDecodeError as exc:
        raise _error(
            f"profile config.json is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from None
    except ValueError:
        raise _error("profile config.json is not valid JSON") from None
    mapping = _expect_mapping(loaded, "profile config.json")
    _reject_secret_json_keys(mapping)
    return dict(mapping)


def _environment_values(environ: Mapping[str, str]) -> dict[str, object]:
    keys = {
        "HINDSIGHT_API_URL": "api_url",
        "HINDSIGHT_API_KEY": "api_key",
        "HINDSIGHT_BANK_ID": "bank_id",
    }
    return {target: environ[source] for source, target in keys.items() if source in environ}


def _merge_mappings(lower: Mapping[str, object], higher: Mapping[str, object]) -> dict[str, object]:
    merged = dict(lower)
    for key, higher_value in higher.items():
        lower_value = merged.get(key)
        if isinstance(lower_value, Mapping) and isinstance(higher_value, Mapping):
            merged[key] = _merge_mappings(lower_value, higher_value)
        else:
            merged[key] = higher_value
    return merged


def _reject_secret_json_keys(root: Mapping[str, object]) -> None:
    stack: list[tuple[object, str]] = [(root, "")]
    while stack:
        value, path = stack.pop()
        if isinstance(value, Mapping):
            for key, nested in value.items():
                key_text = str(key)
                nested_path = f"{path}.{key_text}" if path else key_text
                if _is_secret_key(key_text):
                    raise _error(
                        f"profile JSON contains secret-bearing key {nested_path}; "
                        "use HINDSIGHT_API_KEY instead"
                    )
                stack.append((nested, nested_path))
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                stack.append((nested, f"{path}[{index}]"))


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _is_secret_key(key: str) -> bool:
    words = set(re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).casefold().replace("-", "_").split("_"))
    if words & {
        "authorization",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "token",
    }:
        return True
    compact = re.sub(r"[^a-z0-9]", "", key.casefold())
    return compact in _SECRET_KEY_EXACT or any(
        compact.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES
    )


def _normalize_hostname(hostname: str) -> str:
    candidate = hostname.removesuffix(".")
    if not candidate:
        raise _error("api_url must include a valid host")
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        pass
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise _error("api_url must include a valid host") from None
    labels = ascii_hostname.split(".")
    if len(ascii_hostname) > 253 or any(
        not label
        or len(label) > 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
        for label in labels
    ):
        raise _error("api_url must include a valid host")
    return ascii_hostname


def _expect_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(f"{field_name} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise _error(f"{field_name} keys must be strings")
    return value


def _check_unknown_keys(values: Mapping[str, object], allowed: set[str], location: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        path = f"{location}." if location else ""
        rendered = ", ".join(f"{path}{key}" for key in unknown)
        raise _error(f"unknown key(s): {rendered}")


def _parse_api_key(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _error("api_key must be a string when injected or supplied by the environment")
    return value


def _parse_bank_id(value: object) -> str:
    return _parse_exact_nonempty_string(value, "bank_id")


def _parse_exact_nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise _error(f"{field_name} must be a non-empty string without outer whitespace")
    if _contains_control(value):
        raise _error(f"{field_name} must not contain control characters")
    return value


def _parse_optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error(f"{field_name} must be null or non-empty text")
    if _contains_control(value, allow_newline=True):
        raise _error(f"{field_name} contains unsupported control characters")
    return value


def _contains_control(value: str, *, allow_newline: bool = False) -> bool:
    allowed = {"\n", "\r", "\t"} if allow_newline else set()
    return any(
        (ord(character) < 32 or ord(character) == 127) and character not in allowed
        for character in value
    )


def _parse_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise _error(f"{field_name} must be true or false")
    return value


def _parse_literal(value: object, field_name: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(allowed)
        raise _error(f"{field_name} must be one of: {choices}")
    return value


def _parse_positive_int(value: object, field_name: str, *, maximum: int = MAX_RECALL_TOKENS) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise _error(f"{field_name} must be an integer from 1 through {maximum}")
    return value


def _parse_bounded_float(
    value: object, field_name: str, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"{field_name} must be a number greater than {minimum} and at most {maximum}")
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        raise _error(
            f"{field_name} must be a number greater than {minimum} and at most {maximum}"
        ) from None
    if not math.isfinite(parsed) or parsed <= minimum or parsed > maximum:
        raise _error(f"{field_name} must be a number greater than {minimum} and at most {maximum}")
    return parsed


def _parse_optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _parse_positive_int(value, field_name)


def _parse_recall(value: object) -> RecallConfig:
    values = _expect_mapping(value, "recall")
    _check_unknown_keys(values, _RECALL_KEYS, "recall")

    query_projection = cast(
        QueryProjection,
        _parse_literal(
            values.get("query_projection", "head_tail"),
            "recall.query_projection",
            ("head_tail",),
        ),
    )

    budget_value = values.get("budget")
    budget = cast(
        RecallBudget | None,
        None
        if budget_value is None
        else _parse_literal(budget_value, "recall.budget", ("low", "mid", "high")),
    )
    tag_mode_value = values.get("tag_mode")
    tag_mode = cast(
        RecallTagMode | None,
        None
        if tag_mode_value is None
        else _parse_literal(
            tag_mode_value,
            "recall.tag_mode",
            ("any", "all", "any_strict", "all_strict", "exact"),
        ),
    )
    prefer_value = values.get("prefer_observations")
    prefer_observations = (
        None if prefer_value is None else _parse_bool(prefer_value, "recall.prefer_observations")
    )
    source_value = values.get("include_source_facts")
    include_source_facts = (
        None if source_value is None else _parse_bool(source_value, "recall.include_source_facts")
    )

    return RecallConfig(
        enabled=_parse_bool(values.get("enabled", True), "recall.enabled"),
        query_projection=query_projection,
        timeout_seconds=_parse_bounded_float(
            values.get("timeout_seconds", DEFAULT_RECALL_TIMEOUT_SECONDS),
            "recall.timeout_seconds",
            minimum=0.0,
            maximum=MAX_RECALL_TIMEOUT_SECONDS,
        ),
        input_max_chars=_parse_positive_int(
            values.get("input_max_chars", DEFAULT_RECALL_INPUT_MAX_CHARS),
            "recall.input_max_chars",
            maximum=MAX_RECALL_INPUT_CHARS,
        ),
        context_max_bytes=_parse_positive_int(
            values.get("context_max_bytes", DEFAULT_RECALL_CONTEXT_MAX_BYTES),
            "recall.context_max_bytes",
            maximum=MAX_RECALL_CONTEXT_BYTES,
        ),
        budget=budget,
        max_tokens=_parse_optional_positive_int(values.get("max_tokens"), "recall.max_tokens"),
        types=_parse_recall_types(values.get("types")),
        tags=_parse_optional_tags(values.get("tags"), "recall.tags"),
        tag_mode=tag_mode,
        prefer_observations=prefer_observations,
        min_scores=_parse_min_scores(values.get("min_scores")),
        include_source_facts=include_source_facts,
        max_source_facts_tokens=_parse_optional_positive_int(
            values.get("max_source_facts_tokens"),
            "recall.max_source_facts_tokens",
        ),
    )


def _parse_recall_types(value: object) -> tuple[RecallType, ...] | None:
    if value is None:
        return None
    items = _parse_string_sequence(value, "recall.types", allow_empty=False)
    allowed = {"world", "experience", "observation"}
    if any(item not in allowed for item in items):
        raise _error("recall.types entries must be world, experience, or observation")
    if len(set(items)) != len(items):
        raise _error("recall.types must not contain duplicates")
    return cast(tuple[RecallType, ...], tuple(items))


def _parse_optional_tags(value: object, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _parse_tags(value, field_name)


def _parse_tags(value: object, field_name: str) -> tuple[str, ...]:
    items = _parse_string_sequence(value, field_name, allow_empty=True)
    if len(items) > MAX_TAG_COUNT:
        raise _error(f"{field_name} must contain at most {MAX_TAG_COUNT} tags")
    for item in items:
        _parse_exact_nonempty_string(item, f"{field_name} entry")
        if len(item) > MAX_TAG_CHARS:
            raise _error(f"{field_name} entries must contain at most {MAX_TAG_CHARS} characters")
    if len(set(items)) != len(items):
        raise _error(f"{field_name} must not contain duplicates")
    return tuple(items)


def _parse_string_sequence(value: object, field_name: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error(f"{field_name} must be a list of strings")
    if not allow_empty and not value:
        raise _error(f"{field_name} must not be empty")
    if not all(isinstance(item, str) for item in value):
        raise _error(f"{field_name} must contain only strings")
    return list(value)


def _parse_min_scores(value: object) -> RecallMinScores | None:
    if value is None:
        return None
    values = _expect_mapping(value, "recall.min_scores")
    _check_unknown_keys(values, _SCORE_KEYS, "recall.min_scores")

    def score(name: str) -> float | None:
        raw = values.get(name)
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise _error(f"recall.min_scores.{name} must be a finite non-negative number")
        try:
            parsed = float(raw)
        except (OverflowError, ValueError):
            raise _error(f"recall.min_scores.{name} must be a finite non-negative number") from None
        if not math.isfinite(parsed) or parsed < 0.0:
            raise _error(f"recall.min_scores.{name} must be a finite non-negative number")
        return parsed

    return RecallMinScores(
        semantic=score("semantic"),
        keyword=score("keyword"),
        reranker=score("reranker"),
        final=score("final"),
    )


def _parse_retain(value: object) -> RetainConfig:
    values = _expect_mapping(value, "retain")
    _check_unknown_keys(values, _RETAIN_KEYS, "retain")
    return RetainConfig(
        enabled=_parse_bool(values.get("enabled", True), "retain.enabled"),
        segment_max_bytes=_parse_positive_int(
            values.get("segment_max_bytes", DEFAULT_RETAIN_SEGMENT_MAX_BYTES),
            "retain.segment_max_bytes",
            maximum=MAX_RETAIN_SEGMENT_BYTES,
        ),
        observation_scopes=_parse_observation_scopes(values.get("observation_scopes")),
        tags=_parse_tags(values.get("tags", ()), "retain.tags"),
    )


def _parse_observation_scopes(value: object) -> ObservationScopes:
    if value is None:
        return None
    if isinstance(value, str):
        if value == "combined":
            return "combined"
        if value == "shared":
            return ((),)
        raise _error("retain.observation_scopes must be null, combined, shared, or [[]]")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not value:
            raise _error("retain.observation_scopes=[] is ambiguous; write combined explicitly")
        if len(value) == 1:
            inner = value[0]
            if (
                isinstance(inner, Sequence)
                and not isinstance(inner, (str, bytes, bytearray))
                and not inner
            ):
                return ((),)
    raise _error("retain.observation_scopes must be null, combined, shared, or [[]]")


def _parse_missions(value: object) -> MissionConfig:
    values = _expect_mapping(value, "missions")
    _check_unknown_keys(values, _MISSION_KEYS, "missions")
    policy = cast(
        MissionPolicy,
        _parse_literal(
            values.get("policy", "check"),
            "missions.policy",
            ("off", "check"),
        ),
    )
    return MissionConfig(
        policy=policy,
        retain_mission=_parse_optional_text(
            values.get("retain_mission"), "missions.retain_mission"
        ),
        observations_mission=_parse_optional_text(
            values.get("observations_mission"), "missions.observations_mission"
        ),
    )


def _parse_outbox(home: Path, value: object) -> OutboxConfig:
    values = _expect_mapping(value, "outbox")
    _check_unknown_keys(values, _OUTBOX_KEYS, "outbox")
    path = _parse_outbox_path(home, values.get("path", "better_hindsight/outbox.sqlite3"))
    return OutboxConfig(
        path=path,
        busy_timeout_seconds=_parse_bounded_float(
            values.get("busy_timeout_seconds", DEFAULT_OUTBOX_BUSY_TIMEOUT_SECONDS),
            "outbox.busy_timeout_seconds",
            minimum=0.0,
            maximum=MAX_OUTBOX_BUSY_TIMEOUT_SECONDS,
        ),
    )


def _parse_outbox_path(home: Path, value: object) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _error("outbox.path must be a non-empty profile-local path")
    configured = Path(value)
    candidate = configured if configured.is_absolute() else home / configured
    try:
        normalized = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise _error("outbox.path must resolve inside hermes_home") from None
    try:
        normalized.relative_to(home)
    except ValueError:
        raise _error("outbox.path must remain inside hermes_home") from None
    return normalized


def _parse_principals(value: object) -> tuple[AllowedPrincipal, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error("allowed_principals must be a list")
    principals: list[AllowedPrincipal] = []
    seen: set[tuple[str, IdentifierKind, str]] = set()
    for index, item in enumerate(value):
        location = f"allowed_principals[{index}]"
        values = _expect_mapping(item, location)
        _check_unknown_keys(values, _PRINCIPAL_KEYS, location)
        platform = _parse_exact_nonempty_string(values.get("platform"), f"{location}.platform")
        kind = cast(
            IdentifierKind,
            _parse_literal(
                values.get("identifier_kind"),
                f"{location}.identifier_kind",
                ("user_id", "user_id_alt"),
            ),
        )
        identifier = _parse_exact_nonempty_string(
            values.get("identifier"), f"{location}.identifier"
        )
        principal = AllowedPrincipal(
            platform=platform,
            identifier_kind=kind,
            identifier=identifier,
        )
        key = principal.as_tuple()
        if key in seen:
            raise _error(f"{location} duplicates an earlier allowed principal")
        seen.add(key)
        principals.append(principal)
    return tuple(principals)


__all__ = [
    "AllowedPrincipal",
    "BetterHindsightConfig",
    "ConfigError",
    "IdentifierKind",
    "IntegrationMode",
    "DEFAULT_API_URL",
    "DEFAULT_BANK_ID",
    "DEFAULT_OUTBOX_BUSY_TIMEOUT_SECONDS",
    "PAYLOAD_SCHEMA_VERSION",
    "DEFAULT_RECALL_CONTEXT_MAX_BYTES",
    "DEFAULT_RECALL_INPUT_MAX_CHARS",
    "DEFAULT_RECALL_TIMEOUT_SECONDS",
    "DEFAULT_RETAIN_SEGMENT_MAX_BYTES",
    "MemoryAuthorization",
    "MissionConfig",
    "MissionPolicy",
    "ObservationScopes",
    "OutboxConfig",
    "QueryProjection",
    "RecallBudget",
    "RecallConfig",
    "RecallMinScores",
    "RecallTagMode",
    "RecallType",
    "RetainConfig",
    "derive_destination_fingerprint",
    "load_config",
    "normalize_api_url",
]
