"""Dual-surface plugin registration tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from better_hermes_hindsight.planner import RECALL_PLANNER_TASK

ROOT = Path(__file__).resolve().parents[2]


def _load_plugin_entrypoint() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_better_hindsight_dual_surface_entrypoint",
        ROOT / "__init__.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _State:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _GeneralContext:
    def __init__(self, home: Path) -> None:
        self.state = _State(home / "plugin-data" / "better_hindsight")
        self.llm = object()
        self.tasks: list[tuple[str, str, str, dict[str, object] | None]] = []
        self.hooks: list[tuple[str, object]] = []

    def register_auxiliary_task(
        self,
        key: str,
        *,
        display_name: str,
        description: str,
        defaults: dict[str, object] | None = None,
    ) -> object:
        self.tasks.append((key, display_name, description, defaults))
        return object()

    def register_hook(self, name: str, callback: object) -> object:
        self.hooks.append((name, callback))
        return object()


class _MemoryContext:
    def __init__(self) -> None:
        self.providers: list[object] = []
        self.hook_attempts = 0
        self.task_attempts = 0

    def register_memory_provider(self, provider: object) -> None:
        self.providers.append(provider)

    def register_system_prompt_section(self, *_args: object, **_kwargs: object) -> object:
        return object()

    def register_hook(self, *_args: object, **_kwargs: object) -> object:
        self.hook_attempts += 1
        raise AssertionError("memory discovery must not duplicate the companion hook")

    def register_auxiliary_task(self, *_args: object, **_kwargs: object) -> object:
        self.task_attempts += 1
        raise AssertionError("memory discovery must not duplicate the companion task")


def test_general_plugin_surface_registers_one_planner_hook_and_task(tmp_path: Path) -> None:
    plugin = _load_plugin_entrypoint()
    context = _GeneralContext(tmp_path)

    plugin.register(context)

    assert len(context.tasks) == 1
    assert context.tasks[0][0] == RECALL_PLANNER_TASK
    assert context.tasks[0][1] == "Better Hindsight recall planner"
    assert context.tasks[0][3] == {"temperature": 0.0, "max_tokens": 128}
    assert len(context.hooks) == 1
    assert context.hooks[0][0] == "pre_llm_call"
    assert callable(context.hooks[0][1])


def test_memory_surface_registers_provider_only() -> None:
    plugin = _load_plugin_entrypoint()
    context = _MemoryContext()

    plugin.register(context)

    assert len(context.providers) == 1
    assert context.hook_attempts == 0
    assert context.task_attempts == 0
