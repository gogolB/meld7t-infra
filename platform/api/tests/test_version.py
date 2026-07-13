from __future__ import annotations

import tomllib
from pathlib import Path

from app.version import API_VERSION


def test_api_version_matches_source_project_metadata() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as stream:
        expected = str(tomllib.load(stream)["project"]["version"])
    assert API_VERSION == expected
