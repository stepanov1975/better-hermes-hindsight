"""Contract tests for Better Hindsight's pure typed configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from better_hermes_hindsight.config import (
    DEFAULT_API_URL,
    DEFAULT_BANK_ID,
    DEFAULT_REFLECT_BUDGET,
    DEFAULT_REFLECT_INPUT_MAX_CHARS,
    DEFAULT_REFLECT_INPUT_MAX_TOKENS,
    DEFAULT_REFLECT_MAX_TOKENS,
    DEFAULT_REFLECT_OUTPUT_MAX_BYTES,
    DEFAULT_REFLECT_TIMEOUT_SECONDS,
    MAX_REFLECT_INPUT_CHARS,
    MAX_REFLECT_INPUT_TOKENS,
    MAX_REFLECT_MAX_TOKENS,
    MAX_REFLECT_OUTPUT_BYTES,
    MAX_REFLECT_TIMEOUT_SECONDS,
    OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES,
    PAYLOAD_SCHEMA_VERSION,
    ConfigError,
    load_config,
)
from better_hermes_hindsight.redaction import REDACTION_MARKER


def _write_config(hermes_home: Path, payload: object) -> Path:
    path = hermes_home / "better_hindsight" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_connection_precedence_and_environment_only_api_key(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "api_url": "https://profile.example.test/api/",
            "bank_id": "profile-bank",
            "single_principal": True,
        },
    )
    environ = {
        "HINDSIGHT_API_URL": "https://environment.example.test/",
        "HINDSIGHT_API_KEY": "environment-key-value",
        "HINDSIGHT_BANK_ID": "environment-bank",
    }

    config = load_config(
        hermes_home=tmp_path,
        environ=environ,
        injected={
            "api_url": "HTTPS://INJECTED.EXAMPLE.TEST:443/root/",
            "bank_id": "injected-bank",
        },
    )

    assert config.api_url == "https://injected.example.test/root"
    assert config.bank_id == "injected-bank"
    assert config.api_key == "environment-key-value"
    assert "environment-key-value" not in repr(config)


def test_environment_beats_profile_and_profile_beats_defaults(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "api_url": "https://profile.example.test/",
            "bank_id": "profile-bank",
            "single_principal": False,
        },
    )

    profile = load_config(hermes_home=tmp_path, environ={})
    environment = load_config(
        hermes_home=tmp_path,
        environ={
            "HINDSIGHT_API_URL": "http://environment.example.test:80/",
            "HINDSIGHT_BANK_ID": "environment-bank",
        },
    )
    defaults = load_config(hermes_home=tmp_path / "empty", environ={})

    assert (profile.api_url, profile.bank_id) == (
        "https://profile.example.test",
        "profile-bank",
    )
    assert (environment.api_url, environment.bank_id) == (
        "http://environment.example.test",
        "environment-bank",
    )
    assert (defaults.api_url, defaults.bank_id) == (DEFAULT_API_URL, DEFAULT_BANK_ID)
    assert DEFAULT_BANK_ID == "hermes"
    assert defaults.api_key is None


def test_defaults_follow_the_best_effort_product_contract(tmp_path: Path) -> None:
    config = load_config(hermes_home=tmp_path, environ={})

    assert not hasattr(config, "integration_mode")
    assert config.recall.enabled is True
    assert config.recall.input_max_tokens == 500
    assert config.planner.mode == "off"
    assert config.planner.timeout_seconds == 2.0
    assert config.planner.history_max_exchanges == 4
    assert config.planner.history_max_chars == 6_000
    assert config.planner.query_max_chars == 1_024
    assert config.reflect.enabled is False
    assert config.reflect.timeout_seconds == DEFAULT_REFLECT_TIMEOUT_SECONDS == 60.0
    assert config.reflect.input_max_chars == DEFAULT_REFLECT_INPUT_MAX_CHARS == 4096
    assert config.reflect.input_max_tokens == DEFAULT_REFLECT_INPUT_MAX_TOKENS == 500
    assert config.reflect.output_max_bytes == DEFAULT_REFLECT_OUTPUT_MAX_BYTES == 16_384
    assert config.reflect.budget == DEFAULT_REFLECT_BUDGET == "low"
    assert config.reflect.max_tokens == DEFAULT_REFLECT_MAX_TOKENS == 1024
    assert config.reflect.tags is None
    assert config.reflect.tag_mode is None
    assert config.retain.enabled is False
    assert config.retain.timeout_seconds == 60.0
    assert config.outbox.max_pending_rows == 2_000
    assert config.outbox.max_pending_bytes == 134_217_728
    assert config.outbox.busy_timeout_seconds == 1.0
    assert config.outbox.poll_interval_seconds == 2.0
    assert config.outbox.retry_initial_seconds == 2.0
    assert config.outbox.retry_max_seconds == 300.0
    assert config.diagnostics.enabled is False
    assert config.diagnostics.slow_threshold_seconds == 5.0
    assert config.diagnostics.max_records == 50
    assert config.diagnostics.replay_timeout_seconds == 30.0
    assert not hasattr(config.missions, "policy")


def test_planner_configuration_is_bounded(tmp_path: Path) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "planner": {
                "mode": "active",
                "timeout_seconds": 1.5,
                "history_max_exchanges": 2,
                "history_max_chars": 3_000,
                "query_max_chars": 700,
            }
        },
    )

    assert config.planner.mode == "active"
    assert config.planner.timeout_seconds == 1.5
    assert config.planner.history_max_exchanges == 2
    assert config.planner.history_max_chars == 3_000
    assert config.planner.query_max_chars == 700
    with pytest.raises(ConfigError, match="planner.mode"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"planner": {"mode": "invalid"}},
        )
    with pytest.raises(ConfigError, match="planner.timeout_seconds"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"planner": {"mode": "active", "timeout_seconds": 0}},
        )
    with pytest.raises(ConfigError, match="combined planner and recall deadline"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={
                "recall": {"timeout_seconds": 6.0},
                "planner": {"mode": "active", "timeout_seconds": 2.0},
            },
        )


def test_legacy_planner_mailbox_settings_remain_loadable_for_offline_upgrade(
    tmp_path: Path,
) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "planner": {
                "mode": "active",
                "path": "better_hindsight/custom-plans.sqlite3",
                "mailbox_ttl_seconds": 12.0,
                "busy_timeout_seconds": 0.2,
            }
        },
    )

    assert config.planner.mode == "active"
    with pytest.raises(ConfigError, match="planner.path must remain inside hermes_home"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"planner": {"path": "../outside.sqlite3"}},
        )


def test_disabled_recall_skips_dormant_planner_deadline_constraints(tmp_path: Path) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "recall": {"enabled": False, "timeout_seconds": 30.0},
            "planner": {
                "mode": "active",
                "timeout_seconds": 4.0,
            },
            "reflect": {"enabled": True},
            "retain": {"enabled": True},
        },
    )

    assert config.recall.enabled is False
    assert config.planner.mode == "active"
    assert config.reflect.enabled is True
    assert config.retain.enabled is True


def test_hermes_home_must_be_explicit_valid_and_absolute(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="absolute"):
        load_config(hermes_home=Path("relative/profile"), environ={})

    malformed_home = str(tmp_path) + "/invalid\0profile"
    with pytest.raises(ConfigError, match="valid absolute path") as caught:
        load_config(hermes_home=malformed_home, environ={})
    assert caught.value.__cause__ is None


def test_loader_never_discovers_dotenv_or_cwd_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text(
        "HINDSIGHT_API_URL=https://dotenv.example.test\nHINDSIGHT_BANK_ID=dotenv-bank\n",
        encoding="utf-8",
    )
    (cwd / "config.json").write_text(
        json.dumps({"api_url": "https://cwd.example.test", "bank_id": "cwd-bank"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd)

    config = load_config(hermes_home=tmp_path / "profile", environ={})

    assert config.api_url == DEFAULT_API_URL
    assert config.bank_id == DEFAULT_BANK_ID


def test_process_environment_is_used_without_overriding_injected_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit_home = tmp_path / "explicit-profile"
    _write_config(explicit_home, {"single_principal": True})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-profile"))
    monkeypatch.setenv("HINDSIGHT_API_URL", "https://process.example.test/")
    monkeypatch.setenv("HINDSIGHT_API_KEY", "process-key-value")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "process-bank")

    config = load_config(hermes_home=explicit_home)

    assert config.hermes_home == explicit_home.resolve()
    assert config.api_url == "https://process.example.test"
    assert config.api_key == "process-key-value"
    assert config.bank_id == "process-bank"


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "DO_NOT_ECHO_THIS_VALUE"},
        {"HINDSIGHT_API_KEY": "DO_NOT_ECHO_THIS_VALUE"},
        {"recall": {"nested": {"access_token": "DO_NOT_ECHO_THIS_VALUE"}}},
        {"retain": {"items": [{"client_secret": "DO_NOT_ECHO_THIS_VALUE"}]}},
        {"nested": {"refresh_token": "DO_NOT_ECHO_THIS_VALUE"}},
        {"nested": {"api_secret": "DO_NOT_ECHO_THIS_VALUE"}},
        {"nested": {"password_hint": "DO_NOT_ECHO_THIS_VALUE"}},
    ],
)
def test_secret_bearing_json_keys_are_rejected_at_every_depth(
    tmp_path: Path, payload: object
) -> None:
    _write_config(tmp_path, payload)

    with pytest.raises(ConfigError) as caught:
        load_config(hermes_home=tmp_path, environ={})

    assert "secret-bearing" in str(caught.value)
    assert "DO_NOT_ECHO_THIS_VALUE" not in str(caught.value)


def test_api_key_is_rejected_from_injected_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="HINDSIGHT_API_KEY") as caught:
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"api_key": "DO_NOT_ECHO_THIS_VALUE"},
        )

    assert "DO_NOT_ECHO_THIS_VALUE" not in str(caught.value)


def test_duplicate_profile_json_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "better_hindsight" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"bank_id":"first-bank","bank_id":"second-bank"}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate JSON key"):
        load_config(hermes_home=tmp_path, environ={})


def test_unbounded_json_integer_is_a_sanitized_config_error(tmp_path: Path) -> None:
    path = tmp_path / "better_hindsight" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"recall":{"timeout_seconds":' + ("9" * 5000) + "}}",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="valid JSON") as caught:
        load_config(hermes_home=tmp_path, environ={})

    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "payload, expected_field",
    [
        ({"unexpected": True}, "unexpected"),
        ({"batch_size": 10}, "batch_size"),
        ({"integration_mode": "hybrid"}, "integration_mode"),
        ({"recall": {"unexpected": True}}, "recall.unexpected"),
        ({"reflect": {"unexpected": True}}, "reflect.unexpected"),
        ({"reflect": {"tags_match": "all"}}, "reflect.tags_match"),
        ({"retain": {"unexpected": True}}, "retain.unexpected"),
        ({"retain": {"extraction_mode": "custom"}}, "retain.extraction_mode"),
        ({"missions": {"unexpected": True}}, "missions.unexpected"),
        ({"missions": {"policy": "check"}}, "missions.policy"),
        ({"missions": {"reflect_mission": "not supported"}}, "missions.reflect_mission"),
        ({"outbox": {"unexpected": True}}, "outbox.unexpected"),
        ({"outbox": {"retry_multiplier": 2.0}}, "outbox.retry_multiplier"),
        ({"outbox": {"shutdown_join_seconds": 2.0}}, "outbox.shutdown_join_seconds"),
        ({"authorized_principals": []}, "authorized_principals"),
        ({"recall": {"tags_match": "all"}}, "recall.tags_match"),
        ({"retain": {"tags_mode": "append"}}, "retain.tags_mode"),
        ({"outbox": {"payload_schema": "turn-v2"}}, "outbox.payload_schema"),
        (
            {"outbox": {"row_accounting_allowance_bytes": 1024}},
            "outbox.row_accounting_allowance_bytes",
        ),
        ({"destination_fingerprint": "operator-value"}, "destination_fingerprint"),
        (
            {
                "allowed_principals": [
                    {
                        "platform": "sample-platform",
                        "identifier_kind": "user_id",
                        "identifier": "sample-user",
                        "unexpected": True,
                    }
                ]
            },
            "allowed_principals[0].unexpected",
        ),
        (
            {
                "allowed_principals": [
                    {
                        "platform": "sample-platform",
                        "identifier_kind": "user_id",
                        "identifier_value": "sample-user",
                    }
                ]
            },
            "allowed_principals[0].identifier_value",
        ),
        ({"recall": {"min_scores": {"unexpected": 0.5}}}, "min_scores.unexpected"),
    ],
)
def test_unknown_keys_are_rejected_strictly(
    tmp_path: Path, payload: object, expected_field: str
) -> None:
    _write_config(tmp_path, payload)

    with pytest.raises(ConfigError) as caught:
        load_config(hermes_home=tmp_path, environ={})

    assert expected_field in str(caught.value)


def test_complete_typed_configuration_round_trips(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "api_url": "https://hindsight.example.test/base/",
            "bank_id": "sample-bank",
            "single_principal": True,
            "allowed_principals": [
                {
                    "platform": "sample-gateway",
                    "identifier_kind": "user_id",
                    "identifier": "sample-user-id",
                },
                {
                    "platform": "sample-gateway",
                    "identifier_kind": "user_id_alt",
                    "identifier": "sample-alternate-id",
                },
            ],
            "recall": {
                "enabled": False,
                "timeout_seconds": 2.25,
                "input_max_chars": 3210,
                "input_max_tokens": 499,
                "context_max_bytes": 6543,
                "budget": "low",
                "max_tokens": 777,
                "types": ["observation"],
                "tags": ["project:sample"],
                "tag_mode": "all_strict",
                "prefer_observations": True,
                "min_scores": {"semantic": 0.1, "final": 1.2},
                "include_source_facts": True,
                "max_source_facts_tokens": 222,
            },
            "reflect": {
                "enabled": True,
                "timeout_seconds": 12.5,
                "input_max_chars": 3200,
                "input_max_tokens": 498,
                "output_max_bytes": 12_345,
                "budget": "high",
                "max_tokens": 8192,
                "tags": ["project:sample"],
                "tag_mode": "all_strict",
            },
            "retain": {
                "enabled": False,
                "timeout_seconds": 45.0,
                "segment_max_bytes": 70000,
                "observation_scopes": "shared",
                "tags": ["source:sample"],
            },
            "missions": {
                "retain_mission": "Retain durable user preferences.",
                "observations_mission": "Consolidate stable observations.",
            },
            "outbox": {
                "path": "better_hindsight/custom-outbox.sqlite3",
                "max_pending_rows": 1234,
                "max_pending_bytes": 100_000_000,
                "busy_timeout_seconds": 0.5,
                "poll_interval_seconds": 0.25,
                "retry_initial_seconds": 3.0,
                "retry_max_seconds": 30.0,
            },
        },
    )

    config = load_config(hermes_home=tmp_path, environ={})

    assert not hasattr(config, "integration_mode")
    assert config.recall.enabled is False
    assert not hasattr(config.recall, "query_projection")
    assert config.recall.timeout_seconds == 2.25
    assert config.recall.input_max_chars == 3210
    assert config.recall.input_max_tokens == 499
    assert config.recall.context_max_bytes == 6543
    assert config.recall.budget == "low"
    assert config.recall.max_tokens == 777
    assert config.recall.types == ("observation",)
    assert config.recall.tags == ("project:sample",)
    assert config.recall.tag_mode == "all_strict"
    assert not hasattr(config.recall, "tags_match")
    assert config.recall.prefer_observations is True
    assert config.recall.min_scores is not None
    assert config.recall.min_scores.as_dict() == {"semantic": 0.1, "final": 1.2}
    assert config.recall.include_source_facts is True
    assert config.recall.max_source_facts_tokens == 222
    assert config.reflect.enabled is True
    assert config.reflect.timeout_seconds == 12.5
    assert config.reflect.input_max_chars == 3200
    assert config.reflect.input_max_tokens == 498
    assert config.reflect.output_max_bytes == 12_345
    assert config.reflect.budget == "high"
    assert config.reflect.max_tokens == 8192
    assert config.reflect.tags == ("project:sample",)
    assert config.reflect.tag_mode == "all_strict"
    assert not hasattr(config.reflect, "tags_match")
    assert config.retain.enabled is False
    assert config.retain.timeout_seconds == 45.0
    assert config.retain.segment_max_bytes == 70000
    assert config.retain.observation_scopes == ((),)
    assert config.retain.tags == ("source:sample",)
    assert not hasattr(config.missions, "policy")
    assert config.missions.retain_mission == "Retain durable user preferences."
    assert config.missions.observations_mission == "Consolidate stable observations."
    assert config.outbox.path == tmp_path.resolve() / "better_hindsight/custom-outbox.sqlite3"
    assert config.outbox.payload_schema == PAYLOAD_SCHEMA_VERSION
    assert config.outbox.max_pending_rows == 1234
    assert config.outbox.max_pending_bytes == 100_000_000
    assert config.outbox.busy_timeout_seconds == 0.5
    assert config.outbox.busy_timeout_ms == 500
    assert config.outbox.poll_interval_seconds == 0.25
    assert config.outbox.retry_initial_seconds == 3.0
    assert config.outbox.retry_max_seconds == 30.0
    tiny_timeout = load_config(
        hermes_home=tmp_path / "tiny-timeout",
        environ={},
        injected={"outbox": {"busy_timeout_seconds": 0.0001}},
    )
    assert tiny_timeout.outbox.busy_timeout_ms == 1
    assert config.allowed_principals[0].as_tuple() == (
        "sample-gateway",
        "user_id",
        "sample-user-id",
    )
    assert len(config.destination_fingerprint) == 64
    rendered = repr(config)
    assert "hindsight.example.test" not in rendered
    assert "sample-bank" not in rendered
    assert "sample-user-id" not in rendered
    assert "Retain durable user preferences" not in rendered

    with pytest.raises(FrozenInstanceError):
        config.__setattr__("bank_id", "replacement")


def test_omitted_hindsight_recall_controls_remain_none(tmp_path: Path) -> None:
    config = load_config(hermes_home=tmp_path, environ={})

    assert config.recall.budget is None
    assert config.recall.max_tokens is None
    assert config.recall.types is None
    assert config.recall.tags is None
    assert config.recall.tag_mode is None
    assert config.recall.prefer_observations is None
    assert config.recall.min_scores is None
    assert config.recall.include_source_facts is None
    assert config.recall.max_source_facts_tokens is None
    assert config.retain.observation_scopes is None
    assert config.retain.enabled is False
    assert config.retain.timeout_seconds == 60.0
    assert config.retain.segment_max_bytes == 65536
    assert config.outbox.payload_schema == PAYLOAD_SCHEMA_VERSION
    assert config.outbox.max_pending_rows == 2_000
    assert config.outbox.max_pending_bytes == 134_217_728


def test_reflect_accepts_exact_hard_bounds_and_fixed_tag_policy(tmp_path: Path) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "reflect": {
                "enabled": True,
                "timeout_seconds": MAX_REFLECT_TIMEOUT_SECONDS,
                "input_max_chars": MAX_REFLECT_INPUT_CHARS,
                "input_max_tokens": MAX_REFLECT_INPUT_TOKENS,
                "output_max_bytes": MAX_REFLECT_OUTPUT_BYTES,
                "budget": "mid",
                "max_tokens": MAX_REFLECT_MAX_TOKENS,
                "tags": ["scope:a", "scope:b"],
                "tag_mode": "exact",
            }
        },
    )

    assert config.reflect.timeout_seconds == 300.0
    assert config.reflect.input_max_chars == 65_536
    assert config.reflect.input_max_tokens == 1_048_576
    assert config.reflect.output_max_bytes == 1_048_576
    assert config.reflect.max_tokens == 16_384
    assert config.reflect.tags == ("scope:a", "scope:b")
    assert config.reflect.tag_mode == "exact"


def test_mission_policy_is_removed_but_distinct_mission_texts_remain(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown key\(s\): missions\.policy"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"missions": {"policy": "check"}},
        )

    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "missions": {
                "retain_mission": "Retain durable preferences.",
                "observations_mission": "Consolidate stable observations.",
            }
        },
    )
    assert config.missions.retain_mission == "Retain durable preferences."
    assert config.missions.observations_mission == "Consolidate stable observations."
    assert not hasattr(config.missions, "policy")


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("combined", "combined"),
        ("shared", ((),)),
        ([[]], ((),)),
    ],
)
def test_observation_scope_normalization(tmp_path: Path, value: object, expected: object) -> None:
    injected = {} if value is None else {"retain": {"observation_scopes": value}}

    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={"single_principal": True, **injected},
    )

    assert config.retain.observation_scopes == expected


def test_bare_empty_observation_scope_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="combined.*explicitly"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"retain": {"observation_scopes": []}},
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"api_url": "ftp://service.example.test"},
        {"api_url": "https://user:password@service.example.test"},
        {"api_url": "https://service.example.test?token=value"},
        {"api_url": "https://service.example.test/#fragment"},
        {"api_url": "https:///missing-host"},
        {"api_url": "https://invalid host.example.test"},
        {"bank_id": "   "},
        {"integration_mode": "embedded"},
        {"recall": {"timeout_seconds": 0}},
        {"recall": {"timeout_seconds": 10**1000}},
        {"recall": {"timeout_seconds": 31}},
        {"recall": {"input_max_chars": 0}},
        {"recall": {"input_max_tokens": 0}},
        {"recall": {"input_max_tokens": 1_048_577}},
        {"recall": {"context_max_bytes": 0}},
        {"recall": {"query_projection": "head_tail"}},
        {"recall": {"budget": "extreme"}},
        {"recall": {"max_tokens": 0}},
        {"recall": {"types": ["directive"]}},
        {"recall": {"tags": ["x" * 257]}},
        {"recall": {"tags": ["duplicate", "duplicate"]}},
        {"recall": {"tag_mode": "loose"}},
        {"recall": {"min_scores": {"final": -0.01}}},
        {"recall": {"min_scores": {"semantic": float("nan")}}},
        {"recall": {"min_scores": {"semantic": 10**1000}}},
        {"recall": {"max_source_facts_tokens": 0}},
        {"reflect": {"enabled": 1}},
        {"reflect": {"timeout_seconds": 0}},
        {"reflect": {"timeout_seconds": 301}},
        {"reflect": {"input_max_chars": 0}},
        {"reflect": {"input_max_chars": 65_537}},
        {"reflect": {"input_max_tokens": 0}},
        {"reflect": {"input_max_tokens": 1_048_577}},
        {"reflect": {"output_max_bytes": 0}},
        {"reflect": {"output_max_bytes": 1_048_577}},
        {"reflect": {"budget": "extreme"}},
        {"reflect": {"max_tokens": 0}},
        {"reflect": {"max_tokens": 16_385}},
        {"reflect": {"tags": ["duplicate", "duplicate"]}},
        {"reflect": {"tag_mode": "loose"}},
        {"retain": {"segment_max_bytes": 0}},
        {"retain": {"timeout_seconds": 0}},
        {"retain": {"timeout_seconds": 301}},
        {"retain": {"tags": [f"tag-{index}" for index in range(65)]}},
        {"missions": {"policy": "startup-apply"}},
        {"outbox": {"max_pending_rows": 0}},
        {"outbox": {"max_pending_rows": 100_001}},
        {"outbox": {"max_pending_bytes": 0}},
        {"outbox": {"max_pending_bytes": 1_073_741_825}},
        {"outbox": {"busy_timeout_seconds": 0}},
        {"outbox": {"busy_timeout_seconds": 10**1000}},
        {"outbox": {"path": "invalid\0path.sqlite3"}},
        {"outbox": {"busy_timeout_seconds": 6}},
        {"outbox": {"poll_interval_seconds": 0.09}},
        {"outbox": {"poll_interval_seconds": 61}},
        {"outbox": {"retry_initial_seconds": 0}},
        {"outbox": {"retry_initial_seconds": 3_601}},
        {"outbox": {"retry_max_seconds": 0}},
        {"outbox": {"retry_max_seconds": 3_601}},
        {"outbox": {"path": "../outside.sqlite3"}},
    ],
)
def test_invalid_values_fail_with_sanitized_actionable_errors(
    tmp_path: Path, payload: Mapping[str, object]
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(hermes_home=tmp_path, environ={}, injected=payload)

    message = str(caught.value)
    assert message.startswith("Better Hindsight configuration error:")
    assert "password" not in message
    assert "token=value" not in message


def test_enabled_retention_rejects_a_segment_limit_smaller_than_its_event_envelope(
    tmp_path: Path,
) -> None:
    disabled = load_config(
        hermes_home=tmp_path / "disabled",
        environ={},
        injected={"retain": {"enabled": False, "segment_max_bytes": 1}},
    )
    assert disabled.retain.segment_max_bytes == 1

    with pytest.raises(ConfigError, match="segment_max_bytes.*retained event envelope"):
        load_config(
            hermes_home=tmp_path / "enabled",
            environ={},
            injected={
                "retain": {
                    "enabled": True,
                    "segment_max_bytes": 1,
                    "tags": ["project:sample"],
                }
            },
        )


def test_one_row_outbox_requires_a_complete_two_role_event_envelope(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="segment_max_bytes.*retained event envelope"):
        load_config(
            hermes_home=tmp_path / "one-row-too-small",
            environ={},
            injected={
                "retain": {
                    "enabled": True,
                    "segment_max_bytes": 348,
                    "tags": ["project:sample"],
                },
                "outbox": {"max_pending_rows": 1},
            },
        )

    one_row = load_config(
        hermes_home=tmp_path / "one-row-exact",
        environ={},
        injected={
            "retain": {
                "enabled": True,
                "segment_max_bytes": 378,
                "tags": ["project:sample"],
            },
            "outbox": {"max_pending_rows": 1},
        },
    )
    two_rows = load_config(
        hermes_home=tmp_path / "two-rows-exact",
        environ={},
        injected={
            "retain": {
                "enabled": True,
                "segment_max_bytes": 348,
                "tags": ["project:sample"],
            },
            "outbox": {"max_pending_rows": 2},
        },
    )

    assert one_row.retain.segment_max_bytes == 378
    assert two_rows.retain.segment_max_bytes == 348


def test_retention_capacity_counts_all_rows_needed_by_the_smallest_event(
    tmp_path: Path,
) -> None:
    profile = {
        "retain": {
            "enabled": True,
            "segment_max_bytes": 348,
            "tags": ["project:sample"],
        },
        "outbox": {"max_pending_rows": 2, "max_pending_bytes": 1_372},
    }

    with pytest.raises(ConfigError, match="complete smallest retained event admission"):
        load_config(
            hermes_home=tmp_path / "aggregate-too-small",
            environ={},
            injected=profile,
        )

    profile["outbox"] = {"max_pending_rows": 2, "max_pending_bytes": 2_739}
    config = load_config(
        hermes_home=tmp_path / "aggregate-exact",
        environ={},
        injected=profile,
    )

    assert config.outbox.max_pending_bytes == 2_739


@pytest.mark.parametrize("enabled", [False, True])
def test_retain_tags_reject_non_scalar_unicode_with_a_sanitized_config_error(
    tmp_path: Path,
    enabled: bool,
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"retain": {"enabled": enabled, "tags": ["\ud800"]}},
        )

    message = str(caught.value)
    assert "retain.tags entry must contain only Unicode scalar values" in message
    assert "\ud800" not in message


def test_cross_field_queue_bounds_are_enforced(tmp_path: Path) -> None:
    assert OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES == 1024

    accepted = load_config(
        hermes_home=tmp_path / "accepted",
        environ={},
        injected={
            "retain": {"segment_max_bytes": 2048},
            "outbox": {"max_pending_bytes": 2048 + OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES},
        },
    )
    assert (
        accepted.retain.segment_max_bytes + OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES
        == accepted.outbox.max_pending_bytes
    )

    with pytest.raises(ConfigError, match="segment_max_bytes.*max_pending_bytes"):
        load_config(
            hermes_home=tmp_path / "one-byte-below",
            environ={},
            injected={
                "retain": {"segment_max_bytes": 2048},
                "outbox": {"max_pending_bytes": 2048 + OUTBOX_ROW_ACCOUNTING_ALLOWANCE_BYTES - 1},
            },
        )

    with pytest.raises(ConfigError, match="retry_initial_seconds.*retry_max_seconds"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={
                "outbox": {
                    "retry_initial_seconds": 3.0,
                    "retry_max_seconds": 2.0,
                }
            },
        )


@pytest.mark.parametrize("poll_interval", [0.1, 60.0])
def test_poll_interval_inclusive_bounds_are_accepted(tmp_path: Path, poll_interval: float) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={"outbox": {"poll_interval_seconds": poll_interval}},
    )

    assert config.outbox.poll_interval_seconds == poll_interval


def test_configurable_ceiling_values_are_accepted(tmp_path: Path) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "retain": {"timeout_seconds": 300.0},
            "outbox": {
                "max_pending_rows": 100_000,
                "max_pending_bytes": 1_073_741_824,
                "retry_initial_seconds": 3_600.0,
                "retry_max_seconds": 3_600.0,
            },
        },
    )

    assert config.retain.timeout_seconds == 300.0
    assert config.outbox.max_pending_rows == 100_000
    assert config.outbox.max_pending_bytes == 1_073_741_824
    assert config.outbox.retry_initial_seconds == 3_600.0
    assert config.outbox.retry_max_seconds == 3_600.0


@pytest.mark.parametrize(
    "payload",
    [
        {"retain": {"timeout_seconds": float("nan")}},
        {"outbox": {"busy_timeout_seconds": float("nan")}},
        {"outbox": {"poll_interval_seconds": float("nan")}},
        {"outbox": {"retry_initial_seconds": float("nan")}},
        {"outbox": {"retry_max_seconds": float("nan")}},
    ],
)
def test_new_float_fields_reject_nan(tmp_path: Path, payload: Mapping[str, object]) -> None:
    with pytest.raises(ConfigError):
        load_config(hermes_home=tmp_path, environ={}, injected=payload)


def test_profile_json_rejects_non_finite_outbox_timing(tmp_path: Path) -> None:
    _write_config(tmp_path, {"outbox": {"poll_interval_seconds": float("nan")}})

    with pytest.raises(ConfigError, match="outbox.poll_interval_seconds"):
        load_config(hermes_home=tmp_path, environ={})


def test_malformed_url_does_not_leak_parser_values_through_exception_chaining(
    tmp_path: Path,
) -> None:
    marker = "DO_NOT_ECHO_PORT_VALUE"

    with pytest.raises(ConfigError) as caught:
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"api_url": f"https://service.example.test:{marker}"},
        )

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "principal",
    [
        {
            "platform": "sample-gateway",
            "identifier_kind": "chat_id",
            "identifier": "sample-user",
        },
        {
            "platform": "",
            "identifier_kind": "user_id",
            "identifier": "sample-user",
        },
        {
            "platform": "sample-gateway",
            "identifier_kind": "user_id",
            "identifier": "",
        },
        {
            "platform": " sample-gateway",
            "identifier_kind": "user_id",
            "identifier": "sample-user",
        },
    ],
)
def test_malformed_authorized_principal_is_rejected(tmp_path: Path, principal: object) -> None:
    with pytest.raises(ConfigError, match="allowed_principals"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"single_principal": True, "allowed_principals": [principal]},
        )


def test_duplicate_authorized_principal_is_rejected(tmp_path: Path) -> None:
    principal = {
        "platform": "sample-gateway",
        "identifier_kind": "user_id",
        "identifier": "sample-user",
    }

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={
                "single_principal": True,
                "allowed_principals": [principal, principal],
            },
        )


def test_shared_scope_requires_explicit_single_principal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="single_principal"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"retain": {"observation_scopes": "shared"}},
        )


def test_gateway_authorization_uses_exact_platform_and_identifier_kind(
    tmp_path: Path,
) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "single_principal": True,
            "retain": {"enabled": True},
            "allowed_principals": [
                {
                    "platform": "sample-gateway",
                    "identifier_kind": "user_id",
                    "identifier": "same-looking-value",
                },
                {
                    "platform": "sample-gateway",
                    "identifier_kind": "user_id_alt",
                    "identifier": "alternate-value",
                },
            ],
        },
    )

    primary = config.authorize_gateway(
        platform="sample-gateway",
        user_id="same-looking-value",
        agent_context="primary",
    )
    alternate = config.authorize_gateway(
        platform="sample-gateway",
        user_id_alt="alternate-value",
        agent_context="primary",
    )
    wrong_kind = config.authorize_gateway(
        platform="sample-gateway",
        user_id_alt="same-looking-value",
        agent_context="primary",
    )
    wrong_platform = config.authorize_gateway(
        platform="other-gateway",
        user_id="same-looking-value",
        agent_context="primary",
    )
    context_text_only = config.authorize_gateway(
        platform="sample-gateway",
        agent_context="primary:sample-gateway:same-looking-value",
    )

    assert primary.memory_enabled is True
    assert primary.recall_enabled is True
    assert primary.retain_enabled is True
    assert alternate.memory_enabled is True
    assert wrong_kind.memory_enabled is False
    assert wrong_platform.memory_enabled is False
    assert context_text_only.memory_enabled is False


def test_agent_context_is_only_a_separate_write_gate(tmp_path: Path) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "single_principal": True,
            "reflect": {"enabled": True},
            "retain": {"enabled": True},
            "allowed_principals": [
                {
                    "platform": "sample-gateway",
                    "identifier_kind": "user_id",
                    "identifier": "sample-user",
                }
            ],
        },
    )

    authorization = config.authorize_gateway(
        platform="sample-gateway",
        user_id="sample-user",
        agent_context="secondary",
    )

    assert authorization.recall_enabled is True
    assert authorization.reflect_enabled is True
    assert authorization.retain_enabled is False
    assert authorization.identity_authorized is True
    assert not hasattr(authorization, "agent_context")


def test_reflect_only_authorization_enables_memory_for_an_exact_identity(tmp_path: Path) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "single_principal": True,
            "recall": {"enabled": False},
            "reflect": {"enabled": True},
            "retain": {"enabled": False},
        },
    )

    authorized = config.authorize_cli(agent_context="secondary")
    unauthorized = load_config(
        hermes_home=tmp_path / "unauthorized",
        environ={},
        injected={
            "single_principal": False,
            "recall": {"enabled": False},
            "reflect": {"enabled": True},
            "retain": {"enabled": False},
        },
    ).authorize_cli(agent_context="primary")

    assert authorized.identity_authorized is True
    assert authorized.recall_enabled is False
    assert authorized.reflect_enabled is True
    assert authorized.retain_enabled is False
    assert authorized.memory_enabled is True
    assert unauthorized.reflect_enabled is False
    assert unauthorized.memory_enabled is False


def test_cli_requires_explicit_single_principal_declaration(tmp_path: Path) -> None:
    implicit = load_config(hermes_home=tmp_path, environ={})
    explicit = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={"single_principal": True},
    )

    assert implicit.authorize_cli(agent_context="primary").memory_enabled is False
    assert explicit.authorize_cli(agent_context="primary").memory_enabled is True


def test_static_config_never_enables_multi_user_routing(tmp_path: Path) -> None:
    config = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={
            "single_principal": False,
            "allowed_principals": [
                {
                    "platform": "sample-gateway",
                    "identifier_kind": "user_id",
                    "identifier": "sample-user",
                }
            ],
        },
    )

    authorization = config.authorize_gateway(
        platform="sample-gateway",
        user_id="sample-user",
        agent_context="primary",
    )

    assert authorization.memory_enabled is False


def test_destination_fingerprint_excludes_api_key_and_uses_normalized_destination(
    tmp_path: Path,
) -> None:
    common = {"bank_id": "sample-bank"}
    first = load_config(
        hermes_home=tmp_path,
        environ={"HINDSIGHT_API_KEY": "first-key-value"},
        injected={
            **common,
            "api_url": "HTTPS://SERVICE.EXAMPLE.TEST:443/api/",
        },
    )
    second = load_config(
        hermes_home=tmp_path,
        environ={"HINDSIGHT_API_KEY": "second-key-value"},
        injected={
            **common,
            "api_url": "https://service.example.test/api",
        },
    )
    other_bank = load_config(
        hermes_home=tmp_path,
        environ={},
        injected={**common, "bank_id": "other-bank"},
    )
    assert first.destination_fingerprint == second.destination_fingerprint
    assert first.destination_fingerprint != other_bank.destination_fingerprint
    assert "first-key-value" not in first.destination_fingerprint


def test_retain_transport_policy_is_canonical_and_fingerprint_bound(tmp_path: Path) -> None:
    raw_tag_value = "SYNTHETIC_TRANSPORT_TAG_VALUE"
    redacted_tag = f"api_key={raw_tag_value}"
    common = {
        "api_url": "https://service.example.test/api",
        "bank_id": "sample-bank",
        "single_principal": True,
        "retain": {
            "tags": ["zeta", redacted_tag, "alpha"],
            "observation_scopes": "shared",
        },
    }
    first = load_config(
        hermes_home=tmp_path / "first",
        environ={"HINDSIGHT_API_KEY": "first-key-value"},
        injected={
            **common,
            "api_url": "HTTPS://SERVICE.EXAMPLE.TEST:443/api/",
            "outbox": {"poll_interval_seconds": 0.25},
        },
    )
    equivalent = load_config(
        hermes_home=tmp_path / "equivalent",
        environ={"HINDSIGHT_API_KEY": "second-key-value"},
        injected={
            **common,
            "api_url": "https://service.example.test/api",
            "retain": {
                "tags": [redacted_tag, "alpha", "zeta"],
                "observation_scopes": [[]],
                "timeout_seconds": 299.0,
            },
            "outbox": {
                "poll_interval_seconds": 59.0,
                "retry_initial_seconds": 9.0,
                "retry_max_seconds": 99.0,
            },
        },
    )
    changed_tags = load_config(
        hermes_home=tmp_path / "changed-tags",
        environ={},
        injected={
            **common,
            "retain": {
                "tags": ["alpha", "different"],
                "observation_scopes": "shared",
            },
        },
    )
    changed_scopes = load_config(
        hermes_home=tmp_path / "changed-scopes",
        environ={},
        injected={
            **common,
            "retain": {
                "tags": ["zeta", redacted_tag, "alpha"],
                "observation_scopes": "combined",
            },
        },
    )

    assert first.retain.tags == ("alpha", f"api_key={REDACTION_MARKER}", "zeta")
    assert first.retain.tags == equivalent.retain.tags
    assert first.destination_fingerprint == equivalent.destination_fingerprint
    assert first.destination_fingerprint != changed_tags.destination_fingerprint
    assert first.destination_fingerprint != changed_scopes.destination_fingerprint
    assert raw_tag_value not in repr(first)
    assert raw_tag_value not in first.destination_fingerprint
    assert len(first.destination_fingerprint) == hashlib.sha256().digest_size * 2


def test_distinct_retain_tags_colliding_after_redaction_fail_with_fixed_error(
    tmp_path: Path,
) -> None:
    first_value = "SYNTHETIC_COLLISION_VALUE_ONE"
    second_value = "SYNTHETIC_COLLISION_VALUE_TWO"

    with pytest.raises(ConfigError) as caught:
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={
                "retain": {
                    "tags": [
                        f"api_key={first_value}",
                        f"api_key={second_value}",
                    ]
                }
            },
        )

    assert str(caught.value) == (
        "Better Hindsight configuration error: "
        "retain.tags contain distinct entries that collide after redaction"
    )
    assert first_value not in str(caught.value)
    assert second_value not in str(caught.value)
    assert caught.value.__cause__ is None
