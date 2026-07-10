"""Arq task: run one detector run end-to-end (spec §5.2, §18).

Generic dispatch: the shared prepare (DICOM→BIDS) runs, then the per-detector runner does
compute → package → ingest. MELD is one runner among many (see worker/detectors). Arq runs up to
`max_jobs` runs at once; GPU exclusivity is enforced by the `gpu_lease` mutex (GPU detectors only),
so a CPU-only detector (HippUnfold) runs alongside a GPU job (MELD) rather than behind it."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from arq.connections import RedisSettings
from sqlmodel import Session, select

from app import audit
from app.config import settings as app_settings
from app.db import engine
from app.models import Case, CaseStatus, Cluster, Device, Job, Result, Run, RunStatus

from . import dicom, pipeline
from .config import wsettings
from .detectors import get_runner
from .gpu import gpu_lease, wait_if_paused

_DETECTOR_VERSION = {"meld_fcd": "meld_graph:v2.2.5_gpu", "hippunfold": "hippunfold:latest"}


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
        case_id = run.case_id
        source_role = run.source_role.value if run.source_role else None
        detector = run.detector_id.value
        runner = get_runner(detector)
        run.device = Device.gpu if (runner is None or runner.uses_gpu) else Device.cpu
        _set(session, run, RunStatus.preprocessing)
        audit.record(session, actor="worker", action="run.start", entity_type="run",
                     entity_id=run_id, payload={"detector": detector, "source_role": source_role})
        session.commit()

    workdir = os.path.join(wsettings.meld_data, "work", run_id)
    os.makedirs(workdir, exist_ok=True)

    if runner is None:
        return await _fail(redis, run_id, RunStatus.failed, workdir,
                           f"no worker runner for detector '{detector}'")
    await _push_status(redis, run_id, RunStatus.preprocessing.value)

    try:
        with Session(engine) as session:
            case = session.get(Case, case_id)
            pseudonym = case.pseudonym
            dicom_root = dicom.dicom_root_for(case)

        # prepare (DICOM→BIDS via dcm2niix) is CPU-only — run it outside the GPU mutex.
        rc, subject = await pipeline.run_prepare(
            run_id, source_role, dicom_root, workdir, also_t2=runner.needs_t2)
        if rc != 0:
            return await _fail(redis, run_id, RunStatus.failed, workdir, "prepare failed")

        with Session(engine) as session:
            _set(session, session.get(Run, run_id), RunStatus.inference)
        await _push_status(redis, run_id, RunStatus.inference.value)

        # compute: GPU detectors serialize on the GPU mutex; CPU-only detectors (uses_gpu=False)
        # run concurrently alongside a GPU job instead of queuing behind it (§18).
        if runner.uses_gpu:
            async with gpu_lease(redis, run_id):
                rc, fail_status = await runner.compute(subject, workdir)
        else:
            rc, fail_status = await runner.compute(subject, workdir)
        if rc != 0:
            return await _fail(redis, run_id, fail_status or RunStatus.failed,
                               workdir, f"{detector} rc={rc}")

        # package overlays → Orthanc, then ingest findings
        with Session(engine) as session:
            _set(session, session.get(Run, run_id), RunStatus.packaging)
        await _push_status(redis, run_id, RunStatus.packaging.value)
        uids = await runner.package(subject, pseudonym, workdir)
        ing = await runner.ingest(subject)
        rf, clusters = ing.get("result", {}), ing.get("clusters", [])

        with Session(engine) as session:
            result = Result(run_id=run_id, harmo_code=None,
                            orthanc_study_uid=uids.get("study_uid"),
                            orthanc_t1_uid=uids.get("t1_series_uid"),
                            orthanc_seg_uid=uids.get("seg_series_uid"),
                            report_path=rf.get("report_path"),
                            n_clusters=rf.get("n_clusters", len(clusters)))
            session.add(result)
            session.commit()
            session.refresh(result)
            for c in clusters:
                session.add(Cluster(result_id=result.id, **c))
            r = session.get(Run, run_id)
            r.detector_version = _DETECTOR_VERSION.get(detector, detector)
            _set(session, r, RunStatus.review_ready)
            audit.record(session, actor="worker", action="run.complete", entity_type="run",
                         entity_id=run_id, payload={"detector": detector, "subject": subject,
                                                    "n_clusters": len(clusters),
                                                    "orthanc_study": uids.get("study_uid")})
            session.commit()
            _maybe_finish_case(session, case_id)

        await _push_status(redis, run_id, RunStatus.review_ready.value)
        return {"run_id": run_id, "status": "review_ready", "n_clusters": len(clusters),
                "orthanc_study": uids.get("study_uid")}

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
    max_jobs = 2                       # 1 GPU + 1 CPU detector concurrently; GPU exclusivity is
                                       # enforced by the gpu_lease mutex, not the queue (§18)
    job_timeout = 4 * 60 * 60          # MELD ~1 hr, HippUnfold up to ~1 hr; generous ceiling
    keep_result = 3600
