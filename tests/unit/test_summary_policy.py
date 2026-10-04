"""Strict operator-owned defaults and no-network configuration refusal."""

from pathlib import Path

import pytest

from better_hermes_hindsight.config import load_config
from better_hermes_hindsight.summary_policy import SummaryPolicy, parse_policy


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "auto"},
        {"mode": []},
        {"budget": "unlimited"},
        {"budget": {}},
        {"refresh_after_consolidation": 1},
        {"refresh_cron": "@daily"},
        {"refresh_cron": " 0 4 * * *"},
        {"refresh_cron": "0 4 * * * "},
        {"refresh_cron": "0 0 * * * *"},
        {"refresh_cron": "61 0 * * *"},
        {"refresh_cron": "0 0 * * *", "refresh_after_consolidation": True},
        {"min_refresh_interval_seconds": True},
        {"min_refresh_interval_seconds": -1},
        {"min_refresh_interval_seconds": 31536001},
        {"max_tokens": 0},
        {"max_tokens": 1},
        {"max_tokens": 128},
        {"max_tokens": 255},
        {"max_tokens": 8193},
        {"max_tokens": True},
        {"recall_max_tokens": 16385},
        {"observations_max_tokens": 0},
        {"keep_trace": True},
        {"tags": []},
        {"response_schema": {}},
    ],
)
def test_invalid_policy(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_policy(changes)


@pytest.mark.parametrize("tokens", [256, 1024, 8192])
def test_valid_output_token_bounds(tokens: int) -> None:
    assert parse_policy({"max_tokens": tokens}).max_tokens == tokens


def test_merge_preserves_operator_defaults() -> None:
    base = parse_policy({"mode": "delta", "max_tokens": 512})
    policy = parse_policy({"budget": "low"}, base=base)
    assert policy.mode == "delta" and policy.max_tokens == 512 and policy.budget == "low"
    assert SummaryPolicy().trigger()["refresh_cron"] is None


@pytest.mark.parametrize("flag", ["refresh_enabled", "pages_enabled"])
def test_optins_require_reads(tmp_path: Path, flag: str) -> None:
    with pytest.raises(ValueError):
        load_config(tmp_path, injected={"mental_models": {flag: True}}, environ={})
