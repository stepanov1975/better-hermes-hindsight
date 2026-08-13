"""Immutable ordinary-user release installer contracts."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import install_release

_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_VERSION = "0.1.0a3"


def _artifact_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    runtime_python = tmp_path / "runtime/bin/python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("fixture runtime\n", encoding="utf-8")
    python = tmp_path / "venv/bin/python"
    hermes = python.with_name("hermes")
    python.parent.mkdir(parents=True)
    python.symlink_to(runtime_python)
    hermes.write_text("fixture\n", encoding="utf-8")
    wheel = tmp_path / f"better_hermes_hindsight-{_VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"synthetic immutable wheel")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(
        f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n",
        encoding="ascii",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in install_release._BRIDGE_FILES:
        (source / name).write_text(f"fixture {name}\n", encoding="utf-8")
    return python, wheel, sums, source


def _git(source: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def test_source_identity_requires_exact_release_tag_and_clean_checkout(tmp_path: Path) -> None:
    source = tmp_path / "release"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.1.0a3"\n',
        encoding="utf-8",
    )
    _git(source, "init", "--quiet")
    _git(source, "config", "user.name", "Release fixture")
    _git(source, "config", "user.email", "release@example.invalid")
    _git(source, "add", "pyproject.toml")
    _git(source, "commit", "--quiet", "-m", "release fixture")
    _git(source, "tag", "v0.1.0a3")

    version, commit = install_release._source_identity(source)
    assert version == _VERSION
    assert commit == _git(source, "rev-parse", "HEAD").stdout.strip()

    (source / "downloaded-wheel.whl").write_bytes(b"must stay outside checkout")
    with pytest.raises(ValueError, match="must be clean"):
        install_release._source_identity(source)


def test_installer_binds_profile_to_dedicated_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python, wheel, sums, source = _artifact_fixture(tmp_path)
    launcher = tmp_path / "bin/better-hindsight-test"
    home = tmp_path / "home"
    calls: list[list[str]] = []

    monkeypatch.setattr(
        install_release,
        "_source_identity",
        lambda _source: (_VERSION, _COMMIT),
    )
    monkeypatch.setattr(install_release, "_find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(install_release, "_user_home", lambda: home)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    def fake_run(
        command: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check
        calls.append(command)
        if command[-2:] == ["--force", "--enable"]:
            plugin = home / ".hermes/profiles/better-hindsight-test/plugins/better_hindsight"
            plugin.mkdir(parents=True)
            for name in install_release._BRIDGE_FILES:
                shutil.copy2(source / name, plugin / name)
        return_code = 1 if command[-3:] == ["profile", "show", "better-hindsight-test"] else 0
        stdout = ""
        if command[-2:] == ["get", "memory.provider"]:
            stdout = "better_hindsight\n"
        elif command[0] == str(python.absolute()) and command[1] == "-c":
            stdout = f"{_VERSION}\n"
        return subprocess.CompletedProcess(command, return_code, stdout, "")

    monkeypatch.setattr(install_release, "_run", fake_run)

    assert (
        install_release.main(
            [
                "--profile",
                "better-hindsight-test",
                "--hermes-python",
                str(python),
                "--wheel",
                str(wheel),
                "--sha256sums",
                str(sums),
                "--source-dir",
                str(source),
                "--launcher",
                str(launcher),
            ]
        )
        == 0
    )

    hermes = str(python.absolute().with_name("hermes"))
    assert [
        "/usr/bin/uv",
        "pip",
        "install",
        "--python",
        str(python.absolute()),
        str(wheel.resolve()),
    ] in calls
    assert [
        hermes,
        "profile",
        "create",
        "better-hindsight-test",
        "--no-alias",
        "--no-skills",
        "--description",
        "Dedicated Better Hindsight memory profile.",
    ] in calls
    assert [
        hermes,
        "--profile",
        "better-hindsight-test",
        "plugins",
        "install",
        source.resolve().as_uri(),
        "--force",
        "--enable",
    ] in calls
    assert launcher.read_text(encoding="utf-8") == (
        f'#!/bin/sh\nexec {hermes} -p better-hindsight-test "$@"\n'
    )
    assert launcher.stat().st_mode & 0o777 == 0o755
    metadata_path = home / ".hermes/profiles/better-hindsight-test/better_hindsight/install.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata == {"commit": _COMMIT, "version": _VERSION}


def test_hermes_home_environment_controls_profile_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "custom-hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(configured))
    monkeypatch.setattr(install_release, "_user_home", lambda: tmp_path / "ignored-home")

    assert install_release._hermes_home() == configured


def test_installer_rejects_wheel_checksum_mismatch_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python, wheel, sums, source = _artifact_fixture(tmp_path)
    sums.write_text(f"{'0' * 64}  {wheel.name}\n", encoding="ascii")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        install_release,
        "_source_identity",
        lambda _source: (_VERSION, _COMMIT),
    )
    monkeypatch.setattr(install_release, "_find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(install_release, "_run", lambda command, **_kwargs: calls.append(command))

    with pytest.raises(SystemExit, match="2"):
        install_release.main(
            [
                "--profile",
                "better-hindsight-test",
                "--hermes-python",
                str(python),
                "--wheel",
                str(wheel),
                "--sha256sums",
                str(sums),
                "--source-dir",
                str(source),
            ]
        )

    assert calls == []


def test_failed_update_clears_previous_success_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python, wheel, sums, source = _artifact_fixture(tmp_path)
    hermes_home = tmp_path / "hermes-home"
    receipt = hermes_home / "profiles/test-profile/better_hindsight/install.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        '{"commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","version":"0.1.0a1"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        install_release,
        "_source_identity",
        lambda _source: (_VERSION, _COMMIT),
    )
    monkeypatch.setattr(install_release, "_find_uv", lambda: "/usr/bin/uv")

    def fail_first_mutation(
        command: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(install_release, "_run", fail_first_mutation)

    with pytest.raises(subprocess.CalledProcessError):
        install_release.main(
            [
                "--profile",
                "test-profile",
                "--hermes-python",
                str(python),
                "--wheel",
                str(wheel),
                "--sha256sums",
                str(sums),
                "--source-dir",
                str(source),
            ]
        )

    assert not receipt.exists()
