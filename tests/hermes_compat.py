"""Selected Hermes host identity for compatibility tests.

The default keeps the original 0.19.0 release-commit characterization reproducible.
CI overrides the version and may omit the commit when exercising another released host.
"""

from __future__ import annotations

import json
import os
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit

_HISTORICAL_VERSION = "0.19.0"
_HISTORICAL_COMMIT = "3ef6bbd201263d354fd83ec55b3c306ded2eb72a"

EXPECTED_HERMES_VERSION = os.environ.get(
    "BETTER_HINDSIGHT_EXPECT_HERMES_VERSION",
    _HISTORICAL_VERSION,
)
EXPECTED_HERMES_COMMIT = os.environ.get(
    "BETTER_HINDSIGHT_EXPECT_HERMES_COMMIT",
    _HISTORICAL_COMMIT if EXPECTED_HERMES_VERSION == _HISTORICAL_VERSION else "",
)


def assert_selected_hermes() -> metadata.Distribution:
    """Assert and return the explicitly selected host distribution."""

    distribution = metadata.distribution("hermes-agent")
    assert distribution.version == EXPECTED_HERMES_VERSION
    if EXPECTED_HERMES_COMMIT:
        direct_url_text = distribution.read_text("direct_url.json")
        assert direct_url_text is not None
        direct_url = json.loads(direct_url_text)
        assert direct_url.get("vcs_info", {}).get("commit_id") == EXPECTED_HERMES_COMMIT
    return distribution


def selected_distribution_file(
    distribution: metadata.Distribution,
    relative_path: str,
) -> Path:
    """Locate a host file from either an installed wheel or an editable release checkout."""

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
