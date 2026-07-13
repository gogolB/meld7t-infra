"""Orthanc DICOMweb access + series-role proposal (spec §4, §16).

QIDO to enumerate a study's series; tag-based role proposal that the submitter then confirms or
overrides (propose-then-confirm, never silent-auto). Same patterns as containers/pkg/recon_prepare.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from .config import settings
from .dicom_policy import validate_deidentified_part10
from .harmonization import acquisition_fingerprint, canonical_acquisition
from .models import SeriesRole
from .storage import storage_health

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


def dicomweb_client(url: str | None = None):
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
    target = url or settings.orthanc_dicomweb
    if target == settings.harmonization_orthanc_dicomweb:
        session.auth = (
            settings.harmonization_orthanc_user,
            settings.harmonization_orthanc_password.get_secret_value(),
        )
    return DICOMwebClient(url=target, session=session)


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

def get_study_series(study_uid: str, *, dicomweb_url: str | None = None) -> list[dict]:
    """Return [{series_uid, description, modality, number, instances}] for a study."""
    client = dicomweb_client(dicomweb_url)
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


def get_series_instance_manifest(
        study_uid: str, series_uid: str, *, dicomweb_url: str | None = None) -> dict:
    """Hash the exact Part-10 byte closure for one selected source series.

    Orthanc is the controlled transport store, not the immutable build input.  The admission
    manifest is re-downloaded and hash-checked into a private worker snapshot before estimation,
    so adding, deleting, or replacing an instance after cohort admission fails the build closed.
    """
    client = dicomweb_client(dicomweb_url)
    target = (dicomweb_url or settings.orthanc_dicomweb).rstrip("/")
    datasets = client.search_for_instances(
        study_instance_uid=study_uid,
        series_instance_uid=series_uid,
        fields=["00080018"],
        get_remaining=True,
    )
    sop_uids = [_tag(ds, "00080018") for ds in datasets]
    if (not sop_uids or any(
            not isinstance(uid, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", uid) is None for uid in sop_uids)
            or len(sop_uids) != len(set(sop_uids))):
        raise ValueError("source series has a missing, invalid, or duplicate SOP Instance UID")

    manifest: list[dict] = []
    exact_series: dict | None = None
    total = 0
    for sop_uid in sorted(sop_uids):
        url = (
            f"{target}/studies/{quote(study_uid, safe='')}/series/"
            f"{quote(series_uid, safe='')}/instances/{quote(sop_uid, safe='')}"
        )
        scratch = Path(settings.harmonization_upload_root)
        scratch.mkdir(parents=True, exist_ok=True)
        capacity = storage_health(
            str(scratch),
            minimum_free_bytes=(settings.storage_min_free_bytes
                                + settings.harmonization_max_instance_bytes),
            minimum_free_percent=settings.storage_min_free_percent,
        )
        if not capacity["ready"]:
            raise OSError("harmonization admission scratch is below its storage watermark")
        with tempfile.TemporaryFile(dir=scratch) as exact_bytes:
            with client._session.get(  # dicomweb-client owns the hardened authenticated session.
                    url, headers={"Accept": "application/dicom"}, stream=True,
                    timeout=settings.dicomweb_timeout_seconds, allow_redirects=False) as response:
                response.raise_for_status()
                digest = hashlib.sha256()
                size = 0
                prefix = bytearray()
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if len(prefix) < 132:
                        prefix.extend(chunk[:132 - len(prefix)])
                    size += len(chunk)
                    total += len(chunk)
                    if size > settings.harmonization_max_instance_bytes:
                        raise ValueError("DICOM instance exceeds the configured byte limit")
                    if total > settings.harmonization_max_upload_bytes:
                        raise ValueError("source series exceeds the configured admission byte limit")
                    digest.update(chunk)
                    exact_bytes.write(chunk)
            exact_bytes.flush()
            exact_bytes.seek(0)
            identity = validate_deidentified_part10(
                exact_bytes,
                allowed_transfer_syntaxes=settings.harmonization_allowed_transfer_syntaxes,
                allowed_private_tags=settings.harmonization_allowed_private_tags,
            )
        if (identity["sop_instance_uid"] != sop_uid
                or identity["series_instance_uid"] != series_uid
                or identity["study_instance_uid"] != study_uid):
            raise ValueError("DICOM instance identity differs from the requested source")
        if size < 132 or bytes(prefix[128:132]) != b"DICM":
            raise ValueError("DICOM instance is not a Part-10 object")
        exact_acquisition = canonical_acquisition(identity["acquisition"])
        current_series = {
            "modality": str(identity.get("modality") or "").upper(),
            "description": identity.get("series_description"),
            "patient_id": identity["patient_id"],
            "acquisition": exact_acquisition,
            "fingerprint": acquisition_fingerprint(exact_acquisition),
        }
        if exact_series is None:
            exact_series = current_series
        elif current_series != exact_series:
            raise ValueError(
                "source series has inconsistent exact-byte acquisition metadata"
            )
        manifest.append({
            "sop_instance_uid": sop_uid,
            "sha256": digest.hexdigest(),
            "size": size,
        })
    if exact_series is None:
        raise ValueError("source series contains no exact-byte acquisition evidence")
    return {"instances": manifest, "series": exact_series}
