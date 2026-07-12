"""API routes (spec §5.1). Workflow: create case → sync+confirm series (§16) → build+confirm
recipe (§25.1) → runs created (enqueue lands in Phase 2) → adjudication (append-only, audited)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field as PydanticField,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import func, or_
from sqlmodel import Session, select

from . import audit, orthanc, queue
from .auth import (
    Principal,
    Role,
    get_principal,
    require_admin,
    require_auditor,
    require_reviewer,
    require_submitter,
)
from .config import settings
from .db import engine, get_session
from .detectors import REGISTRY
from .harmonization import (
    canonical_acquisition, case_harmonization_coverage, match_selector, profile_document_sha256,
    run_harmonization_contract, runtime_profile_trusted,
)
from .models import (
    Adjudication, Case, CaseStatus, Cluster, HarmonizationAssignment, HarmonizationProfile,
    HarmonizationProfileStatus, HarmonizationStatus, Job, OutboxEvent, OutboxStatus, Provenance,
    Recipe, Result, Run, RunStatus, Series, SeriesRole, Workup,
)
from .recipe import build_recipe, entry_id, recipe_summary, spec_hash
from .workflow import (
    RECIPE_MUTABLE_CASE_STATES, assert_case_state, logical_run_key, run_input_contract_hash,
    run_outbox_event,
)
from .storage import storage_health

router = APIRouter(prefix="/api")

_SERIES_MUTABLE_CASE_STATES = frozenset({
    CaseStatus.created,
    CaseStatus.series_pending,
    CaseStatus.series_confirmed,
    # A terminal historical workflow may be superseded after an approved Orthanc reimport or a
    # corrected role. Historical recipes/runs/results stay immutable; only the case's next recipe
    # is rebuilt from the refreshed series rows.
    CaseStatus.failed,
    CaseStatus.review_ready,
    CaseStatus.adjudicated,
})
_ACTIVE_SERIES_BLOCKING_RUN_STATES = frozenset({
    RunStatus.created,
    RunStatus.queued,
    RunStatus.preprocessing,
    RunStatus.qc_pending,
    RunStatus.inference,
    RunStatus.packaging,
})


def _assert_series_mutation_has_no_active_runs(session: Session, case_id: str) -> None:
    active = session.exec(select(Run.id).where(
        Run.case_id == case_id,
        Run.status.in_(_ACTIVE_SERIES_BLOCKING_RUN_STATES),
    )).first()
    if active is not None:
        raise HTTPException(409, "series cannot change while a current run is active")


# ---- requests
class ContraindicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hard_block: bool = False
    reasons: list[Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=500,
    )]] = PydanticField(default_factory=list, max_length=20)


class CaseUnblock(BaseModel):
    reason: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=10, max_length=1000,
    )]


class CaseCreate(BaseModel):
    pseudonym: str = PydanticField(min_length=1, max_length=64,
                                   pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    orthanc_study_uid: Optional[str] = PydanticField(
        default=None, min_length=3, max_length=64, pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    contraindications: Optional[ContraindicationInput] = None
    assigned_subject: Optional[str] = PydanticField(default=None, min_length=1, max_length=128)

    @field_validator("assigned_subject")
    @classmethod
    def valid_assigned_subject(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(char in value for char in "\r\n\0"):
            raise ValueError("assigned_subject must be one non-empty identity line")
        return value

    @model_validator(mode="after")
    def input_is_actionable(self):
        if not self.orthanc_study_uid:
            raise ValueError("orthanc_study_uid is required; local imports are operator-managed")
        return self


class RoleConfirm(BaseModel):
    roles: dict[str, SeriesRole] = PydanticField(min_length=1, max_length=100)

    @field_validator("roles")
    @classmethod
    def valid_series_uids(cls, value: dict[str, SeriesRole]) -> dict[str, SeriesRole]:
        if any(len(uid) > 64 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", uid) is None
               for uid in value):
            raise ValueError("roles keys must be valid DICOM SeriesInstanceUIDs")
        return value


class RecipeCreate(BaseModel):
    workup: Workup
    allow_unharmonized: bool = False
    unharmonized_reason: Optional[str] = PydanticField(default=None, max_length=1000)

    @model_validator(mode="after")
    def reason_for_unharmonized(self):
        if self.allow_unharmonized and not (
                self.unharmonized_reason and len(self.unharmonized_reason.strip()) >= 10):
            raise ValueError("allow_unharmonized requires a substantive reason")
        if self.unharmonized_reason:
            self.unharmonized_reason = self.unharmonized_reason.strip()
        return self


class AdjudicationCreate(BaseModel):
    agree: Optional[bool] = None
    confidence: Optional[int] = PydanticField(default=None, ge=1, le=5)
    ground_truth: Optional[str] = PydanticField(default=None, max_length=200)
    notes: Optional[str] = PydanticField(default=None, max_length=4000)
    supersedes: Optional[str] = PydanticField(default=None, max_length=64)


class CaseAssignmentCreate(BaseModel):
    subject: str = PydanticField(min_length=1, max_length=128)

    @field_validator("subject")
    @classmethod
    def valid_subject(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char in value for char in "\r\n\0"):
            raise ValueError("subject must be one non-empty identity line")
        return value


def _get_case(session: Session, case_id: str) -> Case:
    case = session.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    return case


def _get_case_for_update(session: Session, case_id: str) -> Case:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return _get_case(session, case_id)
    case = session.exec(select(Case).where(Case.id == case_id).with_for_update()).first()
    if not case:
        raise HTTPException(404, "case not found")
    return case


def _authorize_case(principal: Principal, case: Case, *, mutate: bool = False) -> None:
    if principal.has_role(Role.admin):
        return
    if case.created_by == principal.actor or case.assigned_to == principal.actor:
        return
    if not mutate and principal.has_role(Role.reviewer):
        return
    raise HTTPException(403, "case access denied")


def _case_public(case: Case) -> dict:
    """Minimum-necessary case response: never expose host paths or contraindication details."""
    return {
        "id": case.id, "pseudonym": case.pseudonym,
        "orthanc_study_uid": case.orthanc_study_uid,
        "status": case.status, "workup": case.workup,
        "harmonization_status": case.harmonization_status,
        "has_assignee": bool(case.assigned_to),
        "has_contraindications": bool(case.contraindications),
        "created_at": case.created_at,
    }


def _series_public(series: Series) -> dict:
    return {
        "id": series.id, "case_id": series.case_id,
        "orthanc_series_uid": series.orthanc_series_uid,
        "series_description": series.series_description, "modality": series.modality,
        "proposed_role": series.proposed_role, "confirmed_role": series.confirmed_role,
        "fingerprint": series.fingerprint, "instance_count": series.instance_count,
    }


def _recipe_public(recipe: Recipe) -> dict:
    entries = []
    for raw in recipe.spec:
        entry = {key: raw.get(key) for key in (
            "entry_id", "detector_id", "detector_label", "source_role",
            "source_series_uid", "status", "note",
        ) if key in raw}
        harmonization = (raw.get("params") or {}).get("harmonization") or {}
        if harmonization:
            entry["harmonization"] = {
                key: harmonization.get(key) for key in ("profile_id", "code", "version", "method", "mode")
                if harmonization.get(key) is not None
            }
        entries.append(entry)
    return {
        "id": recipe.id, "case_id": recipe.case_id, "workup": recipe.workup,
        "spec": entries, "version": recipe.version, "spec_hash": recipe.spec_hash,
        "supersedes": recipe.supersedes, "created_at": recipe.created_at,
        "confirmed_at": recipe.confirmed_at,
    }


def _run_public(run: Run) -> dict:
    return {
        "id": run.id, "case_id": run.case_id, "recipe_id": run.recipe_id,
        "detector_id": run.detector_id, "detector_version": run.detector_version,
        "source_role": run.source_role, "status": run.status, "device": run.device,
        "attempt": run.attempt, "claimed_at": run.claimed_at,
        "completed_at": run.completed_at, "adjudicated_at": run.adjudicated_at,
        "created_at": run.created_at,
        "has_status_reason": bool(run.status_reason), "superseded_by": run.superseded_by,
    }


def _result_public(result: Optional[Result]) -> Optional[dict]:
    if result is None:
        return None
    manifest = result.output_manifest if isinstance(result.output_manifest, dict) else {}
    return {
        "id": result.id, "run_id": result.run_id,
        "orthanc_study_uid": result.orthanc_study_uid,
        "orthanc_t1_uid": result.orthanc_t1_uid,
        "orthanc_seg_uid": result.orthanc_seg_uid,
        "orthanc_probmap_uid": result.orthanc_probmap_uid,
        "harmo_code": result.harmo_code, "n_clusters": result.n_clusters,
        "metric_schema": manifest.get("metric_schema"),
        "has_report": bool(result.report_path), "created_at": result.created_at,
    }


def _job_public(job: Job) -> dict:
    return {
        "id": job.id, "run_id": job.run_id, "stage": job.stage, "status": job.status,
        "device": job.device, "started_at": job.started_at, "finished_at": job.finished_at,
        "retry_count": job.retry_count, "has_error": bool(job.error),
    }


def _public_gpu_owner(value: str | None) -> str | None:
    """Expose the run ID for display without leaking the private claim fencing token."""
    if not value:
        return None
    run_id = value.split(":", 1)[0]
    return run_id if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", run_id) else None


def _record_poll_access(principal: Principal, entity_type: str, entity_id: str,
                        detail: dict | None = None) -> None:
    with Session(engine) as audit_session:
        row = audit.record_access_coalesced(
            audit_session, principal=principal, entity_type=entity_type,
            entity_id=entity_id, detail=detail,
        )
        if row is not None:
            audit_session.commit()


def _validate_recipe_harmonization_contract(
        session: Session, case_id: str, entry: dict) -> None:
    """Prove a pending recipe still names the currently confirmed immutable assignment."""
    contract = (entry.get("params") or {}).get("harmonization") or {}
    profile_id = contract.get("profile_id")
    if not profile_id:
        return
    assignment_id = contract.get("assignment_id")
    if not assignment_id:
        raise HTTPException(409, "recipe predates assignment-bound harmonization; rebuild it")
    assignment = session.get(HarmonizationAssignment, str(assignment_id))
    profile = session.get(HarmonizationProfile, str(profile_id))
    source_uid = entry.get("source_series_uid")
    source = session.exec(select(Series).where(
        Series.case_id == case_id, Series.orthanc_series_uid == source_uid,
        Series.active.is_(True),
    )).first()
    detector = getattr(assignment.detector_id, "value", assignment.detector_id) \
        if assignment is not None else None
    current_match = (match_selector(
        profile.id, profile.selector, source.acquisition or {},
        role=source.confirmed_role.value if source and source.confirmed_role else None,
    ) if profile is not None and source is not None else None)
    if (assignment is None or profile is None or source is None
            or assignment.case_id != case_id
            or assignment.profile_id != profile_id
            or detector != entry.get("detector_id")
            or assignment.source_series_uid != source_uid
            or assignment.status != HarmonizationStatus.confirmed
            or assignment.acquisition_fingerprint != contract.get("acquisition_fingerprint")
            or source.fingerprint != assignment.acquisition_fingerprint
            or bool(assignment.override_reason) != bool(contract.get("selector_override"))
            or profile.status != HarmonizationProfileStatus.active
            or (settings.is_server_mode
                and not runtime_profile_trusted(session, profile))
            or profile.code != contract.get("code")
            or profile.version != contract.get("version")
            or profile.method != contract.get("method")
            or profile.selector != contract.get("selector")
            or (current_match is not None and not current_match.matched
                and contract.get("selector_override") is not True)
            or profile_document_sha256(profile) != contract.get(
                "profile_document_sha256")):
        raise HTTPException(
            409, "recipe harmonization assignment/profile changed; rebuild the recipe"
        )


@router.get("/me")
def whoami(principal: Principal = Depends(get_principal)) -> dict:
    return {"subject": principal.subject, "roles": sorted(role.value for role in principal.roles),
            "auth_method": principal.auth_method, "request_id": principal.request_id}


# ---- cases
@router.post("/cases", status_code=201)
def create_case(body: CaseCreate, principal: Principal = Depends(get_principal),
                session: Session = Depends(get_session)) -> dict:
    if settings.is_server_mode:
        if not (principal.service or principal.has_role(Role.admin)):
            raise HTTPException(403, "server case intake requires an administrator or service identity")
    elif not principal.has_role(Role.submitter):
        raise HTTPException(403, "case intake requires the submitter role")
    existing = session.exec(select(Case).where(
        Case.orthanc_study_uid == body.orthanc_study_uid)).first()
    if existing is not None:
        raise HTTPException(409, "Orthanc study already belongs to a research case")
    contraindications = (body.contraindications.model_dump()
                         if body.contraindications is not None else None)
    assigned_to = (f"user:{body.assigned_subject}" if body.assigned_subject
                   else (None if principal.service else principal.actor))
    case = Case(pseudonym=body.pseudonym, orthanc_study_uid=body.orthanc_study_uid,
                contraindications=contraindications, created_by=principal.actor,
                assigned_to=assigned_to,
                status=CaseStatus.series_pending)
    if body.contraindications and body.contraindications.hard_block:
        case.status = CaseStatus.blocked
    session.add(case)
    audit.record_authenticated(
        session, principal=principal, action="case.create", entity_type="case",
        entity_id=case.id,
        payload={"has_orthanc_study": bool(case.orthanc_study_uid),
                 "hard_blocked": case.status == CaseStatus.blocked,
                 "assigned_subject_present": bool(body.assigned_subject),
                 "assigned_subject_hmac_sha256": (
                     audit.sensitive_digest(body.assigned_subject)
                     if body.assigned_subject else None)},
    )
    session.commit()
    session.refresh(case)
    return _case_public(case)


@router.get("/cases")
def list_cases(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
               principal: Principal = Depends(get_principal),
               session: Session = Depends(get_session)) -> list[dict]:
    statement = select(Case).order_by(Case.created_at.desc()).offset(offset).limit(limit)
    if not (principal.has_role(Role.admin) or principal.has_role(Role.reviewer)):
        statement = statement.where(or_(Case.created_by == principal.actor,
                                        Case.assigned_to == principal.actor))
    rows = session.exec(statement).all()
    audit.record_access_coalesced(
        session, principal=principal, entity_type="case_collection",
        entity_id="list", detail={"count": len(rows), "offset": offset})
    session.commit()
    return [_case_public(case) for case in rows]


@router.post("/admin/cases/{case_id}/assign")
def assign_case(case_id: str, body: CaseAssignmentCreate,
                principal: Principal = Depends(require_admin),
                session: Session = Depends(get_session)) -> dict:
    case = _get_case_for_update(session, case_id)
    previous_present = bool(case.assigned_to)
    case.assigned_to = f"user:{body.subject}"
    session.add(case)
    audit.record_authenticated(
        session, principal=principal, action="case.assign", entity_type="case",
        entity_id=case_id,
        payload={"previous_assignee_present": previous_present,
                 "assigned_subject_hmac_sha256": audit.sensitive_digest(body.subject)},
    )
    session.commit()
    session.refresh(case)
    return _case_public(case)


@router.post("/admin/cases/{case_id}/unblock")
def unblock_case(case_id: str, body: CaseUnblock,
                 principal: Principal = Depends(require_admin),
                 session: Session = Depends(get_session)) -> dict:
    """Release a mistaken/obsolete hard block without erasing its immutable history."""
    case = _get_case_for_update(session, case_id)
    contraindications = dict(case.contraindications or {})
    if case.status != CaseStatus.blocked or contraindications.get("hard_block") is not True:
        raise HTTPException(409, "case does not have an active hard block")
    contraindications["hard_block"] = False
    contraindications["released_at"] = datetime.now(timezone.utc).isoformat()
    contraindications["released_by"] = principal.actor
    contraindications["release_reason_hmac_sha256"] = audit.sensitive_digest(body.reason)
    case.contraindications = contraindications
    has_confirmed_series = session.exec(select(Series.id).where(
        Series.case_id == case_id,
        Series.active.is_(True),
        Series.confirmed_role.is_not(None),
        Series.confirmed_role != SeriesRole.unknown,
    )).first() is not None
    case.status = (CaseStatus.series_confirmed if has_confirmed_series
                   else CaseStatus.series_pending)
    session.add(case)
    audit.record_authenticated(
        session, principal=principal, action="case.unblock", entity_type="case",
        entity_id=case_id,
        payload={
            "reason_hmac_sha256": audit.sensitive_digest(body.reason),
            "restored_status": case.status.value,
        },
    )
    session.commit()
    session.refresh(case)
    return _case_public(case)


@router.get("/cases/{case_id}")
def get_case(case_id: str, principal: Principal = Depends(get_principal),
             session: Session = Depends(get_session)) -> dict:
    case = _get_case(session, case_id)
    _authorize_case(principal, case)
    audit.record_access(session, principal=principal, entity_type="case", entity_id=case_id)
    session.commit()
    return _case_public(case)


# ---- series (§16)
@router.post("/cases/{case_id}/series/sync")
def sync_series(case_id: str, principal: Principal = Depends(require_submitter),
                session: Session = Depends(get_session)) -> list[dict]:
    case = _get_case_for_update(session, case_id)
    _authorize_case(principal, case, mutate=True)
    assert_case_state(case.status, _SERIES_MUTABLE_CASE_STATES, "sync series")
    _assert_series_mutation_has_no_active_runs(session, case_id)
    previous_case_status = case.status
    if not case.orthanc_study_uid:
        raise HTTPException(400, "case has no orthanc_study_uid")
    found = orthanc.get_study_series(case.orthanc_study_uid)
    existing = {s.orthanc_series_uid: s for s in
                session.exec(select(Series).where(Series.case_id == case_id)).all()}
    assignments_by_uid: dict[str, list[HarmonizationAssignment]] = {}
    for assignment in session.exec(select(HarmonizationAssignment).where(
            HarmonizationAssignment.case_id == case_id)).all():
        assignments_by_uid.setdefault(assignment.source_series_uid, []).append(assignment)
    stale_assignments = 0

    def invalidate_assignments(uid: str, reason: str) -> None:
        nonlocal stale_assignments
        for assignment in assignments_by_uid.get(uid, []):
            assignment.status = HarmonizationStatus.blocked
            assignment.confirmed_by = None
            assignment.confirmed_at = None
            assignment.proposal_reasons = [reason]
            assignment.override_reason = None
            session.add(assignment)
            stale_assignments += 1

    seen_uids: set[str] = set()
    for s in found:
        uid = s.get("series_uid")
        if not uid:
            continue
        seen_uids.add(uid)
        row = existing.get(uid) or Series(
            case_id=case_id, orthanc_series_uid=uid,
            proposed_role=orthanc.propose_role(s["description"]))
        row.series_description = s["description"]
        row.modality = s["modality"]
        row.acquisition = s.get("acquisition")
        if row.fingerprint and s.get("fingerprint") and row.fingerprint != s["fingerprint"]:
            invalidate_assignments(
                uid, "acquisition fingerprint changed after assignment; reconfirm profile")
        row.fingerprint = s.get("fingerprint")
        row.instance_count = s.get("instances")
        row.active = True
        row.last_seen_at = datetime.now(timezone.utc)
        session.add(row)
    retired_series = 0
    for uid, row in existing.items():
        if uid not in seen_uids and row.active:
            row.active = False
            session.add(row)
            invalidate_assignments(
                uid, "series is no longer present in the approved Orthanc study")
            retired_series += 1
    case.status = CaseStatus.series_pending
    session.flush()
    case.harmonization_status = case_harmonization_coverage(session, case_id)["status"]
    fingerprints = sorted(s["fingerprint"] for s in found if s.get("fingerprint"))
    case.scanner_fingerprint = (hashlib.sha256("|".join(fingerprints).encode()).hexdigest()
                                if fingerprints else None)
    audit.record_authenticated(session, principal=principal, action="series.sync",
                               entity_type="case", entity_id=case_id,
                               payload={"series_count": len(found),
                                        "scanner_fingerprint": case.scanner_fingerprint,
                                        "stale_harmonization_assignments": stale_assignments,
                                        "retired_series": retired_series,
                                        "previous_case_status": previous_case_status.value,
                                        "terminal_recovery": previous_case_status in {
                                            CaseStatus.failed, CaseStatus.review_ready,
                                            CaseStatus.adjudicated,
                                        }})
    session.commit()
    return [_series_public(row) for row in session.exec(
        select(Series).where(Series.case_id == case_id, Series.active.is_(True))).all()]


@router.get("/cases/{case_id}/series")
def list_series(case_id: str, principal: Principal = Depends(get_principal),
                session: Session = Depends(get_session)) -> list[dict]:
    case = _get_case(session, case_id)
    _authorize_case(principal, case)
    rows = session.exec(select(Series).where(
        Series.case_id == case_id, Series.active.is_(True))).all()
    audit.record_access(session, principal=principal, entity_type="case_series", entity_id=case_id,
                        detail={"count": len(rows)})
    session.commit()
    return [_series_public(row) for row in rows]


@router.post("/cases/{case_id}/series/confirm")
def confirm_series(case_id: str, body: RoleConfirm,
                   principal: Principal = Depends(require_submitter),
                   session: Session = Depends(get_session)) -> list[dict]:
    case = _get_case_for_update(session, case_id)
    _authorize_case(principal, case, mutate=True)
    assert_case_state(case.status, _SERIES_MUTABLE_CASE_STATES - {CaseStatus.created},
                      "confirm series")
    _assert_series_mutation_has_no_active_runs(session, case_id)
    previous_case_status = case.status
    rows = session.exec(select(Series).where(
        Series.case_id == case_id, Series.active.is_(True))).all()
    by_uid = {s.orthanc_series_uid: s for s in rows}
    if set(body.roles) != set(by_uid):
        missing = sorted(set(by_uid) - set(body.roles))
        extra = sorted(set(body.roles) - set(by_uid))
        raise HTTPException(409, {"detail": "every discovered series must be explicitly classified",
                                  "missing": missing, "extra": extra})
    assignments_by_uid: dict[str, list[HarmonizationAssignment]] = {}
    for assignment in session.exec(select(HarmonizationAssignment).where(
            HarmonizationAssignment.case_id == case_id)).all():
        assignments_by_uid.setdefault(assignment.source_series_uid, []).append(assignment)
    invalidated_assignments = 0
    for uid, role in body.roles.items():
        previous_role = by_uid[uid].confirmed_role
        if previous_role is not None and previous_role != role:
            for assignment in assignments_by_uid.get(uid, []):
                assignment.status = HarmonizationStatus.blocked
                assignment.confirmed_by = None
                assignment.confirmed_at = None
                assignment.proposal_reasons = [
                    "confirmed source role changed; reconfirm harmonization profile"
                ]
                assignment.override_reason = None
                session.add(assignment)
                invalidated_assignments += 1
        by_uid[uid].confirmed_role = role
        session.add(by_uid[uid])
    if not any(role != SeriesRole.unknown for role in body.roles.values()):
        raise HTTPException(409, "at least one series must have a usable confirmed role")
    case.status = CaseStatus.series_confirmed
    session.flush()
    case.harmonization_status = case_harmonization_coverage(session, case_id)["status"]
    audit.record_authenticated(
        session, principal=principal, action="series.confirm",
        entity_type="case", entity_id=case_id,
        payload={
            "series_count": len(body.roles),
            "role_counts": {
                role.value: sum(value == role for value in body.roles.values())
                for role in SeriesRole
            },
                "mapping_hmac_sha256": audit.sensitive_digest(
                {uid: role.value for uid, role in body.roles.items()}),
            "invalidated_harmonization_assignments": invalidated_assignments,
            "previous_case_status": previous_case_status.value,
            "terminal_recovery": previous_case_status in {
                CaseStatus.failed, CaseStatus.review_ready, CaseStatus.adjudicated,
            },
        },
    )
    session.commit()
    return [_series_public(row) for row in session.exec(
        select(Series).where(Series.case_id == case_id, Series.active.is_(True))).all()]


# ---- recipe (§25.1)
@router.post("/cases/{case_id}/recipe")
def create_recipe(case_id: str, body: RecipeCreate,
                  principal: Principal = Depends(require_submitter),
                  session: Session = Depends(get_session)) -> dict:
    case = _get_case_for_update(session, case_id)
    _authorize_case(principal, case, mutate=True)
    assert_case_state(case.status, RECIPE_MUTABLE_CASE_STATES, "build recipe")
    if (body.allow_unharmonized and settings.is_server_mode
            and not principal.has_role(Role.admin)):
        raise HTTPException(403, "only an administrator may approve an unharmonized server run")
    if case.contraindications and case.contraindications.get("hard_block") is True:
        case.status = CaseStatus.blocked
        audit.record_authenticated(session, principal=principal, action="case.block",
                                   entity_type="case", entity_id=case_id,
                                   payload={"reason_count": len(case.contraindications.get(
                                       "reasons", []))})
        session.commit()
        raise HTTPException(409, "case has an active hard block")
    rows = session.exec(select(Series).where(
        Series.case_id == case_id, Series.active.is_(True))).all()
    confirmed = {s.orthanc_series_uid: s.confirmed_role.value
                 for s in rows if s.confirmed_role and s.confirmed_role != SeriesRole.unknown}
    if not confirmed:
        raise HTTPException(409, "no explicitly confirmed usable series")

    contracts: dict[tuple[str, str], dict] = {}
    assignments = session.exec(select(HarmonizationAssignment).where(
        HarmonizationAssignment.case_id == case_id,
        HarmonizationAssignment.status == HarmonizationStatus.confirmed,
    )).all()
    series_by_uid = {s.orthanc_series_uid: s for s in rows}
    for assignment in assignments:
        source = series_by_uid.get(assignment.source_series_uid)
        profile = session.get(HarmonizationProfile, assignment.profile_id)
        if (not source or not profile or profile.status.value != "active"
                or source.fingerprint != assignment.acquisition_fingerprint
                or (settings.is_server_mode
                    and not runtime_profile_trusted(session, profile))):
            continue
        contracts[(assignment.detector_id.value, assignment.source_series_uid)] = (
            run_harmonization_contract(profile, assignment))

    entries = build_recipe(
        body.workup,
        confirmed,
        harmonization=contracts,
        require_harmonization=(settings.harmonization_required
                               and not body.allow_unharmonized),
        unharmonized_reason=(body.unharmonized_reason or
                             "harmonization not required in this deployment mode"),
    )
    for entry in entries:
        params = entry.get("params") or {}
        series_uids = params.get("series_uids") or {}
        if not series_uids:
            continue
        fingerprints = {
            role: series_by_uid[uid].fingerprint
            for role, uid in series_uids.items()
            if uid in series_by_uid and series_by_uid[uid].fingerprint
        }
        missing_fingerprints = sorted(set(series_uids) - set(fingerprints))
        acquisitions = {
            role: canonical_acquisition(series_by_uid[uid].acquisition or {})
            for role, uid in series_uids.items()
            if uid in series_by_uid and canonical_acquisition(series_by_uid[uid].acquisition or {})
        }
        missing_fingerprints = sorted(
            set(series_uids) - set(fingerprints) | (set(series_uids) - set(acquisitions)))
        acquisition_manifest = {
            "study_uid": case.orthanc_study_uid,
            "series_by_role": dict(series_uids),
            "fingerprints_by_role": fingerprints,
            "acquisitions_by_role": acquisitions,
        }
        acquisition_manifest["bundle_fingerprint"] = hashlib.sha256(json.dumps(
            acquisition_manifest, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        params["acquisition_manifest"] = acquisition_manifest
        entry["params"] = params
        entry["entry_id"] = entry_id(
            entry["detector_id"], entry.get("source_series_uid"), params)
        if missing_fingerprints and settings.is_server_mode:
            entry["status"] = RunStatus.blocked.value
            note = f"missing acquisition fingerprints for roles {missing_fingerprints}"
            entry["note"] = f"{entry.get('note')}; {note}" if entry.get("note") else note
    previous = session.exec(select(Recipe).where(Recipe.case_id == case_id)
                            .order_by(Recipe.version.desc())).first()
    recipe = Recipe(case_id=case_id, workup=body.workup, spec=entries,
                    version=(previous.version + 1 if previous else 1),
                    supersedes=previous.id if previous else None,
                    spec_hash=spec_hash(entries))
    case.workup = body.workup
    case.status = CaseStatus.recipe_pending
    session.add(recipe)
    audit.record_authenticated(
        session, principal=principal, action="recipe.create", entity_type="case",
        entity_id=case_id,
        payload={"workup": body.workup.value, "version": recipe.version,
                 "spec_hash": recipe.spec_hash, "summary": recipe_summary(entries),
                 "allow_unharmonized": body.allow_unharmonized,
                 "unharmonized_reason_present": bool(body.unharmonized_reason),
                 "unharmonized_reason_hmac_sha256": (
                     audit.sensitive_digest(body.unharmonized_reason)
                     if body.unharmonized_reason else None)},
    )
    session.commit()
    session.refresh(recipe)
    return {"recipe": _recipe_public(recipe), "summary": recipe_summary(entries)}


@router.get("/cases/{case_id}/recipe")
def get_recipe(case_id: str, principal: Principal = Depends(get_principal),
               session: Session = Depends(get_session)) -> dict:
    case = _get_case(session, case_id)
    _authorize_case(principal, case)
    recipe = session.exec(select(Recipe).where(Recipe.case_id == case_id)
                          .order_by(Recipe.version.desc(), Recipe.created_at.desc())).first()
    if not recipe:
        raise HTTPException(404, "no recipe")
    audit.record_access(session, principal=principal, entity_type="recipe", entity_id=recipe.id)
    session.commit()
    return {"recipe": _recipe_public(recipe), "summary": recipe_summary(recipe.spec)}


@router.post("/cases/{case_id}/recipe/confirm")
async def confirm_recipe(case_id: str, principal: Principal = Depends(require_submitter),
                         session: Session = Depends(get_session)) -> list[dict]:
    case = _get_case_for_update(session, case_id)
    _authorize_case(principal, case, mutate=True)
    if settings.is_server_mode:
        capacity = storage_health(
            settings.meld_data,
            minimum_free_bytes=settings.storage_min_free_bytes,
            minimum_free_percent=settings.storage_min_free_percent,
        )
        if not capacity["ready"]:
            raise HTTPException(503, "local compute storage is below its admission watermark")
        if settings.worker_heartbeat_required:
            try:
                redis = queue.get_redis()
                worker_capacity = queue.verify_worker_heartbeat(
                    await redis.get(settings.worker_heartbeat_key)
                )
            except Exception as exc:
                raise HTTPException(503, "worker capacity cannot be verified") from exc
            if worker_capacity.get("ready") is not True:
                raise HTTPException(
                    503,
                    f"worker has no admitted capacity: {worker_capacity.get('status', 'unknown')}",
                )
    assert_case_state(case.status,
                      {CaseStatus.recipe_pending, CaseStatus.recipe_confirmed,
                       CaseStatus.queued, CaseStatus.running}, "confirm recipe")
    statement = (select(Recipe).where(Recipe.case_id == case_id)
                 .order_by(Recipe.version.desc(), Recipe.created_at.desc()))
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    recipe = session.exec(statement).first()
    if not recipe:
        raise HTTPException(404, "no recipe")
    if recipe.confirmed_at is not None:
        return [_run_public(run) for run in session.exec(
            select(Run).where(Run.recipe_id == recipe.id)).all()]
    if not recipe.spec_hash or recipe.spec_hash != spec_hash(recipe.spec):
        raise HTTPException(409, "recipe specification hash changed; rebuild the recipe")
    blocked = [entry for entry in recipe.spec if entry["status"] == RunStatus.blocked.value]
    if blocked:
        raise HTTPException(409, {"detail": "recipe contains blocked runs", "blocked": blocked})
    for entry in recipe.spec:
        _validate_recipe_harmonization_contract(session, case_id, entry)

    runs = []
    for e in recipe.spec:
        if e["status"] not in (RunStatus.created.value, RunStatus.pending.value):
            continue
        built = e["status"] == RunStatus.created.value
        entry_id = e.get("entry_id")
        if not entry_id:
            raise HTTPException(409, "recipe predates immutable entry ids; rebuild it")
        logical_key = logical_run_key(recipe.id, entry_id)
        params = e.get("params") or {}
        input_hash = run_input_contract_hash(
            recipe_id=recipe.id, recipe_spec_hash=recipe.spec_hash,
            logical_key=logical_key, detector_id=e["detector_id"],
            source_role=e.get("source_role"), source_series_uid=e.get("source_series_uid"),
            params=params,
        )
        run = Run(case_id=case_id, recipe_id=recipe.id, detector_id=e["detector_id"],
                  source_role=e.get("source_role"), source_series_uid=e.get("source_series_uid"),
                  params=params, logical_key=logical_key,
                  execution_contract={"schema_version": 2,
                                      "input_contract_sha256": input_hash},
                  status=RunStatus.queued if built else RunStatus.pending)
        session.add(run)
        runs.append(run)
        if built:
            session.add(run_outbox_event(run))
        harmonization = (e.get("params") or {}).get("harmonization")
        session.add(Provenance(
            run_id=run.id,
            params=e.get("params") or {},
            input_series_uid=e.get("source_series_uid"),
            source_manifest={"series_uids": (e.get("params") or {}).get("series_uids", {})},
            harmonization=harmonization,
            release_manifest_digest=getattr(settings, "release_manifest_digest", None),
        ))
    if not any(run.status == RunStatus.queued for run in runs):
        raise HTTPException(409, "recipe has no runnable detector entries")
    if recipe.supersedes:
        prior_runs = session.exec(select(Run).where(Run.recipe_id == recipe.supersedes)).all()
        replacements = {
            (getattr(run.detector_id, "value", run.detector_id),
             getattr(run.source_role, "value", run.source_role), run.source_series_uid): run
            for run in runs
        }
        for prior in prior_runs:
            replacement = replacements.get((
                getattr(prior.detector_id, "value", prior.detector_id),
                getattr(prior.source_role, "value", prior.source_role),
                prior.source_series_uid,
            ))
            if replacement is not None:
                prior.superseded_by = replacement.id
                session.add(prior)
    recipe.confirmed_at = datetime.now(timezone.utc)
    case.status = CaseStatus.queued
    audit.record_authenticated(
        session, principal=principal, action="recipe.confirm", entity_type="recipe",
        entity_id=recipe.id,
        payload={"runs": len(runs), "queued": sum(r.status == RunStatus.queued for r in runs),
                 "spec_hash": recipe.spec_hash},
    )
    session.commit()
    for r in runs:
        session.refresh(r)
    # Best-effort immediate dispatch. Failure is durable in outbox and retried by reconciliation.
    await queue.dispatch_outbox_events(session)
    return [_run_public(run) for run in runs]


# ---- runs
@router.get("/cases/{case_id}/runs")
def list_runs(case_id: str, principal: Principal = Depends(get_principal),
              session: Session = Depends(get_session)) -> list[dict]:
    case = _get_case(session, case_id)
    _authorize_case(principal, case)
    rows = session.exec(select(Run).where(Run.case_id == case_id)
                        .order_by(Run.created_at.desc())).all()
    audit.record_access_coalesced(
        session, principal=principal, entity_type="case_runs", entity_id=case_id,
        detail={"count": len(rows)})
    session.commit()
    return [_run_public(row) for row in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: str, principal: Principal = Depends(get_principal),
            session: Session = Depends(get_session)) -> dict:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    _authorize_case(principal, _get_case(session, run.case_id))
    result = session.exec(select(Result).where(Result.run_id == run_id)).first()
    clusters = (session.exec(select(Cluster).where(Cluster.result_id == result.id)).all()
                if result else [])
    jobs = session.exec(select(Job).where(Job.run_id == run_id)).all()
    adjudications = session.exec(select(Adjudication).where(
        Adjudication.run_id == run_id
    ).order_by(Adjudication.ts, Adjudication.id)).all()
    frames = _verified_frames(result)
    audit.record_access(session, principal=principal, entity_type="run", entity_id=run_id)
    session.commit()
    return {"run": _run_public(run), "result": _result_public(result),
            "clusters": clusters, "jobs": [_job_public(job) for job in jobs],
            "adjudications": adjudications, "frames": frames}


@router.post("/runs/{run_id}/adjudication")
def adjudicate(run_id: str, body: AdjudicationCreate,
               principal: Principal = Depends(require_reviewer),
               session: Session = Depends(get_session)) -> Adjudication:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    _authorize_case(principal, _get_case(session, run.case_id))
    if run.status not in {RunStatus.review_ready, RunStatus.adjudicated}:
        raise HTTPException(409, "only a review-ready run can be adjudicated")
    if not session.exec(select(Result).where(Result.run_id == run_id)).first():
        raise HTTPException(409, "run has no validated result")
    if body.supersedes:
        prior = session.get(Adjudication, body.supersedes)
        if not prior or prior.run_id != run_id:
            raise HTTPException(409, "supersedes must reference an adjudication for this run")
        already_corrected = session.exec(select(Adjudication.id).where(
            Adjudication.supersedes == prior.id
        )).first()
        if already_corrected is not None:
            raise HTTPException(409, "that adjudication already has a correction")
    adj = Adjudication(run_id=run_id, reviewer=principal.actor, agree=body.agree,
                       confidence=body.confidence, ground_truth=body.ground_truth,
                       notes=body.notes, supersedes=body.supersedes)
    session.add(adj)
    run.status = RunStatus.adjudicated
    run.adjudicated_at = datetime.now(timezone.utc)
    case = _get_case(session, run.case_id)
    current_runs = session.exec(select(Run).where(
        Run.recipe_id == run.recipe_id,
        Run.status != RunStatus.pending,
        Run.superseded_by.is_(None),
    )).all()
    if current_runs and all(row.status == RunStatus.adjudicated for row in current_runs):
        case.status = CaseStatus.adjudicated
        session.add(case)
    audit.record_authenticated(
        session, principal=principal, action="adjudication.create",
        entity_type="run", entity_id=run_id,
        payload={"adjudication_id": adj.id, "agree": body.agree,
                 "confidence": body.confidence,
                 "ground_truth_present": bool(body.ground_truth),
                 "ground_truth_hmac_sha256": (audit.sensitive_digest(body.ground_truth)
                                          if body.ground_truth else None),
                 "notes_present": bool(body.notes),
                 "notes_hmac_sha256": (audit.sensitive_digest(body.notes)
                                        if body.notes else None),
                 "supersedes": body.supersedes},
    )
    session.commit()
    session.refresh(adj)
    return adj


# ---- MDT: reports, key frames, concordance, summary (§9.1, §25.6)
def _report_abs(result: Optional[Result]) -> Optional[str]:
    """Resolve the stored (relative) report path against the mounted meld-data root."""
    if not result or not result.report_path:
        return None
    relative = Path(result.report_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    root = Path(settings.meld_data).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if settings.is_server_mode and not _artifact_matches_manifest(result, relative, candidate):
        return None
    return str(candidate)


def _artifact_matches_manifest(result: Result, relative: Path, candidate: Path) -> bool:
    """Verify a served result artifact against the worker's immutable output manifest."""
    manifest = result.output_manifest if isinstance(result.output_manifest, dict) else {}
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not candidate.is_file() or candidate.is_symlink():
        return False
    item = next((row for row in files if isinstance(row, dict)
                 and row.get("path") == relative.as_posix()), None)
    if item is None or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
        return False
    digest = hashlib.sha256()
    size = 0
    try:
        with candidate.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == item["sha256"] and size == int(item.get("size", -1))


def _report_dir(result: Optional[Result]) -> Optional[str]:
    p = _report_abs(result)
    return os.path.dirname(p) if p and os.path.isdir(os.path.dirname(p)) else None


def _verified_frames(result: Optional[Result]) -> list[str]:
    directory = _report_dir(result)
    if not directory:
        return []
    root = Path(settings.meld_data).resolve()
    frames = []
    for raw in sorted(Path(directory).glob("*.png")):
        candidate = raw.resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if not settings.is_server_mode or _artifact_matches_manifest(result, relative, candidate):
            frames.append(candidate.name)
    return frames


@router.get("/runs/{run_id}/report")
def run_report(run_id: str, principal: Principal = Depends(get_principal),
               session: Session = Depends(get_session)):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    _authorize_case(principal, _get_case(session, run.case_id))
    result = session.exec(select(Result).where(Result.run_id == run_id)).first()
    p = _report_abs(result)
    if not p or not os.path.isfile(p):
        raise HTTPException(404, "no report")
    audit.record_access(session, principal=principal, entity_type="run_report", entity_id=run_id,
                        operation="export")
    session.commit()
    return FileResponse(p, media_type="application/pdf",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/runs/{run_id}/frames")
def run_frames(run_id: str, principal: Principal = Depends(get_principal),
               session: Session = Depends(get_session)) -> list[str]:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    _authorize_case(principal, _get_case(session, run.case_id))
    result = session.exec(select(Result).where(Result.run_id == run_id)).first()
    frames = _verified_frames(result)
    audit.record_access(session, principal=principal, entity_type="run_frames", entity_id=run_id,
                        detail={"count": len(frames)})
    session.commit()
    return frames


@router.get("/runs/{run_id}/frames/{name}")
def run_frame(run_id: str, name: str, principal: Principal = Depends(get_principal),
              session: Session = Depends(get_session)):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    _authorize_case(principal, _get_case(session, run.case_id))
    result = session.exec(select(Result).where(Result.run_id == run_id)).first()
    d = _report_dir(result)
    if name != os.path.basename(name) or not name.lower().endswith(".png"):
        raise HTTPException(404, "no frame")
    path = os.path.join(d or "", name)
    if not d or name not in _verified_frames(result) or not os.path.isfile(path):
        raise HTTPException(404, "no frame")
    audit.record_access(session, principal=principal, entity_type="run_frame", entity_id=run_id,
                        operation="read", detail={"frame": name})
    session.commit()
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


def _concordance(session: Session, case_id: str) -> dict:
    recipe = session.exec(select(Recipe).where(
        Recipe.case_id == case_id, Recipe.confirmed_at.is_not(None))
        .order_by(Recipe.version.desc(), Recipe.created_at.desc())).first()
    if not recipe:
        return {"runs": [], "regions": [], "detectors_with_findings": 0,
                "concordant_regions": 0, "research_only": True,
                "method": "distinct-detector spatial overlap only",
                "spatial_concordance_available": False,
                "eligible_localized_detectors": 0}
    runs = session.exec(select(Run).where(
        Run.case_id == case_id,
        Run.recipe_id == recipe.id,
        Run.superseded_by.is_(None),
        Run.status.in_([RunStatus.review_ready, RunStatus.adjudicated]),
    )).all()
    run_info, regions = [], {}
    localized_detectors: set[str] = set()
    for run in runs:
        result = session.exec(select(Result).where(Result.run_id == run.id)).first()
        clusters = (session.exec(select(Cluster).where(Cluster.result_id == result.id)).all()
                    if result else [])
        harmonization = (run.params or {}).get("harmonization", {})
        eligible = bool(
            harmonization.get("profile_id")
            and harmonization.get("selector_override") is not True
        )
        run_info.append({"run_id": run.id, "detector": run.detector_id.value,
                         "source_role": run.source_role, "status": run.status.value,
                         "n_clusters": len(clusters), "harmonization": harmonization,
                         "overlap_eligible": eligible})
        for c in clusters:
            saliency = c.saliency or {}
            if saliency.get("flagged") is False:
                continue
            spatial_key = saliency.get("spatial_key")
            if spatial_key is None and isinstance(saliency.get("mni"), list) \
                    and len(saliency["mni"]) == 3:
                spatial_key = "mni10:" + ":".join(
                    str(int(round(float(v) / 10.0) * 10)) for v in saliency["mni"])
            # Unlocalized text labels are displayed but can never produce concordance.
            key = spatial_key or f"unlocalized:{run.id}:{c.id}"
            region = regions.setdefault(key, {"hemi": c.hemi, "location": c.location,
                                               "by_run": {}, "detectors": set(),
                                               "spatial_key": spatial_key})
            region["by_run"][run.id] = c.confidence
            if eligible:
                region["detectors"].add(run.detector_id.value)
                if spatial_key is not None:
                    localized_detectors.add(run.detector_id.value)
    region_list = [{"hemi": value["hemi"], "location": value["location"],
                    "spatial_key": value["spatial_key"], "by_run": value["by_run"],
                    "detectors": sorted(value["detectors"]),
                    "concordant": value["spatial_key"] is not None
                    and len(value["detectors"]) >= 2}
                   for value in regions.values()]
    region_list.sort(key=lambda r: (-len(r["by_run"]), r["location"] or ""))
    with_findings = {r["detector"] for r in run_info if r["n_clusters"] > 0}
    return {"runs": run_info, "regions": region_list,
            "detectors_with_findings": len(with_findings),
            "concordant_regions": sum(1 for r in region_list if r["concordant"]),
            "spatial_concordance_available": len(localized_detectors) >= 2,
            "eligible_localized_detectors": len(localized_detectors),
            "research_only": True, "method": "distinct-detector spatial overlap only"}


@router.get("/cases/{case_id}/concordance")
def concordance(case_id: str, principal: Principal = Depends(get_principal),
                session: Session = Depends(get_session)) -> dict:
    case = _get_case(session, case_id)
    _authorize_case(principal, case)
    result = _concordance(session, case_id)
    audit.record_access(session, principal=principal, entity_type="case_concordance",
                        entity_id=case_id)
    session.commit()
    return result


@router.get("/cases/{case_id}/summary")
def case_summary(case_id: str, principal: Principal = Depends(get_principal),
                 session: Session = Depends(get_session)) -> dict:
    """One aggregate for the MDT screen: runs + results + clusters + adjudications + concordance."""
    case = _get_case(session, case_id)
    _authorize_case(principal, case)
    latest_recipe = session.exec(select(Recipe).where(
        Recipe.case_id == case_id, Recipe.confirmed_at.is_not(None)
    ).order_by(Recipe.version.desc(), Recipe.created_at.desc())).first()
    runs = (session.exec(select(Run).where(
        Run.recipe_id == latest_recipe.id, Run.superseded_by.is_(None))).all()
        if latest_recipe else [])
    out_runs, adjudications = [], []
    for run in runs:
        result = session.exec(select(Result).where(Result.run_id == run.id)).first()
        clusters = (session.exec(select(Cluster).where(Cluster.result_id == result.id)).all()
                    if result else [])
        frames = _verified_frames(result)
        out_runs.append({"run": _run_public(run), "result": _result_public(result),
                         "clusters": clusters, "frames": frames})
        for a in session.exec(select(Adjudication).where(Adjudication.run_id == run.id)).all():
            adjudications.append(a)
    audit.record_access(session, principal=principal, entity_type="case_summary", entity_id=case_id)
    session.commit()
    return {"case": _case_public(case), "runs": out_runs, "adjudications": adjudications,
            "concordance": _concordance(session, case_id)}


# ---- system / queue / admin / audit
@router.get("/system")
async def system(principal: Principal = Depends(get_principal),
                 session: Session = Depends(get_session)) -> dict:
    elevated = (principal.has_role(Role.admin) or principal.has_role(Role.reviewer)
                or principal.has_role(Role.auditor))
    status_statement = select(Run.status, func.count(Run.id)).group_by(Run.status)
    case_count_statement = select(func.count(Case.id))
    if not elevated:
        status_statement = status_statement.join(Case, Run.case_id == Case.id).where(
            or_(Case.created_by == principal.actor, Case.assigned_to == principal.actor))
        case_count_statement = case_count_statement.where(or_(
            Case.created_by == principal.actor, Case.assigned_to == principal.actor))
    status_rows = session.exec(status_statement).all()
    r = queue.get_redis()
    by_status = {status.value: count for status, count in status_rows}
    total = sum(by_status.values())
    pending_outbox = session.exec(select(func.count(OutboxEvent.id)).where(
        OutboxEvent.status.in_([OutboxStatus.pending, OutboxStatus.failed,
                                OutboxStatus.publishing]))).one()
    raw_gpu_owner = await r.get(queue.GPU_INUSE_KEY)
    gpu_owner = _public_gpu_owner(raw_gpu_owner)
    if gpu_owner and not elevated:
        authorized_owner = session.exec(select(Run.id).join(
            Case, Run.case_id == Case.id).where(
                Run.id == gpu_owner,
                or_(Case.created_by == principal.actor, Case.assigned_to == principal.actor),
            )).first()
        if authorized_owner is None:
            gpu_owner = None
    response = {
        "cases": session.exec(case_count_statement).one(),
        "runs": {"total": total, "by_status": by_status},
        "outbox_pending": pending_outbox,
        "gpu": {"in_use_run": gpu_owner, "busy": bool(raw_gpu_owner),
                "queue_paused": bool(await r.get(queue.QUEUE_PAUSED_KEY))},
        "detectors": {d.id.value: d.status for d in REGISTRY.values()},
    }
    await asyncio.to_thread(
        _record_poll_access, principal, "system_status", "current", None)
    return response


@router.get("/queue")
async def queue_view(principal: Principal = Depends(get_principal),
                     session: Session = Depends(get_session)) -> dict:
    """Live run board for the dashboard (§9.1) — GPU-ordered."""
    r = queue.get_redis()
    statement = select(Run).where(
        Run.status.in_([RunStatus.queued, RunStatus.preprocessing, RunStatus.inference,
                        RunStatus.qc_pending, RunStatus.packaging]))
    if not (principal.has_role(Role.admin) or principal.has_role(Role.reviewer)):
        owned_cases = select(Case.id).where(or_(
            Case.created_by == principal.actor, Case.assigned_to == principal.actor))
        statement = statement.where(Run.case_id.in_(owned_cases))
    active = session.exec(statement).all()
    raw_gpu_owner = await r.get(queue.GPU_INUSE_KEY)
    owner = _public_gpu_owner(raw_gpu_owner)
    if owner not in {run.id for run in active}:
        owner = None
    response = {
        "in_use_run": owner,
        "busy": bool(raw_gpu_owner),
        "paused": bool(await r.get(queue.QUEUE_PAUSED_KEY)),
        "active": [{"run_id": x.id, "case_id": x.case_id, "detector": x.detector_id.value,
                    "source_role": x.source_role, "status": x.status.value} for x in active],
    }
    await asyncio.to_thread(
        _record_poll_access, principal, "queue_status", "active", {"count": len(active)})
    return response


@router.post("/admin/pause")
async def pause_queue(principal: Principal = Depends(require_admin),
                      session: Session = Depends(get_session)) -> dict:
    await queue.get_redis().set(queue.QUEUE_PAUSED_KEY, "1")
    audit.record_authenticated(session, principal=principal, action="queue.pause",
                               entity_type="queue", entity_id="gpu")
    session.commit()
    return {"paused": True}


@router.post("/admin/resume")
async def resume_queue(principal: Principal = Depends(require_admin),
                       session: Session = Depends(get_session)) -> dict:
    await queue.get_redis().delete(queue.QUEUE_PAUSED_KEY)
    audit.record_authenticated(session, principal=principal, action="queue.resume",
                               entity_type="queue", entity_id="gpu")
    session.commit()
    return {"paused": False}


@router.post("/admin/outbox/dispatch")
async def dispatch_outbox(principal: Principal = Depends(require_admin),
                          session: Session = Depends(get_session)) -> dict:
    counts = await queue.dispatch_outbox_events(session)
    audit.record_authenticated(session, principal=principal, action="outbox.dispatch",
                               entity_type="outbox", entity_id="run.enqueue", payload=counts)
    session.commit()
    return counts


@router.post("/admin/runs/{run_id}/retry")
async def retry_failed_run(run_id: str, principal: Principal = Depends(require_admin),
                           session: Session = Depends(get_session)) -> dict:
    statement = select(Run).where(Run.id == run_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    run = session.exec(statement).first()
    if not run:
        raise HTTPException(404, "run not found")
    if run.status not in {RunStatus.failed, RunStatus.failed_oom}:
        raise HTTPException(409, "only a failed run can be retried")
    if session.exec(select(Result).where(Result.run_id == run.id)).first():
        raise HTTPException(409, "a run with a persisted result cannot be retried")
    provenance = session.exec(select(Provenance).where(Provenance.run_id == run.id)).first()
    if (settings.is_server_mode and (
            provenance is None or not provenance.release_manifest_digest
            or provenance.release_manifest_digest != settings.release_manifest_digest)):
        raise HTTPException(
            409, "cross-release retry is forbidden; build and confirm a new recipe version"
        )
    run.status = RunStatus.queued
    run.status_reason = None
    run.claimed_at = None
    run.completed_at = None
    run.adjudicated_at = None
    run.claim_token = None
    run.heartbeat_at = None
    run.lease_expires_at = None
    case = _get_case(session, run.case_id)
    case.status = CaseStatus.queued
    session.add(run)
    session.add(case)
    session.add(run_outbox_event(run))
    audit.record_authenticated(
        session, principal=principal, action="run.retry", entity_type="run", entity_id=run.id,
        payload={"next_claim_number": run.attempt + 1},
    )
    session.commit()
    session.refresh(run)
    await queue.dispatch_outbox_events(session)
    return _run_public(run)


@router.post("/audit/verify")
def audit_verify(principal: Principal = Depends(require_auditor),
                 session: Session = Depends(get_session)) -> dict:
    result = audit.verify_chain(session)
    audit.record_authenticated(session, principal=principal, action="audit.verify",
                               entity_type="audit", entity_id="chain",
                               payload={"ok": result.get("ok"), "count": result.get("count")})
    session.commit()
    return result
