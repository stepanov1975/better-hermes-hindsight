from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.install_hermes_test_host import install_host


@pytest.mark.parametrize("overrides", [[], ["Pillow==13.0.0"]])
def test_install_preserves_selected_interpreter_and_host_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: list[str]
) -> None:
    import json

    host = tmp_path / "host"
    host.mkdir()
    (host / "pyproject.toml").write_text(
        "[tool.uv]\noverride-dependencies = " + json.dumps(overrides), encoding="utf-8"
    )
    python = tmp_path / "venv/bin/python"
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert command[3:5] == ["--python", str(python)]
        if command[2] == "install":
            assert command[-2:] == ["-e", str(host)]
            if overrides:
                index = command.index("--overrides")
                assert Path(command[index + 1]).read_text().splitlines() == overrides
            else:
                assert "--overrides" not in command
            assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    install_host(host, python)
    assert [call[2] for call in calls] == ["install", "check"]


@pytest.mark.parametrize("classification", [None, [], ["unexpected dependency"]])
def test_only_host_owned_exact_override_classification_accepts_a_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, classification: list[str] | None
) -> None:
    host = tmp_path / "host"
    (host / "pm").mkdir(parents=True)
    (host / "pyproject.toml").write_text("[project]\nname = 'host'\n", encoding="utf-8")
    (host / "pm/pip_check.py").write_text(
        "def unexpected_incompatibilities(report, lock):\n"
        "    assert report == 'complete check report'\n"
        "    assert lock.name == 'uv.lock'\n"
        f"    return {classification!r}\n",
        encoding="utf-8",
    )

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if command[2] == "install":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "complete check report", "")

    monkeypatch.setattr(subprocess, "run", run)
    if classification == []:
        install_host(host, tmp_path / "venv/bin/python")
    else:
        with pytest.raises(subprocess.CalledProcessError):
            install_host(host, tmp_path / "venv/bin/python")
