"""Standard Hermes CLI entry point for Better Hindsight."""

if __package__:
    from .better_hermes_hindsight.operator_cli import better_hindsight_command, register_cli
else:  # Direct source import by test and inspection tools.
    from better_hermes_hindsight.operator_cli import better_hindsight_command, register_cli

__all__ = ["better_hindsight_command", "register_cli"]
