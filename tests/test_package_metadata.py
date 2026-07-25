"""Tests for stable public project metadata."""

from importlib.metadata import version

from better_hermes_hindsight import DISTRIBUTION_NAME, PROVIDER_ID, __version__


def test_distribution_version_matches_installed_metadata() -> None:
    assert __version__ == version(DISTRIBUTION_NAME)


def test_provider_id_is_distinct_from_bundled_hindsight() -> None:
    assert PROVIDER_ID == "better_hindsight"
    assert PROVIDER_ID != "hindsight"
