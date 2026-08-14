#!/usr/bin/env python3
"""Verify release distributions contain only the intended package payload."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("sdist", type=Path)
    args = parser.parse_args()
    with tarfile.open(args.sdist, "r:gz") as archive:
        names = archive.getnames()
    if any("/tests/" in name or name.endswith("/tests") for name in names):
        raise SystemExit("sdist must not contain repository tests")
    required = (
        "/pyproject.toml",
        "/README.md",
        "/__init__.py",
        "/after-install.md",
        "/cli.py",
        "/plugin.yaml",
        "/better_hermes_hindsight/__init__.py",
        "/better_hermes_hindsight/provider.py",
    )
    missing = [suffix for suffix in required if not any(name.endswith(suffix) for name in names)]
    if missing:
        raise SystemExit("sdist is missing required payload: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
