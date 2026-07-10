"""Local, live-stack end-to-end test: submit a brain → spawn the worker → assert a real result.

NOT a CI test. It needs the running services (postgres, redis) and a detector container image, and
it launches an actual Arq worker subprocess that runs a real detector on real imaging. Run it with:

    just e2e                      # defaults: MAP detector (~3 min), pilot DICOM
    MELD7T_E2E_DETECTOR=hippunfold just e2e     # ~15 min
    MELD7T_E2E_DETECTOR=meld_fcd  just e2e      # ~50 min (GPU)

It is skipped unless MELD7T_E2E=1 (the justfile target sets it), and skips gracefully if the DICOM
tier isn't mounted. MAP is the default because it's the fastest full run (SPM segmentation + a CPU
morphometry pass), so the test exercises prepare → compute → ingest → Result/Cluster writes end to
end without a 50-minute wait.
"""
import os
import pathlib
import subprocess
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]        # …/meld7t

# Load the worker's loopback env (DB/redis URLs) if the caller hasn't already sourced it.
if "MELD7T_DB_URL" not in os.environ:
    envf = REPO / "secrets" / "worker.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip())

E2E = os.environ.get("MELD7T_E2E") == "1"
DETECTOR = os.environ.get("MELD7T_E2E_DETECTOR", "map")
DICOM = os.environ.get("MELD7T_E2E_DICOM", str(REPO / "data" / "raw" / "subject 1 clean" / "DICOM"))
TIMEOUT_S = int(os.environ.get("MELD7T_E2E_TIMEOUT", "1200"))   # 20 min ceiling (MAP finishes ~3)

pytestmark = pytest.mark.skipif(
    not E2E, reason="local live-stack E2E — run via `just e2e` (needs services + a detector image)")

sys.path[:0] = [str(REPO / "platform" / "api"), str(REPO / "platform" / "worker")]


def _dicom_available() -> bool:
    try:
        return os.path.isdir(DICOM) and any(os.scandir(DICOM))
    except OSError:
        return False


def _spawn_worker() -> subprocess.Popen:
    """Launch a real Arq worker exactly as run-dev.sh does (this IS the 'spawn the worker' step)."""
    arq = os.path.join(os.path.dirname(sys.executable), "arq")
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO}/platform/api:{REPO}/platform/worker"
    return subprocess.Popen([arq, "worker.tasks.WorkerSettings"], cwd=str(REPO), env=env)


def test_submit_brain_spawns_worker_produces_result():
    if not _dicom_available():
        pytest.skip(f"DICOM tier not mounted at {DICOM} — run `just mount-data` first")

    import asyncio

    from arq import create_pool
    from arq.connections import RedisSettings
    from sqlmodel import Session, select

    from app import models
    from app.config import settings
    from app.db import engine
    from app.models import (Case, CaseStatus, Cluster, DetectorId, Recipe, Result, Run,
                            RunStatus, Series, SeriesRole, Workup)
    from app.recipe import build_recipe

    uid = "e2e-uni"
    # 1) a case on the brain, with a confirmed T1 (UNI) series — the submit step.
    with Session(engine) as s:
        case = Case(pseudonym="E2E-LOCAL", dicom_path=DICOM, status=CaseStatus.series_confirmed)
        s.add(case)
        s.commit()
        s.refresh(case)
        cid = case.id
        s.add(Series(case_id=cid, orthanc_series_uid=uid, series_description="E2E UNI",
                     proposed_role=SeriesRole.t1_uni, confirmed_role=SeriesRole.t1_uni))
        s.commit()

    # 2) build the recipe and materialise the one target run (like recipe/confirm does).
    entries = build_recipe(Workup.both, {uid: SeriesRole.t1_uni.value})
    target = [e for e in entries if e["detector_id"] == DETECTOR and e["status"] == "created"]
    assert target, f"recipe did not schedule a '{DETECTOR}' run (is it built?)"
    with Session(engine) as s:
        recipe = Recipe(case_id=cid, workup=Workup.both, spec=entries)
        s.add(recipe)
        s.commit()
        s.refresh(recipe)
        run = Run(case_id=cid, recipe_id=recipe.id, detector_id=DetectorId(DETECTOR),
                  source_role=SeriesRole.t1_uni, source_series_uid=uid,
                  status=RunStatus.queued, params={})
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id

    # 3) enqueue it on the real queue.
    async def _enqueue():
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await pool.enqueue_job("run_detector", rid)

    asyncio.run(_enqueue())

    # 4) spawn the worker and 5) wait for the run to reach a terminal state.
    worker = _spawn_worker()
    terminal = {RunStatus.review_ready, RunStatus.failed, RunStatus.failed_oom, RunStatus.blocked}
    final = None
    try:
        deadline = time.time() + TIMEOUT_S
        while time.time() < deadline:
            with Session(engine) as s:
                final = s.get(Run, rid).status
            if final in terminal:
                break
            time.sleep(5)
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            worker.kill()

    # 6) assert a real, complete result was written.
    assert final == RunStatus.review_ready, f"run ended in {final} (not review_ready)"
    with Session(engine) as s:
        run = s.get(Run, rid)
        assert run.device is not None                     # the worker set the compute device
        assert run.detector_version                       # and stamped the detector version
        result = s.exec(select(Result).where(Result.run_id == rid)).first()
        assert result is not None, "no Result row written"
        clusters = s.exec(select(Cluster).where(Cluster.result_id == result.id)).all()
        assert result.n_clusters == len(clusters)         # bookkeeping matches the rows
