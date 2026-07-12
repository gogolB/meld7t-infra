"""Admin-only on-server harmonization cohort and build workflow."""
from __future__ import annotations

import csv
import io
import math
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field as PydanticField, field_validator, model_validator
from sqlalchemy import text
from sqlmodel import Session, func, select

from . import audit, queue, upload_receipt
from .auth import Principal, Role, require_admin, require_roles
from .cohort_builder import (
    canonical_sha256, deterministic_folds, frozen_manifest,
    lock_harmonization_orthanc_mutation, subject_key_hmac,
)
from .config import settings
from .db import get_session
from .harmonization import (
    canonical_acquisition,
    lock_profile_activation,
    mark_profile_integrity_dirty,
    match_selector,
    profile_document_sha256,
    rank_profiles,
    runtime_profile_trusted,
    selectors_may_overlap,
    validate_profile_semantics,
    validate_selector,
    verify_artifact_manifest,
    sha256_file,
)
from .models import (
    AcquisitionObservation,
    DetectorId,
    HarmonizationBuild,
    HarmonizationBuildStatus,
    HarmonizationCohort,
    HarmonizationCohortStatus,
    HarmonizationCohortStudy,
    HarmonizationDemographic,
    HarmonizationFoldResult,
    HarmonizationProfile,
    HarmonizationProfileStatus,
    HarmonizationUpload,
    HarmonizationUploadStatus,
    OutboxEvent,
    Series,
    SeriesRole,
)
from .orthanc import get_series_instance_manifest, get_study_series, propose_role
from .storage import storage_health


router = APIRouter(prefix="/api", tags=["harmonization-cohorts"])
require_harmonization_operator = require_roles(Role.admin, Role.auditor)
_DIGEST_REF = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


class CohortCreate(BaseModel):
    name: str = PydanticField(min_length=1, max_length=160)
    site_code: str = PydanticField(min_length=1, max_length=64,
                                   pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    profile_code: str = PydanticField(min_length=2, max_length=32,
                                      pattern=r"^H[A-Za-z0-9][A-Za-z0-9_-]*$")
    profile_version: int = PydanticField(ge=1)
    source_role: SeriesRole
    selector: dict[str, Any]
    min_controls: int = PydanticField(default=20, ge=20, le=500)
    cv_folds: int = PydanticField(default=5, ge=2, le=10)

    @field_validator("source_role")
    @classmethod
    def usable_role(cls, value: SeriesRole) -> SeriesRole:
        if value not in {SeriesRole.t1_uni, SeriesRole.t1_mprage}:
            raise ValueError("MELD cohorts require a t1_uni or t1_mprage source role")
        return value


class StudyImportItem(BaseModel):
    study_uid: str = PydanticField(min_length=3, max_length=64,
                                   pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    subject_key: str = PydanticField(min_length=1, max_length=128)
    source_series_uid: Optional[str] = PydanticField(
        default=None, min_length=3, max_length=64, pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    included: bool = True
    exclusion_reason: Optional[str] = PydanticField(default=None, max_length=500)

    @model_validator(mode="after")
    def excluded_requires_reason(self) -> "StudyImportItem":
        if not self.included and not (self.exclusion_reason or "").strip():
            raise ValueError("excluded studies require a reason")
        if self.included:
            self.exclusion_reason = None
        return self


class StudyImport(BaseModel):
    studies: list[StudyImportItem] = PydanticField(min_length=1, max_length=100)


class StudyDecision(BaseModel):
    included: bool
    exclusion_reason: Optional[str] = PydanticField(default=None, max_length=500)


class UploadCreate(BaseModel):
    filename: str = PydanticField(min_length=1, max_length=255)
    total_size: int = PydanticField(gt=0)
    sha256: str = PydanticField(pattern=r"^[0-9a-fA-F]{64}$")
    content_type: str = PydanticField(default="application/octet-stream", max_length=128)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if Path(value).name != value or value in {".", ".."} or any(c in value for c in "\r\n\0"):
            raise ValueError("filename must be a safe basename")
        if not value.lower().endswith((".dcm", ".dicom", ".zip")):
            raise ValueError("browser upload must be DICOM or a ZIP archive")
        return value


class UploadRollbackResolution(BaseModel):
    action: Literal["preserve", "delete"]
    reason: str = PydanticField(min_length=20, max_length=2000)
    evidence_sha256: str = PydanticField(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("reason")
    @classmethod
    def substantive_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 20 or "\0" in value:
            raise ValueError("rollback resolution requires a substantive safe reason")
        return value


class BuildCreate(BaseModel):
    builder_image_digest: str = PydanticField(min_length=1, max_length=512)
    acceptance_criteria: dict[str, Any]

    @field_validator("builder_image_digest")
    @classmethod
    def pinned_image(cls, value: str) -> str:
        if _DIGEST_REF.fullmatch(value) is None:
            raise ValueError("builder image must be pinned by sha256 manifest digest")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def bounded_criteria(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value or len(repr(value)) > 50_000:
            raise ValueError("versioned acceptance criteria are required and must be bounded")
        methodology = str(value.get("methodology_sha256", ""))
        if re.fullmatch(r"[0-9a-fA-F]{64}", methodology) is None:
            raise ValueError("acceptance criteria require methodology_sha256")
        value = {**value, "methodology_sha256": methodology.lower()}
        for field in ("required_metrics", "final_required_metrics"):
            required = value.get(field)
            if field == "final_required_metrics" and required is None:
                continue
            if not isinstance(required, dict) or not required or len(required) > 64:
                raise ValueError(f"{field} must be a non-empty bounded metric map")
            for name, bounds in required.items():
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", str(name)) is None:
                    raise ValueError(f"{field} contains an invalid metric name")
                if (not isinstance(bounds, dict) or not set(bounds).intersection({"min", "max"})
                        or set(bounds) - {"min", "max"}):
                    raise ValueError(
                        f"{field}.{name} requires only a minimum and/or maximum")
                normalized: dict[str, float] = {}
                for bound, raw in bounds.items():
                    if isinstance(raw, bool):
                        raise ValueError(f"{field}.{name}.{bound} must be finite")
                    try:
                        normalized[bound] = float(raw)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{field}.{name}.{bound} must be finite") from exc
                    if not math.isfinite(normalized[bound]):
                        raise ValueError(f"{field}.{name}.{bound} must be finite")
                if ("min" in normalized and "max" in normalized
                        and normalized["min"] > normalized["max"]):
                    raise ValueError(f"{field}.{name}.min must not exceed max")
        return value


class BuildValidation(BaseModel):
    scientific_validation: dict[str, Any]

    @field_validator("scientific_validation")
    @classmethod
    def bounded_report(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value or len(repr(value)) > 100_000:
            raise ValueError("scientific validation report is required and must be bounded")
        return value


class BuildRejection(BaseModel):
    reason: str = PydanticField(min_length=20, max_length=2000)
    evidence_sha256: str = PydanticField(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 20 or any(char in value for char in "\0"):
            raise ValueError("rejection reason must contain at least 20 safe characters")
        return value


def _cohort_counts(session: Session, cohort_id: str) -> dict[str, int]:
    studies = session.exec(select(HarmonizationCohortStudy).where(
        HarmonizationCohortStudy.cohort_id == cohort_id)).all()
    demographics = session.exec(select(HarmonizationDemographic).where(
        HarmonizationDemographic.cohort_id == cohort_id)).all()
    uploads = session.exec(select(HarmonizationUpload).where(
        HarmonizationUpload.cohort_id == cohort_id)).all()
    return {
        "studies": len(studies),
        "included": sum(row.included for row in studies),
        "excluded": sum(not row.included for row in studies),
        "demographics": len(demographics),
        "uploads": len(uploads),
    }


def _study_public(row: HarmonizationCohortStudy) -> dict[str, Any]:
    minimized_series = []
    for item in row.series_manifest:
        public_item = {key: value for key, value in item.items()
                       if key != "instance_manifest"}
        instances = item.get("instance_manifest")
        if isinstance(instances, list):
            public_item.update({
                "instance_manifest_count": len(instances),
                "instance_manifest_bytes": sum(
                    int(instance.get("size", 0)) for instance in instances
                    if isinstance(instance, dict)),
                "instance_manifest_sha256": canonical_sha256(instances),
            })
        minimized_series.append(public_item)
    return {
        "id": row.id,
        "study_uid": row.orthanc_study_uid,
        "subject_key_hmac": row.subject_key_hmac,
        "included": row.included,
        "exclusion_reason": row.exclusion_reason,
        "acquisition_fingerprint": row.acquisition_fingerprint,
        "acquisition": row.acquisition,
        # Raw SOP Instance UIDs are retained only in the protected build contract, not returned
        # through routine cohort reads.
        "series_manifest": minimized_series,
        "study_sha256": row.study_sha256,
        "imported_at": row.imported_at,
    }


def _build_public(row: HarmonizationBuild) -> dict[str, Any]:
    return {
        "id": row.id, "cohort_id": row.cohort_id, "attempt": row.attempt,
        "status": row.status, "stage": row.stage, "progress": row.progress,
        "builder_image_digest": row.builder_image_digest,
        "builder_adapter_sha256": row.builder_adapter_sha256,
        "acceptance_criteria": row.acceptance_criteria,
        "cv_plan": row.cv_plan, "qc_summary": row.qc_report,
        "artifact_manifest": row.artifact_manifest, "profile_id": row.profile_id,
        "rejection_summary": row.rejection_summary,
        "error_code": row.error_code, "initiated_by": row.initiated_by,
        "validated_by": row.validated_by, "activated_by": row.activated_by,
        "created_at": row.created_at, "started_at": row.started_at,
        "completed_at": row.completed_at, "heartbeat_at": row.heartbeat_at,
    }


def _upload_result_public(row: HarmonizationUpload, *, include_subject_mapping: bool) -> dict | None:
    if row.import_result is None:
        return None
    result = dict(row.import_result)
    if not include_subject_mapping and isinstance(result.get("studies"), list):
        result["studies"] = [
            {key: value for key, value in item.items() if key != "subject_key"}
            for item in result["studies"] if isinstance(item, dict)
        ]
    return result


def _cohort_public(session: Session, cohort: HarmonizationCohort, *, detail: bool = False,
                   include_upload_subjects: bool = False) -> dict:
    result = {
        "id": cohort.id, "name": cohort.name, "site_code": cohort.site_code,
        "profile_code": cohort.profile_code, "profile_version": cohort.profile_version,
        "source_role": cohort.source_role, "selector": cohort.selector,
        "min_controls": cohort.min_controls, "cv_folds": cohort.cv_folds,
        "status": cohort.status, "counts": _cohort_counts(session, cohort.id),
        "demographics_manifest": cohort.demographics_manifest,
        "frozen_manifest": cohort.frozen_manifest, "created_by": cohort.created_by,
        "approved_by": cohort.approved_by, "created_at": cohort.created_at,
        "frozen_at": cohort.frozen_at,
    }
    if detail:
        studies = session.exec(select(HarmonizationCohortStudy).where(
            HarmonizationCohortStudy.cohort_id == cohort.id
        ).order_by(HarmonizationCohortStudy.imported_at)).all()
        builds = session.exec(select(HarmonizationBuild).where(
            HarmonizationBuild.cohort_id == cohort.id
        ).order_by(HarmonizationBuild.attempt.desc())).all()
        uploads = session.exec(select(HarmonizationUpload).where(
            HarmonizationUpload.cohort_id == cohort.id
        ).order_by(HarmonizationUpload.created_at.desc())).all()
        result.update({
            "studies": [_study_public(row) for row in studies],
            "builds": [_build_public(row) for row in builds],
            "uploads": [{
                "id": row.id, "filename": row.filename, "total_size": row.total_size,
                "received_size": row.received_size, "sha256": row.sha256,
                "status": row.status, "last_error": row.last_error,
                "import_result": _upload_result_public(
                    row, include_subject_mapping=include_upload_subjects),
                "mapping_redacted_at": row.mapping_redacted_at,
            } for row in uploads],
        })
    return result


def _mutable_cohort(session: Session, cohort_id: str) -> HarmonizationCohort:
    statement = select(HarmonizationCohort).where(HarmonizationCohort.id == cohort_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    cohort = session.exec(statement).first()
    if cohort is None:
        raise HTTPException(404, "harmonization cohort not found")
    if cohort.status in {HarmonizationCohortStatus.frozen, HarmonizationCohortStatus.archived}:
        raise HTTPException(409, "frozen/archived cohort membership is immutable")
    return cohort


def _locked_build(session: Session, build_id: str) -> HarmonizationBuild | None:
    statement = select(HarmonizationBuild).where(HarmonizationBuild.id == build_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.exec(statement).first()


def _assert_no_pending_orthanc_rollback(session: Session) -> None:
    """Fence new admission against durable or in-progress receipt-governed deletion."""
    lock_harmonization_orthanc_mutation(session)
    pending = [
        row for row in session.exec(select(HarmonizationUpload).where(
            HarmonizationUpload.status == HarmonizationUploadStatus.failed
        )).all()
        if (row.import_result or {}).get("phase") in {
            "rollback_incomplete", "rollback_delete_approved"}
    ]
    if pending:
        raise HTTPException(
            409, "resolve pending harmonization Orthanc rollback before new admission")


@router.post("/harmonization/cohorts", status_code=201)
def create_cohort(body: CohortCreate, principal: Principal = Depends(require_admin),
                  session: Session = Depends(get_session)) -> dict:
    try:
        validate_selector(body.selector)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.selector.get("roles") and body.source_role.value not in body.selector["roles"]:
        raise HTTPException(422, "selector roles do not include the cohort source role")
    cohort = HarmonizationCohort(**body.model_dump(), created_by=principal.actor)
    session.add(cohort)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.cohort.create",
        entity_type="harmonization_cohort", entity_id=cohort.id,
        payload={"site_code": body.site_code, "profile_code": body.profile_code,
                 "profile_version": body.profile_version},
    )
    session.commit()
    return _cohort_public(session, cohort, detail=True, include_upload_subjects=True)


@router.get("/harmonization/cohorts")
def list_cohorts(principal: Principal = Depends(require_harmonization_operator),
                 session: Session = Depends(get_session)) -> list[dict]:
    rows = session.exec(select(HarmonizationCohort).order_by(
        HarmonizationCohort.created_at.desc())).all()
    audit.record_access(session, principal=principal, entity_type="harmonization_cohort_collection",
                        entity_id="all", detail={"count": len(rows)})
    session.commit()
    return [_cohort_public(session, row) for row in rows]


@router.get("/harmonization/cohorts/{cohort_id}")
def get_cohort(cohort_id: str, principal: Principal = Depends(require_harmonization_operator),
               session: Session = Depends(get_session)) -> dict:
    cohort = session.get(HarmonizationCohort, cohort_id)
    if cohort is None:
        raise HTTPException(404, "harmonization cohort not found")
    audit.record_access(session, principal=principal, entity_type="harmonization_cohort",
                        entity_id=cohort_id, detail={})
    session.commit()
    return _cohort_public(
        session, cohort, detail=True,
        include_upload_subjects=Role.admin in principal.roles,
    )


@router.post("/harmonization/cohorts/{cohort_id}/studies/import")
def import_studies(cohort_id: str, body: StudyImport,
                   principal: Principal = Depends(require_admin),
                   session: Session = Depends(get_session)) -> dict:
    # Hashing tens of gigabytes can take minutes. Snapshot the immutable cohort contract, release
    # the transaction, prepare bounded manifests, then re-lock and atomically revalidate/insert.
    initial = session.get(HarmonizationCohort, cohort_id)
    if initial is None:
        raise HTTPException(404, "harmonization cohort not found")
    if initial.status in {HarmonizationCohortStatus.frozen, HarmonizationCohortStatus.archived}:
        raise HTTPException(409, "frozen/archived cohort membership is immutable")
    contract = {
        "source_role": initial.source_role,
        "selector": initial.selector,
        "profile_code": initial.profile_code,
        "profile_version": initial.profile_version,
    }
    session.rollback()
    prepared: list[dict[str, Any]] = []
    seen_studies: set[str] = set()
    seen_subjects: set[str] = set()
    for item in body.studies:
        subject_hmac = subject_key_hmac(cohort_id, item.subject_key)
        if item.study_uid in seen_studies or subject_hmac in seen_subjects:
            raise HTTPException(409, "study UID or deidentified subject is already in this cohort")
        seen_studies.add(item.study_uid)
        seen_subjects.add(subject_hmac)
        try:
            series = get_study_series(
                item.study_uid, dicomweb_url=settings.harmonization_orthanc_dicomweb)
        except Exception as exc:
            raise HTTPException(502, f"harmonization Orthanc lookup failed: {type(exc).__name__}") from exc
        candidates = [row for row in series if (
            row.get("series_uid") == item.source_series_uid if item.source_series_uid
            else propose_role(row.get("description")) == contract["source_role"]
        )]
        if len(candidates) != 1:
            raise HTTPException(
                409, "study must have exactly one selected source series for the cohort role")
        source = candidates[0]
        if str(source.get("modality", "")).upper() != "MR":
            raise HTTPException(409, "MELD cohort source series must have MR modality")
        acquisition = canonical_acquisition(source.get("acquisition") or {})
        fingerprint = source.get("fingerprint")
        if (not acquisition or not fingerprint
                or isinstance(acquisition.get("rows"), bool)
                or not isinstance(acquisition.get("rows"), (int, float))
                or isinstance(acquisition.get("columns"), bool)
                or not isinstance(acquisition.get("columns"), (int, float))
                or acquisition["rows"] <= 0 or acquisition["columns"] <= 0
                or not acquisition.get("voxel_spacing_mm")):
            raise HTTPException(409, "source series lacks required acquisition metadata")
        match = match_selector(
            cohort_id, contract["selector"], acquisition,
            role=contract["source_role"].value,
        )
        if not match.matched:
            raise HTTPException(409, "study source series does not match the cohort selector")
        try:
            admission = get_series_instance_manifest(
                item.study_uid, str(source["series_uid"]),
                dicomweb_url=settings.harmonization_orthanc_dicomweb,
            )
        except Exception as exc:
            raise HTTPException(
                502, f"harmonization source hashing failed: {type(exc).__name__}") from exc
        instances = admission["instances"]
        exact_source = admission["series"]
        if exact_source.get("modality") != "MR":
            raise HTTPException(409, "exact source instances must have MR modality")
        if subject_key_hmac(cohort_id, str(exact_source.get("patient_id") or "")) != subject_hmac:
            raise HTTPException(
                409, "cohort subject key must equal the exact DICOM pseudonymous PatientID")
        if (not item.source_series_uid
                and propose_role(exact_source.get("description")) != contract["source_role"]):
            raise HTTPException(
                409, "source role changed between series lookup and exact-byte admission")
        acquisition = canonical_acquisition(exact_source.get("acquisition") or {})
        fingerprint = exact_source.get("fingerprint")
        if (not acquisition or not fingerprint
                or isinstance(acquisition.get("rows"), bool)
                or not isinstance(acquisition.get("rows"), (int, float))
                or isinstance(acquisition.get("columns"), bool)
                or not isinstance(acquisition.get("columns"), (int, float))
                or acquisition["rows"] <= 0 or acquisition["columns"] <= 0
                or not acquisition.get("voxel_spacing_mm")):
            raise HTTPException(409, "exact source bytes lack required acquisition metadata")
        exact_match = match_selector(
            cohort_id, contract["selector"], acquisition,
            role=contract["source_role"].value,
        )
        if not exact_match.matched:
            raise HTTPException(
                409, "exact source bytes do not match the cohort selector")
        if source.get("instances") is not None and int(source["instances"]) != len(instances):
            raise HTTPException(409, "source series instance count changed during admission")
        manifest = []
        for series_row in sorted(series, key=lambda value: str(value.get("series_uid"))):
            selected = series_row.get("series_uid") == source.get("series_uid")
            entry = {
                "series_uid": series_row.get("series_uid"),
                "instance_count": (len(instances) if selected else series_row.get("instances")),
                "fingerprint": (fingerprint if selected else series_row.get("fingerprint")),
                "role": (contract["source_role"].value if selected
                         else propose_role(series_row.get("description")).value),
                "selected_source": selected,
            }
            if selected:
                entry["instance_manifest"] = instances
                entry["deidentification_policy"] = {
                    "profile": "DICOM Basic Application Confidentiality Profile",
                    "patient_identity_removed": True,
                    "burned_in_annotation": "NO",
                    "private_tag_allowlist_sha256": canonical_sha256(
                        settings.harmonization_allowed_private_tags),
                    "pixel_review_caveat": (
                        "DICOM attestation and metadata screening do not replace site pixel-PHI "
                        "review when required by policy"
                    ),
                }
            manifest.append(entry)
        prepared.append({
            "cohort_id": cohort_id, "orthanc_study_uid": item.study_uid,
            "subject_key_hmac": subject_hmac, "included": item.included,
            "exclusion_reason": item.exclusion_reason,
            "acquisition_fingerprint": fingerprint, "acquisition": acquisition,
            "series_manifest": manifest, "study_sha256": canonical_sha256(manifest),
        })

    _assert_no_pending_orthanc_rollback(session)
    cohort = _mutable_cohort(session, cohort_id)
    if any(getattr(cohort, key) != value for key, value in contract.items()):
        raise HTTPException(409, "cohort contract changed while studies were being admitted")
    # The first pass avoids a long-lived SQL transaction while hashing large studies. Re-read the
    # exact selected byte closure under the Orthanc mutation fence before committing membership;
    # otherwise a completed rollback between those two phases could leave a frozen manifest that
    # names objects no longer present in the dedicated store.
    for value in prepared:
        selected = next(
            (item for item in value["series_manifest"] if item.get("selected_source")), None)
        if selected is None:
            raise HTTPException(409, "prepared cohort source closure is invalid")
        try:
            current = get_series_instance_manifest(
                value["orthanc_study_uid"], str(selected["series_uid"]),
                dicomweb_url=settings.harmonization_orthanc_dicomweb,
            )
        except Exception as exc:
            raise HTTPException(
                409, "exact cohort source closure changed before admission") from exc
        exact = current.get("series") if isinstance(current, dict) else None
        instances = current.get("instances") if isinstance(current, dict) else None
        if (not isinstance(exact, dict) or not isinstance(instances, list)
                or canonical_sha256(instances)
                != canonical_sha256(selected.get("instance_manifest"))
                or str(exact.get("modality", "")).upper() != "MR"
                or subject_key_hmac(
                    cohort_id, str(exact.get("patient_id") or ""))
                != value["subject_key_hmac"]
                or canonical_acquisition(exact.get("acquisition") or {})
                != value["acquisition"]
                or exact.get("fingerprint") != value["acquisition_fingerprint"]):
            raise HTTPException(409, "exact cohort source closure changed before admission")
    existing_rows = session.exec(select(HarmonizationCohortStudy).where(
        HarmonizationCohortStudy.cohort_id == cohort.id)).all()
    existing_studies = {row.orthanc_study_uid for row in existing_rows}
    existing_subjects = {row.subject_key_hmac for row in existing_rows}
    if (existing_studies.intersection(seen_studies)
            or existing_subjects.intersection(seen_subjects)):
        raise HTTPException(409, "study UID or deidentified subject is already in this cohort")
    admitted_bytes = sum(
        int(instance.get("size", 0))
        for value in [*(row.series_manifest for row in existing_rows),
                      *(item["series_manifest"] for item in prepared)]
        for series_item in value
        for instance in series_item.get("instance_manifest", [])
        if isinstance(instance, dict)
    )
    if admitted_bytes > settings.harmonization_cohort_quota_bytes:
        raise HTTPException(413, "admitted source series exceed this cohort's storage quota")
    imported = []
    for value in prepared:
        row = HarmonizationCohortStudy(**value)
        session.add(row)
        imported.append(row)
    cohort.status = HarmonizationCohortStatus.draft
    cohort.demographics_manifest = None
    for demographic in session.exec(select(HarmonizationDemographic).where(
            HarmonizationDemographic.cohort_id == cohort.id)).all():
        session.delete(demographic)
    session.add(cohort)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.cohort.study_import",
        entity_type="harmonization_cohort", entity_id=cohort.id,
        payload={"count": len(imported), "included": sum(row.included for row in imported)},
    )
    session.commit()
    return _cohort_public(session, cohort, detail=True, include_upload_subjects=True)


@router.post("/harmonization/cohorts/{cohort_id}/studies/{study_id}/decision")
def decide_study(cohort_id: str, study_id: str, body: StudyDecision,
                 principal: Principal = Depends(require_admin),
                 session: Session = Depends(get_session)) -> dict:
    cohort = _mutable_cohort(session, cohort_id)
    row = session.get(HarmonizationCohortStudy, study_id)
    if row is None or row.cohort_id != cohort.id:
        raise HTTPException(404, "cohort study not found")
    if not body.included and not (body.exclusion_reason or "").strip():
        raise HTTPException(422, "excluded studies require a reason")
    membership_changed = row.included != body.included
    row.included = body.included
    row.exclusion_reason = None if body.included else body.exclusion_reason
    if membership_changed:
        cohort.status = HarmonizationCohortStatus.draft
        cohort.demographics_manifest = None
        session.add(cohort)
        for demographic in session.exec(select(HarmonizationDemographic).where(
                HarmonizationDemographic.cohort_id == cohort.id)).all():
            session.delete(demographic)
    session.add(row)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.cohort.study_decision",
        entity_type="harmonization_cohort_study", entity_id=row.id,
        payload={"included": row.included, "reason_present": bool(row.exclusion_reason)},
    )
    session.commit()
    return _study_public(row)


@router.post("/harmonization/cohorts/{cohort_id}/demographics")
async def import_demographics(cohort_id: str, request: Request,
                              principal: Principal = Depends(require_admin),
                              session: Session = Depends(get_session)) -> dict:
    cohort = _mutable_cohort(session, cohort_id)
    raw = await request.body()
    if not raw or len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "demographics CSV must be between 1 byte and 2 MiB")
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "demographics CSV must be UTF-8") from exc
    except csv.Error as exc:
        raise HTTPException(422, "demographics CSV is malformed") from exc
    expected_headers = {"ID", "Age", "Sex"}
    if set(fieldnames or []) != expected_headers:
        raise HTTPException(422, "demographics CSV headers must be exactly ID,Age,Sex")
    parsed: list[tuple[str, float, str]] = []
    seen: set[str] = set()
    sex_map = {
        "f": "female", "female": "female", "m": "male", "male": "male",
        "o": "other", "other": "other", "nonbinary": "other", "non-binary": "other",
        "intersex": "intersex",
    }
    line = 1
    try:
        for line, value in enumerate(reader, 2):
            if None in value or any(value.get(name) is None for name in expected_headers):
                raise ValueError
            digest = subject_key_hmac(cohort.id, value["ID"])
            age = float(value["Age"])
            sex = sex_map[value["Sex"].strip().lower()]
            if not 0 <= age <= 120 or digest in seen:
                raise ValueError
            seen.add(digest)
            parsed.append((digest, age, sex))
    except (csv.Error, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"invalid or duplicate demographics row at line {line}") from exc
    included = session.exec(select(HarmonizationCohortStudy).where(
        HarmonizationCohortStudy.cohort_id == cohort.id,
        HarmonizationCohortStudy.included.is_(True))).all()
    expected = {row.subject_key_hmac for row in included}
    if seen != expected:
        raise HTTPException(409, "demographics IDs must exactly match included cohort subjects")
    if len({age for _, age, _ in parsed}) < 2 or len({sex for _, _, sex in parsed}) < 2:
        raise HTTPException(409, "MELD cohort requires non-zero age and sex variance")
    sex_counts = Counter(sex for _, _, sex in parsed)
    if min(sex_counts.values()) < cohort.cv_folds:
        raise HTTPException(
            409, "each normalized sex stratum needs at least one control per CV fold")
    for row in session.exec(select(HarmonizationDemographic).where(
            HarmonizationDemographic.cohort_id == cohort.id)).all():
        session.delete(row)
    for digest, age, sex in parsed:
        session.add(HarmonizationDemographic(
            cohort_id=cohort.id, subject_key_hmac=digest, age=age, sex=sex))
    minimized = sorted(
        ({"subject_key_hmac": digest, "age": age, "sex": sex}
         for digest, age, sex in parsed), key=lambda row: row["subject_key_hmac"])
    cohort.demographics_manifest = {
        "count": len(parsed), "sha256": canonical_sha256(minimized),
        "age_has_variance": True, "sex_has_variance": True,
    }
    cohort.status = (HarmonizationCohortStatus.cohort_ready
                     if len(included) >= cohort.min_controls else HarmonizationCohortStatus.draft)
    session.add(cohort)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.cohort.demographics_import",
        entity_type="harmonization_cohort", entity_id=cohort.id,
        payload={"count": len(parsed), "manifest_sha256": cohort.demographics_manifest["sha256"]},
    )
    session.commit()
    return _cohort_public(session, cohort, detail=True, include_upload_subjects=True)


@router.post("/harmonization/cohorts/{cohort_id}/freeze")
def freeze_cohort(cohort_id: str, principal: Principal = Depends(require_admin),
                  session: Session = Depends(get_session)) -> dict:
    cohort = _mutable_cohort(session, cohort_id)
    studies = session.exec(select(HarmonizationCohortStudy).where(
        HarmonizationCohortStudy.cohort_id == cohort.id)).all()
    included = [row for row in studies if row.included]
    demographics = session.exec(select(HarmonizationDemographic).where(
        HarmonizationDemographic.cohort_id == cohort.id)).all()
    if len(included) < cohort.min_controls:
        raise HTTPException(409, f"cohort needs at least {cohort.min_controls} included controls")
    for study in included:
        selected = [item for item in study.series_manifest if item.get("selected_source") is True]
        if len(selected) != 1 or not selected[0].get("instance_manifest"):
            raise HTTPException(409, "every included control needs one content-hashed source series")
        instances = selected[0]["instance_manifest"]
        if (int(selected[0].get("instance_count") or 0) != len(instances)
                or any(re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))) is None
                       or int(item.get("size", 0)) <= 0 for item in instances)
                or not isinstance(selected[0].get("deidentification_policy"), dict)):
            raise HTTPException(409, "source instance or deidentification manifest is incomplete")
    if {row.subject_key_hmac for row in included} != {row.subject_key_hmac for row in demographics}:
        raise HTTPException(409, "demographics do not exactly cover included controls")
    fingerprints = {row.acquisition_fingerprint for row in included}
    if len(fingerprints) != 1:
        raise HTTPException(409, "one cohort must contain one scanner/protocol fingerprint")
    plan = deterministic_folds([{
        "subject_key_hmac": row.subject_key_hmac, "age": row.age, "sex": row.sex,
    } for row in demographics], cohort.cv_folds)
    manifest = frozen_manifest(cohort=cohort, studies=studies, demographics=demographics)
    manifest["cv_plan_sha256"] = canonical_sha256(plan)
    manifest["manifest_sha256"] = canonical_sha256(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"})
    cohort.frozen_manifest = manifest
    cohort.status = HarmonizationCohortStatus.frozen
    cohort.approved_by = principal.actor
    cohort.frozen_at = datetime.now(timezone.utc)
    session.add(cohort)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.cohort.freeze",
        entity_type="harmonization_cohort", entity_id=cohort.id,
        payload={"included": len(included), "excluded": len(studies) - len(included),
                 "manifest_sha256": manifest["manifest_sha256"], "cv_folds": cohort.cv_folds},
    )
    session.commit()
    return _cohort_public(session, cohort, detail=True, include_upload_subjects=True)


@router.post("/harmonization/cohorts/{cohort_id}/uploads", status_code=201)
def create_upload(cohort_id: str, body: UploadCreate,
                  principal: Principal = Depends(require_admin),
                  session: Session = Depends(get_session)) -> dict:
    _assert_no_pending_orthanc_rollback(session)
    cohort = _mutable_cohort(session, cohort_id)
    if body.total_size > settings.harmonization_max_upload_bytes:
        raise HTTPException(413, "upload exceeds configured harmonization limit")
    # Serialize reservations across cohorts.  Free-space snapshots alone allow two concurrent
    # cohorts to reserve the same bytes on one filesystem.
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(5567949153544167252)"))
    staged_statuses = [
        HarmonizationUploadStatus.receiving,
        HarmonizationUploadStatus.staged,
        HarmonizationUploadStatus.importing,
    ]
    used = int(session.exec(select(func.coalesce(func.sum(HarmonizationUpload.total_size), 0)).where(
        HarmonizationUpload.cohort_id == cohort.id,
        HarmonizationUpload.status.in_(staged_statuses))).one())
    if used + body.total_size > settings.harmonization_cohort_quota_bytes:
        raise HTTPException(413, "upload exceeds this cohort's configured storage quota")
    suffix = Path(body.filename).suffix.lower()
    row = HarmonizationUpload(
        cohort_id=cohort.id, filename="pending" + suffix, content_type=body.content_type,
        total_size=body.total_size, sha256=body.sha256.lower(), created_by=principal.actor,
    )
    # Original workstation filenames commonly leak names/MRNs. Retain only an opaque upload ID and
    # the validated format suffix; the browser can display its local filename during transfer.
    row.filename = f"upload-{row.id}{suffix}"
    root = Path(settings.harmonization_upload_root)
    root.mkdir(parents=True, exist_ok=True)
    reserved_remaining = int(session.exec(select(func.coalesce(func.sum(
        HarmonizationUpload.total_size - HarmonizationUpload.received_size), 0)).where(
            HarmonizationUpload.status == HarmonizationUploadStatus.receiving)).one())
    capacity = storage_health(
        str(root), minimum_free_bytes=(settings.storage_min_free_bytes
                                      + reserved_remaining + body.total_size),
        minimum_free_percent=settings.storage_min_free_percent,
    )
    if not capacity["ready"]:
        raise HTTPException(507, "harmonization upload storage is below its admission watermark")
    path = root / row.storage_key
    path.touch(mode=0o600, exist_ok=False)
    try:
        session.add(row)
        audit.record_authenticated(
            session, principal=principal, action="harmonization.upload.create",
            entity_type="harmonization_upload", entity_id=row.id,
            payload={"total_size": row.total_size, "sha256": row.sha256,
                     "archive": suffix == ".zip"},
        )
        session.commit()
    except Exception:
        session.rollback()
        path.unlink(missing_ok=True)
        raise
    return {"id": row.id, "status": row.status, "received_size": 0,
            "total_size": row.total_size, "max_chunk_size": settings.harmonization_upload_chunk_bytes}


@router.put("/harmonization/cohorts/{cohort_id}/uploads/{upload_id}")
async def upload_chunk(cohort_id: str, upload_id: str, request: Request,
                       offset: int = Query(ge=0), principal: Principal = Depends(require_admin),
                       session: Session = Depends(get_session)) -> dict:
    _mutable_cohort(session, cohort_id)
    statement = select(HarmonizationUpload).where(
        HarmonizationUpload.id == upload_id, HarmonizationUpload.cohort_id == cohort_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = session.exec(statement).first()
    if row is None:
        raise HTTPException(404, "upload not found")
    if row.status != HarmonizationUploadStatus.receiving or offset != row.received_size:
        raise HTTPException(409, "upload offset/status mismatch")
    path = Path(settings.harmonization_upload_root) / row.storage_key
    if not path.is_file() or path.is_symlink():
        raise HTTPException(409, "upload staging file does not match durable offset")
    durable_size = path.stat().st_size
    if durable_size < offset:
        raise HTTPException(409, "upload staging file is shorter than its durable offset")
    # Bytes are fsynced before the SQL offset commit.  After a process crash the file can be
    # longer than the authoritative database offset; truncating only that uncommitted suffix makes
    # the same chunk retry safe and deterministic.
    if durable_size > offset:
        with path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            os.fsync(handle.fileno())
    size = 0
    with path.open("r+b") as handle:
        handle.seek(offset)
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if (size > settings.harmonization_upload_chunk_bytes
                        or row.received_size + size > row.total_size):
                    raise HTTPException(413, "chunk exceeds configured upload limits")
                handle.write(chunk)
            if size == 0:
                raise HTTPException(422, "upload chunk cannot be empty")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.truncate(offset)
            handle.flush()
            os.fsync(handle.fileno())
            raise
    row.received_size += size
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        with path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            os.fsync(handle.fileno())
        raise
    return {"id": row.id, "status": row.status, "received_size": row.received_size,
            "total_size": row.total_size}


@router.get("/harmonization/cohorts/{cohort_id}/uploads/{upload_id}")
def get_upload(cohort_id: str, upload_id: str,
               principal: Principal = Depends(require_admin),
               session: Session = Depends(get_session)) -> dict:
    row = session.exec(select(HarmonizationUpload).where(
        HarmonizationUpload.id == upload_id,
        HarmonizationUpload.cohort_id == cohort_id)).first()
    if row is None:
        raise HTTPException(404, "upload not found")
    audit.record_access(session, principal=principal, entity_type="harmonization_upload",
                        entity_id=row.id, detail={"status": row.status.value})
    session.commit()
    return {
        "id": row.id, "status": row.status, "received_size": row.received_size,
        "total_size": row.total_size,
        "max_chunk_size": settings.harmonization_upload_chunk_bytes,
        "import_result": row.import_result, "last_error": row.last_error,
    }


@router.get("/harmonization/cohorts/{cohort_id}/uploads/{upload_id}/rollback-evidence")
def get_upload_rollback_evidence(
        cohort_id: str, upload_id: str,
        principal: Principal = Depends(require_admin),
        session: Session = Depends(get_session)) -> dict:
    row = session.exec(select(HarmonizationUpload).where(
        HarmonizationUpload.id == upload_id,
        HarmonizationUpload.cohort_id == cohort_id,
    )).first()
    result = dict(row.import_result or {}) if row is not None else {}
    if (row is None or row.status != HarmonizationUploadStatus.failed
            or result.get("phase") not in {
                "rollback_incomplete", "rollback_delete_approved"}):
        raise HTTPException(409, "upload has no protected rollback evidence")
    receipt = Path(settings.harmonization_upload_root) / f"{row.storage_key}.receipt"
    try:
        header, intents, completed = upload_receipt.load_receipt(receipt)
        upload_receipt.validate_header(
            header,
            upload_sha256=row.sha256,
            instance_manifest_sha256=result.get("instance_manifest_sha256"),
            instance_count=result.get("instance_count"),
        )
        digest = upload_receipt.evidence_sha256(header, intents, completed)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(409, "protected rollback receipt is missing or invalid") from exc
    if digest != result.get("receipt_evidence_sha256"):
        raise HTTPException(409, "protected rollback receipt evidence has changed")
    evidence = [{
        "file_sha256": file_sha256,
        "sop_instance_uid": intent["sop_instance_uid"],
        "size": intent["size"],
        "orthanc_instance_id": (completed.get(file_sha256) or {}).get("instance_id"),
        "worker_owned": (completed.get(file_sha256) or {}).get("owned"),
        "response_recorded": file_sha256 in completed,
    } for file_sha256, intent in sorted(intents.items())]
    audit.record_access(
        session, principal=principal, entity_type="harmonization_upload_rollback_evidence",
        entity_id=row.id, detail={"receipt_evidence_sha256": digest,
                                  "instance_count": len(evidence)},
    )
    session.commit()
    return {
        "schema_version": 1,
        "upload_id": row.id,
        "receipt_evidence_sha256": digest,
        "pending_counts": {key: int(result.get(key, 0)) for key in (
            "owned_delete_failures", "ambiguous_instances",
            "candidate_verification_failures", "receipt_integrity_failures",
            "referenced_instances",
        )},
        "instances": evidence,
    }


@router.post("/harmonization/cohorts/{cohort_id}/uploads/{upload_id}/complete")
def complete_upload(cohort_id: str, upload_id: str,
                    principal: Principal = Depends(require_admin),
                    session: Session = Depends(get_session)) -> dict:
    initial = session.exec(select(HarmonizationUpload).where(
        HarmonizationUpload.id == upload_id,
        HarmonizationUpload.cohort_id == cohort_id,
    )).first()
    if initial is None:
        raise HTTPException(404, "upload not found")
    if initial.status in {
            HarmonizationUploadStatus.staged,
            HarmonizationUploadStatus.importing,
            HarmonizationUploadStatus.imported,
    }:
        return {"id": initial.id, "status": initial.status,
                "received_size": initial.received_size,
                "total_size": initial.total_size, "import_result": initial.import_result}
    if initial.status != HarmonizationUploadStatus.receiving:
        raise HTTPException(409, "failed upload sessions cannot be completed")
    # Release the read snapshot before acquiring the global mutation fence and row locks in their
    # canonical order. Recheck the row below to retain idempotency across a concurrent completion.
    session.rollback()
    _assert_no_pending_orthanc_rollback(session)
    _mutable_cohort(session, cohort_id)
    statement = select(HarmonizationUpload).where(
        HarmonizationUpload.id == upload_id,
        HarmonizationUpload.cohort_id == cohort_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = session.exec(statement).first()
    if row is None:
        raise HTTPException(404, "upload not found")
    if row.status in {
            HarmonizationUploadStatus.staged,
            HarmonizationUploadStatus.importing,
            HarmonizationUploadStatus.imported,
    }:
        return {"id": row.id, "status": row.status, "received_size": row.received_size,
                "total_size": row.total_size, "import_result": row.import_result}
    if row.status != HarmonizationUploadStatus.receiving:
        raise HTTPException(409, "failed upload sessions cannot be completed")
    path = Path(settings.harmonization_upload_root) / row.storage_key
    regular = path.is_file() and not path.is_symlink()
    staged_size = path.stat().st_size if regular else -1
    actual = sha256_file(path) if regular else ""
    if row.received_size != row.total_size or staged_size != row.total_size or actual != row.sha256:
        row.status, row.last_error = HarmonizationUploadStatus.failed, "size_or_hash_mismatch"
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            pass
        session.add(row)
        audit.record_authenticated(
            session, principal=principal, action="harmonization.upload.reject",
            entity_type="harmonization_upload", entity_id=row.id,
            payload={"reason": row.last_error, "received_size": row.received_size},
        )
        session.commit()
        raise HTTPException(409, "completed upload size or checksum differs")
    row.status = HarmonizationUploadStatus.staged
    row.completed_at = datetime.now(timezone.utc)
    row.updated_at = row.completed_at
    session.add(row)
    event = OutboxEvent(
        dedupe_key=f"harmonization.upload.ingest:{row.id}", topic="harmonization.upload.ingest",
        aggregate_type="harmonization_upload", aggregate_id=row.id,
        payload={"upload_id": row.id},
    )
    session.add(event)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.upload.complete",
        entity_type="harmonization_upload", entity_id=row.id,
        payload={"size": row.total_size, "sha256": row.sha256},
    )
    session.commit()
    return {"id": row.id, "status": row.status, "received_size": row.received_size,
            "total_size": row.total_size}


@router.post("/harmonization/cohorts/{cohort_id}/uploads/{upload_id}/rollback-resolution")
def resolve_upload_rollback(
        cohort_id: str, upload_id: str, body: UploadRollbackResolution,
        principal: Principal = Depends(require_admin),
        session: Session = Depends(get_session)) -> dict:
    statement = select(HarmonizationUpload).where(
        HarmonizationUpload.id == upload_id,
        HarmonizationUpload.cohort_id == cohort_id,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = session.exec(statement).first()
    if row is None:
        raise HTTPException(404, "upload not found")
    current = dict(row.import_result or {})
    if (row.status != HarmonizationUploadStatus.failed
            or current.get("phase") not in {
                "rollback_incomplete", "rollback_delete_approved"}):
        raise HTTPException(409, "upload has no ambiguous rollback awaiting resolution")
    now = datetime.now(timezone.utc)
    owned_failures = int(current.get("owned_delete_failures", 0))
    ambiguous = int(current.get("ambiguous_instances", 0))
    integrity_failures = int(current.get("receipt_integrity_failures", 0))
    candidate_failures = int(current.get("candidate_verification_failures", 0))
    referenced = int(current.get("referenced_instances", 0))
    receipt_evidence = str(current.get("receipt_evidence_sha256", ""))
    if body.action == "preserve":
        if (integrity_failures or candidate_failures or (owned_failures and not referenced)
                or not (ambiguous or referenced)):
            raise HTTPException(
                409, "preserve is allowed only for ambiguous or already referenced objects")
    elif (integrity_failures or referenced
          or re.fullmatch(r"[0-9a-f]{64}", receipt_evidence) is None
          or body.evidence_sha256.lower() != receipt_evidence):
        raise HTTPException(
            422, "exact deletion requires no cohort reference and the canonical receipt SHA-256")
    resolution = {
        "action": body.action,
        "reason": body.reason,
        "evidence_sha256": body.evidence_sha256.lower(),
        "approved_by": principal.actor,
        "approved_at": now.isoformat(),
    }
    current["resolution"] = resolution
    if body.action == "preserve":
        current["phase"] = "failed"
        current["rollback_pending_instances"] = 0
        current["owned_delete_failures"] = 0
        current["ambiguous_instances"] = 0
        current["receipt_integrity_failures"] = 0
        current["candidate_verification_failures"] = 0
        current["referenced_instances"] = 0
        row.last_error = "rollback_ambiguity_preserved_by_admin"
    else:
        current["phase"] = "rollback_delete_approved"
        row.last_error = "rollback_ambiguity_delete_approved"
    row.import_result = current
    row.updated_at = now
    session.add(row)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.upload.rollback_resolve",
        entity_type="harmonization_upload", entity_id=row.id,
        payload={
            "resolution": body.action,
            "reason_sha256": canonical_sha256(body.reason),
            "evidence_sha256": body.evidence_sha256.lower(),
            "pending_instances": current.get("rollback_pending_instances"),
        },
    )
    session.commit()
    if body.action == "preserve":
        receipt = Path(settings.harmonization_upload_root) / f"{row.storage_key}.receipt"
        try:
            root = Path(settings.harmonization_upload_root).resolve()
            if receipt.parent.resolve() == root and receipt.is_file() and not receipt.is_symlink():
                receipt.unlink()
        except OSError:
            pass
    return {
        "id": row.id, "status": row.status,
        "import_result": row.import_result, "last_error": row.last_error,
    }


@router.post("/harmonization/cohorts/{cohort_id}/builds", status_code=201)
async def create_build(cohort_id: str, body: BuildCreate,
                       principal: Principal = Depends(require_admin),
                       session: Session = Depends(get_session)) -> dict:
    adapter_sha256 = settings.harmonization_builder_adapter_sha256
    if adapter_sha256 is None:
        raise HTTPException(
            503, "site-accepted harmonization builder adapter is not configured")
    if settings.is_server_mode:
        try:
            raw_heartbeat = await queue.get_redis().get(
                settings.harmonization_builder_heartbeat_key)
            builder_health = queue.verify_worker_heartbeat(
                raw_heartbeat,
                max_age_s=settings.harmonization_builder_heartbeat_max_age_s,
                expected_capacity_kind="harmonization-builder",
                expected_images={"meld": settings.meld_image},
                expected_adapter_sha256=adapter_sha256,
            )
        except Exception as exc:
            raise HTTPException(503, "harmonization builder heartbeat is unavailable") from exc
        if not builder_health.get("ready"):
            raise HTTPException(503, "harmonization builder service is not ready")
        if (builder_health.get("capacity") or {}).get("adapter_ready") is not True:
            raise HTTPException(
                503, "site-accepted harmonization builder adapter is not configured")
    # Heartbeats are necessarily sampled. The durable database gate and shared rollback mutation
    # fence close the interval between the last healthy heartbeat and a failed import receipt.
    _assert_no_pending_orthanc_rollback(session)
    # Serialize admission across cohorts.  Row locks alone only protect two
    # requests for the same cohort, so concurrent requests for different
    # cohorts could otherwise both observe an empty live-build set.
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(5567949153544167253)"))
    statement = select(HarmonizationCohort).where(HarmonizationCohort.id == cohort_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    cohort = session.exec(statement).first()
    if cohort is None:
        raise HTTPException(404, "harmonization cohort not found")
    if cohort.status != HarmonizationCohortStatus.frozen or not cohort.frozen_manifest:
        raise HTTPException(409, "cohort must be frozen before building")
    if cohort.approved_by != principal.actor:
        raise HTTPException(403, "the administrator who froze the cohort must start its build")
    if settings.is_server_mode and body.builder_image_digest != settings.meld_image:
        raise HTTPException(409, "builder image must equal the signed MELD image for this release")
    active = session.exec(select(HarmonizationBuild).where(
        HarmonizationBuild.status.in_([
            HarmonizationBuildStatus.queued, HarmonizationBuildStatus.building,
            HarmonizationBuildStatus.qc_review, HarmonizationBuildStatus.validated,
        ]))).first()
    if active:
        raise HTTPException(
            409, "only one live harmonization build is permitted on this server")
    if session.exec(select(HarmonizationBuild).where(
            HarmonizationBuild.cohort_id == cohort.id,
            HarmonizationBuild.status == HarmonizationBuildStatus.active)).first():
        raise HTTPException(409, "cohort already has an active profile")
    attempt = int(session.exec(select(func.count(HarmonizationBuild.id)).where(
        HarmonizationBuild.cohort_id == cohort.id)).one()) + 1
    demographics = session.exec(select(HarmonizationDemographic).where(
        HarmonizationDemographic.cohort_id == cohort.id)).all()
    cv_plan = deterministic_folds([{
        "subject_key_hmac": row.subject_key_hmac, "age": row.age, "sex": row.sex,
    } for row in demographics], cohort.cv_folds)
    build = HarmonizationBuild(
        cohort_id=cohort.id, attempt=attempt, initiated_by=principal.actor,
        builder_image_digest=body.builder_image_digest,
        builder_adapter_sha256=adapter_sha256,
        acceptance_criteria=body.acceptance_criteria, cv_plan={"folds": cv_plan},
    )
    session.add(build)
    session.add(OutboxEvent(
        dedupe_key=f"harmonization.build.enqueue:{build.id}:attempt:{attempt}",
        topic="harmonization.build.enqueue", aggregate_type="harmonization_build",
        aggregate_id=build.id, payload={"build_id": build.id, "attempt": attempt},
    ))
    audit.record_authenticated(
        session, principal=principal, action="harmonization.build.queue",
        entity_type="harmonization_build", entity_id=build.id,
        payload={"cohort_manifest_sha256": cohort.frozen_manifest["manifest_sha256"],
                 "builder_image_digest": body.builder_image_digest,
                 "builder_adapter_sha256": adapter_sha256,
                 "acceptance_criteria_sha256": canonical_sha256(body.acceptance_criteria)},
    )
    session.commit()
    return _build_public(build)


@router.get("/harmonization/builds/{build_id}")
def get_build(build_id: str, principal: Principal = Depends(require_harmonization_operator),
              session: Session = Depends(get_session)) -> dict:
    build = session.get(HarmonizationBuild, build_id)
    if build is None:
        raise HTTPException(404, "harmonization build not found")
    result = _build_public(build)
    folds = session.exec(select(HarmonizationFoldResult).where(
        HarmonizationFoldResult.build_id == build.id
    ).order_by(HarmonizationFoldResult.fold_index)).all()
    result["fold_results"] = [{
        "fold_index": row.fold_index, "train_count": row.train_count,
        "holdout_count": row.holdout_count, "membership_hmac_sha256": row.membership_hmac_sha256,
        "status": row.status, "metrics": row.metrics,
        "resource_usage": row.resource_usage,
    } for row in folds]
    audit.record_access(session, principal=principal, entity_type="harmonization_build",
                        entity_id=build.id, detail={"status": build.status.value})
    session.commit()
    return result


@router.get("/harmonization/builds/{build_id}/qc")
def get_build_qc(build_id: str, principal: Principal = Depends(require_harmonization_operator),
                 session: Session = Depends(get_session)) -> dict:
    build = session.get(HarmonizationBuild, build_id)
    if build is None:
        raise HTTPException(404, "harmonization build not found")
    if build.status not in {HarmonizationBuildStatus.qc_review, HarmonizationBuildStatus.validated,
                            HarmonizationBuildStatus.active}:
        raise HTTPException(409, "QC is unavailable until cross-validation completes")
    audit.record_access(session, principal=principal, entity_type="harmonization_build_qc",
                        entity_id=build.id, detail={})
    session.commit()
    return build.qc_report or {}


@router.post("/harmonization/builds/{build_id}/cancel")
def cancel_build(build_id: str, principal: Principal = Depends(require_admin),
                 session: Session = Depends(get_session)) -> dict:
    build = _locked_build(session, build_id)
    if build is None:
        raise HTTPException(404, "harmonization build not found")
    if build.status not in {HarmonizationBuildStatus.queued, HarmonizationBuildStatus.building}:
        raise HTTPException(409, "only a queued or running build may be cancelled")
    if build.status == HarmonizationBuildStatus.building and build.stage == "publishing":
        raise HTTPException(
            409, "publication is already durable and must finish deterministic reconciliation")
    previous_stage = build.stage
    build.status, build.stage = HarmonizationBuildStatus.cancelled, "cancelled"
    build.completed_at = datetime.now(timezone.utc)
    build.heartbeat_at = None
    build.lease_expires_at = None
    session.add(build)
    audit.record_authenticated(session, principal=principal, action="harmonization.build.cancel",
                               entity_type="harmonization_build", entity_id=build.id,
                               payload={"previous_stage": previous_stage})
    session.commit()
    return _build_public(build)


def _generated_root(profile: HarmonizationProfile) -> str:
    return (settings.harmonization_generated_root
            if (profile.parameters or {}).get("storage_scope") == "generated"
            else settings.harmonization_root)


def _validate_build_evidence(session: Session, build: HarmonizationBuild,
                             cohort: HarmonizationCohort,
                             profile: HarmonizationProfile) -> None:
    qc = build.qc_report or {}
    artifact_manifest = build.artifact_manifest or {}
    parameters = profile.parameters or {}
    scientific_validation = parameters.get("scientific_validation") or {}
    frozen = cohort.frozen_manifest or {}
    studies = session.exec(select(HarmonizationCohortStudy).where(
        HarmonizationCohortStudy.cohort_id == cohort.id,
        HarmonizationCohortStudy.included.is_(True))).all()
    demographics = session.exec(select(HarmonizationDemographic).where(
        HarmonizationDemographic.cohort_id == cohort.id)).all()
    frozen_studies = {
        row.get("subject_key_hmac"): row for row in frozen.get("studies", [])
        if isinstance(row, dict)
    }
    study_evidence_matches = (
        len(studies) == len(frozen_studies)
        and all(
            row.subject_key_hmac in frozen_studies
            and frozen_studies[row.subject_key_hmac].get("study_sha256") == row.study_sha256
            and frozen_studies[row.subject_key_hmac].get("acquisition_fingerprint")
            == row.acquisition_fingerprint
            and frozen_studies[row.subject_key_hmac].get("series_manifest_sha256")
            == canonical_sha256(row.series_manifest)
            for row in studies
        )
    )
    demographic_evidence = sorted(({
        "subject_key_hmac": row.subject_key_hmac, "age": row.age, "sex": row.sex,
    } for row in demographics), key=lambda row: row["subject_key_hmac"])
    if (cohort.status != HarmonizationCohortStatus.frozen
            or qc.get("all_folds_succeeded") is not True
            or qc.get("report_sha256") != canonical_sha256({
                key: value for key, value in qc.items() if key != "report_sha256"})
            or frozen.get("manifest_sha256") != canonical_sha256({
                key: value for key, value in frozen.items() if key != "manifest_sha256"})
            or qc.get("cohort_manifest_sha256") != frozen.get("manifest_sha256")
            or qc.get("acceptance_criteria_sha256") != canonical_sha256(
                build.acceptance_criteria)
            or frozen.get("selector") != cohort.selector
            or frozen.get("source_role") != cohort.source_role.value
            or frozen.get("profile") != {
                "code": cohort.profile_code, "version": cohort.profile_version,
                "detector_id": "meld_fcd",
            }
            or frozen.get("cv_plan_sha256") != canonical_sha256(
                (build.cv_plan or {}).get("folds"))
            or frozen.get("demographics_sha256") != canonical_sha256(demographic_evidence)
            or not study_evidence_matches
            or build.artifact_manifest != profile.artifact_manifest
            or parameters.get("internal_cv_report_sha256")
            != qc.get("report_sha256")
            or parameters.get("build_images", {}).get("meld")
            != build.builder_image_digest
            or re.fullmatch(
                r"[0-9a-f]{64}", str(build.builder_adapter_sha256 or "")
            ) is None
            or qc.get("builder_adapter_sha256") != build.builder_adapter_sha256
            or artifact_manifest.get("builder_adapter_sha256")
            != build.builder_adapter_sha256
            or parameters.get("builder_adapter_sha256") != build.builder_adapter_sha256
            or not isinstance(scientific_validation, dict)
            or scientific_validation.get("builder_adapter_sha256")
            != build.builder_adapter_sha256):
        raise ValueError("build QC, cohort, artifact, or builder evidence is inconsistent")
    plan = (build.cv_plan or {}).get("folds")
    folds = session.exec(select(HarmonizationFoldResult).where(
        HarmonizationFoldResult.build_id == build.id
    ).order_by(HarmonizationFoldResult.fold_index)).all()
    if (not isinstance(plan, list) or len(plan) != cohort.cv_folds or len(folds) != len(plan)
            or any(row.fold_index != index or row.status != "passed"
                   or row.membership_hmac_sha256 != plan[index].get("membership_hmac_sha256")
                   or row.train_count != len(plan[index].get("train_subject_hmacs", []))
                   or row.holdout_count != len(plan[index].get("holdout_subject_hmacs", []))
                   for index, row in enumerate(folds))
            or qc.get("metrics") != [row.metrics for row in folds]
            or qc.get("resource_usage") != [row.resource_usage for row in folds]):
        raise ValueError("cross-validation fold evidence is incomplete or inconsistent")


@router.post("/harmonization/builds/{build_id}/validate")
def validate_build(build_id: str, body: BuildValidation,
                   principal: Principal = Depends(require_admin),
                   session: Session = Depends(get_session)) -> dict:
    build = _locked_build(session, build_id)
    if build is None:
        raise HTTPException(404, "harmonization build not found")
    if build.status != HarmonizationBuildStatus.qc_review or not build.profile_id:
        raise HTTPException(409, "build is not awaiting QC validation")
    if build.initiated_by == principal.actor:
        raise HTTPException(403, "build validation requires an independent administrator")
    profile = session.get(HarmonizationProfile, build.profile_id)
    if profile is None or profile.status != HarmonizationProfileStatus.draft:
        raise HTTPException(409, "build candidate profile is unavailable")
    report = body.scientific_validation
    if report.get("builder_adapter_sha256") != build.builder_adapter_sha256:
        raise HTTPException(
            422, "scientific validation adapter differs from the admitted build")
    if str(report.get("methodology_sha256", "")).lower() != str(
            build.acceptance_criteria.get("methodology_sha256", "")).lower():
        raise HTTPException(
            422, "scientific validation methodology differs from the build acceptance policy")
    if report.get("independent_reviewer") not in {principal.actor, principal.subject}:
        raise HTTPException(403, "validation report reviewer does not match authenticated validator")
    cohort_studies = session.exec(select(HarmonizationCohortStudy).where(
        HarmonizationCohortStudy.cohort_id == build.cohort_id)).all()
    included_studies = [row for row in cohort_studies if row.included]
    report_qc = report.get("qc") or {}
    if (report_qc.get("included") != len(included_studies)
            or report_qc.get("excluded") != len(cohort_studies) - len(included_studies)):
        raise HTTPException(422, "scientific validation QC counts differ from frozen cohort")
    if set(report.get("acquisition_fingerprints") or []) != {
            row.acquisition_fingerprint for row in included_studies}:
        raise HTTPException(
            422, "scientific validation acquisition fingerprints differ from frozen cohort")
    profile.parameters = {**(profile.parameters or {}), "scientific_validation": report}
    try:
        cohort = session.get(HarmonizationCohort, build.cohort_id)
        if cohort is None:
            raise ValueError("frozen cohort is unavailable")
        _validate_build_evidence(session, build, cohort, profile)
        verified = verify_artifact_manifest(profile.artifact_manifest, _generated_root(profile))
        validate_profile_semantics(profile, verified)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"candidate validation failed: {exc}") from exc
    profile.status = HarmonizationProfileStatus.validated
    profile.validated_by = principal.actor
    profile.validated_at = datetime.now(timezone.utc)
    profile.validation_summary = {
        "artifact_manifest": verified,
        "scientific": {key: report.get(key) for key in (
            "approval_id", "approved_at", "methodology_sha256",
            "golden_case_evidence_sha256", "metrics_sha256",
            "builder_adapter_sha256")},
        "internal_cross_validation": build.qc_report,
    }
    build.status, build.stage, build.validated_by = (
        HarmonizationBuildStatus.validated, "validated", principal.actor)
    session.add(profile)
    session.add(build)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.build.validate",
        entity_type="harmonization_build", entity_id=build.id,
        payload={"profile_id": profile.id, "approval_id": report.get("approval_id"),
                 "qc_report_sha256": canonical_sha256(build.qc_report),
                 "builder_adapter_sha256": build.builder_adapter_sha256},
    )
    session.commit()
    return _build_public(build)


@router.post("/harmonization/builds/{build_id}/reject")
def reject_build(build_id: str, body: BuildRejection,
                 principal: Principal = Depends(require_admin),
                 session: Session = Depends(get_session)) -> dict:
    build = _locked_build(session, build_id)
    if build is None:
        raise HTTPException(404, "harmonization build not found")
    if build.status != HarmonizationBuildStatus.qc_review or not build.profile_id:
        raise HTTPException(409, "only a candidate awaiting QC review may be rejected")
    if build.initiated_by == principal.actor:
        raise HTTPException(403, "candidate rejection requires an independent administrator")
    profile = session.get(HarmonizationProfile, build.profile_id)
    cohort = session.get(HarmonizationCohort, build.cohort_id)
    if (profile is None or profile.status != HarmonizationProfileStatus.draft
            or cohort is None or cohort.status != HarmonizationCohortStatus.frozen):
        raise HTTPException(409, "candidate profile or frozen cohort is unavailable")
    now = datetime.now(timezone.utc)
    profile.status = HarmonizationProfileStatus.retired
    build.status = HarmonizationBuildStatus.failed
    build.stage = "rejected"
    build.error_code = "scientific_validation_rejected"
    build.validated_by = principal.actor
    build.completed_at = now
    build.rejection_summary = {
        "reason": body.reason,
        "evidence_sha256": body.evidence_sha256.lower(),
        "rejected_by": principal.actor,
        "rejected_at": now.isoformat(),
        "requires_new_profile_version": True,
    }
    cohort.status = HarmonizationCohortStatus.archived
    session.add(profile)
    session.add(build)
    session.add(cohort)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.build.reject",
        entity_type="harmonization_build", entity_id=build.id,
        payload={
            "profile_id": profile.id,
            "reason_sha256": canonical_sha256(body.reason),
            "evidence_sha256": body.evidence_sha256.lower(),
            "requires_new_profile_version": True,
        },
    )
    session.commit()
    mark_profile_integrity_dirty()
    return _build_public(build)


@router.post("/harmonization/builds/{build_id}/activate")
def activate_build(build_id: str, principal: Principal = Depends(require_admin),
                   session: Session = Depends(get_session)) -> dict:
    lock_profile_activation(session)
    build = _locked_build(session, build_id)
    if build is None:
        raise HTTPException(404, "harmonization build not found")
    if build.status != HarmonizationBuildStatus.validated or not build.profile_id:
        raise HTTPException(409, "build must pass independent validation first")
    if principal.actor in {build.initiated_by, build.validated_by}:
        raise HTTPException(403, "activation requires an administrator independent of build and validation")
    profile = session.get(HarmonizationProfile, build.profile_id)
    if profile is None or profile.status != HarmonizationProfileStatus.validated:
        raise HTTPException(409, "validated candidate profile is unavailable")
    for candidate in session.exec(select(HarmonizationProfile).where(
            HarmonizationProfile.status == HarmonizationProfileStatus.active)).all():
        if candidate.code == profile.code:
            raise HTTPException(409, "retire the active version of this profile code first")
        if candidate.detector_id == profile.detector_id and selectors_may_overlap(
                candidate.selector, profile.selector):
            raise HTTPException(409, "active profile has an overlapping scanner/protocol selector")
    try:
        cohort = session.get(HarmonizationCohort, build.cohort_id)
        if cohort is None:
            raise ValueError("frozen cohort is unavailable")
        _validate_build_evidence(session, build, cohort, profile)
        verified = verify_artifact_manifest(profile.artifact_manifest, _generated_root(profile))
        validate_profile_semantics(profile, verified)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"candidate activation failed: {exc}") from exc
    profile.status = HarmonizationProfileStatus.active
    build.status, build.stage, build.activated_by = (
        HarmonizationBuildStatus.active, "active", principal.actor)
    build.completed_at = datetime.now(timezone.utc)
    session.add(profile)
    session.add(build)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.build.activate",
        entity_type="harmonization_build", entity_id=build.id,
        payload={"profile_id": profile.id, "manifest_sha256": verified["manifest_sha256"],
                 "storage_scope": "generated",
                 "builder_adapter_sha256": build.builder_adapter_sha256},
    )
    session.commit()
    mark_profile_integrity_dirty()
    return _build_public(build)


@router.get("/harmonization/builds/{build_id}/release-export")
def export_build_for_release(
        build_id: str, principal: Principal = Depends(require_harmonization_operator),
        session: Session = Depends(get_session)) -> dict:
    """Return the signed-release document/copy plan for one locally active generated profile."""
    build = session.get(HarmonizationBuild, build_id)
    if (build is None or build.status != HarmonizationBuildStatus.active
            or not build.profile_id):
        raise HTTPException(409, "only an active generated build can be exported")
    profile = session.get(HarmonizationProfile, build.profile_id)
    cohort = session.get(HarmonizationCohort, build.cohort_id)
    if (profile is None or profile.status != HarmonizationProfileStatus.active or cohort is None
            or (profile.parameters or {}).get("storage_scope") != "generated"):
        raise HTTPException(409, "active generated profile evidence is unavailable")
    try:
        _validate_build_evidence(session, build, cohort, profile)
        verified = verify_artifact_manifest(profile.artifact_manifest, _generated_root(profile))
        validate_profile_semantics(profile, verified)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"generated profile export failed: {exc}") from exc
    parameters = {**(profile.parameters or {}), "storage_scope": "release"}
    document = {
        "code": profile.code, "version": profile.version, "name": profile.name,
        "method": profile.method,
        "detector_id": getattr(profile.detector_id, "value", profile.detector_id),
        "selector": profile.selector, "artifact_manifest": profile.artifact_manifest,
        "parameters": parameters,
    }
    inventory_entry = {
        "code": profile.code, "version": profile.version,
        "detector_id": document["detector_id"],
        "document_sha256": profile_document_sha256(document),
    }
    audit.record_access(
        session, principal=principal, entity_type="harmonization_release_export",
        entity_id=build.id,
        detail={"profile_id": profile.id,
                "document_sha256": inventory_entry["document_sha256"],
                "builder_adapter_sha256": build.builder_adapter_sha256},
    )
    session.commit()
    return {
        "schema_version": 1,
        "profile_document": document,
        "suggested_profile_path": f"profiles/{profile.code}-v{profile.version}.json",
        "expected_inventory_entry": inventory_entry,
        "builder_adapter_sha256": build.builder_adapter_sha256,
        "artifact_copy_plan": [{
            "generated_relative_path": item["path"],
            "release_relative_path": item["path"],
            "sha256": item["sha256"], "size": item.get("size"),
        } for item in profile.artifact_manifest.get("files", [])],
        "release_signing_required": True,
    }


@router.get("/harmonization/coverage")
def harmonization_coverage(principal: Principal = Depends(require_harmonization_operator),
                           session: Session = Depends(get_session)) -> dict:
    profiles = session.exec(select(HarmonizationProfile).where(
        HarmonizationProfile.status == HarmonizationProfileStatus.active)).all()
    if settings.is_server_mode:
        profiles = [profile for profile in profiles
                    if runtime_profile_trusted(session, profile)]
    series = session.exec(select(Series).where(
        Series.active.is_(True), Series.confirmed_role.is_not(None),
        Series.fingerprint.is_not(None))).all()
    grouped: dict[tuple[str, str], list[Series]] = {}
    for row in series:
        if row.confirmed_role == SeriesRole.unknown:
            continue
        grouped.setdefault((row.confirmed_role.value, row.fingerprint), []).append(row)
    observations = []
    current_keys: set[tuple[SeriesRole, str]] = set()
    for (role, fingerprint), rows in grouped.items():
        current_keys.add((SeriesRole(role), fingerprint))
        matches = rank_profiles(profiles, rows[0].acquisition or {}, role=role,
                                detector_id=DetectorId.meld_fcd.value)
        status = "uncovered" if not matches else (
            "ambiguous" if len(matches) > 1 and matches[0].score == matches[1].score else "covered")
        profile_id = matches[0].profile_id if status == "covered" else None
        observation = session.exec(select(AcquisitionObservation).where(
            AcquisitionObservation.detector_id == DetectorId.meld_fcd,
            AcquisitionObservation.source_role == SeriesRole(role),
            AcquisitionObservation.acquisition_fingerprint == fingerprint)).first()
        if observation is None:
            observation = AcquisitionObservation(
                detector_id=DetectorId.meld_fcd, source_role=SeriesRole(role),
                acquisition_fingerprint=fingerprint, acquisition=canonical_acquisition(
                    rows[0].acquisition or {}), first_seen_at=min(row.last_seen_at for row in rows),
            )
        observation.case_count = len({row.case_id for row in rows})
        observation.coverage_status = status
        observation.profile_id = profile_id
        observation.last_seen_at = max(row.last_seen_at for row in rows)
        session.add(observation)
        observations.append({
            "id": observation.id, "detector_id": "meld_fcd", "source_role": role,
            "acquisition_fingerprint": fingerprint, "acquisition": observation.acquisition,
            "case_count": observation.case_count, "status": status,
            "profile_id": profile_id, "candidate_profile_ids": [match.profile_id for match in matches],
            "first_seen_at": observation.first_seen_at, "last_seen_at": observation.last_seen_at,
        })
    for observation in session.exec(select(AcquisitionObservation).where(
            AcquisitionObservation.detector_id == DetectorId.meld_fcd)).all():
        if (observation.source_role, observation.acquisition_fingerprint) in current_keys:
            continue
        observation.coverage_status = "stale"
        session.add(observation)
        observations.append({
            "id": observation.id, "detector_id": "meld_fcd",
            "source_role": observation.source_role.value,
            "acquisition_fingerprint": observation.acquisition_fingerprint,
            "acquisition": observation.acquisition, "case_count": observation.case_count,
            "status": "stale", "profile_id": observation.profile_id,
            "candidate_profile_ids": [], "first_seen_at": observation.first_seen_at,
            "last_seen_at": observation.last_seen_at,
        })
    audit.record_access(session, principal=principal, entity_type="harmonization_coverage",
                        entity_id="meld_fcd", detail={"observations": len(observations)})
    session.commit()
    summary = {status: sum(row["status"] == status for row in observations)
               for status in ("covered", "uncovered", "ambiguous", "stale")}
    return {"summary": summary, "observations": sorted(
        observations, key=lambda row: (row["status"], row["source_role"],
                                       row["acquisition_fingerprint"]))}
