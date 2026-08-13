"""Public package metadata for Better Hermes Hindsight."""

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION_NAME = "better-hermes-hindsight"
PROVIDER_ID = "better_hindsight"

try:
    __version__ = version(DISTRIBUTION_NAME)
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.1.0a2"

__all__ = ["DISTRIBUTION_NAME", "PROVIDER_ID", "__version__"]
