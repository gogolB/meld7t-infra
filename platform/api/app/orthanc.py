"""Orthanc DICOMweb access + series-role proposal (spec §4, §16).

QIDO to enumerate a study's series; tag-based role proposal that the submitter then confirms or
overrides (propose-then-confirm, never silent-auto). Same patterns as containers/pkg/recon_prepare.
"""
from __future__ import annotations

import re

from .config import settings
from .models import SeriesRole

# ordered: first match wins. inv1/inv2 before uni so "sag t1 inv1" doesn't match a bare 'uni' rule.
_ROLE_PATTERNS: list[tuple[SeriesRole, str]] = [
    (SeriesRole.t1_inv1, r"inv1"),
    (SeriesRole.t1_inv2, r"inv2"),
    (SeriesRole.t1_uni, r"\buni\b|mp2rage.*uni"),
    (SeriesRole.flair, r"flair|dark[\s_-]*fluid"),
    (SeriesRole.t2, r"t2.*space|spc|space"),
    (SeriesRole.t1_mprage, r"mprage|t1\+orig|\borig\b|t1"),
]


def propose_role(series_description: str | None) -> SeriesRole:
    desc = (series_description or "").lower()
    for role, pat in _ROLE_PATTERNS:
        if re.search(pat, desc):
            return role
    return SeriesRole.unknown


def _client():
    from dicomweb_client.api import DICOMwebClient

    return DICOMwebClient(url=settings.orthanc_dicomweb)


def _tag(ds: dict, tag: str, default=None):
    v = ds.get(tag, {}).get("Value", [default])
    return v[0] if v else default


def get_study_series(study_uid: str) -> list[dict]:
    """Return [{series_uid, description, modality, number, instances}] for a study."""
    client = _client()
    out = []
    for ds in client.search_for_series(study_instance_uid=study_uid):
        out.append({
            "series_uid": _tag(ds, "0020000E"),
            "description": _tag(ds, "0008103E"),
            "modality": _tag(ds, "00080060"),
            "number": _tag(ds, "00200011"),
        })
    return out
