"""Hermes CLI adapter for Better Hindsight operator commands."""

from __future__ import annotations

import json
import os
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .management import ManagementResult

_MAX_JSON_BYTES = 1024


def register_cli(parser: ArgumentParser) -> None:
    """Register the bounded operator command grammar."""

    commands = parser.add_subparsers(dest="better_hindsight_action", required=True)
    commands.add_parser("status", help="Inspect the local retention outbox")
    commands.add_parser("canary", help="Run the explicitly enabled synthetic Hindsight canary")

    watchdog = commands.add_parser(
        "watchdog",
        help="Evaluate bounded status and canary artifacts",
        allow_abbrev=False,
    )
    from .watchdog import register_cli_arguments

    register_cli_arguments(watchdog)

    missions = commands.add_parser("missions", help="Check or explicitly apply bank missions")
    mission_actions = missions.add_subparsers(
        dest="better_hindsight_mission_action",
        required=True,
    )
    mission_actions.add_parser("check", help="Compare configured and remote missions")
    apply_parser = mission_actions.add_parser(
        "apply",
        help="Apply configured mission drift and verify it",
        allow_abbrev=False,
    )
    apply_parser.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="Confirm this remote write attempt",
    )


def better_hindsight_command(args: Namespace) -> None:
    """Execute one management command and preserve Hermes exit behavior."""

    action = getattr(args, "better_hindsight_action", None)
    if action == "canary":
        from .canary import main as canary_main

        _finish(canary_main())
        return
    if action == "watchdog":
        from .watchdog import run_from_namespace

        _finish(run_from_namespace(args))
        return

    command = _command_name(args)
    try:
        from .config import load_config

        config = load_config(
            hermes_home=_selected_hermes_home(),
            environ=os.environ,
        )
    except Exception:
        result = _fixed_result(command, "configuration_invalid")
    else:
        try:
            from .management import apply_missions, check_missions, status

            if command == "status":
                result = status(config)
            elif command == "missions_check":
                result = check_missions(config)
            else:
                result = apply_missions(config, confirmed=args.confirm is True)
        except Exception:
            error = {
                "status": "status_unavailable",
                "missions_check": "mission_check_unavailable",
                "missions_apply": "mission_prewrite_unavailable",
            }[command]
            result = _fixed_result(command, error)

    encoded = _canonical_json(result.payload)
    print(encoded)
    if result.exit_code:
        raise SystemExit(result.exit_code)


def _finish(exit_code: int) -> None:
    if exit_code:
        raise SystemExit(exit_code)


def _command_name(args: Namespace) -> str:
    action = getattr(args, "better_hindsight_action", None)
    if action == "status":
        return "status"
    if action == "missions":
        mission_action = getattr(args, "better_hindsight_mission_action", None)
        if mission_action == "check":
            return "missions_check"
        if mission_action == "apply":
            return "missions_apply"
    raise SystemExit(2)


def _selected_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _fixed_result(command: str, error: str) -> ManagementResult:
    from .management import ManagementResult

    return ManagementResult(
        payload={"command": command, "error": error, "result": "error"},
        exit_code=3,
    )


def _canonical_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise RuntimeError("Better Hindsight management output exceeded its fixed bound.")
    return encoded


__all__ = ["better_hindsight_command", "register_cli"]
