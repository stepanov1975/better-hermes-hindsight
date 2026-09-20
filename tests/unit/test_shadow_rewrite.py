"""Worker resource bounds survive import aliases, failures and process shutdown."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread

import pytest

from better_hermes_hindsight import shadow_rewrite as worker
from tests.integration.helpers import clean_subprocess_env


def test_import_alias_cannot_bypass_busy_slot() -> None:
    spec = importlib.util.spec_from_file_location("fixture_shadow_alias", worker.__file__)
    assert spec is not None and spec.loader is not None
    alias = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alias)
    entered, release = Event(), Event()

    def blocked() -> None:
        entered.set()
        assert release.wait(5)

    assert worker.submit_shadow(blocked) == "submitted"
    try:
        assert entered.wait(2)
        for _ in range(100):
            assert alias.submit_shadow(lambda: pytest.fail("unbounded worker")) == "busy"
    finally:
        release.set()
        assert worker.drain_shadow_for_tests()
    assert alias.submit_shadow(lambda: None) == "submitted"
    assert worker.drain_shadow_for_tests()


def test_start_failure_and_call_exception_release_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic private failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(Thread, "start", fail)
        assert worker.submit_shadow(lambda: None) == "unavailable"
    assert worker.submit_shadow(fail) == "submitted"
    assert worker.drain_shadow_for_tests()
    assert worker.submit_shadow(lambda: None) == "submitted"
    assert worker.drain_shadow_for_tests()


def test_stuck_shadow_does_not_hold_process_exit(tmp_path: Path) -> None:
    script = """
from threading import Event
from better_hermes_hindsight.shadow_rewrite import submit_shadow
entered = Event()
def stuck():
    entered.set()
    Event().wait()
assert submit_shadow(stuck) == "submitted"
assert entered.wait(2)
print("exit without joining shadow")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=clean_subprocess_env(
            tmp_path,
            hermes_home=tmp_path / "hermes",
            no_proxy="*",
            extra={"PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        ),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "exit without joining shadow"
