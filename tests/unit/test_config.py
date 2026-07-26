"""Contract tests for Better Hindsight's pure typed configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from better_hermes_hindsight.config import (
    DEFAULT_API_URL,
    DEFAULT_BANK_ID,
    PAYLOAD_SCHEMA_VERSION,
    ConfigError,
    load_config,
)


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
        ({"recall": {"unexpected": True}}, "recall.unexpected"),
        ({"retain": {"unexpected": True}}, "retain.unexpected"),
        ({"retain": {"extraction_mode": "custom"}}, "retain.extraction_mode"),
        ({"missions": {"unexpected": True}}, "missions.unexpected"),
        ({"missions": {"reflect_mission": "not supported"}}, "missions.reflect_mission"),
        ({"outbox": {"unexpected": True}}, "outbox.unexpected"),
        ({"authorized_principals": []}, "authorized_principals"),
        ({"recall": {"tags_match": "all"}}, "recall.tags_match"),
        ({"retain": {"tags_mode": "append"}}, "retain.tags_mode"),
        ({"outbox": {"payload_schema": "turn-v2"}}, "outbox.payload_schema"),
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
            "integration_mode": "context",
            "recall": {
                "enabled": False,
                "query_projection": "head_tail",
                "timeout_seconds": 2.25,
                "input_max_chars": 3210,
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
            "retain": {
                "enabled": False,
                "segment_max_bytes": 70000,
                "observation_scopes": "shared",
                "tags": ["source:sample"],
            },
            "missions": {
                "policy": "check",
                "retain_mission": "Retain durable user preferences.",
                "observations_mission": "Consolidate stable observations.",
            },
            "outbox": {
                "path": "better_hindsight/custom-outbox.sqlite3",
                "busy_timeout_seconds": 0.5,
            },
        },
    )

    config = load_config(hermes_home=tmp_path, environ={})

    assert config.integration_mode == "context"
    assert config.recall.enabled is False
    assert config.recall.query_projection == "head_tail"
    assert config.recall.timeout_seconds == 2.25
    assert config.recall.input_max_chars == 3210
    assert config.recall.context_max_bytes == 6543
    assert config.recall.budget == "low"
    assert config.recall.max_tokens == 777
    assert config.recall.types == ("observation",)
    assert config.recall.tags == ("project:sample",)
    assert config.recall.tag_mode == "all_strict"
    assert config.recall.tags_match == "all_strict"
    assert config.recall.prefer_observations is True
    assert config.recall.min_scores is not None
    assert config.recall.min_scores.as_dict() == {"semantic": 0.1, "final": 1.2}
    assert config.recall.include_source_facts is True
    assert config.recall.max_source_facts_tokens == 222
    assert config.retain.enabled is False
    assert config.retain.segment_max_bytes == 70000
    assert config.retain.observation_scopes == ((),)
    assert config.retain.tags == ("source:sample",)
    assert config.missions.policy == "check"
    assert config.missions.retain_mission == "Retain durable user preferences."
    assert config.missions.observations_mission == "Consolidate stable observations."
    assert config.outbox.path == tmp_path.resolve() / "better_hindsight/custom-outbox.sqlite3"
    assert config.outbox.payload_schema == PAYLOAD_SCHEMA_VERSION
    assert config.outbox.busy_timeout_seconds == 0.5
    assert config.outbox.busy_timeout_ms == 500
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
    assert config.recall.query_projection == "head_tail"
    assert config.recall.max_tokens is None
    assert config.recall.types is None
    assert config.recall.tags is None
    assert config.recall.tag_mode is None
    assert config.recall.prefer_observations is None
    assert config.recall.min_scores is None
    assert config.recall.include_source_facts is None
    assert config.recall.max_source_facts_tokens is None
    assert config.retain.observation_scopes is None
    assert config.retain.segment_max_bytes == 65536
    assert config.outbox.payload_schema == PAYLOAD_SCHEMA_VERSION


def test_mission_apply_is_an_operator_command_not_a_config_value(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path, {"missions": {"policy": "apply"}})

    with pytest.raises(ConfigError, match="off, check"):
        load_config(hermes_home=tmp_path, environ={})

    config_path = tmp_path / "better_hindsight" / "config.json"
    config_path.unlink()
    with pytest.raises(ConfigError, match="off, check"):
        load_config(
            hermes_home=tmp_path,
            environ={},
            injected={"missions": {"policy": "apply"}},
        )


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
        {"recall": {"context_max_bytes": 0}},
        {"recall": {"query_projection": "full"}},
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
        {"retain": {"segment_max_bytes": 0}},
        {"retain": {"tags": [f"tag-{index}" for index in range(65)]}},
        {"missions": {"policy": "startup-apply"}},
        {"outbox": {"busy_timeout_seconds": 0}},
        {"outbox": {"busy_timeout_seconds": 10**1000}},
        {"outbox": {"path": "invalid\0path.sqlite3"}},
        {"outbox": {"busy_timeout_seconds": 6}},
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
    assert authorization.retain_enabled is False
    assert authorization.agent_context == "secondary"


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
