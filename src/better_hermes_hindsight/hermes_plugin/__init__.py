"""Filesystem shim consumed by the released Hermes memory-provider loader."""

from typing import Protocol

from better_hermes_hindsight.provider import create_provider


class _RegistrationContext(Protocol):
    def register_memory_provider(self, provider: object) -> None: ...


def register(ctx: _RegistrationContext) -> None:
    """Register exactly one zero-argument Better Hindsight provider."""

    register_provider = getattr(ctx, "register_memory_provider", None)
    if callable(register_provider):
        register_provider(create_provider())


__all__ = ["register"]
