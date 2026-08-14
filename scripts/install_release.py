#!/usr/bin/env python3
"""Install one verified Better Hindsight release into a selected Hermes interpreter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from contextlib import suppress
from pathlib import Path

_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BRIDGE_FILES = ("__init__.py", "cli.py", "plugin.yaml")


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def _find_uv() -> str | None:
    return shutil.which("uv")


def _user_home() -> Path:
    return Path.home()


def _absolute_path(path: Path) -> Path:
    """Return an absolute path without dereferencing a virtualenv symlink."""

    return Path(os.path.abspath(os.path.expanduser(path)))


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return _absolute_path(Path(configured)) if configured else _user_home() / ".hermes"


def _expected_digest(sums_path: Path, wheel_name: str) -> str:
    matches: list[str] = []
    for line in sums_path.read_text(encoding="ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if separator and name == wheel_name and re.fullmatch(r"[0-9a-f]{64}", digest):
            matches.append(digest)
    if len(matches) != 1:
        raise ValueError("wheel checksum entry missing or ambiguous")
    return matches[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(source_dir: Path) -> tuple[str, str]:
    project = tomllib.loads((source_dir / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    commit = _run(["git", "-C", str(source_dir), "rev-parse", "HEAD"]).stdout.strip().lower()
    if not _COMMIT.fullmatch(commit):
        raise ValueError("source checkout does not have a full Git commit")
    tag = _run(
        ["git", "-C", str(source_dir), "describe", "--tags", "--exact-match", "HEAD"],
        check=False,
    )
    if tag.returncode != 0 or tag.stdout.strip() != f"v{version}":
        raise ValueError(f"source checkout must be exact tag v{version}")
    if _run(["git", "-C", str(source_dir), "status", "--porcelain"]).stdout:
        raise ValueError("source checkout must be clean")
    return version, commit


def _write_private_json(path: Path, document: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _write_launcher(path: Path, hermes: Path, profile: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        f'#!/bin/sh\nexec {shlex.quote(str(hermes))} -p {shlex.quote(profile)} "$@"\n',
        encoding="utf-8",
    )
    os.chmod(temporary, 0o755)
    os.replace(temporary, path)


def _verify_install(
    *,
    python: Path,
    hermes: Path,
    profile: str,
    version: str,
    launcher: Path,
    source_dir: Path,
    profile_home: Path,
) -> None:
    installed_version = _run(
        [
            str(python),
            "-c",
            ("from importlib.metadata import version; print(version('better-hermes-hindsight'))"),
        ]
    ).stdout.strip()
    if installed_version != version:
        raise RuntimeError("installed package version does not match release")

    plugin_dir = profile_home / "plugins/better_hindsight"
    mismatched = [
        name
        for name in _BRIDGE_FILES
        if not (plugin_dir / name).is_file()
        or _sha256(plugin_dir / name) != _sha256(source_dir / name)
    ]
    if mismatched:
        raise RuntimeError(
            "installed plugin bridge does not match the tagged source: " + ", ".join(mismatched)
        )

    provider = _run(
        [
            str(hermes),
            "--profile",
            profile,
            "config",
            "get",
            "memory.provider",
        ]
    ).stdout
    if "better_hindsight" not in provider:
        raise RuntimeError("Better Hindsight is not the selected memory provider")
    expected_launcher = (
        f'#!/bin/sh\nexec {shlex.quote(str(hermes))} -p {shlex.quote(profile)} "$@"\n'
    )
    if launcher.read_text(encoding="utf-8") != expected_launcher:
        raise RuntimeError("generated launcher does not match the selected interpreter")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--hermes-python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sha256sums", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--launcher", type=Path)
    args = parser.parse_args(argv)

    if not _PROFILE.fullmatch(args.profile) or args.profile in {"default", "hermes", "root"}:
        parser.error("--profile must be a safe non-default Hermes profile name")

    source_dir = args.source_dir.resolve()
    python = _absolute_path(args.hermes_python)
    hermes = python.with_name("hermes")
    wheel = args.wheel.resolve()
    sums = args.sha256sums.resolve()
    launcher = (args.launcher or (_user_home() / ".local/bin" / args.profile)).resolve()
    uv = _find_uv()
    if uv is None or not python.is_file() or not hermes.is_file():
        parser.error("uv, the selected Python, and its sibling hermes launcher are required")

    version, commit = _source_identity(source_dir)
    expected_wheel = f"better_hermes_hindsight-{version}-py3-none-any.whl"
    if wheel.name != expected_wheel:
        parser.error(f"--wheel must be {expected_wheel}")
    if _sha256(wheel) != _expected_digest(sums, wheel.name):
        parser.error("wheel checksum mismatch")

    profile_home = _hermes_home() / "profiles" / args.profile
    receipt = profile_home / "better_hindsight/install.json"
    with suppress(FileNotFoundError):
        receipt.unlink()

    _run([uv, "pip", "install", "--python", str(python), str(wheel)])
    _run([uv, "pip", "check", "--python", str(python)])

    profile = _run([str(hermes), "profile", "show", args.profile], check=False)
    if profile.returncode != 0:
        _run(
            [
                str(hermes),
                "profile",
                "create",
                args.profile,
                "--no-alias",
                "--no-skills",
                "--description",
                "Dedicated Better Hindsight memory profile.",
            ]
        )
    _run(
        [
            str(hermes),
            "--profile",
            args.profile,
            "plugins",
            "install",
            source_dir.as_uri(),
            "--force",
            "--enable",
        ]
    )
    _run(
        [
            str(hermes),
            "--profile",
            args.profile,
            "config",
            "set",
            "memory.provider",
            "better_hindsight",
        ]
    )

    _write_launcher(launcher, hermes, args.profile)
    _verify_install(
        python=python,
        hermes=hermes,
        profile=args.profile,
        version=version,
        launcher=launcher,
        source_dir=source_dir,
        profile_home=profile_home,
    )
    _write_private_json(
        receipt,
        {"commit": commit, "version": version},
    )
    print(
        json.dumps(
            {
                "commit": commit,
                "launcher": str(launcher),
                "profile": args.profile,
                "version": version,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
