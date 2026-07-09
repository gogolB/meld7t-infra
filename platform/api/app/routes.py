"""API routes (spec §5.1). Workflow: create case → sync+confirm series (§16) → build+confirm
recipe (§25.1) → runs created (enqueue lands in Phase 2) → adjudication (append-only, audited)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from . import audit, orthanc
from .db import get_session
from .detectors import REGISTRY
from .models import (
    Adjudication, Case, CaseStatus, Cluster, Recipe, Result, Run, RunStatus,
    Series, SeriesRole, Workup,
)
from .recipe import build_recipe, recipe_summary

router = APIRouter(prefix="/api")


# ---- requests
class CaseCreate(BaseModel):
    pseudonym: str
    orthanc_study_uid: Optional[str] = None
    contraindications: Optional[dict] = None


class RoleConfirm(BaseModel):
    roles: dict[str, str]           # series_uid -> SeriesRole value


class RecipeCreate(BaseModel):
    workup: Workup


class AdjudicationCreate(BaseModel):
    reviewer: str
    agree: Optional[bool] = None
    confidence: Optional[int] = None
    ground_truth: Optional[str] = None
    notes: Optional[str] = None
    supersedes: Optional[str] = None


def _get_case(session: Session, case_id: str) -> Case:
    case = session.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    return case


# ---- cases
@router.post("/cases")
def create_case(body: CaseCreate, session: Session = Depends(get_session)) -> Case:
    case = Case(pseudonym=body.pseudonym, orthanc_study_uid=body.orthanc_study_uid,
                contraindications=body.contraindications,
                status=CaseStatus.series_pending if body.orthanc_study_uid else CaseStatus.created)
    session.add(case)
    audit.record(session, actor="system", action="case.create",
                 entity_type="case", entity_id=case.id,
                 payload={"pseudonym": case.pseudonym, "study": case.orthanc_study_uid})
    session.commit()
    session.refresh(case)
    return case


@router.get("/cases")
def list_cases(session: Session = Depends(get_session)) -> list[Case]:
    return session.exec(select(Case).order_by(Case.created_at.desc())).all()


@router.get("/cases/{case_id}")
def get_case(case_id: str, session: Session = Depends(get_session)) -> Case:
    return _get_case(session, case_id)


# ---- series (§16)
@router.post("/cases/{case_id}/series/sync")
def sync_series(case_id: str, session: Session = Depends(get_session)) -> list[Series]:
    case = _get_case(session, case_id)
    if not case.orthanc_study_uid:
        raise HTTPException(400, "case has no orthanc_study_uid")
    found = orthanc.get_study_series(case.orthanc_study_uid)
    existing = {s.orthanc_series_uid for s in
                session.exec(select(Series).where(Series.case_id == case_id)).all()}
    for s in found:
        if s["series_uid"] in existing:
            continue
        session.add(Series(
            case_id=case_id, orthanc_series_uid=s["series_uid"],
            series_description=s["description"], modality=s["modality"],
            proposed_role=orthanc.propose_role(s["description"])))
    case.status = CaseStatus.series_pending
    session.commit()
    return session.exec(select(Series).where(Series.case_id == case_id)).all()


@router.get("/cases/{case_id}/series")
def list_series(case_id: str, session: Session = Depends(get_session)) -> list[Series]:
    _get_case(session, case_id)
    return session.exec(select(Series).where(Series.case_id == case_id)).all()


@router.post("/cases/{case_id}/series/confirm")
def confirm_series(case_id: str, body: RoleConfirm,
                   session: Session = Depends(get_session)) -> list[Series]:
    case = _get_case(session, case_id)
    rows = session.exec(select(Series).where(Series.case_id == case_id)).all()
    by_uid = {s.orthanc_series_uid: s for s in rows}
    for uid, role in body.roles.items():
        if uid not in by_uid:
            raise HTTPException(400, f"series {uid} not in case")
        by_uid[uid].confirmed_role = SeriesRole(role)
    case.status = CaseStatus.series_confirmed
    audit.record(session, actor="system", action="series.confirm",
                 entity_type="case", entity_id=case_id, payload={"roles": body.roles})
    session.commit()
    return session.exec(select(Series).where(Series.case_id == case_id)).all()


# ---- recipe (§25.1)
@router.post("/cases/{case_id}/recipe")
def create_recipe(case_id: str, body: RecipeCreate,
                  session: Session = Depends(get_session)) -> dict:
    case = _get_case(session, case_id)
    rows = session.exec(select(Series).where(Series.case_id == case_id)).all()
    confirmed = {s.orthanc_series_uid: (s.confirmed_role or s.proposed_role).value
                 for s in rows if (s.confirmed_role or s.proposed_role) != SeriesRole.unknown}
    if not confirmed:
        raise HTTPException(400, "no series with a role — confirm series first")
    entries = build_recipe(body.workup, confirmed)
    recipe = Recipe(case_id=case_id, workup=body.workup, spec=entries)
    case.workup = body.workup
    case.status = CaseStatus.recipe_pending
    session.add(recipe)
    audit.record(session, actor="system", action="recipe.create", entity_type="case",
                 entity_id=case_id, payload={"workup": body.workup.value,
                                             "summary": recipe_summary(entries)})
    session.commit()
    session.refresh(recipe)
    return {"recipe": recipe, "summary": recipe_summary(entries)}


@router.get("/cases/{case_id}/recipe")
def get_recipe(case_id: str, session: Session = Depends(get_session)) -> dict:
    _get_case(session, case_id)
    recipe = session.exec(select(Recipe).where(Recipe.case_id == case_id)
                          .order_by(Recipe.created_at.desc())).first()
    if not recipe:
        raise HTTPException(404, "no recipe")
    return {"recipe": recipe, "summary": recipe_summary(recipe.spec)}


@router.post("/cases/{case_id}/recipe/confirm")
def confirm_recipe(case_id: str, session: Session = Depends(get_session)) -> list[Run]:
    case = _get_case(session, case_id)
    recipe = session.exec(select(Recipe).where(Recipe.case_id == case_id)
                          .order_by(Recipe.created_at.desc())).first()
    if not recipe:
        raise HTTPException(404, "no recipe")
    runs = []
    for e in recipe.spec:
        if e["status"] not in (RunStatus.created.value, RunStatus.pending.value):
            continue
        run = Run(case_id=case_id, recipe_id=recipe.id, detector_id=e["detector_id"],
                  source_role=e.get("source_role"), source_series_uid=e.get("source_series_uid"),
                  params=e.get("params") or {}, status=RunStatus(e["status"]))
        session.add(run)
        runs.append(run)
    recipe.confirmed_at = datetime.now(timezone.utc)
    case.status = CaseStatus.recipe_confirmed          # enqueue happens in Phase 2
    audit.record(session, actor="system", action="recipe.confirm", entity_type="recipe",
                 entity_id=recipe.id, payload={"runs": len(runs)})
    session.commit()
    for r in runs:
        session.refresh(r)
    return runs


# ---- runs
@router.get("/cases/{case_id}/runs")
def list_runs(case_id: str, session: Session = Depends(get_session)) -> list[Run]:
    _get_case(session, case_id)
    return session.exec(select(Run).where(Run.case_id == case_id)).all()


@router.get("/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(get_session)) -> dict:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    result = session.exec(select(Result).where(Result.run_id == run_id)).first()
    clusters = (session.exec(select(Cluster).where(Cluster.result_id == result.id)).all()
                if result else [])
    return {"run": run, "result": result, "clusters": clusters}


@router.post("/runs/{run_id}/adjudication")
def adjudicate(run_id: str, body: AdjudicationCreate,
               session: Session = Depends(get_session)) -> Adjudication:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    adj = Adjudication(run_id=run_id, reviewer=body.reviewer, agree=body.agree,
                       confidence=body.confidence, ground_truth=body.ground_truth,
                       notes=body.notes, supersedes=body.supersedes)
    session.add(adj)
    audit.record(session, actor=body.reviewer, action="adjudication.create",
                 entity_type="run", entity_id=run_id,
                 payload={"agree": body.agree, "confidence": body.confidence,
                          "ground_truth": body.ground_truth, "supersedes": body.supersedes})
    session.commit()
    session.refresh(adj)
    return adj


# ---- system / audit
@router.get("/system")
def system(session: Session = Depends(get_session)) -> dict:
    n_cases = len(session.exec(select(Case)).all())
    n_runs = len(session.exec(select(Run)).all())
    return {"cases": n_cases, "runs": n_runs,
            "detectors": {d.id.value: d.status for d in REGISTRY.values()}}


@router.get("/audit/verify")
def audit_verify(session: Session = Depends(get_session)) -> dict:
    return audit.verify_chain(session)
