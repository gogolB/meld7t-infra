"""Orthanc DICOMweb access + series-role proposal (spec §4, §16).

QIDO to enumerate a study's series; tag-based role proposal that the submitter then confirms or
overrides (propose-then-confirm, never silent-auto). Same patterns as containers/pkg/recon_prepare.
"""
from __future__ import annotations

import re

from .config import settings
from .harmonization import acquisition_fingerprint, canonical_acquisition
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


def dicomweb_client():
    from dicomweb_client.api import DICOMwebClient
    import requests

    class InternalSession(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs.setdefault("timeout", settings.dicomweb_timeout_seconds)
            kwargs.setdefault("allow_redirects", False)
            return super().request(method, url, **kwargs)

    session = InternalSession()
    # Never leak internal DICOM requests through workstation proxy environment variables.
    session.trust_env = False
    return DICOMwebClient(url=settings.orthanc_dicomweb, session=session)


def _tag(ds: dict, tag: str, default=None):
    v = ds.get(tag, {}).get("Value", [default])
    return v[0] if v else default


def _values(ds: dict, tag: str) -> list:
    return list(ds.get(tag, {}).get("Value", []) or [])


def _float_tag(ds: dict, tag: str):
    try:
        return float(_tag(ds, tag))
    except (TypeError, ValueError):
        return None


def _int_tag(ds: dict, tag: str):
    try:
        return int(_tag(ds, tag))
    except (TypeError, ValueError):
        return None


def _float_values(ds: dict, tag: str) -> list[float]:
    result = []
    for value in _values(ds, tag):
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            return []
    return result


_SERIES_FIELDS = [
    "00080060", "0008103E", "00080070", "00081090", "00081010",
    "00180087", "00181020", "00181030", "00180024", "00180020",
    "00180080", "00180081", "00180082", "00181314", "00180050",
    "00280010", "00280011", "00280030", "00200011", "00201209",
    "00180088", "00181250", "00181251", "00180085", "00180095",
    "00180094", "00181310", "00181312", "00180089", "00189077",
    "00189078", "00189069", "00189155", "00181100", "00180091",
    "00180083", "00180023", "00089208", "00089209",
    "00080008", "00281053", "00281052", "00280101", "00280103",
]


def get_study_series(study_uid: str) -> list[dict]:
    """Return [{series_uid, description, modality, number, instances}] for a study."""
    client = dicomweb_client()
    out = []
    for ds in client.search_for_series(study_instance_uid=study_uid, fields=_SERIES_FIELDS):
        acquisition = {
            "manufacturer": _tag(ds, "00080070"),
            "model": _tag(ds, "00081090"),
            "station_name": _tag(ds, "00081010"),
            "field_strength_t": _float_tag(ds, "00180087"),
            "software_versions": _values(ds, "00181020"),
            "protocol_name": _tag(ds, "00181030"),
            "sequence_name": _tag(ds, "00180024"),
            "scanning_sequence": _values(ds, "00180020"),
            "repetition_time_ms": _float_tag(ds, "00180080"),
            "echo_time_ms": _float_tag(ds, "00180081"),
            "inversion_time_ms": _float_tag(ds, "00180082"),
            "flip_angle_deg": _float_tag(ds, "00181314"),
            "slice_thickness_mm": _float_tag(ds, "00180050"),
            "spacing_between_slices_mm": _float_tag(ds, "00180088"),
            "receive_coil_name": _tag(ds, "00181250"),
            "transmit_coil_name": _tag(ds, "00181251"),
            "imaged_nucleus": _tag(ds, "00180085"),
            "pixel_bandwidth_hz": _float_tag(ds, "00180095"),
            "percent_phase_fov": _float_tag(ds, "00180094"),
            "acquisition_matrix": [
                int(value) for value in _values(ds, "00181310")
                if str(value).strip().isdigit()
            ],
            "phase_encoding_direction": _tag(ds, "00181312"),
            "phase_encoding_steps": _int_tag(ds, "00180089"),
            "parallel_acquisition": _tag(ds, "00189077"),
            "parallel_technique": _tag(ds, "00189078"),
            "acceleration_factor_in_plane": _float_tag(ds, "00189069"),
            "acceleration_factor_out_of_plane": _float_tag(ds, "00189155"),
            "reconstruction_diameter_mm": _float_tag(ds, "00181100"),
            "echo_train_length": _int_tag(ds, "00180091"),
            "number_of_averages": _float_tag(ds, "00180083"),
            "mr_acquisition_type": _tag(ds, "00180023"),
            "complex_image_component": _tag(ds, "00089208"),
            "acquisition_contrast": _tag(ds, "00089209"),
            "image_type": _values(ds, "00080008"),
            "rescale_slope": _float_tag(ds, "00281053"),
            "rescale_intercept": _float_tag(ds, "00281052"),
            "bits_stored": _int_tag(ds, "00280101"),
            "pixel_representation": _int_tag(ds, "00280103"),
            "rows": _int_tag(ds, "00280010"),
            "columns": _int_tag(ds, "00280011"),
            "voxel_spacing_mm": _float_values(ds, "00280030"),
        }
        canonical = canonical_acquisition(acquisition)
        out.append({
            "series_uid": _tag(ds, "0020000E"),
            "description": _tag(ds, "0008103E"),
            "modality": _tag(ds, "00080060"),
            "number": _int_tag(ds, "00200011"),
            "instances": _int_tag(ds, "00201209"),
            "acquisition": acquisition,
            "fingerprint": acquisition_fingerprint(acquisition) if canonical else None,
        })
    return out
