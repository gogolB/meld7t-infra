"""FastAPI app entrypoint with bounded background reconciliation and real readiness."""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, text
from sqlmodel import Session, select

from . import audit, queue
from .auth import correlation_id_middleware
from .config import settings
from .db import engine
from .harmonization import (
    canonical_json_sha256,
    profile_document_sha256,
    profile_artifact_root,
    profile_integrity_generation,
    selectors_may_overlap,
    validate_profile_semantics,
    verify_artifact_manifest,
)
from .harmonization_routes import router as harmonization_router
from .cohort_routes import router as cohort_router
from .models import (
    HarmonizationBuild, HarmonizationBuildStatus, HarmonizationCohort,
    HarmonizationCohortStatus, HarmonizationProfile, HarmonizationProfileStatus,
    OutboxEvent, OutboxStatus,
)
from .routes import router
from .storage import storage_health
from .version import API_VERSION


EXPECTED_SCHEMA_REVISION = "a3e7c1d9b5f2"
_log = logging.getLogger(__name__)
_reconciler_started_at = datetime.now(timezone.utc)
_reconciler_state: dict[str, object] = {
    "last_success": None,
    "last_error": None,
    "consecutive_failures": 0,
}
_profile_integrity_state: dict[str, object] = {
    "last_scan": None,
    "generation": -1,
    "ready": False,
    "active": 0,
    "verified": 0,
    "expected": len(settings.harmonization_expected_profiles),
    "failures": [{"error": "not_scanned"}],
}


def _reap_stale_runs_off_loop() -> None:
    with Session(engine) as session:
        queue.reap_stale_runs(session)
        queue.reap_stale_harmonization_uploads(session)
        queue.reap_stale_harmonization_builds(session)


async def _outbox_loop() -> None:
    interval = float(getattr(settings, "outbox_dispatch_interval_s", 5.0))
    # Let ASGI lifespan finish advertising startup before any database, broker, or thread-pool
    # reconciliation work begins.  This also prevents a slow first broker connection from making
    # service startup appear hung to systemd/TestClient.
    await asyncio.sleep(min(interval, 1.0))
    while True:
        try:
            with Session(engine) as session:
                await queue.dispatch_outbox_events(session)
                await queue.reconcile_queued_runs(session)
                await queue.reconcile_harmonization_jobs(session)
            await asyncio.to_thread(_reap_stale_runs_off_loop)
            _reconciler_state.update({
                "last_success": datetime.now(timezone.utc),
                "last_error": None,
                "consecutive_failures": 0,
            })
        except Exception as exc:
            _reconciler_state.update({
                "last_error": type(exc).__name__,
                "consecutive_failures": int(
                    _reconciler_state["consecutive_failures"] or 0) + 1,
            })
            _log.exception("durable outbox/reaper reconciliation cycle failed")
        await asyncio.sleep(interval)


def _scan_harmonization_profiles_off_loop() -> dict[str, object]:
    """Hash the exact active profile closure away from request and event-loop paths."""
    failures: list[dict[str, object]] = []
    verified_count = 0
    try:
        with Session(engine) as session:
            profiles = session.exec(select(HarmonizationProfile).where(
                HarmonizationProfile.status == HarmonizationProfileStatus.active
            )).all()
            generated_builds = session.exec(select(HarmonizationBuild).where(
                HarmonizationBuild.status == HarmonizationBuildStatus.active
            )).all()
            build_cohorts = {
                build.id: session.get(HarmonizationCohort, build.cohort_id)
                for build in generated_builds
            }
        expected = {
            (item.code, item.version): item
            for item in settings.harmonization_expected_profiles
        }
        active = {(profile.code, profile.version): profile for profile in profiles}
        generated_by_profile = {
            build.profile_id: build for build in generated_builds if build.profile_id
        }
        generated_profiles = {
            profile.id for profile in profiles
            if isinstance(profile.parameters, dict)
            and profile.parameters.get("storage_scope") == "generated"
        }
        if (not expected and not generated_profiles
                and not settings.harmonization_cohort_bootstrap_allowed):
            failures.append({"error": "signed_expected_inventory_missing"})
        for key, item in expected.items():
            profile = active.get(key)
            if profile is None:
                failures.append({
                    "code": item.code, "version": item.version,
                    "error": "expected_profile_not_active",
                })
                continue
            detector = getattr(profile.detector_id, "value", profile.detector_id)
            if detector != item.detector_id:
                failures.append({
                    "code": item.code, "version": item.version,
                    "error": "detector_mismatch",
                })
                continue
            if profile_document_sha256(profile) != item.document_sha256:
                failures.append({
                    "code": item.code, "version": item.version,
                    "error": "profile_document_mismatch",
                })
        for key in sorted(set(active) - set(expected)):
            profile = active[key]
            build = generated_by_profile.get(profile.id)
            cohort = build_cohorts.get(build.id) if build else None
            qc = build.qc_report if build else None
            artifact_manifest = build.artifact_manifest if build else None
            scientific_validation = (
                profile.parameters.get("scientific_validation")
                if isinstance(profile.parameters, dict) else None
            )
            generated_valid = (
                profile.id in generated_profiles
                and build is not None
                and build.builder_image_digest
                == (profile.parameters.get("build_images") or {}).get("meld")
                and bool(build.validated_by) and bool(build.activated_by)
                and len({build.initiated_by, build.validated_by, build.activated_by}) == 3
                and isinstance(qc, dict) and qc.get("all_folds_succeeded") is True
                and cohort is not None and cohort.status == HarmonizationCohortStatus.frozen
                and isinstance(cohort.frozen_manifest, dict)
                and cohort.frozen_manifest.get("manifest_sha256")
                == canonical_json_sha256({
                    name: value for name, value in cohort.frozen_manifest.items()
                    if name != "manifest_sha256"
                })
                and profile.parameters.get("cohort_manifest_sha256")
                == cohort.frozen_manifest.get("manifest_sha256")
                and qc.get("report_sha256") == canonical_json_sha256({
                    name: value for name, value in qc.items() if name != "report_sha256"
                })
                and profile.parameters.get("internal_cv_report_sha256")
                == qc.get("report_sha256")
                and profile.artifact_manifest == build.artifact_manifest
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(build.builder_adapter_sha256 or "")
                )
                is not None
                and qc.get("builder_adapter_sha256") == build.builder_adapter_sha256
                and isinstance(artifact_manifest, dict)
                and artifact_manifest.get("builder_adapter_sha256")
                == build.builder_adapter_sha256
                and profile.parameters.get("builder_adapter_sha256")
                == build.builder_adapter_sha256
                and isinstance(scientific_validation, dict)
                and scientific_validation.get("builder_adapter_sha256")
                == build.builder_adapter_sha256
            )
            if not generated_valid:
                failures.append({
                    "code": key[0], "version": key[1],
                    "error": "unexpected_or_unproven_generated_profile",
                })
        # Concurrent activation of different profile codes cannot be represented by a simple
        # database uniqueness constraint.  Recheck the conservative selector-disjointness rule
        # during every integrity scan so an ambiguous active set can never remain ready.
        for index, left in enumerate(profiles):
            for right in profiles[index + 1:]:
                left_detector = getattr(left.detector_id, "value", left.detector_id)
                right_detector = getattr(right.detector_id, "value", right.detector_id)
                if left_detector != right_detector:
                    continue
                try:
                    overlaps = selectors_may_overlap(left.selector, right.selector)
                except ValueError as exc:
                    failures.append({
                        "code": left.code,
                        "other_code": right.code,
                        "error": f"invalid_selector:{type(exc).__name__}",
                    })
                    continue
                if overlaps:
                    failures.append({
                        "code": left.code,
                        "other_code": right.code,
                        "detector_id": left_detector,
                        "error": "overlapping_active_selectors",
                    })
        for profile in profiles:
            try:
                verified = verify_artifact_manifest(
                    profile.artifact_manifest, profile_artifact_root(profile)
                )
                validate_profile_semantics(profile, verified)
                verified_count += 1
            except (OSError, ValueError) as exc:
                failures.append({
                    "profile_id": profile.id, "error": type(exc).__name__,
                })
    except Exception as exc:
        profiles = []
        failures.append({"error": f"scan_failed:{type(exc).__name__}"})
    return {
        "last_scan": datetime.now(timezone.utc),
        "generation": profile_integrity_generation(),
        "ready": not failures,
        "active": len(profiles),
        "verified": verified_count,
        "expected": len(settings.harmonization_expected_profiles),
        "failures": failures,
    }


async def _profile_integrity_loop() -> None:
    global _profile_integrity_state
    while True:
        last_scan = _profile_integrity_state.get("last_scan")
        due = (
            not isinstance(last_scan, datetime)
            or (datetime.now(timezone.utc) - last_scan).total_seconds()
            >= settings.harmonization_integrity_scan_interval_s
            or _profile_integrity_state.get("generation") != profile_integrity_generation()
        )
        if due:
            _profile_integrity_state = await asyncio.to_thread(
                _scan_harmonization_profiles_off_loop
            )
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [asyncio.create_task(_outbox_loop(), name="meld7t-outbox-dispatch")]
    if settings.harmonization_required:
        tasks.append(asyncio.create_task(
            _profile_integrity_loop(), name="harmonization-integrity-scan"
        ))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


server_mode = getattr(settings, "is_server_mode", True)
if server_mode:
    settings.require_harmonization_orthanc_credentials()
app = FastAPI(
    title="MELD 7T Research Platform API",
    version=API_VERSION,
    docs_url=None if server_mode else "/docs",
    redoc_url=None if server_mode else "/redoc",
    openapi_url=None if server_mode else "/openapi.json",
    lifespan=lifespan,
)
app.middleware("http")(correlation_id_middleware)
app.include_router(router)
app.include_router(harmonization_router)
app.include_router(cohort_router)


@app.exception_handler(audit.AuditLedgerError)
async def audit_unavailable(request: Request, _exc: audit.AuditLedgerError):
    return JSONResponse(
        status_code=503,
        content={
            "detail": "required immutable audit ledger is unavailable; no change was committed",
            "request_id": getattr(request.state, "correlation_id", None),
        },
        headers={"Cache-Control": "private, no-store", "Retry-After": "30"},
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if settings.is_server_mode and request.method not in {"GET", "HEAD", "OPTIONS"}:
        fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
        origin = request.headers.get("Origin")
        host = request.headers.get("Host", "").lower()
        origin_host = urlsplit(origin).netloc.lower() if origin else ""
        csrf_marker = request.headers.get("X-MELD-CSRF", "")
        if (csrf_marker != "1" or fetch_site == "cross-site"
                or (origin is not None and origin_host != host)):
            return JSONResponse(
                status_code=403,
                content={"detail": "state-changing request failed the origin/CSRF policy"},
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "private, no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict:
    checks: dict[str, object] = {}
    try:
        with Session(engine) as session:
            session.execute(text("SELECT 1")).scalar_one()
            revision = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            pending_statuses = [OutboxStatus.pending, OutboxStatus.failed, OutboxStatus.publishing]
            pending_count = int(session.exec(select(func.count(OutboxEvent.id)).where(
                OutboxEvent.status.in_(pending_statuses))).one())
            oldest = session.exec(select(func.min(OutboxEvent.created_at)).where(
                OutboxEvent.status.in_(pending_statuses))).one()
        checks["database"] = "ok"
        checks["schema_revision"] = (
            "ok" if revision == EXPECTED_SCHEMA_REVISION
            else f"mismatch:{revision or 'missing'}"
        )
        if oldest is not None:
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            lag_seconds = max(0, int((datetime.now(timezone.utc) - oldest).total_seconds()))
        else:
            lag_seconds = 0
        checks["run_outbox"] = {
            "ready": lag_seconds <= settings.outbox_max_lag_s,
            "pending": pending_count,
            "oldest_age_seconds": lag_seconds,
            "maximum_age_seconds": settings.outbox_max_lag_s,
        }
    except Exception as exc:
        checks["database"] = f"failed:{type(exc).__name__}"
    try:
        redis = queue.get_redis()
        checks["redis"] = "ok" if await redis.ping() else "failed"
        if settings.worker_heartbeat_required:
            checks["worker_consumer"] = queue.verify_worker_heartbeat(
                await redis.get(settings.worker_heartbeat_key)
            )
        if settings.harmonization_required:
            checks["harmonization_builder"] = queue.verify_worker_heartbeat(
                await redis.get(settings.harmonization_builder_heartbeat_key),
                max_age_s=settings.harmonization_builder_heartbeat_max_age_s,
                expected_capacity_kind="harmonization-builder",
                expected_images={"meld": settings.meld_image},
                expected_adapter_sha256=settings.harmonization_builder_adapter_sha256,
            )
    except Exception as exc:
        checks["redis"] = f"failed:{type(exc).__name__}"
        if settings.worker_heartbeat_required:
            checks["worker_consumer"] = {
                "ready": False, "status": "broker_unavailable",
            }
        if settings.harmonization_required:
            checks["harmonization_builder"] = {
                "ready": False, "status": "broker_unavailable",
            }
    checks["report_storage"] = storage_health(
        settings.meld_data,
        minimum_free_bytes=settings.storage_min_free_bytes,
        minimum_free_percent=settings.storage_min_free_percent,
    )
    now = datetime.now(timezone.utc)
    last_success = _reconciler_state["last_success"]
    grace = max(30.0, settings.outbox_dispatch_interval_s * 3)
    success_age = ((now - last_success).total_seconds()
                   if isinstance(last_success, datetime) else None)
    startup_age = (now - _reconciler_started_at).total_seconds()
    reconciler_ready = (
        int(_reconciler_state["consecutive_failures"] or 0) < 3
        and ((success_age is not None and success_age <= settings.outbox_max_lag_s)
             or (success_age is None and startup_age <= grace))
    )
    checks["background_reconciler"] = {
        "ready": reconciler_ready,
        "last_success_age_seconds": int(success_age) if success_age is not None else None,
        "consecutive_failures": _reconciler_state["consecutive_failures"],
        "last_error": _reconciler_state["last_error"],
    }
    if settings.harmonization_required:
        checks["harmonization_storage"] = (
            "ok" if Path(settings.harmonization_root).is_dir() else "missing")
        checks["harmonization_upload_storage"] = storage_health(
            settings.harmonization_upload_root,
            minimum_free_bytes=settings.storage_min_free_bytes,
            minimum_free_percent=settings.storage_min_free_percent,
        )
        checks["harmonization_generated_storage"] = (
            "ok" if Path(settings.harmonization_generated_root).is_dir() else "missing")
        integrity = dict(_profile_integrity_state)
        scan_at = integrity.pop("last_scan", None)
        scan_age = ((now - scan_at).total_seconds()
                    if isinstance(scan_at, datetime) else None)
        integrity["last_scan_age_seconds"] = (
            int(scan_age) if scan_age is not None else None
        )
        dirty = integrity.get("generation") != profile_integrity_generation()
        integrity["dirty"] = dirty
        integrity["ready"] = bool(
            integrity.get("ready") and not dirty and scan_age is not None
            and scan_age <= settings.harmonization_integrity_max_age_s
        )
        checks["harmonization_profiles"] = integrity
    ledger_health = getattr(audit, "ledger_health", None)
    if callable(ledger_health):
        # The proof-capable immudb client is synchronous and may wait for its configured
        # network timeout.  Never stall every request on the ASGI event loop while readiness is
        # probing an unavailable ledger.
        checks["audit_ledger"] = await asyncio.to_thread(ledger_health)
    failures = [
        value for key, value in checks.items()
        if not (value == "ok" or (isinstance(value, dict) and value.get("ready") is True))
    ]
    if failures:
        raise HTTPException(503, {"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}
