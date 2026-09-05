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

from .redaction import redact_sensitive_text

DEFAULT_API_URL = "http://localhost:8888"
DEFAULT_BANK_ID = "hermes"
LEGACY_PAYLOAD_SCHEMA_VERSION = "better-hindsight-turn-v1"
PAYLOAD_SCHEMA_VERSION = "better-hindsight-turn-v2"
RETAINED_EVENT_RECORD_SCHEMA = "better-hindsight-retained-event-v2"
RETAINED_MODEL_RECORD_SCHEMA = "better-hindsight-retained-memory-v1"
DEFAULT_RECALL_TIMEOUT_SECONDS = 3.5
DEFAULT_RECALL_INPUT_MAX_CHARS = 4096
DEFAULT_RECALL_INPUT_MAX_TOKENS = 500
DEFAULT_RECALL_CONTEXT_MAX_BYTES = 8192
DEFAULT_PLANNER_TIMEOUT_SECONDS = 2.0
DEFAULT_PLANNER_HISTORY_MAX_EXCHANGES = 4
DEFAULT_PLANNER_HISTORY_MAX_CHARS = 6_000
DEFAULT_PLANNER_QUERY_MAX_CHARS = 1_024
_LEGACY_DEFAULT_PLANNER_MAILBOX_PATH = "better_hindsight/recall_plans.sqlite3"
_LEGACY_MAX_PLANNER_MAILBOX_TTL_SECONDS = 60.0
_LEGACY_MAX_PLANNER_BUSY_TIMEOUT_SECONDS = 1.0
PLANNER_AND_RECALL_BUDGET_SECONDS = 7.5
DEFAULT_REFLECT_TIMEOUT_SECONDS = 60.0
DEFAULT_REFLECT_INPUT_MAX_CHARS = 4096
DEFAULT_REFLECT_INPUT_MAX_TOKENS = 500
DEFAULT_REFLECT_OUTPUT_MAX_BYTES = 16_384
DEFAULT_REFLECT_BUDGET = "low"
DEFAULT_REFLECT_MAX_TOKENS = 1024
DEFAULT_RETAIN_TIMEOUT_SECONDS = 60.0
DEFAULT_RETAIN_SEGMENT_MAX_BYTES = 65536
DEFAULT_OUTBOX_MAX_PENDING_ROWS = 2_000
DEFAULT_OUTBOX_MAX_PENDING_BYTES = 134_217_728
DEFAULT_OUTBOX_BUSY_TIMEOUT_SECONDS = 1.0
DEFAULT_OUTBOX_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_OUTBOX_RETRY_INITIAL_SECONDS = 2.0
DEFAULT_OUTBOX_RETRY_MAX_SECONDS = 300.0
DEFAULT_DIAGNOSTIC_SLOW_THRESHOLD_SECONDS = 5.0
DEFAULT_DIAGNOSTIC_MAX_RECORDS = 50
DEFAULT_DIAGNOSTIC_REPLAY_TIMEOUT_SECONDS = 30.0
OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES = 1024

MAX_RECALL_TIMEOUT_SECONDS = 30.0
MAX_RECALL_INPUT_CHARS = 65536
MAX_RECALL_INPUT_TOKENS = 1_048_576
MAX_RECALL_CONTEXT_BYTES = 1_048_576
MAX_RECALL_TOKENS = 1_048_576
MAX_PLANNER_TIMEOUT_SECONDS = 4.0
MAX_PLANNER_HISTORY_EXCHANGES = 20
MAX_PLANNER_HISTORY_CHARS = 65_536
MAX_PLANNER_QUERY_CHARS = 8_192
MAX_REFLECT_TIMEOUT_SECONDS = 300.0
MAX_REFLECT_INPUT_CHARS = 65_536
MAX_REFLECT_INPUT_TOKENS = 1_048_576
MAX_REFLECT_OUTPUT_BYTES = 1_048_576
MAX_REFLECT_MAX_TOKENS = 16_384
MAX_RETAIN_TIMEOUT_SECONDS = 300.0
MAX_RETAIN_SEGMENT_BYTES = 16_777_216
MAX_OUTBOX_PENDING_ROWS = 100_000
MAX_OUTBOX_PENDING_BYTES = 1_073_741_824
MAX_OUTBOX_BUSY_TIMEOUT_SECONDS = 5.0
MIN_OUTBOX_POLL_INTERVAL_SECONDS = 0.1
MAX_OUTBOX_POLL_INTERVAL_SECONDS = 60.0
MAX_OUTBOX_RETRY_SECONDS = 3_600.0
MAX_DIAGNOSTIC_RECORDS = 500
MAX_DIAGNOSTIC_REPLAY_TIMEOUT_SECONDS = 300.0
MAX_TAG_COUNT = 64
MAX_TAG_CHARS = 256

IdentifierKind: TypeAlias = Literal["user_id", "user_id_alt"]
RecallBudget: TypeAlias = Literal["low", "mid", "high"]
RecallType: TypeAlias = Literal["world", "experience", "observation"]
RecallTagMode: TypeAlias = Literal["any", "all", "any_strict", "all_strict", "exact"]
PlannerMode: TypeAlias = Literal["off", "shadow", "active"]
ReflectBudget: TypeAlias = Literal["low", "mid", "high"]
ReflectTagMode: TypeAlias = Literal["any", "all", "any_strict", "all_strict", "exact"]
ObservationScopes: TypeAlias = Literal["combined"] | tuple[tuple[str, ...], ...] | None


class _CanonicalRetainTags(tuple[str, ...]):
    """Marker type for the one validated, redacted, sorted retain-tag tuple."""


_ROOT_KEYS = {
    "api_url",
    "api_key",
    "bank_id",
    "single_principal",
    "allowed_principals",
    "recall",
    "planner",
    "reflect",
    "retain",
    "missions",
    "outbox",
    "diagnostics",
}
_RECALL_KEYS = {
    "enabled",
    "timeout_seconds",
    "input_max_chars",
    "input_max_tokens",
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
_PLANNER_KEYS = {
    "path",
    "mode",
    "timeout_seconds",
    "history_max_exchanges",
    "history_max_chars",
    "query_max_chars",
    "mailbox_ttl_seconds",
    "busy_timeout_seconds",
}
_REFLECT_KEYS = {
    "enabled",
    "timeout_seconds",
    "input_max_chars",
    "input_max_tokens",
    "output_max_bytes",
    "budget",
    "max_tokens",
    "tags",
    "tag_mode",
}
_RETAIN_KEYS = {
    "enabled",
    "timeout_seconds",
    "segment_max_bytes",
    "observation_scopes",
    "tags",
}
_MISSION_KEYS = {"retain_mission", "observations_mission"}
_OUTBOX_KEYS = {
    "path",
    "max_pending_rows",
    "max_pending_bytes",
    "busy_timeout_seconds",
    "poll_interval_seconds",
    "retry_initial_seconds",
    "retry_max_seconds",
}
_DIAGNOSTIC_KEYS = {
    "enabled",
    "path",
    "slow_threshold_seconds",
    "max_records",
    "replay_timeout_seconds",
}
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
    """Optional supported-Hindsight score floors; omitted stages impose no floor."""

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
    """Bounded local recall policy plus optional supported-Hindsight controls."""

    enabled: bool = True
    timeout_seconds: float = DEFAULT_RECALL_TIMEOUT_SECONDS
    input_max_chars: int = DEFAULT_RECALL_INPUT_MAX_CHARS
    input_max_tokens: int = DEFAULT_RECALL_INPUT_MAX_TOKENS
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


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Bounded context-aware recall planner policy."""

    mode: PlannerMode = "off"
    timeout_seconds: float = DEFAULT_PLANNER_TIMEOUT_SECONDS
    history_max_exchanges: int = DEFAULT_PLANNER_HISTORY_MAX_EXCHANGES
    history_max_chars: int = DEFAULT_PLANNER_HISTORY_MAX_CHARS
    query_max_chars: int = DEFAULT_PLANNER_QUERY_MAX_CHARS


@dataclass(frozen=True, slots=True)
class ReflectConfig:
    """Bounded, fixed policy for explicit server-side reflection."""

    enabled: bool = False
    timeout_seconds: float = DEFAULT_REFLECT_TIMEOUT_SECONDS
    input_max_chars: int = DEFAULT_REFLECT_INPUT_MAX_CHARS
    input_max_tokens: int = DEFAULT_REFLECT_INPUT_MAX_TOKENS
    output_max_bytes: int = DEFAULT_REFLECT_OUTPUT_MAX_BYTES
    budget: ReflectBudget = cast(ReflectBudget, DEFAULT_REFLECT_BUDGET)
    max_tokens: int = DEFAULT_REFLECT_MAX_TOKENS
    tags: tuple[str, ...] | None = None
    tag_mode: ReflectTagMode | None = None


@dataclass(frozen=True, slots=True)
class RetainConfig:
    """Typed retain inputs; this module performs no retention work."""

    enabled: bool = False
    timeout_seconds: float = DEFAULT_RETAIN_TIMEOUT_SECONDS
    segment_max_bytes: int = DEFAULT_RETAIN_SEGMENT_MAX_BYTES
    observation_scopes: ObservationScopes = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MissionConfig:
    """Configured desired mission text used only by explicit operator check/apply commands."""

    retain_mission: str | None = field(default=None, repr=False)
    observations_mission: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class OutboxConfig:
    """Profile-scoped outbox identity, logical caps, and bounded worker timing."""

    path: Path = field(repr=False)
    payload_schema: str = field(default=PAYLOAD_SCHEMA_VERSION, init=False)
    max_pending_rows: int = DEFAULT_OUTBOX_MAX_PENDING_ROWS
    max_pending_bytes: int = DEFAULT_OUTBOX_MAX_PENDING_BYTES
    busy_timeout_seconds: float = DEFAULT_OUTBOX_BUSY_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_OUTBOX_POLL_INTERVAL_SECONDS
    retry_initial_seconds: float = DEFAULT_OUTBOX_RETRY_INITIAL_SECONDS
    retry_max_seconds: float = DEFAULT_OUTBOX_RETRY_MAX_SECONDS

    @property
    def busy_timeout_ms(self) -> int:
        """Return the configured SQLite busy timeout in whole milliseconds."""
        return math.ceil(self.busy_timeout_seconds * 1000)


@dataclass(frozen=True, slots=True)
class DiagnosticConfig:
    """Opt-in profile-local capture policy for replayable slow recalls."""

    enabled: bool = False
    path: Path = field(repr=False, default=Path("better_hindsight/recall_diagnostics"))
    slow_threshold_seconds: float = DEFAULT_DIAGNOSTIC_SLOW_THRESHOLD_SECONDS
    max_records: int = DEFAULT_DIAGNOSTIC_MAX_RECORDS
    replay_timeout_seconds: float = DEFAULT_DIAGNOSTIC_REPLAY_TIMEOUT_SECONDS

    @property
    def slow_threshold_ms(self) -> int:
        """Return the configured slow threshold in whole milliseconds."""
        return math.ceil(self.slow_threshold_seconds * 1000)


@dataclass(frozen=True, slots=True)
class MemoryAuthorization:
    """A pre-client authorization result for one Hermes provider handle."""

    identity_authorized: bool
    recall_enabled: bool
    reflect_enabled: bool
    retain_enabled: bool

    @property
    def memory_enabled(self) -> bool:
        """Whether this handle may perform any memory operation."""
        return self.recall_enabled or self.reflect_enabled or self.retain_enabled


@dataclass(frozen=True, slots=True)
class BetterHindsightConfig:
    """Complete immutable Better Hindsight configuration."""

    hermes_home: Path = field(repr=False)
    api_url: str = field(repr=False)
    api_key: str | None = field(default=None, repr=False)
    bank_id: str = field(default=DEFAULT_BANK_ID, repr=False)
    single_principal: bool = False
    allowed_principals: tuple[AllowedPrincipal, ...] = field(default=(), repr=False)
    recall: RecallConfig = field(default_factory=RecallConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    reflect: ReflectConfig = field(default_factory=ReflectConfig)
    retain: RetainConfig = field(default_factory=RetainConfig)
    missions: MissionConfig = field(default_factory=MissionConfig)
    outbox: OutboxConfig = field(
        default_factory=lambda: OutboxConfig(Path("better_hindsight/outbox.sqlite3"))
    )
    diagnostics: DiagnosticConfig = field(default_factory=DiagnosticConfig)

    @property
    def destination_fingerprint(self) -> str:
        """Return the code-derived, credential-free destination identity."""
        return derive_destination_fingerprint(
            api_url=self.api_url,
            bank_id=self.bank_id,
            retain_tags=self.retain.tags,
            observation_scopes=self.retain.observation_scopes,
        )

    @property
    def legacy_destination_fingerprint(self) -> str:
        """Return the compatible pre-event-record destination identity."""
        return derive_destination_fingerprint(
            api_url=self.api_url,
            bank_id=self.bank_id,
            retain_tags=self.retain.tags,
            observation_scopes=self.retain.observation_scopes,
            payload_schema=LEGACY_PAYLOAD_SCHEMA_VERSION,
        )

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
            reflect_enabled=identity_authorized and self.reflect.enabled,
            retain_enabled=(
                identity_authorized and self.retain.enabled and agent_context == "primary"
            ),
        )


def load_config(
    hermes_home: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    injected: Mapping[str, object] | None = None,
) -> BetterHindsightConfig:
    """Load and validate configuration with deterministic, explicit precedence.

    Precedence, highest first, is explicit non-secret test ``injected`` values, standard
    ``HINDSIGHT_*`` process
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
    principals = _parse_principals(merged.get("allowed_principals", ()))
    recall = _parse_recall(merged.get("recall", {}))
    planner = _parse_planner(home, merged.get("planner", {}))
    reflect = _parse_reflect(merged.get("reflect", {}))
    retain = _parse_retain(merged.get("retain", {}))
    missions = _parse_missions(merged.get("missions", {}))
    outbox = _parse_outbox(home, merged.get("outbox", {}))
    diagnostics = _parse_diagnostics(home, merged.get("diagnostics", {}))

    if retain.observation_scopes == ((),) and not single_principal:
        raise _error("retain.observation_scopes='shared' requires explicit single_principal=true")
    if (
        recall.enabled
        and planner.mode != "off"
        and planner.timeout_seconds + recall.timeout_seconds > PLANNER_AND_RECALL_BUDGET_SECONDS
    ):
        raise _error(
            "combined planner and recall deadline must not exceed "
            f"{PLANNER_AND_RECALL_BUDGET_SECONDS} seconds"
        )
    minimum_segment_bytes = _minimum_retained_segment_bytes(
        retain.tags,
        max_pending_rows=outbox.max_pending_rows,
    )
    if retain.enabled and retain.segment_max_bytes < minimum_segment_bytes:
        raise _error(
            "retain.segment_max_bytes must be at least "
            f"{minimum_segment_bytes} bytes for the retained event envelope with configured tags"
        )
    if retain.segment_max_bytes + OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES > outbox.max_pending_bytes:
        raise _error(
            "retain.segment_max_bytes plus the code-owned row allowance must not exceed "
            "outbox.max_pending_bytes"
        )
    minimum_admission_bytes = _minimum_retained_admission_bytes(
        retain.tags,
        max_pending_rows=outbox.max_pending_rows,
        segment_max_bytes=retain.segment_max_bytes,
    )
    if retain.enabled and minimum_admission_bytes > outbox.max_pending_bytes:
        raise _error(
            "outbox.max_pending_bytes must fit the complete smallest retained event admission "
            "with configured tags"
        )
    if outbox.retry_initial_seconds > outbox.retry_max_seconds:
        raise _error("outbox.retry_initial_seconds must not exceed outbox.retry_max_seconds")

    return BetterHindsightConfig(
        hermes_home=home,
        api_url=api_url,
        api_key=api_key,
        bank_id=bank_id,
        single_principal=single_principal,
        allowed_principals=principals,
        recall=recall,
        planner=planner,
        reflect=reflect,
        retain=retain,
        missions=missions,
        outbox=outbox,
        diagnostics=diagnostics,
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


def derive_destination_fingerprint(
    *,
    api_url: object,
    bank_id: object,
    retain_tags: object = (),
    observation_scopes: object = None,
    payload_schema: object = PAYLOAD_SCHEMA_VERSION,
) -> str:
    """Hash normalized destination and retain transport policy without credentials or timing."""
    normalized_url = normalize_api_url(api_url)
    normalized_bank = _parse_bank_id(bank_id)
    canonical_tags = canonicalize_retain_tags(retain_tags)
    normalized_scopes = _parse_observation_scopes(observation_scopes)
    normalized_payload_schema = _parse_exact_nonempty_string(payload_schema, "payload_schema")
    payload = json.dumps(
        {
            "api_url": normalized_url,
            "bank_id": normalized_bank,
            "observation_scopes": normalized_scopes,
            "payload_schema": normalized_payload_schema,
            "retain_tags": canonical_tags,
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
        raise _error("HINDSIGHT_API_KEY must be a string")
    return value


def _parse_bank_id(value: object) -> str:
    return _parse_exact_nonempty_string(value, "bank_id")


def _parse_exact_nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise _error(f"{field_name} must be a non-empty string without outer whitespace")
    if _contains_control(value):
        raise _error(f"{field_name} must not contain control characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise _error(f"{field_name} must contain only Unicode scalar values")
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


def _parse_closed_bounded_float(
    value: object, field_name: str, *, minimum: float, maximum: float
) -> float:
    message = f"{field_name} must be a number from {minimum} through {maximum}"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(message)
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        raise _error(message) from None
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise _error(message)
    return parsed


def _parse_optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _parse_positive_int(value, field_name)


def _parse_recall(value: object) -> RecallConfig:
    values = _expect_mapping(value, "recall")
    _check_unknown_keys(values, _RECALL_KEYS, "recall")

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
        input_max_tokens=_parse_positive_int(
            values.get("input_max_tokens", DEFAULT_RECALL_INPUT_MAX_TOKENS),
            "recall.input_max_tokens",
            maximum=MAX_RECALL_INPUT_TOKENS,
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


def _parse_planner(home: Path, value: object) -> PlannerConfig:
    values = _expect_mapping(value, "planner")
    _check_unknown_keys(values, _PLANNER_KEYS, "planner")
    timeout_seconds = _parse_bounded_float(
        values.get("timeout_seconds", DEFAULT_PLANNER_TIMEOUT_SECONDS),
        "planner.timeout_seconds",
        minimum=0.0,
        maximum=MAX_PLANNER_TIMEOUT_SECONDS,
    )
    if timeout_seconds <= 0:
        raise _error(
            "planner.timeout_seconds must be greater than 0.0 and at most "
            f"{MAX_PLANNER_TIMEOUT_SECONDS}"
        )
    if "mailbox_ttl_seconds" in values:
        _parse_bounded_float(
            values["mailbox_ttl_seconds"],
            "planner.mailbox_ttl_seconds",
            minimum=0.0,
            maximum=_LEGACY_MAX_PLANNER_MAILBOX_TTL_SECONDS,
        )
    if "busy_timeout_seconds" in values:
        _parse_bounded_float(
            values["busy_timeout_seconds"],
            "planner.busy_timeout_seconds",
            minimum=0.0,
            maximum=_LEGACY_MAX_PLANNER_BUSY_TIMEOUT_SECONDS,
        )
    _parse_planner_path(
        home,
        values.get("path", _LEGACY_DEFAULT_PLANNER_MAILBOX_PATH),
    )
    return PlannerConfig(
        mode=cast(
            PlannerMode,
            _parse_literal(values.get("mode", "off"), "planner.mode", ("off", "shadow", "active")),
        ),
        timeout_seconds=timeout_seconds,
        history_max_exchanges=_parse_positive_int(
            values.get("history_max_exchanges", DEFAULT_PLANNER_HISTORY_MAX_EXCHANGES),
            "planner.history_max_exchanges",
            maximum=MAX_PLANNER_HISTORY_EXCHANGES,
        ),
        history_max_chars=_parse_positive_int(
            values.get("history_max_chars", DEFAULT_PLANNER_HISTORY_MAX_CHARS),
            "planner.history_max_chars",
            maximum=MAX_PLANNER_HISTORY_CHARS,
        ),
        query_max_chars=_parse_positive_int(
            values.get("query_max_chars", DEFAULT_PLANNER_QUERY_MAX_CHARS),
            "planner.query_max_chars",
            maximum=MAX_PLANNER_QUERY_CHARS,
        ),
    )


def _parse_planner_path(home: Path, value: object) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _error("planner.path must be a non-empty profile-local path")
    configured = Path(value)
    candidate = configured if configured.is_absolute() else home / configured
    try:
        normalized = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise _error("planner.path must resolve inside hermes_home") from None
    try:
        normalized.relative_to(home)
    except ValueError:
        raise _error("planner.path must remain inside hermes_home") from None
    return normalized


def _parse_reflect(value: object) -> ReflectConfig:
    values = _expect_mapping(value, "reflect")
    _check_unknown_keys(values, _REFLECT_KEYS, "reflect")

    budget = cast(
        ReflectBudget,
        _parse_literal(
            values.get("budget", DEFAULT_REFLECT_BUDGET),
            "reflect.budget",
            ("low", "mid", "high"),
        ),
    )
    tag_mode_value = values.get("tag_mode")
    tag_mode = cast(
        ReflectTagMode | None,
        None
        if tag_mode_value is None
        else _parse_literal(
            tag_mode_value,
            "reflect.tag_mode",
            ("any", "all", "any_strict", "all_strict", "exact"),
        ),
    )
    return ReflectConfig(
        enabled=_parse_bool(values.get("enabled", False), "reflect.enabled"),
        timeout_seconds=_parse_bounded_float(
            values.get("timeout_seconds", DEFAULT_REFLECT_TIMEOUT_SECONDS),
            "reflect.timeout_seconds",
            minimum=0.0,
            maximum=MAX_REFLECT_TIMEOUT_SECONDS,
        ),
        input_max_chars=_parse_positive_int(
            values.get("input_max_chars", DEFAULT_REFLECT_INPUT_MAX_CHARS),
            "reflect.input_max_chars",
            maximum=MAX_REFLECT_INPUT_CHARS,
        ),
        input_max_tokens=_parse_positive_int(
            values.get("input_max_tokens", DEFAULT_REFLECT_INPUT_MAX_TOKENS),
            "reflect.input_max_tokens",
            maximum=MAX_REFLECT_INPUT_TOKENS,
        ),
        output_max_bytes=_parse_positive_int(
            values.get("output_max_bytes", DEFAULT_REFLECT_OUTPUT_MAX_BYTES),
            "reflect.output_max_bytes",
            maximum=MAX_REFLECT_OUTPUT_BYTES,
        ),
        budget=budget,
        max_tokens=_parse_positive_int(
            values.get("max_tokens", DEFAULT_REFLECT_MAX_TOKENS),
            "reflect.max_tokens",
            maximum=MAX_REFLECT_MAX_TOKENS,
        ),
        tags=_parse_optional_tags(values.get("tags"), "reflect.tags"),
        tag_mode=tag_mode,
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


def canonicalize_retain_tags(value: object) -> tuple[str, ...]:
    """Return the sole validated, redacted, sorted retain-tag representation."""
    if isinstance(value, _CanonicalRetainTags):
        return value

    items = _parse_tags(value, "retain.tags")
    try:
        redacted = tuple(redact_sensitive_text(item) for item in items)
    except Exception:
        raise _error("retain.tags could not be safely canonicalized") from None
    if any(
        not item or item.strip() != item or _contains_control(item) or len(item) > MAX_TAG_CHARS
        for item in redacted
    ):
        raise _error("retain.tags could not be safely canonicalized")
    if len(set(redacted)) != len(redacted):
        raise _error("retain.tags contain distinct entries that collide after redaction")
    return _CanonicalRetainTags(sorted(redacted))


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
        enabled=_parse_bool(values.get("enabled", False), "retain.enabled"),
        timeout_seconds=_parse_bounded_float(
            values.get("timeout_seconds", DEFAULT_RETAIN_TIMEOUT_SECONDS),
            "retain.timeout_seconds",
            minimum=0.0,
            maximum=MAX_RETAIN_TIMEOUT_SECONDS,
        ),
        segment_max_bytes=_parse_positive_int(
            values.get("segment_max_bytes", DEFAULT_RETAIN_SEGMENT_MAX_BYTES),
            "retain.segment_max_bytes",
            maximum=MAX_RETAIN_SEGMENT_BYTES,
        ),
        observation_scopes=_parse_observation_scopes(values.get("observation_scopes")),
        tags=canonicalize_retain_tags(values.get("tags", ())),
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
    return MissionConfig(
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
        max_pending_rows=_parse_positive_int(
            values.get("max_pending_rows", DEFAULT_OUTBOX_MAX_PENDING_ROWS),
            "outbox.max_pending_rows",
            maximum=MAX_OUTBOX_PENDING_ROWS,
        ),
        max_pending_bytes=_parse_positive_int(
            values.get("max_pending_bytes", DEFAULT_OUTBOX_MAX_PENDING_BYTES),
            "outbox.max_pending_bytes",
            maximum=MAX_OUTBOX_PENDING_BYTES,
        ),
        busy_timeout_seconds=_parse_bounded_float(
            values.get("busy_timeout_seconds", DEFAULT_OUTBOX_BUSY_TIMEOUT_SECONDS),
            "outbox.busy_timeout_seconds",
            minimum=0.0,
            maximum=MAX_OUTBOX_BUSY_TIMEOUT_SECONDS,
        ),
        poll_interval_seconds=_parse_closed_bounded_float(
            values.get("poll_interval_seconds", DEFAULT_OUTBOX_POLL_INTERVAL_SECONDS),
            "outbox.poll_interval_seconds",
            minimum=MIN_OUTBOX_POLL_INTERVAL_SECONDS,
            maximum=MAX_OUTBOX_POLL_INTERVAL_SECONDS,
        ),
        retry_initial_seconds=_parse_bounded_float(
            values.get("retry_initial_seconds", DEFAULT_OUTBOX_RETRY_INITIAL_SECONDS),
            "outbox.retry_initial_seconds",
            minimum=0.0,
            maximum=MAX_OUTBOX_RETRY_SECONDS,
        ),
        retry_max_seconds=_parse_bounded_float(
            values.get("retry_max_seconds", DEFAULT_OUTBOX_RETRY_MAX_SECONDS),
            "outbox.retry_max_seconds",
            minimum=0.0,
            maximum=MAX_OUTBOX_RETRY_SECONDS,
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


def _parse_diagnostics(home: Path, value: object) -> DiagnosticConfig:
    values = _expect_mapping(value, "diagnostics")
    _check_unknown_keys(values, _DIAGNOSTIC_KEYS, "diagnostics")
    return DiagnosticConfig(
        enabled=_parse_bool(values.get("enabled", False), "diagnostics.enabled"),
        path=_parse_diagnostic_path(
            home, values.get("path", "better_hindsight/recall_diagnostics")
        ),
        slow_threshold_seconds=_parse_closed_bounded_float(
            values.get("slow_threshold_seconds", DEFAULT_DIAGNOSTIC_SLOW_THRESHOLD_SECONDS),
            "diagnostics.slow_threshold_seconds",
            minimum=0.1,
            maximum=MAX_RECALL_TIMEOUT_SECONDS,
        ),
        max_records=_parse_positive_int(
            values.get("max_records", DEFAULT_DIAGNOSTIC_MAX_RECORDS),
            "diagnostics.max_records",
            maximum=MAX_DIAGNOSTIC_RECORDS,
        ),
        replay_timeout_seconds=_parse_closed_bounded_float(
            values.get("replay_timeout_seconds", DEFAULT_DIAGNOSTIC_REPLAY_TIMEOUT_SECONDS),
            "diagnostics.replay_timeout_seconds",
            minimum=0.1,
            maximum=MAX_DIAGNOSTIC_REPLAY_TIMEOUT_SECONDS,
        ),
    )


def _parse_diagnostic_path(home: Path, value: object) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _error("diagnostics.path must be a non-empty profile-local path")
    configured = Path(value)
    candidate = configured if configured.is_absolute() else home / configured
    try:
        normalized = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise _error("diagnostics.path must resolve inside hermes_home") from None
    try:
        normalized.relative_to(home)
    except ValueError:
        raise _error("diagnostics.path must remain inside hermes_home") from None
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


def _minimum_retained_segment_bytes(
    tags: Sequence[str],
    *,
    max_pending_rows: int,
) -> int:
    roles = [{"content": "x", "role": "assistant"}]
    if max_pending_rows == 1:
        roles.insert(0, {"content": "x", "role": "user"})
    return _minimum_retained_content_bytes(tags, roles=roles)


def _minimum_retained_admission_bytes(
    tags: Sequence[str],
    *,
    max_pending_rows: int,
    segment_max_bytes: int,
) -> int:
    complete_bytes = _minimum_retained_content_bytes(
        tags,
        roles=(
            {"content": "x", "role": "user"},
            {"content": "x", "role": "assistant"},
        ),
    )
    if max_pending_rows == 1 or complete_bytes <= segment_max_bytes:
        return complete_bytes + OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES
    user_bytes = _minimum_retained_content_bytes(
        tags,
        roles=({"content": "x", "role": "user"},),
    )
    assistant_bytes = _minimum_retained_content_bytes(
        tags,
        roles=({"content": "x", "role": "assistant"},),
    )
    return user_bytes + assistant_bytes + 2 * OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES


def _minimum_retained_content_bytes(
    tags: Sequence[str],
    *,
    roles: Sequence[dict[str, str]],
) -> int:
    content = json.dumps(
        {
            "event_id": "0" * 32,
            "occurred_at": "2000-01-01T00:00:00.000000+00:00",
            "payload_schema": PAYLOAD_SCHEMA_VERSION,
            "record_schema": RETAINED_EVENT_RECORD_SCHEMA,
            "roles": list(roles),
            "session_sha256": "0" * 64,
            "tags": list(tags),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return len(content.encode("utf-8"))


__all__ = [
    "AllowedPrincipal",
    "BetterHindsightConfig",
    "ConfigError",
    "DiagnosticConfig",
    "IdentifierKind",
    "DEFAULT_API_URL",
    "DEFAULT_BANK_ID",
    "DEFAULT_DIAGNOSTIC_MAX_RECORDS",
    "DEFAULT_DIAGNOSTIC_REPLAY_TIMEOUT_SECONDS",
    "DEFAULT_DIAGNOSTIC_SLOW_THRESHOLD_SECONDS",
    "DEFAULT_OUTBOX_BUSY_TIMEOUT_SECONDS",
    "DEFAULT_OUTBOX_MAX_PENDING_BYTES",
    "DEFAULT_OUTBOX_MAX_PENDING_ROWS",
    "DEFAULT_OUTBOX_POLL_INTERVAL_SECONDS",
    "DEFAULT_OUTBOX_RETRY_INITIAL_SECONDS",
    "DEFAULT_OUTBOX_RETRY_MAX_SECONDS",
    "OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES",
    "LEGACY_PAYLOAD_SCHEMA_VERSION",
    "PAYLOAD_SCHEMA_VERSION",
    "RETAINED_EVENT_RECORD_SCHEMA",
    "RETAINED_MODEL_RECORD_SCHEMA",
    "DEFAULT_RECALL_CONTEXT_MAX_BYTES",
    "DEFAULT_RECALL_INPUT_MAX_CHARS",
    "DEFAULT_RECALL_TIMEOUT_SECONDS",
    "DEFAULT_REFLECT_BUDGET",
    "DEFAULT_REFLECT_INPUT_MAX_CHARS",
    "DEFAULT_REFLECT_INPUT_MAX_TOKENS",
    "DEFAULT_REFLECT_MAX_TOKENS",
    "DEFAULT_REFLECT_OUTPUT_MAX_BYTES",
    "DEFAULT_REFLECT_TIMEOUT_SECONDS",
    "DEFAULT_RETAIN_SEGMENT_MAX_BYTES",
    "DEFAULT_RETAIN_TIMEOUT_SECONDS",
    "MAX_REFLECT_INPUT_CHARS",
    "MAX_REFLECT_INPUT_TOKENS",
    "MAX_REFLECT_MAX_TOKENS",
    "MAX_REFLECT_OUTPUT_BYTES",
    "MAX_REFLECT_TIMEOUT_SECONDS",
    "MemoryAuthorization",
    "MissionConfig",
    "ObservationScopes",
    "OutboxConfig",
    "RecallBudget",
    "RecallConfig",
    "RecallMinScores",
    "RecallTagMode",
    "RecallType",
    "ReflectBudget",
    "ReflectConfig",
    "ReflectTagMode",
    "RetainConfig",
    "canonicalize_retain_tags",
    "derive_destination_fingerprint",
    "load_config",
    "normalize_api_url",
]
