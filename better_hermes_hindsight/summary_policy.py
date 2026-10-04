"""Operator-owned refresh defaults; no startup synchronization or scheduler."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SummaryPolicy:
    mode: str = "full"
    budget: str = "mid"
    refresh_after_consolidation: bool = False
    refresh_cron: str | None = None
    min_refresh_interval_seconds: int = 0
    max_tokens: int = 1024
    recall_max_tokens: int = 4096
    observations_max_tokens: int = 5000

    def trigger(self, *, modern: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "mode": self.mode,
            "refresh_after_consolidation": self.refresh_after_consolidation,
            "refresh_cron": self.refresh_cron,
            "min_refresh_interval_seconds": self.min_refresh_interval_seconds,
            "fact_types": None,
            "exclude_mental_models": True,
            "exclude_mental_model_ids": None,
            "tags_match": "any",
            "tag_groups": None,
            "include_chunks": False,
            "recall_max_tokens": self.recall_max_tokens,
            "recall_chunks_max_tokens": 0,
            "response_schema": None,
            "keep_trace": False,
        }
        if modern:
            result.update(
                budget=self.budget,
                reflect_search_observations_max_tokens=self.observations_max_tokens,
                reflect_search_observations_include_entities=False,
            )
        return result


def validate_fixed_policy(model: dict[str, Any]) -> None:
    """Refuse inherited scope, trace, or schema controls before paid refresh/edit."""
    trigger = model.get("trigger")
    if model.get("tags") != [] or not isinstance(trigger, dict):
        raise ValueError("Summary requires bank-wide fixed policy.")
    editable = {
        "mode",
        "budget",
        "refresh_after_consolidation",
        "refresh_cron",
        "min_refresh_interval_seconds",
        "recall_max_tokens",
        "reflect_search_observations_max_tokens",
    }
    for key, expected in SummaryPolicy().trigger().items():
        if key not in editable and trigger.get(key) != expected:
            raise ValueError("Summary requires fixed retrieval and tracing policy.")
    policy_fields = {key: trigger[key] for key in editable if key in trigger}
    if "reflect_search_observations_max_tokens" in policy_fields:
        policy_fields["observations_max_tokens"] = policy_fields.pop(
            "reflect_search_observations_max_tokens"
        )
    if model.get("max_tokens") is not None:
        policy_fields["max_tokens"] = model["max_tokens"]
    parse_policy(policy_fields)


def parse_policy(value: object, *, base: SummaryPolicy | None = None) -> SummaryPolicy:
    if not isinstance(value, dict) or set(value) - set(asdict(SummaryPolicy())):
        raise ValueError("Invalid summary creation policy.")
    values: dict[str, Any] = {**asdict(base or SummaryPolicy()), **value}
    if (
        not isinstance(values["mode"], str)
        or values["mode"] not in {"full", "delta"}
        or not isinstance(values["budget"], str)
        or values["budget"] not in {"low", "mid", "high"}
    ):
        raise ValueError("Invalid summary refresh mode or budget.")
    if type(values["refresh_after_consolidation"]) is not bool:
        raise ValueError("Invalid consolidation trigger.")
    cron = values["refresh_cron"]
    if cron is not None:
        from croniter import croniter  # type: ignore[import-untyped]

        if (
            not isinstance(cron, str)
            or cron != cron.strip()
            or len(cron) > 120
            or len(cron.split()) != 5
        ):
            raise ValueError("Refresh cron requires five fields (UTC).")
        if not croniter.is_valid(cron):
            raise ValueError("Invalid refresh cron.")
        if values["refresh_after_consolidation"]:
            raise ValueError("Refresh triggers are mutually exclusive.")
    for key, minimum, maximum in (
        ("min_refresh_interval_seconds", 0, 31_536_000),
        ("max_tokens", 256, 8192),
        ("recall_max_tokens", 1, 16_384),
        ("observations_max_tokens", 1, 16_384),
    ):
        if type(values[key]) is not int or not minimum <= values[key] <= maximum:
            raise ValueError("Invalid summary token budget or interval.")
    return SummaryPolicy(**values)
