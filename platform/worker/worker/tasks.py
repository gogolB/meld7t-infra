"""Arq task: run one detector run end-to-end (spec §5.2, §18). max_jobs=1 → GPU-serialized."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from arq.connections import RedisSettings
from sqlmodel import Session, select

from app import audit
from app.config import settings as app_settings
from app.db import engine
from app.models import (
    Case, CaseStatus, Cluster, Device, Job, Result, Run, RunStatus,
)

from . import dicom, ingest, pipeline
from .config import wsettings
from .gpu import gpu_lease, wait_if_paused


def _set(session: Session, run: Run, status: RunStatus) -> None:
    run.status = status
    session.add(run)
    session.commit()


async def _push_status(redis, run_id: str, status: str) -> None:
    await redis.set(f"meld7t:run:{run_id}", status)


async def run_detector(ctx, run_id: str) -> dict:
    redis = ctx["redis"]
    await wait_if_paused(redis)

    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            return {"run_id": run_id, "error": "not found"}
        if run.status == RunStatus.pending:
            return {"run_id": run_id, "skipped": "pending detector"}
        # capture primitives while the session is open (avoid DetachedInstanceError)
        case_id = run.case_id
        source_role = run.source_role.value if run.source_role else None
        detector = run.detector_id.value
        run.device = Device.gpu
        _set(session, run, RunStatus.preprocessing)
        audit.record(session, actor="worker", action="run.start", entity_type="run",
                     entity_id=run_id, payload={"detector": detector, "source_role": source_role})
        session.commit()

    await _push_status(redis, run_id, RunStatus.preprocessing.value)
    workdir = os.path.join(wsettings.meld_data, "work", run_id)
    os.makedirs(workdir, exist_ok=True)

    try:
        with Session(engine) as session:
            case = session.get(Case, case_id)
            dicom_root = dicom.dicom_root_for(case)

        async with gpu_lease(redis, run_id):
            rc, subject = await pipeline.run_prepare(run_id, source_role, dicom_root, workdir)
            if rc != 0:
                return await _fail(redis, run_id, RunStatus.failed, workdir, "prepare failed")

            with Session(engine) as session:
                r = session.get(Run, run_id)
                _set(session, r, RunStatus.inference)
            await _push_status(redis, run_id, RunStatus.inference.value)

            rc = await pipeline.run_meld(subject, workdir)
            if rc != 0:
                status = (RunStatus.failed_oom
                          if pipeline.is_oom(os.path.join(workdir, "meld.log"))
                          else RunStatus.failed)
                return await _fail(redis, run_id, status, workdir, f"meld rc={rc}")

        # ingest (packaging → Orthanc lands in Phase 3)
        with Session(engine) as session:
            r = session.get(Run, run_id)
            _set(session, r, RunStatus.packaging)
            rf = ingest.result_fields(wsettings.meld_data, subject)
            result = Result(run_id=run_id, **rf, harmo_code=None)
            session.add(result)
            session.commit()
            session.refresh(result)
            for c in ingest.parse_clusters(wsettings.meld_data, subject):
                session.add(Cluster(result_id=result.id, **c))
            r.detector_version = "meld_graph:v2.2.5_gpu"
            _set(session, r, RunStatus.review_ready)
            audit.record(session, actor="worker", action="run.complete", entity_type="run",
                         entity_id=run_id, payload={"n_clusters": rf["n_clusters"],
                                                    "subject": subject})
            session.commit()
            _maybe_finish_case(session, case_id)

        await _push_status(redis, run_id, RunStatus.review_ready.value)
        return {"run_id": run_id, "status": "review_ready", "n_clusters": rf["n_clusters"]}

    except Exception as e:  # noqa: BLE001 — any failure is retained for the support bundle
        return await _fail(redis, run_id, RunStatus.failed, workdir, repr(e))


async def _fail(redis, run_id: str, status: RunStatus, workdir: str, msg: str) -> dict:
    with Session(engine) as session:
        r = session.get(Run, run_id)
        if r:
            r.status = status
            session.add(Job(run_id=run_id, stage="run", status=status.value,
                            error=msg, workdir=workdir,
                            finished_at=datetime.now(timezone.utc)))
            audit.record(session, actor="worker", action="run.fail", entity_type="run",
                         entity_id=run_id, payload={"status": status.value, "error": msg})
            session.commit()
    await _push_status(redis, run_id, status.value)
    return {"run_id": run_id, "status": status.value, "error": msg}


def _maybe_finish_case(session: Session, case_id: str) -> None:
    runs = session.exec(select(Run).where(Run.case_id == case_id)).all()
    active = [r for r in runs if r.status not in
              (RunStatus.review_ready, RunStatus.failed, RunStatus.failed_oom,
               RunStatus.blocked, RunStatus.pending, RunStatus.adjudicated)]
    if not active:
        case = session.get(Case, case_id)
        if case and case.status == CaseStatus.running:
            case.status = CaseStatus.review_ready
            session.add(case)
            session.commit()


class WorkerSettings:
    functions = [run_detector]
    redis_settings = RedisSettings.from_dsn(app_settings.redis_url)
    max_jobs = 1                       # single global GPU semaphore (§18)
    job_timeout = 3 * 60 * 60          # MELD ~1 hr; generous ceiling
    keep_result = 3600
