"""ARQ detector task with immutable input, output, and completion contracts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from arq import Retry
from arq.connections import RedisSettings
from sqlmodel import Session, select

from app import audit
from app import queue as app_queue
from app.config import settings as app_settings
from app.db import engine
from app.models import (
    Case, CaseStatus, Cluster, Device, Job, Provenance, Recipe, Result, Run, RunStatus, Series,
)
from app.recipe import spec_hash
from app.workflow import run_input_contract_hash
from app.storage import storage_health

from . import dicom, pipeline
from .config import wsettings
from .detectors import get_runner
from .detectors.base import DetectorCompletion
from .gpu import gpu_lease, wait_if_paused
from .harmonization import ResolvedHarmonization, resolve_harmonization


_log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _publish_worker_heartbeat(redis, boot_id: str) -> None:
    images = {
        "pkg": wsettings.pkg_image,
        "meld_fcd": wsettings.meld_image,
        "map": wsettings.map_image,
        "hippunfold": wsettings.hippunfold_image,
    }
    storage = await asyncio.to_thread(
        storage_health,
        wsettings.meld_data,
        minimum_free_bytes=wsettings.storage_admission_min_free_bytes,
        minimum_free_percent=wsettings.storage_min_free_percent,
    )
    value = app_queue.make_worker_heartbeat(
        boot_id=boot_id,
        release_manifest_digest=wsettings.release_manifest_digest,
        git_sha=wsettings.git_sha,
        os_checksum=wsettings.os_checksum,
        images=images,
        capacity={
            "max_jobs": wsettings.worker_max_jobs,
            "max_gpu_jobs": 1,
            "storage_ready": storage["ready"],
            "free_bytes": storage["free_bytes"],
            "required_free_bytes": wsettings.storage_admission_min_free_bytes,
        },
    )
    await redis.set(
        app_settings.worker_heartbeat_key,
        value,
        ex=wsettings.worker_heartbeat_ttl_s,
    )


async def _worker_heartbeat_loop(redis, boot_id: str) -> None:
    while True:
        try:
            await _publish_worker_heartbeat(redis, boot_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The Redis key will expire and make API readiness/intake fail closed. Keep retrying so
            # a transient broker or statvfs error can recover without leaving a live-but-stuck ARQ
            # worker that only a manual restart repairs.
            _log.exception("worker capacity heartbeat publication failed")
        await asyncio.sleep(wsettings.worker_heartbeat_interval_s)


async def _worker_startup(ctx: dict[str, Any]) -> None:
    boot_id = str(uuid.uuid4())
    await _publish_worker_heartbeat(ctx["redis"], boot_id)
    ctx["worker_heartbeat_task"] = asyncio.create_task(
        _worker_heartbeat_loop(ctx["redis"], boot_id), name="worker-capacity-heartbeat"
    )


async def _worker_shutdown(ctx: dict[str, Any]) -> None:
    task = ctx.get("worker_heartbeat_task")
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _detector_version(detector: str) -> str:
    return {
        "meld_fcd": wsettings.meld_image,
        "hippunfold": wsettings.hippunfold_image,
        "map": f"{wsettings.map_image} (experimental MAP-inspired morphometry)",
    }.get(detector, detector)


def _execution_contract(detector: str, run: Run, recipe: Recipe) -> dict[str, Any]:
    detector_image = {
        "meld_fcd": wsettings.meld_image,
        "hippunfold": wsettings.hippunfold_image,
        "map": wsettings.map_image,
    }.get(detector)
    contract = {
        "schema_version": 2,
        "input_contract_sha256": run_input_contract_hash(
            recipe_id=recipe.id, recipe_spec_hash=recipe.spec_hash or "",
            logical_key=run.logical_key, detector_id=detector,
            source_role=_role(run.source_role) if run.source_role else None,
            source_series_uid=run.source_series_uid, params=dict(run.params or {}),
        ),
        "release_manifest_digest": wsettings.release_manifest_digest,
        "git_sha": wsettings.git_sha,
        "os_checksum": wsettings.os_checksum,
        "images": {"pkg": wsettings.pkg_image, "detector": detector_image},
        "deadlines": {
            "subprocess_timeout_s": wsettings.subprocess_timeout_s,
            "subprocess_stop_grace_s": wsettings.subprocess_stop_grace_s,
            "whole_run_timeout_s": wsettings.run_wall_timeout_s,
            "claim_lease_s": wsettings.run_claim_lease_s,
            "claim_heartbeat_s": wsettings.run_claim_heartbeat_s,
        },
    }
    if detector == "map":
        contract["scientific_code"] = {"map_segment_sha256": wsettings.map_script_sha256}
    if detector == "hippunfold":
        contract["scientific_parameters"] = {
            "ai_threshold_pct": wsettings.hippunfold_ai_threshold_pct,
        }
        contract["scientific_assets"] = {
            "hippunfold_cache_sha256": wsettings.hippunfold_cache_sha256,
        }
    return contract


def _failure_code(message: str) -> str:
    prefix = message.split(":", 1)[0].strip().lower().replace(" ", "_")
    code = re.sub(r"[^a-z0-9_.-]", "_", prefix)[:80]
    return code or "execution_failed"


async def _push_status(redis, run_id: str, status: str) -> None:
    # PostgreSQL is authoritative; dashboard cache failure must not turn a committed successful run
    # into a failed one.
    try:
        await redis.set(f"meld7t:run:{run_id}", status)
    except Exception:
        pass


def _role(value: Any) -> str:
    return str(getattr(value, "value", value))


def _resolve_acquisition(session: Session, run: Run, case: Case, needs_t2: bool
                         ) -> dicom.AcquisitionRequest:
    source_role = _role(run.source_role) if run.source_role else ""
    if source_role not in {"t1_uni", "t1_mprage"} or not run.source_series_uid:
        raise ValueError("run requires an exact confirmed T1 source role and SeriesInstanceUID")

    rows = session.exec(select(Series).where(
        Series.case_id == run.case_id, Series.active.is_(True))).all()
    by_uid = {row.orthanc_series_uid: row for row in rows}
    source = by_uid.get(run.source_series_uid)
    if source is None or not source.confirmed_role or _role(source.confirmed_role) != source_role:
        raise ValueError("run source UID is not a case series confirmed for its source role")

    params = dict(run.params or {})
    declared = params.get("series_uids") or {}
    companions = params.get("companion_series_uids") or {}
    input_manifest = params.get("acquisition_manifest") or run.source_manifest or {}
    if not isinstance(declared, dict) or not isinstance(companions, dict) or not isinstance(
            input_manifest, dict):
        raise ValueError("series UID/manifest run parameters must be objects")
    manifest_roles = (input_manifest.get("series_by_role") or
                      input_manifest.get("series_uids") or {})
    manifest_fingerprints = input_manifest.get("fingerprints_by_role") or {}
    manifest_acquisitions = input_manifest.get("acquisitions_by_role") or {}
    if (not isinstance(manifest_roles, dict) or not isinstance(manifest_fingerprints, dict)
            or not isinstance(manifest_acquisitions, dict)):
        raise ValueError("acquisition manifest series/fingerprint fields must be objects")
    explicit = {**manifest_roles, **declared, **companions}
    if explicit.get(source_role, run.source_series_uid) != run.source_series_uid:
        raise ValueError("source SeriesInstanceUID conflicts with acquisition contract")

    required = [source_role]
    if source_role == "t1_uni":
        required.extend(("t1_inv1", "t1_inv2"))
    if needs_t2:
        required.append("t2")
    series_by_role = {source_role: run.source_series_uid}
    expected_counts: dict[str, int] = {}
    expected_fingerprints: dict[str, str] = {}
    expected_acquisitions: dict[str, dict[str, Any]] = {}
    for role in required:
        if role == source_role:
            row = source
        elif explicit.get(role):
            row = by_uid.get(str(explicit[role]))
            if row is None or not row.confirmed_role or _role(row.confirmed_role) != role:
                raise ValueError(f"explicit companion UID is not confirmed as {role}")
        else:
            matches = [candidate for candidate in rows
                       if candidate.confirmed_role and _role(candidate.confirmed_role) == role]
            if len(matches) != 1:
                raise ValueError(
                    f"role {role} requires one exact companion UID; found {len(matches)}")
            row = matches[0]
        series_by_role[role] = row.orthanc_series_uid
        expected_fingerprint = manifest_fingerprints.get(role)
        if (app_settings.is_server_mode and (
                not expected_fingerprint or row.fingerprint != expected_fingerprint)):
            raise ValueError(f"series {role} fingerprint differs from acquisition contract")
        if expected_fingerprint:
            expected_fingerprints[row.orthanc_series_uid] = str(expected_fingerprint)
            acquisition = manifest_acquisitions.get(role)
            if not isinstance(acquisition, dict) or not acquisition:
                raise ValueError(f"series {role} lacks its acquisition metadata contract")
            expected_acquisitions[row.orthanc_series_uid] = acquisition
        count = getattr(row, "instance_count", None)
        if count is not None:
            if int(count) < 1:
                raise ValueError(f"series {row.orthanc_series_uid} has invalid expected count")
            expected_counts[row.orthanc_series_uid] = int(count)

    study_uid = input_manifest.get("study_uid") or case.orthanc_study_uid
    if input_manifest.get("bundle_fingerprint"):
        canonical_bundle = {
            "study_uid": input_manifest.get("study_uid"),
            "series_by_role": manifest_roles,
            "fingerprints_by_role": manifest_fingerprints,
            "acquisitions_by_role": manifest_acquisitions,
        }
        actual_bundle_hash = hashlib.sha256(json.dumps(
            canonical_bundle, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        if actual_bundle_hash != input_manifest["bundle_fingerprint"]:
            raise ValueError("acquisition bundle fingerprint is inconsistent")
    return dicom.AcquisitionRequest(
        run_id=run.id,
        series_by_role=series_by_role,
        study_uid=str(study_uid) if study_uid else None,
        expected_counts=expected_counts,
        expected_fingerprints=expected_fingerprints,
        expected_acquisitions=expected_acquisitions,
    )


def _read_recon_provenance(subject: str, expected_uids: set[str]) -> tuple[dict, str]:
    relative = os.path.join("input", subject, "anat", f"{subject}_recon-provenance.json")
    path = Path(wsettings.meld_data) / relative
    try:
        provenance = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"missing or invalid reconstruction provenance: {relative}") from exc
    if not isinstance(provenance, dict) or provenance.get("subject") != subject:
        raise ValueError("reconstruction provenance has wrong subject/schema")

    used: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("series_uid"):
                used.add(str(value["series_uid"]))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(provenance.get("series"))
    if used != expected_uids:
        raise ValueError(
            f"reconstruction used series {sorted(used)}, expected {sorted(expected_uids)}")
    return provenance, relative


def _hash_artifacts(relative_paths: tuple[str, ...] | list[str]) -> dict:
    root = Path(wsettings.meld_data).resolve(strict=True)
    files = []
    seen: set[str] = set()
    for item in relative_paths:
        relative = Path(item)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"detector artifact path is unsafe: {item}")
        candidate = root / relative
        components = [root / Path(*relative.parts[:index])
                      for index in range(1, len(relative.parts) + 1)]
        if any(component.is_symlink() for component in components):
            raise ValueError(f"detector artifact path contains a symlink: {item}")
        try:
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"required detector artifact is missing: {item}") from exc
        if root not in path.parents or not path.is_file():
            raise ValueError(f"detector artifact escapes data root or is not regular: {item}")
        normalized = relative.as_posix()
        if normalized in seen:
            continue
        seen.add(normalized)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        if size == 0:
            raise ValueError(f"required detector artifact is empty: {item}")
        files.append({"path": normalized, "sha256": digest.hexdigest(), "size": size})
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {"schema_version": 1, "files": files,
            "manifest_sha256": hashlib.sha256(canonical).hexdigest()}


def completion_bundle_sha256(*, run_fields: dict[str, Any], record_contract: dict[str, Any],
                             output_manifest: dict[str, Any],
                             provenance_contract: dict[str, Any]) -> str:
    """Hash a reconstructable completion preimage, excluding only its self-reference."""
    output_copy = json.loads(json.dumps(output_manifest, sort_keys=True, default=str))
    output_copy.pop("completion_bundle_sha256", None)
    provenance_copy = json.loads(json.dumps(
        provenance_contract, sort_keys=True, default=str))
    output_hashes = provenance_copy.get("output_hashes")
    if isinstance(output_hashes, dict):
        output_hashes.pop("completion_bundle", None)
    preimage = {
        "schema_version": 1,
        **run_fields,
        "record_contract": record_contract,
        "output_manifest": output_copy,
        "provenance": provenance_copy,
    }
    return hashlib.sha256(json.dumps(
        preimage, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _transition(run_id: str, status: RunStatus, claim_token: str) -> None:
    with Session(engine) as session:
        run = session.exec(select(Run).where(Run.id == run_id).with_for_update()).one()
        if run.claim_token != claim_token:
            raise RuntimeError("run claim was lost before stage transition")
        if run.status not in {RunStatus.preprocessing, RunStatus.inference, RunStatus.packaging}:
            raise RuntimeError(f"cannot transition run from terminal/non-running state {run.status}")
        run.status = status
        run.heartbeat_at = _now()
        run.lease_expires_at = run.heartbeat_at + timedelta(seconds=wsettings.run_claim_lease_s)
        session.add(run)
        session.commit()


def _claim(run_id: str) -> tuple[str, str | None, str, Any, dict, str] | None:
    """Atomically claim only queued work; duplicate ARQ delivery becomes a no-op."""
    with Session(engine) as session:
        run = session.exec(select(Run).where(Run.id == run_id).with_for_update()).first()
        if not run or run.status != RunStatus.queued:
            return None
        detector = _role(run.detector_id)
        runner = get_runner(detector)
        recipe = session.get(Recipe, run.recipe_id)
        if (recipe is None or not recipe.spec_hash
                or recipe.spec_hash != spec_hash(recipe.spec)):
            run.status = RunStatus.failed
            run.status_reason = "recipe_contract_mismatch"
            run.completed_at = _now()
            session.add(run)
            audit.record(
                session, actor="service:worker", action="run.contract_reject",
                entity_type="run", entity_id=run_id,
                payload={"attempt": run.attempt, "reason": "recipe_contract_mismatch"},
            )
            _maybe_finish_case(session, run.case_id)
            session.commit()
            return None
        actual_contract = _execution_contract(detector, run, recipe)
        provenance = session.exec(
            select(Provenance).where(Provenance.run_id == run_id).with_for_update()
        ).first()
        expected_release = provenance.release_manifest_digest if provenance else None
        mismatch = (
            expected_release != wsettings.release_manifest_digest
            if expected_release is not None or app_settings.is_server_mode else False
        )
        if run.execution_contract is not None:
            if (run.execution_contract.get("input_contract_sha256")
                    != actual_contract["input_contract_sha256"]):
                mismatch = True
            # The API pre-creates only the input hash. Once a worker binds runtime provenance,
            # every retry must match the full execution contract byte-for-byte.
            elif set(run.execution_contract) != {"schema_version", "input_contract_sha256"} \
                    and run.execution_contract != actual_contract:
                mismatch = True
        if mismatch:
            run.status = RunStatus.failed
            run.status_reason = "execution_contract_mismatch"
            run.completed_at = _now()
            session.add(run)
            audit.record(
                session, actor="service:worker", action="run.contract_reject",
                entity_type="run", entity_id=run_id,
                payload={"attempt": run.attempt, "expected_release_present": bool(expected_release)},
            )
            _maybe_finish_case(session, run.case_id)
            session.commit()
            return None
        run.execution_contract = actual_contract
        claim_token = str(uuid.uuid4())
        claimed_at = _now()
        run.status = RunStatus.preprocessing
        run.claimed_at = claimed_at
        run.heartbeat_at = claimed_at
        run.lease_expires_at = claimed_at + timedelta(seconds=wsettings.run_claim_lease_s)
        run.claim_token = claim_token
        run.attempt = int(getattr(run, "attempt", 0) or 0) + 1
        run.device = Device.gpu if (runner is None or runner.uses_gpu) else Device.cpu
        session.add(run)
        case = session.get(Case, run.case_id)
        if case is not None and case.status == CaseStatus.queued:
            case.status = CaseStatus.running
            session.add(case)
        session.add(Job(run_id=run_id, stage="run", status=RunStatus.preprocessing.value,
                        device=run.device, started_at=_now()))
        audit.record(session, actor="service:worker", action="run.start", entity_type="run",
                     entity_id=run_id, payload={"detector": detector,
                                                "source_role": _role(run.source_role)
                                                if run.source_role else None})
        session.commit()
        return (run.case_id, (_role(run.source_role) if run.source_role else None), detector,
                runner, dict(run.params or {}), claim_token)


def _renew_claim(run_id: str, claim_token: str) -> bool | None:
    with Session(engine) as session:
        run = session.exec(select(Run).where(Run.id == run_id).with_for_update()).first()
        if run is None or run.claim_token != claim_token:
            return False
        if run.status not in {
                RunStatus.preprocessing, RunStatus.inference, RunStatus.packaging}:
            return None
        now = _now()
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=wsettings.run_claim_lease_s)
        session.add(run)
        session.commit()
        return True


async def _heartbeat_claim(run_id: str, claim_token: str,
                           owner: asyncio.Task, finished: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    last_success = loop.time()
    while True:
        await asyncio.sleep(wsettings.run_claim_heartbeat_s)
        try:
            renewed = await asyncio.to_thread(_renew_claim, run_id, claim_token)
        except Exception:
            # A brief database interruption is tolerated, but stop the child process with one
            # heartbeat interval still left before another worker may reap/retry this claim.
            if loop.time() - last_success >= (
                    wsettings.run_claim_lease_s - wsettings.run_claim_heartbeat_s):
                owner.cancel()
                return
            continue
        if renewed is None:  # normal terminal transition won the race
            return
        if renewed is False:  # token changed or row disappeared: this attempt is fenced out
            if finished.is_set():
                return
            owner.cancel()
            return
        last_success = loop.time()


async def run_detector(ctx, run_id: str) -> dict:
    redis = ctx["redis"]
    await wait_if_paused(redis)
    if wsettings.deployment_mode in {"research", "production"}:
        capacity = await asyncio.to_thread(
            storage_health,
            wsettings.meld_data,
            minimum_free_bytes=wsettings.storage_admission_min_free_bytes,
            minimum_free_percent=wsettings.storage_min_free_percent,
        )
        if not capacity["ready"]:
            raise Retry(defer=300)
    claimed = await asyncio.to_thread(_claim, run_id)
    if claimed is None:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            return {"run_id": run_id, "skipped": "not queued",
                    "status": run.status.value if run else "not_found"}
    case_id, source_role, detector, runner, params, claim_token = claimed
    workdir = os.path.join(wsettings.meld_data, "work", run_id, claim_token)
    os.makedirs(workdir, exist_ok=True)
    await _push_status(redis, run_id, RunStatus.preprocessing.value)
    owner = asyncio.current_task()
    assert owner is not None
    finished = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_claim(run_id, claim_token, owner, finished),
        name=f"run-heartbeat:{run_id}")

    try:
        if runner is None:
            return await _fail(redis, run_id, RunStatus.failed, workdir,
                               f"no worker runner for detector '{detector}'", claim_token,
                               finished)
        harmonization = await asyncio.to_thread(
            resolve_harmonization, params.get("harmonization")
        )
        runner.validate_harmonization(harmonization)
        with Session(engine) as session:
            run = session.get(Run, run_id)
            case = session.get(Case, case_id)
            if not run or not case:
                raise ValueError("run or case disappeared after claim")
            request = _resolve_acquisition(session, run, case, runner.needs_t2)
            pseudonym = case.pseudonym

        dicom_root, source_manifest = await asyncio.to_thread(
            dicom.dicom_root_for, case, request
        )
        with Session(engine) as session:
            run = session.exec(select(Run).where(Run.id == run_id).with_for_update()).one()
            if run.claim_token != claim_token:
                raise RuntimeError("run claim was lost during DICOM acquisition")
            if run.source_manifest is not None and run.source_manifest != source_manifest:
                raise ValueError(
                    "DICOM source manifest changed across attempts; create a new recipe/run"
                )
            if run.source_manifest is None:
                run.source_manifest = source_manifest
            session.add(run)
            session.commit()

        rc, subject = await pipeline.run_prepare(
            run_id, source_role, request.series_by_role, dicom_root, workdir,
            also_t2=runner.needs_t2, claim_token=claim_token,
        )
        if rc != 0:
            return await _fail(redis, run_id, RunStatus.failed, workdir,
                               f"prepare failed with rc={rc}", claim_token, finished)
        recon_provenance, recon_relative = _read_recon_provenance(
            subject, set(request.series_by_role.values()))

        _transition(run_id, RunStatus.inference, claim_token)
        await _push_status(redis, run_id, RunStatus.inference.value)
        if runner.uses_gpu:
            async with gpu_lease(redis, run_id, claim_token):
                rc, fail_status = await runner.compute(subject, workdir, harmonization)
        else:
            rc, fail_status = await runner.compute(subject, workdir, harmonization)
        if rc != 0:
            return await _fail(redis, run_id, fail_status or RunStatus.failed,
                               workdir, f"{detector} rc={rc}", claim_token, finished)

        _transition(run_id, RunStatus.packaging, claim_token)
        await _push_status(redis, run_id, RunStatus.packaging.value)
        ingested = await runner.ingest(subject, workdir, harmonization)
        # Parse and validate every local scientific output before publishing derived DICOM.
        validated_ingest = runner.validate_ingest(ingested)
        if await asyncio.to_thread(_renew_claim, run_id, claim_token) is not True:
            raise RuntimeError("run claim was lost before external publication")
        uids = await runner.package(
            subject, pseudonym, workdir, run_id,
            expected_clusters=len(validated_ingest.clusters),
        )
        if await asyncio.to_thread(_renew_claim, run_id, claim_token) is not True:
            raise RuntimeError("run claim was lost during external publication")
        completed: DetectorCompletion = runner.validate_completion(ingested, uids)
        prepared = [os.path.join("input", subject, "anat", f"{subject}_T1w.nii.gz")]
        if runner.needs_t2:
            prepared.append(os.path.join("input", subject, "anat", f"{subject}_T2w.nii.gz"))
        output_manifest = await asyncio.to_thread(
            _hash_artifacts, [recon_relative, *prepared, *completed.artifacts]
        )
        record_contract = {
            "result": completed.result,
            "clusters": completed.clusters,
            "uids": completed.uids,
        }
        output_manifest["record_contract_sha256"] = hashlib.sha256(
            json.dumps(record_contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if completed.result.get("detector_summary") is not None:
            output_manifest["detector_summary"] = completed.result["detector_summary"]
        if completed.result.get("metric_schema") is not None:
            output_manifest["metric_schema"] = completed.result["metric_schema"]
        if completed.uids.get("dicom_manifest_sha256"):
            output_manifest["dicom_manifest_sha256"] = completed.uids[
                "dicom_manifest_sha256"]
            output_manifest["dicom_sop_count"] = int(completed.uids["dicom_sop_count"])

        with Session(engine) as session:
            run = session.exec(select(Run).where(Run.id == run_id).with_for_update()).one()
            if run.claim_token != claim_token:
                raise RuntimeError("run claim was lost before completion")
            if run.status != RunStatus.packaging:
                raise RuntimeError(f"run left packaging state before completion: {run.status}")
            if session.exec(select(Result).where(Result.run_id == run_id)).first():
                raise RuntimeError("run already has a result")
            rf = completed.result
            result = Result(
                run_id=run_id,
                harmo_code=rf.get("harmo_code"),
                orthanc_study_uid=completed.uids.get("study_uid"),
                orthanc_t1_uid=completed.uids.get("t1_series_uid"),
                orthanc_seg_uid=completed.uids.get("seg_series_uid"),
                report_path=rf.get("report_path"),
                n_clusters=rf["n_clusters"],
                output_manifest=output_manifest,
            )
            session.add(result)
            session.flush()
            for cluster in completed.clusters:
                session.add(Cluster(result_id=result.id, **cluster))
            # Recipe confirmation pre-creates this row in the same transaction as the run/outbox.
            # Updating it here preserves the one-provenance-per-run invariant.  The fallback keeps
            # older research databases upgradeable without fabricating a second row.
            provenance = session.exec(
                select(Provenance).where(Provenance.run_id == run_id).with_for_update()
            ).first() or Provenance(run_id=run_id)
            provenance.os_checksum = wsettings.os_checksum
            provenance.image_digests = {
                "pkg": wsettings.pkg_image, "detector": _detector_version(detector)}
            provenance.tool_versions = {"detector": _detector_version(detector)}
            provenance.git_sha = wsettings.git_sha
            provenance.params = params
            provenance.input_series_uid = run.source_series_uid
            provenance.recon_provenance = recon_provenance
            provenance.source_manifest = source_manifest
            provenance.harmonization = harmonization.metadata if harmonization else None
            provenance.output_hashes = {
                item["path"]: item["sha256"] for item in output_manifest["files"]}
            provenance.output_hashes["record_contract"] = output_manifest[
                "record_contract_sha256"]
            if output_manifest.get("dicom_manifest_sha256"):
                provenance.output_hashes["dicom_manifest"] = output_manifest[
                    "dicom_manifest_sha256"]
            provenance.release_manifest_digest = wsettings.release_manifest_digest
            provenance_contract = {
                "os_checksum": provenance.os_checksum,
                "image_digests": provenance.image_digests,
                "tool_versions": provenance.tool_versions,
                "git_sha": provenance.git_sha,
                "params": provenance.params,
                "input_series_uid": provenance.input_series_uid,
                "recon_provenance": provenance.recon_provenance,
                "source_manifest": provenance.source_manifest,
                "harmonization": provenance.harmonization,
                "output_hashes": provenance.output_hashes,
                "release_manifest_digest": provenance.release_manifest_digest,
            }
            run_fields = {
                "run_id": run.id,
                "recipe_id": run.recipe_id,
                "logical_key": run.logical_key,
                "attempt": run.attempt,
                "execution_contract": run.execution_contract,
            }
            completion_bundle_digest = completion_bundle_sha256(
                run_fields=run_fields, record_contract=record_contract,
                output_manifest=output_manifest, provenance_contract=provenance_contract,
            )
            output_manifest["completion_bundle_sha256"] = completion_bundle_digest
            provenance.output_hashes["completion_bundle"] = completion_bundle_digest
            result.output_manifest = output_manifest
            session.add(result)
            session.add(provenance)
            run.detector_version = _detector_version(detector)
            run.status = RunStatus.review_ready
            run.completed_at = _now()
            run.claim_token = None
            run.heartbeat_at = None
            run.lease_expires_at = None
            session.add(run)
            job = session.exec(select(Job).where(Job.run_id == run_id).order_by(Job.started_at.desc())).first()
            if job:
                job.status = RunStatus.review_ready.value
                job.finished_at = _now()
                session.add(job)
            audit.record(session, actor="service:worker", action="run.complete", entity_type="run",
                         entity_id=run_id, payload={"detector": detector, "subject": subject,
                                                    "n_clusters": len(completed.clusters),
                                                    "source_manifest_hmac_sha256": (
                                                        audit.sensitive_digest(source_manifest)),
                                                    "output_manifest_sha256": output_manifest[
                                                        "manifest_sha256"],
                                                    "record_contract_sha256": output_manifest[
                                                        "record_contract_sha256"],
                                                    "dicom_manifest_sha256": output_manifest.get(
                                                        "dicom_manifest_sha256"),
                                                    "completion_bundle_sha256": (
                                                        completion_bundle_digest),
                                                    "release_manifest_digest": (
                                                        wsettings.release_manifest_digest)})
            _maybe_finish_case(session, case_id)
            session.commit()
            # No await occurs between the terminal commit and this event, so heartbeat cannot
            # misclassify normal token clearing as a stale/reaped attempt.
            finished.set()

        await _push_status(redis, run_id, RunStatus.review_ready.value)
        return {"run_id": run_id, "status": RunStatus.review_ready.value,
                "n_clusters": len(completed.clusters),
                "orthanc_study": completed.uids.get("study_uid")}

    except asyncio.CancelledError:
        await asyncio.shield(_fail(redis, run_id, RunStatus.failed, workdir,
                                   "worker task cancelled; child process terminated", claim_token,
                                   finished))
        raise
    except Exception as exc:  # any failure is retained for the research support bundle
        return await _fail(redis, run_id, RunStatus.failed, workdir,
                           f"{type(exc).__name__}: {exc}", claim_token, finished)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def _fail(redis, run_id: str, status: RunStatus, workdir: str, msg: str,
                claim_token: str | None = None,
                finished: asyncio.Event | None = None) -> dict:
    msg = msg[:4000]
    public_code = _failure_code(msg)
    final_status = status
    terminal_commit = False
    with Session(engine) as session:
        run = session.exec(select(Run).where(Run.id == run_id).with_for_update()).first()
        if run:
            if claim_token is not None and run.claim_token != claim_token:
                final_status = run.status
                return {"run_id": run_id, "status": final_status.value,
                        "error": "claim_lost"}
            if run.status in {RunStatus.review_ready, RunStatus.adjudicated}:
                final_status = run.status
                terminal_commit = True
            else:
                run.status = status
                run.status_reason = public_code
                run.completed_at = _now()
                run.claim_token = None
                run.heartbeat_at = None
                run.lease_expires_at = None
                session.add(run)
                job = session.exec(
                    select(Job).where(Job.run_id == run_id).order_by(Job.started_at.desc())
                ).first()
                if job is None:
                    job = Job(run_id=run_id, stage="run", workdir=workdir)
                job.status = status.value
                job.error = public_code
                job.finished_at = _now()
                session.add(job)
                audit.record(
                    session, actor="service:worker", action="run.fail", entity_type="run",
                    entity_id=run_id,
                    payload={"status": status.value,
                             "error_category": msg.split(":", 1)[0][:120],
                             "error_hmac_sha256": audit.sensitive_digest(msg)},
                )
                _maybe_finish_case(session, run.case_id)
                session.commit()
                terminal_commit = True
    if terminal_commit and finished is not None:
        finished.set()
    await _push_status(redis, run_id, final_status.value)
    return {"run_id": run_id, "status": final_status.value, "error": public_code}


def _maybe_finish_case(session: Session, case_id: str) -> None:
    """Update the parent only when every runnable child is terminal; failures never become ready."""
    recipe = session.exec(select(Recipe).where(
        Recipe.case_id == case_id, Recipe.confirmed_at.is_not(None)
    ).order_by(Recipe.version.desc(), Recipe.created_at.desc())).first()
    if recipe is None:
        return
    runs = session.exec(select(Run).where(
        Run.recipe_id == recipe.id, Run.superseded_by.is_(None))).all()
    active = [run for run in runs if run.status in {
        RunStatus.created, RunStatus.queued, RunStatus.preprocessing, RunStatus.qc_pending,
        RunStatus.inference, RunStatus.packaging,
    }]
    if active:
        return
    case = session.get(Case, case_id)
    if not case or case.status not in {CaseStatus.queued, CaseStatus.running}:
        return
    if any(run.status in {RunStatus.failed, RunStatus.failed_oom, RunStatus.blocked} for run in runs):
        case.status = CaseStatus.failed
    else:
        case.status = CaseStatus.review_ready
    session.add(case)


class WorkerSettings:
    functions = [run_detector]
    redis_settings = RedisSettings.from_dsn(app_settings.redis_url)
    max_jobs = wsettings.worker_max_jobs
    # One coherent deadline includes GPU waiting and every stage. The task's cancellation handler
    # fences the claim, terminates process groups, and records a retained failure bundle.
    job_timeout = wsettings.run_wall_timeout_s
    max_tries = 10_080  # one-minute pause deferrals for up to seven days
    keep_result = 3600
    on_startup = _worker_startup
    on_shutdown = _worker_shutdown
