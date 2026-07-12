"""Versioned, researcher-confirmed multi-scanner/protocol harmonization workflow."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PydanticField, field_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from . import audit
from .auth import Principal, Role, get_principal, require_admin, require_submitter
from .config import settings
from .db import get_session
from .detectors import REGISTRY
from .harmonization import (
    PROFILE_METHOD_BY_DETECTOR,
    case_harmonization_coverage,
    canonical_acquisition,
    mark_profile_integrity_dirty,
    match_selector,
    lock_profile_activation,
    profile_artifact_root,
    rank_profiles,
    runtime_profile_trusted,
    selectors_may_overlap,
    validate_profile_semantics,
    validate_selector,
    verify_artifact_manifest,
)
from .models import (
    Case,
    CaseStatus,
    DetectorId,
    HarmonizationAssignment,
    HarmonizationProfile,
    HarmonizationProfileStatus,
    HarmonizationStatus,
    Series,
)


router = APIRouter(prefix="/api", tags=["harmonization"])


def _require_offline_profile_workflow() -> None:
    if settings.is_server_mode:
        raise HTTPException(
            403,
            "server profiles must come from signed release import or the linked cohort builder",
        )


def _case_access(principal: Principal, case: Case, *, mutate: bool = False) -> None:
    if (principal.has_role(Role.admin) or case.created_by == principal.actor
            or case.assigned_to == principal.actor):
        return
    if not mutate and principal.has_role(Role.reviewer):
        return
    raise HTTPException(403, "case access denied")


class ProfileCreate(BaseModel):
    code: str = PydanticField(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    version: int = PydanticField(ge=1)
    name: str = PydanticField(min_length=1, max_length=160)
    method: str = PydanticField(min_length=1, max_length=64,
                                pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    detector_id: Optional[DetectorId] = None
    selector: dict[str, Any]
    artifact_manifest: dict[str, Any]
    parameters: dict[str, Any] = PydanticField(default_factory=dict)

    @field_validator("selector", "artifact_manifest", "parameters")
    @classmethod
    def bounded_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(repr(value)) > 100_000:
            raise ValueError("profile JSON is too large")
        return value


class AssignmentCreate(BaseModel):
    profile_id: str = PydanticField(min_length=1, max_length=64)
    detector_id: DetectorId
    source_series_uid: str = PydanticField(
        min_length=1, max_length=64, pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    override_reason: Optional[str] = PydanticField(default=None, max_length=1000)


def _profile_public(profile: HarmonizationProfile) -> dict[str, Any]:
    validation = profile.validation_summary or {}
    artifact_validation = validation.get("artifact_manifest", validation)
    scientific_validation = validation.get("scientific", {})
    return {
        "id": profile.id, "code": profile.code, "version": profile.version,
        "name": profile.name, "method": profile.method,
        "detector_id": profile.detector_id,
        "status": profile.status,
        "generated": (profile.parameters or {}).get("storage_scope") == "generated",
        "validation_summary": ({
            "manifest_sha256": artifact_validation.get("manifest_sha256"),
            "artifact_count": len(artifact_validation.get("files", [])),
            "scientific_approval_id": scientific_validation.get("approval_id"),
            "scientifically_validated": bool(scientific_validation),
        } if profile.validation_summary is not None else None),
    }


def _assignment_public(assignment: HarmonizationAssignment | None) -> dict[str, Any] | None:
    if assignment is None:
        return None
    return {
        "id": assignment.id, "case_id": assignment.case_id,
        "profile_id": assignment.profile_id, "detector_id": assignment.detector_id,
        "source_series_uid": assignment.source_series_uid,
        "acquisition_fingerprint": assignment.acquisition_fingerprint,
        "status": assignment.status, "proposal_score": assignment.proposal_score,
        "proposal_reasons": assignment.proposal_reasons,
        "override_reason_present": bool(assignment.override_reason),
        "created_at": assignment.created_at, "confirmed_at": assignment.confirmed_at,
    }


@router.get("/harmonization/profiles")
def list_profiles(status: Optional[HarmonizationProfileStatus] = None,
                  principal: Principal = Depends(get_principal),
                  session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    if not (principal.has_role(Role.admin) or principal.has_role(Role.auditor)):
        raise HTTPException(403, "full harmonization profile inventory is admin/auditor only")
    statement = select(HarmonizationProfile).order_by(
        HarmonizationProfile.code, HarmonizationProfile.version.desc())
    if status:
        statement = statement.where(HarmonizationProfile.status == status)
    rows = session.exec(statement).all()
    audit.record_access(
        session, principal=principal, entity_type="harmonization_profile_collection",
        entity_id=status.value if status else "all", detail={"count": len(rows)},
    )
    session.commit()
    return [_profile_public(p) for p in rows]


@router.post("/harmonization/profiles", status_code=201)
def create_profile(body: ProfileCreate, principal: Principal = Depends(require_admin),
                   session: Session = Depends(get_session)) -> dict[str, Any]:
    _require_offline_profile_workflow()
    exists = session.exec(select(HarmonizationProfile).where(
        HarmonizationProfile.code == body.code,
        HarmonizationProfile.version == body.version,
    )).first()
    if exists:
        raise HTTPException(409, "profile code/version already exists")
    try:
        validate_selector(body.selector)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    detector = body.detector_id.value if body.detector_id else None
    expected_method = PROFILE_METHOD_BY_DETECTOR.get(detector) if detector else None
    if expected_method is not None and body.method != expected_method:
        raise HTTPException(
            422, f"detector {detector} requires harmonization method {expected_method}"
        )
    profile = HarmonizationProfile(
        code=body.code, version=body.version, name=body.name, method=body.method,
        detector_id=body.detector_id, selector=body.selector,
        artifact_manifest=body.artifact_manifest, parameters=body.parameters,
        created_by=principal.actor,
    )
    session.add(profile)
    audit.record_authenticated(session, principal=principal, action="harmonization.profile.create",
                               entity_type="harmonization_profile", entity_id=profile.id,
                               payload={"code": profile.code, "version": profile.version,
                                        "method": profile.method})
    session.commit()
    session.refresh(profile)
    return _profile_public(profile)


@router.post("/harmonization/profiles/{profile_id}/validate")
def validate_profile(profile_id: str, principal: Principal = Depends(require_admin),
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    _require_offline_profile_workflow()
    statement = select(HarmonizationProfile).where(HarmonizationProfile.id == profile_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    profile = session.exec(statement).first()
    if profile is None:
        raise HTTPException(404, "profile not found")
    if profile.status != HarmonizationProfileStatus.draft:
        raise HTTPException(409, "only a draft profile can be scientifically validated")
    if profile.created_by == principal.actor:
        raise HTTPException(403, "profile validation requires an independent administrator")
    report = profile.parameters.get("scientific_validation", {})
    if report.get("independent_reviewer") not in {principal.actor, principal.subject}:
        raise HTTPException(
            403, "authenticated validator does not match the signed independent reviewer"
        )
    try:
        verified = verify_artifact_manifest(
            profile.artifact_manifest, profile_artifact_root(profile)
        )
        validate_profile_semantics(profile, verified)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"profile validation failed: {exc}") from exc
    profile.status = HarmonizationProfileStatus.validated
    profile.validated_by = principal.actor
    profile.validated_at = datetime.now(timezone.utc)
    profile.validation_summary = {
        "artifact_manifest": verified,
        "scientific": {
            "approval_id": report.get("approval_id"),
            "approved_at": report.get("approved_at"),
            "methodology_sha256": report.get("methodology_sha256"),
            "golden_case_evidence_sha256": report.get("golden_case_evidence_sha256"),
        },
    }
    session.add(profile)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.profile.validate",
        entity_type="harmonization_profile", entity_id=profile.id,
        payload={
            "code": profile.code, "version": profile.version,
            "manifest_sha256": verified.get("manifest_sha256"),
            "approval_id": report.get("approval_id"),
            "methodology_sha256": report.get("methodology_sha256"),
            "golden_case_evidence_sha256": report.get("golden_case_evidence_sha256"),
        },
    )
    session.commit()
    session.refresh(profile)
    return _profile_public(profile)


@router.post("/harmonization/profiles/{profile_id}/activate")
def activate_profile(profile_id: str, principal: Principal = Depends(require_admin),
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    _require_offline_profile_workflow()
    lock_profile_activation(session)
    statement = select(HarmonizationProfile).where(HarmonizationProfile.id == profile_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    profile = session.exec(statement).first()
    if not profile:
        raise HTTPException(404, "profile not found")
    if profile.status == HarmonizationProfileStatus.retired:
        raise HTTPException(409, "retired profile cannot be reactivated; create a new version")
    if profile.status == HarmonizationProfileStatus.active:
        return _profile_public(profile)
    if profile.status != HarmonizationProfileStatus.validated:
        raise HTTPException(409, "profile must pass independent scientific validation first")
    if profile.validated_by == principal.actor:
        raise HTTPException(403, "profile activation requires a second administrator")
    other_active = session.exec(select(HarmonizationProfile).where(
        HarmonizationProfile.code == profile.code,
        HarmonizationProfile.status == HarmonizationProfileStatus.active,
        HarmonizationProfile.id != profile.id,
    )).first()
    if other_active is not None:
        raise HTTPException(
            409, "retire the currently active version of this profile code before activation"
        )
    # Exact selector duplicates under different codes are ambiguous to a researcher and rank at
    # the same score.  Refuse them at activation; intentional coverage changes need a disjoint
    # selector or retirement of the old profile.
    active_profiles = session.exec(select(HarmonizationProfile).where(
        HarmonizationProfile.status == HarmonizationProfileStatus.active,
        HarmonizationProfile.id != profile.id,
    )).all()
    overlapping_selector = next((candidate for candidate in active_profiles
                                 if candidate.detector_id == profile.detector_id
                                 and selectors_may_overlap(
                                     candidate.selector, profile.selector)), None)
    if overlapping_selector is not None:
        raise HTTPException(
            409,
            "an active profile for this detector has an overlapping selector; retire it or make "
            "the scanner/protocol selectors disjoint",
        )
    try:
        files = profile.artifact_manifest.get("files", [])
        if profile.method in {"identity", "none"} and not files:
            if settings.is_server_mode:
                raise ValueError(
                    "identity profiles cannot be activated in a server deployment; use the "
                    "explicit unharmonized research override instead"
                )
            verified = {"files": [], "manifest_sha256": None}
        else:
            verified = verify_artifact_manifest(profile.artifact_manifest,
                                                profile_artifact_root(profile))
            validate_profile_semantics(profile, verified)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"artifact verification failed: {exc}") from exc
    profile.status = HarmonizationProfileStatus.active
    # Keep the independent validation actor/evidence; activation is separately immutable-audited.
    session.add(profile)
    try:
        audit.record_authenticated(
            session, principal=principal, action="harmonization.profile.activate",
            entity_type="harmonization_profile", entity_id=profile.id,
            payload={"code": profile.code, "version": profile.version,
                     "manifest_sha256": verified.get("manifest_sha256")},
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            409, "another active version of this profile code already exists"
        ) from exc
    session.refresh(profile)
    mark_profile_integrity_dirty()
    return _profile_public(profile)


@router.post("/harmonization/profiles/{profile_id}/retire")
def retire_profile(profile_id: str, principal: Principal = Depends(require_admin),
                   session: Session = Depends(get_session)) -> dict[str, Any]:
    statement = select(HarmonizationProfile).where(HarmonizationProfile.id == profile_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    profile = session.exec(statement).first()
    if not profile:
        raise HTTPException(404, "profile not found")
    profile.status = HarmonizationProfileStatus.retired
    session.add(profile)
    audit.record_authenticated(session, principal=principal, action="harmonization.profile.retire",
                               entity_type="harmonization_profile", entity_id=profile.id,
                               payload={"code": profile.code, "version": profile.version})
    session.commit()
    mark_profile_integrity_dirty()
    return _profile_public(profile)


@router.get("/cases/{case_id}/harmonization/candidates")
def harmonization_candidates(case_id: str, principal: Principal = Depends(get_principal),
                             session: Session = Depends(get_session)) -> dict[str, Any]:
    case = session.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    _case_access(principal, case)
    profiles = session.exec(select(HarmonizationProfile).where(
        HarmonizationProfile.status == HarmonizationProfileStatus.active)).all()
    if settings.is_server_mode:
        profiles = [profile for profile in profiles
                    if runtime_profile_trusted(session, profile)]
    assignments = session.exec(select(HarmonizationAssignment).where(
        HarmonizationAssignment.case_id == case_id)).all()
    assignment_by_target = {(a.detector_id.value, a.source_series_uid): a for a in assignments}
    targets = []
    series_rows = session.exec(select(Series).where(
        Series.case_id == case_id, Series.active.is_(True))).all()
    for series in series_rows:
        if series.confirmed_role is None or series.confirmed_role.value == "unknown":
            continue
        for detector in REGISTRY.values():
            if (detector.status != "built"
                    or detector.id.value not in PROFILE_METHOD_BY_DETECTOR
                    or series.confirmed_role not in detector.source_roles):
                continue
            matches = rank_profiles(profiles, series.acquisition or {},
                                    role=series.confirmed_role.value,
                                    detector_id=detector.id.value)
            candidate_rows = [
                {"profile": _profile_public(session.get(HarmonizationProfile, match.profile_id)),
                 "score": match.score, "reasons": match.reasons}
                for match in matches
            ]
            candidate_rows.sort(key=lambda row: (
                -row["score"], row["profile"]["code"], -row["profile"]["version"]))
            assignment = assignment_by_target.get(
                (detector.id.value, series.orthanc_series_uid))
            assignment_public = _assignment_public(assignment)
            if assignment_public is not None:
                assignment_public["stale"] = (
                    assignment.acquisition_fingerprint != series.fingerprint)
            targets.append({
                "detector_id": detector.id.value,
                "source_series_uid": series.orthanc_series_uid,
                "source_role": series.confirmed_role.value,
                "fingerprint": series.fingerprint,
                "assignment": assignment_public,
                "ambiguous_top": (len(candidate_rows) > 1
                                  and candidate_rows[0]["score"] == candidate_rows[1]["score"]),
                "candidates": candidate_rows,
            })
    audit.record_access(
        session, principal=principal, entity_type="harmonization_candidates",
        entity_id=case_id, detail={"targets": len(targets)},
    )
    session.commit()
    coverage = case_harmonization_coverage(session, case_id)
    return {
        "case_id": case_id,
        "status": coverage["status"],
        "coverage": {key: value for key, value in coverage.items() if key != "status"},
        "targets": targets,
    }


@router.get("/cases/{case_id}/harmonization/assignments")
def list_assignments(case_id: str, principal: Principal = Depends(get_principal),
                     session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    case = session.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    _case_access(principal, case)
    rows = session.exec(select(HarmonizationAssignment).where(
        HarmonizationAssignment.case_id == case_id)).all()
    result = [{"assignment": _assignment_public(row), "profile": _profile_public(
        session.get(HarmonizationProfile, row.profile_id))} for row in rows]
    audit.record_access(
        session, principal=principal, entity_type="harmonization_assignments",
        entity_id=case_id, detail={"count": len(rows)},
    )
    session.commit()
    return result


@router.post("/cases/{case_id}/harmonization/assign")
def assign_profile(case_id: str, body: AssignmentCreate,
                   principal: Principal = Depends(require_submitter),
                   session: Session = Depends(get_session)) -> dict[str, Any]:
    statement = select(Case).where(Case.id == case_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    case = session.exec(statement).first()
    if not case:
        raise HTTPException(404, "case not found")
    _case_access(principal, case, mutate=True)
    if case.status not in {
            CaseStatus.series_confirmed, CaseStatus.recipe_pending, CaseStatus.failed,
            CaseStatus.review_ready, CaseStatus.adjudicated}:
        raise HTTPException(409, f"cannot assign harmonization while case is {case.status.value}")
    series = session.exec(select(Series).where(
        Series.case_id == case_id,
        Series.orthanc_series_uid == body.source_series_uid,
        Series.active.is_(True),
    )).first()
    if not series or series.confirmed_role is None:
        raise HTTPException(409, "source series must belong to case and be explicitly confirmed")
    if (not series.fingerprint or not series.acquisition
            or not canonical_acquisition(series.acquisition)):
        raise HTTPException(409, "source series lacks acquisition metadata/fingerprint; sync it again")
    profile = session.get(HarmonizationProfile, body.profile_id)
    if not profile or profile.status != HarmonizationProfileStatus.active:
        raise HTTPException(409, "harmonization profile is not active")
    if settings.is_server_mode and not runtime_profile_trusted(session, profile):
        raise HTTPException(
            409, "harmonization profile lacks signed-release or cohort-build trust evidence")
    if profile.detector_id is not None and profile.detector_id != body.detector_id:
        raise HTTPException(409, "profile is for a different detector")
    required_method = PROFILE_METHOD_BY_DETECTOR.get(body.detector_id.value)
    if required_method is None or profile.method != required_method:
        raise HTTPException(
            409, f"detector does not support profile method {profile.method!r}"
        )
    detector = REGISTRY.get(body.detector_id)
    if not detector or series.confirmed_role not in detector.source_roles:
        raise HTTPException(409, "detector cannot consume this confirmed series role")
    match = match_selector(profile.id, profile.selector, series.acquisition or {},
                           role=series.confirmed_role.value)
    if (not match.matched and settings.is_server_mode
            and not principal.has_role(Role.admin)):
        raise HTTPException(403, "only an administrator may approve a selector override")
    if not match.matched and not (body.override_reason and len(body.override_reason.strip()) >= 10):
        raise HTTPException(409, "profile does not match; a substantive override_reason is required")

    assignment = session.exec(select(HarmonizationAssignment).where(
        HarmonizationAssignment.case_id == case_id,
        HarmonizationAssignment.detector_id == body.detector_id,
        HarmonizationAssignment.source_series_uid == body.source_series_uid,
    )).first()
    if assignment is None:
        assignment = HarmonizationAssignment(
            case_id=case_id, profile_id=profile.id, detector_id=body.detector_id,
            source_series_uid=body.source_series_uid,
            acquisition_fingerprint=series.fingerprint,
        )
    assignment.profile_id = profile.id
    assignment.acquisition_fingerprint = series.fingerprint
    assignment.status = HarmonizationStatus.confirmed
    assignment.proposal_score = match.score
    assignment.proposal_reasons = list(match.reasons)
    assignment.confirmed_by = principal.actor
    assignment.confirmed_at = datetime.now(timezone.utc)
    assignment.override_reason = body.override_reason
    session.add(assignment)
    session.flush()
    coverage = case_harmonization_coverage(session, case_id)
    case.harmonization_status = coverage["status"]
    session.add(case)
    audit.record_authenticated(
        session, principal=principal, action="harmonization.assignment.confirm",
        entity_type="case", entity_id=case_id,
        payload={"profile_id": profile.id, "profile_code": profile.code,
                 "profile_version": profile.version, "detector_id": body.detector_id.value,
                 "source_series_uid_hmac_sha256": audit.sensitive_digest(
                     body.source_series_uid),
                 "matched": match.matched, "score": match.score,
                 "override_reason_present": bool(body.override_reason),
                 "case_coverage": coverage["coverage"],
                 "confirmed_targets": coverage["confirmed"],
                 "required_targets": coverage["required"],
                 "override_reason_hmac_sha256": (audit.sensitive_digest(body.override_reason)
                                                  if body.override_reason else None)},
    )
    session.commit()
    session.refresh(assignment)
    return {"assignment": _assignment_public(assignment), "profile": _profile_public(profile)}
