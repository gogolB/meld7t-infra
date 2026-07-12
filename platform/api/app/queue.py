"""Redis/Arq access for the API — enqueue runs, read live status (spec §5.2, §18)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus
from sqlalchemy import or_
from sqlmodel import Session, select

from .config import settings
from .models import (
    Case, CaseStatus, HarmonizationBuild, HarmonizationBuildStatus,
    HarmonizationUpload, HarmonizationUploadStatus, Job as JobRow, OutboxEvent, OutboxStatus,
    Run, RunStatus,
)

GPU_INUSE_KEY = "meld7t:gpu:inuse"
QUEUE_PAUSED_KEY = "meld7t:queue:paused"

_pool = None
_redis = None


def _heartbeat_signature(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(
        settings.audit_hmac_key.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()


def make_worker_heartbeat(*, boot_id: str, release_manifest_digest: str | None,
                          git_sha: str | None, os_checksum: str | None,
                          images: dict[str, str | None], observed_at: float | None = None,
                          capacity: dict[str, object] | None = None) -> str:
    """Build the authenticated, non-secret worker capacity statement stored in Redis."""
    payload = {
        "schema_version": 1,
        "boot_id": boot_id,
        "observed_at": observed_at if observed_at is not None else time.time(),
        "release_manifest_digest": release_manifest_digest,
        "git_sha": git_sha,
        "os_checksum": os_checksum,
        "images": images,
        "capacity": capacity or {
            "max_jobs": 2,
            "max_gpu_jobs": 1,
            "storage_ready": True,
        },
    }
    payload["hmac_sha256"] = _heartbeat_signature(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def verify_worker_heartbeat(raw: str | bytes | None, *, now: float | None = None,
                            max_age_s: int | None = None,
                            expected_capacity_kind: str | None = None,
                            expected_images: dict[str, str | None] | None = None,
                            expected_adapter_sha256: str | None = None) -> dict:
    """Validate freshness, HMAC, and signed-release identity for API readiness."""
    if raw is None:
        return {"ready": False, "status": "missing"}
    try:
        value = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("schema")
        signature = value.pop("hmac_sha256")
        if not isinstance(signature, str) or not hmac.compare_digest(
                signature, _heartbeat_signature(value)):
            return {"ready": False, "status": "authentication_failed"}
        observed = float(value["observed_at"])
        if not math.isfinite(observed):
            raise ValueError("timestamp")
        age = (now if now is not None else time.time()) - observed
        if age < -30:
            return {"ready": False, "status": "clock_skew", "age_seconds": int(age)}
        if age > (max_age_s or settings.worker_heartbeat_max_age_s):
            return {"ready": False, "status": "stale", "age_seconds": int(age)}
        if value.get("release_manifest_digest") != settings.release_manifest_digest:
            return {"ready": False, "status": "release_mismatch", "age_seconds": int(age)}
        if not isinstance(value.get("images"), dict) or not value["images"]:
            return {"ready": False, "status": "invalid_contract", "age_seconds": int(age)}
        capacity = value.get("capacity")
        if (not isinstance(capacity, dict)
                or isinstance(capacity.get("max_jobs"), bool)
                or not isinstance(capacity.get("max_jobs"), int)
                or not 1 <= capacity["max_jobs"] <= 8
                or capacity.get("max_gpu_jobs") != 1
                or not isinstance(capacity.get("storage_ready"), bool)):
            return {"ready": False, "status": "invalid_contract", "age_seconds": int(age)}
        if (expected_capacity_kind is not None
                and capacity.get("kind") != expected_capacity_kind):
            return {"ready": False, "status": "wrong_worker_kind", "age_seconds": int(age)}
        if expected_images is not None and any(
                value["images"].get(name) != expected
                for name, expected in expected_images.items()):
            return {"ready": False, "status": "image_mismatch", "age_seconds": int(age)}
        if expected_adapter_sha256 is not None and (
                capacity.get("adapter_ready") is not True
                or capacity.get("adapter_sha256") != expected_adapter_sha256):
            return {
                "ready": False, "status": "adapter_mismatch",
                "age_seconds": int(age), "capacity": capacity,
            }
        if capacity["storage_ready"] is not True:
            return {
                "ready": False,
                "status": "storage_capacity_unavailable",
                "age_seconds": max(0, int(age)),
                "capacity": capacity,
            }
        return {
            "ready": True,
            "status": "healthy",
            "age_seconds": max(0, int(age)),
            "release_manifest_digest": value.get("release_manifest_digest"),
            "git_sha": value.get("git_sha"),
            "os_checksum": value.get("os_checksum"),
            "capacity": capacity,
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {"ready": False, "status": "malformed"}


def _mirror_audit_record(record_id: str) -> None:
    """Run proof-capable synchronous immudb reconciliation off the ASGI event loop."""
    from . import audit
    from .db import engine

    with Session(engine) as mirror_session:
        try:
            audit.mirror_record(mirror_session, record_id)
        except Exception:
            # Preserve the explicit failed/attempt status written by mirror_record.
            mirror_session.commit()
            raise
        mirror_session.commit()


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


def get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def enqueue_run(run_id: str, *, attempt: int = 0) -> None:
    pool = await get_pool()
    # A deterministic id makes re-dispatch after a crash safe: Arq returns None if it already exists.
    await pool.enqueue_job("run_detector", run_id, _job_id=f"run:{run_id}:attempt:{attempt}")


async def enqueue_harmonization_build(build_id: str, *, attempt: int = 1,
                                      dispatch_token: str | None = None) -> None:
    pool = await get_pool()
    token = dispatch_token or f"attempt-{attempt}"
    await pool.enqueue_job(
        "run_harmonization_build", build_id,
        _job_id=f"harmonization-build:{build_id}:{token}",
        _queue_name=settings.harmonization_builder_queue,
    )


async def enqueue_harmonization_upload(upload_id: str,
                                       dispatch_token: str | None = None) -> None:
    pool = await get_pool()
    token = dispatch_token or "initial"
    await pool.enqueue_job(
        "ingest_harmonization_upload", upload_id,
        _job_id=f"harmonization-upload:{upload_id}:{token}",
        _queue_name=settings.harmonization_builder_queue,
    )


async def dispatch_outbox_events(session: Session, *, limit: int = 50) -> dict[str, int]:
    """Publish durable outbox rows to Arq with a recoverable lease.

    Workflow state and outbox rows commit together. If the process dies after Redis accepts a job
    but before ``published`` commits, the deterministic Arq job id makes the next dispatch harmless.
    """
    now = datetime.now(timezone.utc)
    statement = (
        select(OutboxEvent)
        .where(
            OutboxEvent.available_at <= now,
            or_(
                OutboxEvent.status.in_([OutboxStatus.pending, OutboxStatus.failed]),
                (OutboxEvent.status == OutboxStatus.publishing)
                & (OutboxEvent.lease_until < now),
            ),
        )
        .order_by(OutboxEvent.created_at)
        .limit(limit)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    events = list(session.exec(statement).all())
    counts = {"published": 0, "failed": 0}
    for event in events:
        event.status = OutboxStatus.publishing
        event.lease_until = now + timedelta(minutes=2)
        event.attempts += 1
        session.add(event)
        session.commit()
        try:
            if event.topic == "run.enqueue":
                await enqueue_run(str(event.payload["run_id"]),
                                  attempt=int(event.payload.get("attempt", 0)))
            elif event.topic == "harmonization.build.enqueue":
                await enqueue_harmonization_build(
                    str(event.payload["build_id"]),
                    attempt=int(event.payload.get("attempt", 1)),
                    dispatch_token=event.payload.get("dispatch_token"),
                )
            elif event.topic == "harmonization.upload.ingest":
                await enqueue_harmonization_upload(
                    str(event.payload["upload_id"]),
                    dispatch_token=event.payload.get("dispatch_token"),
                )
            elif event.topic == "audit.mirror":
                await asyncio.to_thread(
                    _mirror_audit_record, str(event.payload["audit_record_id"]))
            else:
                raise ValueError(f"unsupported outbox topic {event.topic!r}")
        except Exception as exc:
            event.status = OutboxStatus.failed
            event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            event.available_at = datetime.now(timezone.utc) + timedelta(
                seconds=min(300, 2 ** min(event.attempts, 8)))
            event.lease_until = None
            counts["failed"] += 1
        else:
            event.status = OutboxStatus.published
            event.published_at = datetime.now(timezone.utc)
            event.lease_until = None
            event.last_error = None
            counts["published"] += 1
        session.add(event)
        session.commit()
    return counts


async def reconcile_queued_runs(session: Session, *, limit: int = 100) -> dict[str, int]:
    """Re-open a published handoff when Redis no longer has its still-queued SQL run.

    Redis AOF makes this uncommon, but a broker restore must not strand committed research work.
    The grace window avoids racing the normal enqueue/worker-claim handoff.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.queue_reconcile_grace_s)
    runs = list(session.exec(
        select(Run).where(Run.status == RunStatus.queued).order_by(Run.created_at).limit(limit)
    ).all())
    pool = None
    counts = {"checked": 0, "created": 0, "reopened": 0}
    for run in runs:
        dedupe_key = f"run.enqueue:{run.logical_key}:attempt:{run.attempt}"
        event = session.exec(select(OutboxEvent).where(
            OutboxEvent.dedupe_key == dedupe_key)).first()
        if event is None:
            session.add(OutboxEvent(
                dedupe_key=dedupe_key, topic="run.enqueue", aggregate_type="run",
                aggregate_id=run.id, payload={"run_id": run.id, "attempt": run.attempt},
            ))
            counts["created"] += 1
            continue
        if event.status != OutboxStatus.published or not event.published_at \
                or event.published_at >= cutoff:
            continue
        counts["checked"] += 1
        if pool is None:
            pool = await get_pool()
        job_id = f"run:{run.id}:attempt:{run.attempt}"
        status = await Job(job_id, pool).status()
        if status not in {JobStatus.not_found, JobStatus.complete}:
            continue
        event.status = OutboxStatus.pending
        event.available_at = datetime.now(timezone.utc)
        event.published_at = None
        event.last_error = f"reconciled missing Redis job ({status.value})"
        session.add(event)
        counts["reopened"] += 1
    if counts["created"] or counts["reopened"]:
        session.commit()
    return counts


async def reconcile_harmonization_jobs(session: Session, *, limit: int = 100) -> dict[str, int]:
    """Reopen broker handoffs that left durable builds/uploads waiting without a Redis job."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.queue_reconcile_grace_s)
    targets: list[tuple[str, str, str, str, dict]] = []
    for build in session.exec(select(HarmonizationBuild).where(
            HarmonizationBuild.status == HarmonizationBuildStatus.queued
    ).order_by(HarmonizationBuild.created_at).limit(limit)).all():
        targets.append((
            f"harmonization.build.enqueue:{build.id}:attempt:{build.attempt}",
            "harmonization.build.enqueue", build.id,
            f"harmonization-build:{build.id}:attempt-{build.attempt}",
            {"build_id": build.id, "attempt": build.attempt},
        ))
    remaining = max(0, limit - len(targets))
    for upload in session.exec(select(HarmonizationUpload).where(
            HarmonizationUpload.status == HarmonizationUploadStatus.staged
    ).order_by(HarmonizationUpload.updated_at).limit(remaining)).all():
        targets.append((
            f"harmonization.upload.ingest:{upload.id}",
            "harmonization.upload.ingest", upload.id,
            f"harmonization-upload:{upload.id}:initial",
            {"upload_id": upload.id},
        ))

    pool = None
    counts = {"checked": 0, "created": 0, "reopened": 0}
    for dedupe_key, topic, aggregate_id, initial_job_id, payload in targets:
        event = session.exec(select(OutboxEvent).where(
            OutboxEvent.dedupe_key == dedupe_key)).first()
        if event is None:
            session.add(OutboxEvent(
                dedupe_key=dedupe_key, topic=topic,
                aggregate_type=("harmonization_build" if "build" in topic
                                else "harmonization_upload"),
                aggregate_id=aggregate_id, payload=payload,
            ))
            counts["created"] += 1
            continue
        published_at = event.published_at
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if (event.status != OutboxStatus.published or published_at is None
                or published_at >= cutoff):
            continue
        counts["checked"] += 1
        if pool is None:
            pool = await get_pool()
        token = event.payload.get("dispatch_token")
        if topic == "harmonization.build.enqueue":
            job_id = (f"harmonization-build:{aggregate_id}:{token}"
                      if token else initial_job_id)
        else:
            job_id = (f"harmonization-upload:{aggregate_id}:{token}"
                      if token else initial_job_id)
        status = await Job(job_id, pool).status()
        if status not in {JobStatus.not_found, JobStatus.complete}:
            continue
        dispatch_token = f"reconcile-{event.attempts + 1}"
        event.payload = {**payload, "dispatch_token": dispatch_token}
        event.status = OutboxStatus.pending
        event.available_at = datetime.now(timezone.utc)
        event.published_at = None
        event.last_error = f"reconciled missing Redis job ({status.value})"
        session.add(event)
        counts["reopened"] += 1
    if counts["created"] or counts["reopened"]:
        session.commit()
    return counts


def reap_stale_runs(session: Session, *, limit: int = 100) -> dict[str, int]:
    """Fail expired active claims so a worker/host crash cannot strand a case forever.

    Reusing deterministic SOP UIDs is only safe under the run's pinned execution contract, so the
    reaper does not automatically rerun work. An administrator may retry the failed run; the worker
    then verifies the exact release/image/OS contract before accepting it.
    """
    from . import audit

    now = datetime.now(timezone.utc)
    active = (RunStatus.preprocessing, RunStatus.inference, RunStatus.packaging)
    statement = (
        select(Run)
        .where(Run.status.in_(active), Run.lease_expires_at.is_not(None),
               Run.lease_expires_at <= now)
        .order_by(Run.lease_expires_at)
        .limit(limit)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    rows = list(session.exec(statement).all())
    if not rows:
        return {"reaped": 0, "cases_failed": 0}

    case_ids: set[str] = set()
    for run in rows:
        previous_status = run.status.value
        run.status = RunStatus.failed
        run.status_reason = "worker_lease_expired"
        run.completed_at = now
        run.claim_token = None
        run.heartbeat_at = None
        run.lease_expires_at = None
        session.add(run)
        job = session.exec(
            select(JobRow).where(JobRow.run_id == run.id).order_by(JobRow.started_at.desc())
        ).first()
        if job is not None and job.finished_at is None:
            job.status = RunStatus.failed.value
            job.error = "worker_lease_expired"
            job.finished_at = now
            session.add(job)
        case_ids.add(run.case_id)
        audit.record(
            session, actor="service:api-reconciler", action="run.lease_expire",
            entity_type="run", entity_id=run.id,
            payload={"previous_status": previous_status, "attempt": run.attempt},
        )

    failed_cases = 0
    for case_id in case_ids:
        remaining = session.exec(select(Run).where(
            Run.case_id == case_id, Run.status.in_(active + (RunStatus.queued, RunStatus.created))
        )).first()
        case = session.get(Case, case_id)
        if remaining is None and case is not None and case.status in {
                CaseStatus.queued, CaseStatus.running}:
            case.status = CaseStatus.failed
            session.add(case)
            failed_cases += 1
    session.commit()
    return {"reaped": len(rows), "cases_failed": failed_cases}


def reap_stale_harmonization_uploads(session: Session, *, limit: int = 100) -> dict[str, int]:
    """Expire abandoned chunk sessions and remove only their build-owned staging files."""
    from . import audit

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.harmonization_upload_expiry_hours)
    statement = select(HarmonizationUpload).where(
        HarmonizationUpload.status == HarmonizationUploadStatus.receiving,
        HarmonizationUpload.updated_at <= cutoff,
    ).order_by(HarmonizationUpload.updated_at).limit(limit)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    rows = session.exec(statement).all()
    removed = 0
    root = Path(settings.harmonization_upload_root).resolve()
    for row in rows:
        candidate = root / row.storage_key
        file_removed = False
        try:
            if candidate.parent == root and candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
                removed += 1
                file_removed = True
        except OSError:
            pass
        row.status = HarmonizationUploadStatus.failed
        row.last_error = "upload_session_expired"
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        audit.record(
            session, actor="service:api-reconciler", action="harmonization.upload.expire",
            entity_type="harmonization_upload", entity_id=row.id,
            payload={"received_size": row.received_size, "staging_file_removed": file_removed},
        )
    terminal_removed = 0
    terminal_rows = session.exec(select(HarmonizationUpload).where(
        HarmonizationUpload.status.in_([
            HarmonizationUploadStatus.imported, HarmonizationUploadStatus.failed,
        ]),
        HarmonizationUpload.staging_cleaned_at.is_(None),
    ).order_by(HarmonizationUpload.updated_at).limit(limit)).all()
    for row in terminal_rows:
        candidate = root / row.storage_key
        source_clean = not candidate.exists() and not candidate.is_symlink()
        try:
            if candidate.parent == root and candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
                terminal_removed += 1
                source_clean = True
        except OSError:
            pass
        # A pending receipt is the builder/operator's deletion ledger and must remain, but it is
        # not "clean": leave the marker null so a post-resolution unlink failure is retried.
        receipt = root / f"{row.storage_key}.receipt"
        receipt_protected = (row.import_result or {}).get("phase") in {
            "rollback_incomplete", "rollback_delete_approved"}
        receipt_clean = not receipt.exists() and not receipt.is_symlink()
        if not receipt_protected and not receipt_clean:
            try:
                if receipt.parent == root and receipt.is_file() and not receipt.is_symlink():
                    receipt.unlink()
                    terminal_removed += 1
                    receipt_clean = True
            except OSError:
                pass
        if source_clean and receipt_clean and not receipt_protected:
            row.staging_cleaned_at = datetime.now(timezone.utc)
            session.add(row)
        elif source_clean and receipt_protected:
            # Rotate protected rows behind other terminal cleanup work without claiming completion.
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
    mapping_statement = select(HarmonizationUpload).where(
        HarmonizationUpload.status == HarmonizationUploadStatus.imported,
        HarmonizationUpload.mapping_redacted_at.is_(None),
        HarmonizationUpload.updated_at <= cutoff,
    ).order_by(HarmonizationUpload.updated_at).limit(limit)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        mapping_statement = mapping_statement.with_for_update(skip_locked=True)
    mapping_rows = session.exec(mapping_statement).all()
    mappings_redacted = 0
    for row in mapping_rows:
        result = dict(row.import_result or {})
        studies = result.get("studies")
        if isinstance(studies, list):
            result["studies"] = [
                {"study_uid": item.get("study_uid")}
                for item in studies if isinstance(item, dict) and item.get("study_uid")
            ]
        result["subject_mappings_redacted"] = True
        row.import_result = result
        row.mapping_redacted_at = datetime.now(timezone.utc)
        session.add(row)
        audit.record(
            session, actor="service:api-reconciler",
            action="harmonization.upload.subject_mapping_redact",
            entity_type="harmonization_upload", entity_id=row.id,
            payload={"study_count": len(result.get("studies") or [])},
        )
        mappings_redacted += 1
    if rows or terminal_rows or mapping_rows:
        session.commit()
    return {"expired": len(rows), "files_removed": removed,
            "terminal_files_removed": terminal_removed,
            "subject_mappings_redacted": mappings_redacted}


def reap_stale_harmonization_builds(session: Session, *, limit: int = 20) -> dict[str, int]:
    """Fail interrupted compute stages and re-dispatch the atomic publication stage."""
    from . import audit

    now = datetime.now(timezone.utc)
    statement = select(HarmonizationBuild).where(
        HarmonizationBuild.status == HarmonizationBuildStatus.building,
        HarmonizationBuild.lease_expires_at.is_not(None),
        HarmonizationBuild.lease_expires_at <= now,
    ).order_by(HarmonizationBuild.lease_expires_at).limit(limit)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    rows = session.exec(statement).all()
    failed = resumed = 0
    for build in rows:
        if build.stage == "publishing" and build.artifact_manifest and build.qc_report:
            token = "resume-publication"
            dedupe_key = f"harmonization.build.resume:{build.id}"
            event = session.exec(select(OutboxEvent).where(
                OutboxEvent.dedupe_key == dedupe_key)).first()
            if event is None:
                session.add(OutboxEvent(
                    dedupe_key=dedupe_key, topic="harmonization.build.enqueue",
                    aggregate_type="harmonization_build", aggregate_id=build.id,
                    payload={"build_id": build.id, "attempt": build.attempt,
                             "dispatch_token": token},
                ))
                audit.record(
                    session, actor="service:api-reconciler",
                    action="harmonization.build.resume",
                    entity_type="harmonization_build", entity_id=build.id,
                    payload={"stage": build.stage},
                )
                resumed += 1
            elif (event.status == OutboxStatus.published and event.published_at
                  and event.published_at <= now - timedelta(seconds=3700)):
                # Builder results are retained for one hour. After that, reopening the same
                # deterministic job ID is safe if publication still has not committed.
                event.status = OutboxStatus.pending
                event.available_at = now
                event.published_at = None
                event.last_error = "reopened stale publication resume"
                session.add(event)
                resumed += 1
            build.lease_expires_at = now + timedelta(seconds=settings.harmonization_builder_lease_s)
            session.add(build)
        else:
            build.status, build.stage = HarmonizationBuildStatus.failed, "failed"
            build.error_code = "builder_lease_expired"
            build.completed_at = now
            build.heartbeat_at = None
            build.lease_expires_at = None
            session.add(build)
            audit.record(
                session, actor="service:api-reconciler", action="harmonization.build.expire",
                entity_type="harmonization_build", entity_id=build.id,
                payload={"error_code": build.error_code},
            )
            failed += 1
    if rows:
        session.commit()
    return {"failed": failed, "publication_resumed": resumed}
