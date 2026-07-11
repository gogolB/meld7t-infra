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
from datetime import datetime, timezone

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
sys.path.insert(0, str(REPO / "containers" / "pkg"))


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
    env["MELD7T_DICOM_IMPORT_ROOT"] = os.path.realpath(DICOM)
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
    from app.models import (Case, CaseStatus, Cluster, DetectorId, Provenance, Recipe, Result, Run,
                            RunStatus, Series, SeriesRole, Workup)
    from app.recipe import build_recipe, spec_hash
    from app.workflow import logical_run_key, run_input_contract_hash
    from recon_prepare import discover

    discovered = discover(DICOM)
    confirmed = {uid: item["role"] for uid, item in discovered.items()
                 if item["role"] != SeriesRole.unknown.value}
    assert confirmed, "pilot DICOM contains no classifiable research series"
    # 1) a case with exact, real DICOM UIDs and confirmed roles — the submit step.
    with Session(engine) as s:
        case = Case(pseudonym="E2E-LOCAL", created_by="e2e", dicom_path=DICOM,
                    status=CaseStatus.series_confirmed)
        s.add(case)
        s.commit()
        s.refresh(case)
        cid = case.id
        for uid, role in confirmed.items():
            s.add(Series(
                case_id=cid, orthanc_series_uid=uid,
                series_description=discovered[uid]["tags"].get("description"),
                proposed_role=SeriesRole(role), confirmed_role=SeriesRole(role),
                instance_count=discovered[uid]["tags"]["instance_count"],
            ))
        s.commit()

    # 2) build the recipe and materialise the one target run (like recipe/confirm does).
    entries = build_recipe(Workup.both, confirmed, require_harmonization=False,
                           unharmonized_reason="local end-to-end test")
    target = [e for e in entries if e["detector_id"] == DETECTOR and e["status"] == "created"]
    assert target, f"recipe did not schedule a '{DETECTOR}' run (is it built?)"
    target = target[0]
    with Session(engine) as s:
        recipe = Recipe(
            case_id=cid, workup=Workup.both, spec=entries,
            spec_hash=spec_hash(entries), confirmed_at=datetime.now(timezone.utc),
        )
        s.add(recipe)
        s.commit()
        s.refresh(recipe)
        logical_key = logical_run_key(recipe.id, target["entry_id"])
        input_hash = run_input_contract_hash(
            recipe_id=recipe.id, recipe_spec_hash=recipe.spec_hash,
            logical_key=logical_key, detector_id=DETECTOR,
            source_role=target["source_role"],
            source_series_uid=target["source_series_uid"], params=target["params"],
        )
        run = Run(case_id=cid, recipe_id=recipe.id, detector_id=DetectorId(DETECTOR),
                  source_role=SeriesRole(target["source_role"]),
                  source_series_uid=target["source_series_uid"],
                  logical_key=logical_key, status=RunStatus.queued, params=target["params"],
                  execution_contract={"schema_version": 2,
                                      "input_contract_sha256": input_hash})
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id
        case = s.get(Case, cid)
        case.status = CaseStatus.queued
        s.add(case)
        s.add(Provenance(
            run_id=rid, params=target["params"],
            input_series_uid=target["source_series_uid"],
            harmonization=target["params"].get("harmonization"),
            release_manifest_digest=settings.release_manifest_digest,
        ))
        s.commit()

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
        assert result.output_manifest and result.output_manifest["files"]
        clusters = s.exec(select(Cluster).where(Cluster.result_id == result.id)).all()
        assert result.n_clusters == len(clusters)         # bookkeeping matches the rows
        provenance = s.exec(select(Provenance).where(Provenance.run_id == rid)).one()
        assert provenance.source_manifest and provenance.recon_provenance
        assert provenance.harmonization["mode"] == "unharmonized"
