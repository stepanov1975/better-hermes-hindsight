"""Observe the Hermes host selected for compatibility tests.

The project follows the intended current checkout. Tests verify that imported host
sources belong to the installed distribution, but do not reject a host solely
because its version or commit changed.
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _observed_identity() -> tuple[str, str]:
    distribution = metadata.distribution("hermes-agent")
    commit = ""
    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text:
        direct_url = json.loads(direct_url_text)
        value = direct_url.get("vcs_info", {}).get("commit_id")
        if isinstance(value, str):
            commit = value
    return distribution.version, commit


EXPECTED_HERMES_VERSION, EXPECTED_HERMES_COMMIT = _observed_identity()


def assert_selected_hermes() -> metadata.Distribution:
    """Return the installed host after basic identity validation."""

    distribution = metadata.distribution("hermes-agent")
    assert distribution.version
    return distribution


def selected_distribution_file(
    distribution: metadata.Distribution,
    relative_path: str,
) -> Path:
    """Locate a host file from either an installed wheel or editable checkout."""

    files = distribution.files or ()
    entry = next((item for item in files if str(item) == relative_path), None)
    if entry is not None:
        return Path(str(distribution.locate_file(entry))).resolve()

    direct_url_text = distribution.read_text("direct_url.json")
    assert direct_url_text is not None
    direct_url = json.loads(direct_url_text)
    assert direct_url.get("dir_info", {}).get("editable") is True
    source_root = Path(unquote(urlsplit(direct_url["url"]).path))
    return (source_root / relative_path).resolve()
