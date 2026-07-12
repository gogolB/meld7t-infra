"""Resolve the API version from installed or source project metadata."""
from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path


_DISTRIBUTION_NAME = "meld7t-api"


def _source_project_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def resolve_api_version() -> str:
    """Use adjacent source metadata when present, otherwise installed wheel metadata."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if pyproject.is_file():
        return _source_project_version()
    try:
        return distribution_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "meld7t-api version metadata is unavailable; install the distribution or "
            "deploy its pyproject.toml beside app/"
        ) from exc


API_VERSION = resolve_api_version()
