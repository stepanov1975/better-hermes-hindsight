"""Standard Hermes memory-plugin entry point for Better Hindsight."""

from typing import Protocol

if __package__:
    from .better_hermes_hindsight.provider import create_provider
else:  # Direct source import by test and inspection tools.
    from better_hermes_hindsight.provider import create_provider


class _RegistrationContext(Protocol):
    def register_memory_provider(self, provider: object) -> None: ...


def register(ctx: _RegistrationContext) -> None:
    """Register one provider instance when loaded by the memory-plugin host."""

    register_memory_provider = getattr(ctx, "register_memory_provider", None)
    if callable(register_memory_provider):
        register_memory_provider(create_provider())


__all__ = ["register"]
