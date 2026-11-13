#!/usr/bin/env python3
"""Install a selected Hermes checkout into a disposable compatibility environment.

Honor the selected host's own dependency overrides, not Better-owned exceptions.
This helper is for test environments only; it never selects or updates a live home.
"""

from __future__ import annotations

import argparse
import runpy
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import cast


def install_host(host: Path, python: Path) -> None:
    host = host.resolve()
    python = python.absolute()
    with (host / "pyproject.toml").open("rb") as stream:
        overrides = (
            tomllib.load(stream).get("tool", {}).get("uv", {}).get("override-dependencies", [])
        )
    with tempfile.TemporaryDirectory(prefix="better-host-overrides-") as temporary:
        command = ["uv", "pip", "install", "--python", str(python)]
        if overrides:
            override_file = Path(temporary) / "overrides.txt"
            override_file.write_text("\n".join(overrides) + "\n", encoding="utf-8")
            command.extend(["--overrides", str(override_file)])
        subprocess.run([*command, "-e", str(host)], check=True)
    checked = subprocess.run(
        ["uv", "pip", "check", "--python", str(python)],
        check=False,
        capture_output=True,
        text=True,
    )
    report = checked.stdout + checked.stderr
    print(report, end="")
    if checked.returncode == 0:
        return
    checker = host / "pm" / "pip_check.py"
    if checked.returncode == 1 and checker.is_file():
        classify = cast(
            Callable[[str, Path], list[str] | None],
            runpy.run_path(str(checker))["unexpected_incompatibilities"],
        )
        if classify(report, host / "uv.lock") == []:
            print("Only dependency overrides recorded in the selected Hermes lock were reported.")
            return
    raise subprocess.CalledProcessError(checked.returncode, checked.args, output=report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("host", type=Path)
    parser.add_argument("--python", type=Path, default=Path(".venv/bin/python"))
    args = parser.parse_args()
    install_host(args.host, args.python)


if __name__ == "__main__":
    main()
