"""Standard Hermes memory-plugin entry point for Better Hindsight."""

from pathlib import Path
from typing import Protocol, cast

if __package__:
    from .better_hermes_hindsight.formatting import SYSTEM_PROMPT_BLOCK
    from .better_hermes_hindsight.planner import (
        _RegistrationContext as _PlannerRegistrationContext,
    )
    from .better_hermes_hindsight.planner import register_companion
    from .better_hermes_hindsight.provider import create_provider
else:  # Direct source import by test and inspection tools.
    from better_hermes_hindsight.formatting import SYSTEM_PROMPT_BLOCK
    from better_hermes_hindsight.planner import (
        _RegistrationContext as _PlannerRegistrationContext,
    )
    from better_hermes_hindsight.planner import register_companion
    from better_hermes_hindsight.provider import create_provider


class _RegistrationContext(Protocol):
    def register_memory_provider(self, provider: object) -> None: ...


def register(ctx: _RegistrationContext) -> None:
    """Register the provider or its companion, depending on the host surface."""

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

    register_hook = getattr(ctx, "register_hook", None)
    register_auxiliary_task = getattr(ctx, "register_auxiliary_task", None)
    state = getattr(ctx, "state", None)
    llm = getattr(ctx, "llm", None)
    data_dir = getattr(state, "data_dir", None)
    if (
        callable(register_hook)
        and callable(register_auxiliary_task)
        and llm is not None
        and isinstance(data_dir, Path)
        and data_dir.parent.name == "plugin-data"
    ):
        register_companion(
            cast(_PlannerRegistrationContext, ctx),
            data_dir.parent.parent.resolve(),
        )
        return

    register_memory_provider = getattr(ctx, "register_memory_provider", None)
    if callable(register_memory_provider):
        register_memory_provider(
            create_provider(
                system_prompt_section_registrar=register_trust_policy,
            )
        )


__all__ = ["register"]
