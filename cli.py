"""Hermes root-plugin bridge for Better Hindsight operator commands."""

from better_hermes_hindsight.hermes_plugin.cli import (
    better_hindsight_command,
    register_cli,
)

__all__ = ["better_hindsight_command", "register_cli"]
