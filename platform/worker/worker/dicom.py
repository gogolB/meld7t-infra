"""DICOM input acquisition for a case. Local staging now; Orthanc WADO-RS is the production path
(Phase 3/4 wires study upload → Orthanc → WADO). The compute job never touches the network (§27):
the worker fetches to a local dir, then bind-mounts it into the --network=none pkg container."""
from __future__ import annotations

import os

from .config import wsettings


def dicom_root_for(case) -> str:
    """Return a local directory containing the case's DICOM series subdirs.

    Order: explicit case.dicom_path (local staging) → {dicom_staging}/{case_id} → WADO pull.
    """
    if getattr(case, "dicom_path", None) and os.path.isdir(case.dicom_path):
        return case.dicom_path
    staged = os.path.join(wsettings.dicom_staging, case.id)
    if os.path.isdir(staged):
        return staged
    if case.orthanc_study_uid:
        return wado_pull(case.orthanc_study_uid, staged)
    raise FileNotFoundError(
        f"no DICOM for case {case.id}: set dicom_path, stage under {staged}, or ingest a study")


def wado_pull(study_uid: str, dest: str) -> str:
    """Retrieve a study's instances from Orthanc to `dest` (one .dcm per instance)."""
    from app.config import settings
    from dicomweb_client.api import DICOMwebClient

    os.makedirs(dest, exist_ok=True)
    client = DICOMwebClient(url=settings.orthanc_dicomweb)
    for i, inst in enumerate(client.retrieve_study(study_uid)):
        inst.save_as(os.path.join(dest, f"{i:06d}.dcm"))
    return dest
