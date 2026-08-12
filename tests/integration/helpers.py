"""Small shared primitives for isolated Hermes integration subprocesses."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

_INHERITED_ENVIRONMENT = (
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONASYNCIODEBUG",
    "PYTHONTRACEMALLOC",
    "PYTHONWARNINGS",
    "TMPDIR",
    "TZ",
)


def clean_subprocess_env(
    root: Path,
    *,
    hermes_home: Path | None,
    no_proxy: str,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a finite environment for one isolated Hermes subprocess."""

    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    environment = {name: os.environ[name] for name in _INHERITED_ENVIRONMENT if name in os.environ}
    environment.update(
        {
            "HOME": str(home),
            "NO_PROXY": no_proxy,
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "no_proxy": no_proxy,
        }
    )
    if hermes_home is not None:
        environment["HERMES_HOME"] = str(hermes_home)
    if extra is not None:
        environment.update(extra)
    return environment


def materialize_packaged_shim(
    *,
    source: Path,
    hermes_home: Path,
    names: Sequence[str],
) -> Path:
    """Copy the requested packaged plugin bridge files into a Hermes profile."""

    destination = hermes_home / "plugins" / "better_hindsight"
    destination.mkdir(parents=True, exist_ok=True)
    for name in names:
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, destination / name)
    return destination


def write_host_selection(hermes_home: Path, provider: str = "better_hindsight") -> None:
    """Select one memory provider in an isolated Hermes profile."""

    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        f"memory:\n  provider: {provider}\n",
        encoding="utf-8",
    )
