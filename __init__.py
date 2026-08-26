"""Standard Hermes memory-plugin entry point for Better Hindsight."""

from typing import Protocol

if __package__:
    from .better_hermes_hindsight.formatting import SYSTEM_PROMPT_BLOCK
    from .better_hermes_hindsight.provider import create_provider
else:  # Direct source import by test and inspection tools.
    from better_hermes_hindsight.formatting import SYSTEM_PROMPT_BLOCK
    from better_hermes_hindsight.provider import create_provider


class _RegistrationContext(Protocol):
    def register_memory_provider(self, provider: object) -> None: ...


def register(ctx: _RegistrationContext) -> None:
    """Register one provider instance when loaded by the memory-plugin host."""

    register_system_prompt_section = getattr(ctx, "register_system_prompt_section", None)

    def register_trust_policy() -> object | None:
        if not callable(register_system_prompt_section):
            return None
        return register_system_prompt_section(
            "better_hindsight.recall_trust_policy",
            SYSTEM_PROMPT_BLOCK,
            position="after_memory",
            max_chars=len(SYSTEM_PROMPT_BLOCK),
        )

    register_memory_provider = getattr(ctx, "register_memory_provider", None)
    if callable(register_memory_provider):
        register_memory_provider(
            create_provider(
                system_prompt_section_registrar=register_trust_policy,
            )
        )


__all__ = ["register"]
