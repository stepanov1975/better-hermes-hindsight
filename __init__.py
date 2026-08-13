"""Hermes root-plugin bridge for Better Hindsight provider registration."""

from __future__ import annotations

from typing import Protocol

from better_hermes_hindsight.provider import create_provider


class _RegistrationContext(Protocol):
    def register_memory_provider(self, provider: object) -> None:
        """Register one memory provider with released Hermes."""


def register(ctx: _RegistrationContext) -> None:
    """Register the installed wheel's provider through Hermes's public plugin context."""

    register_provider = getattr(ctx, "register_memory_provider", None)
    if callable(register_provider):
        register_provider(create_provider())


__all__ = ["register"]
