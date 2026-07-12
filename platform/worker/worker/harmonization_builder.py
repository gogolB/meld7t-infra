"""Single-concurrency worker for cohort uploads and MELD profile estimation.

The scientific implementation is an explicit, site-accepted adapter executable.  The worker owns
state transitions, deterministic folds, resource/time fences, artifact publication, and auditing;
the adapter owns DICOM preparation and the pinned MELD invocation and returns bounded metrics.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import resource
import signal
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from arq import Retry
from arq.connections import RedisSettings
from dicomweb_client.api import DICOMwebClient
from sqlmodel import Session, select

from app import audit
from app import queue as app_queue
from app.cohort_builder import (
    canonical_sha256, lock_harmonization_orthanc_mutation, qc_summary,
)
from app.config import settings
from app.dicom_policy import PROHIBITED_DIRECT_KEYWORDS, validate_deidentified_part10
from app.db import engine
from app.harmonization import canonical_json_sha256, sha256_file
from app.models import (
    DetectorId,
    HarmonizationBuild,
    HarmonizationBuildStatus,
    HarmonizationCohort,
    HarmonizationCohortStudy,
    HarmonizationDemographic,
    HarmonizationFoldResult,
    HarmonizationProfile,
    HarmonizationProfileStatus,
    HarmonizationUpload,
    HarmonizationUploadStatus,
)
from app.storage import storage_health
from app import upload_receipt

from .config import wsettings
from .gpu import gpu_lease, wait_if_paused


class BuildCancelled(RuntimeError):
    pass


class BuildAdmissionDeferred(RuntimeError):
    """A queued build cannot cross the durable Orthanc rollback fence yet."""


class PublicationPending(RuntimeError):
    """Filesystem publication succeeded but its atomic SQL/audit finalization must retry."""


_log = logging.getLogger(__name__)


def _require_storage_capacity(path: str | Path, *, incoming_bytes: int = 0) -> None:
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    required = wsettings.storage_min_free_bytes + incoming_bytes
    free_percent = usage.free * 100 / max(usage.total, 1)
    if usage.free < required or free_percent < wsettings.storage_min_free_percent:
        raise RuntimeError("harmonization_storage_watermark_reached")


def _require_builder_credentials() -> None:
    if (wsettings.deployment_mode in {"research", "production"}
            and len(wsettings.harmonization_orthanc_password.get_secret_value()) < 32):
        raise RuntimeError("harmonization_builder_orthanc_credential_invalid")


def _lease_deadline(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(
        seconds=wsettings.harmonization_builder_lease_s)


def _renew_build_lease(build_id: str) -> None:
    with Session(engine) as session:
        build = session.get(HarmonizationBuild, build_id)
        if build is None or build.status == HarmonizationBuildStatus.cancelled:
            raise BuildCancelled("harmonization_build_cancelled")
        if build.status != HarmonizationBuildStatus.building:
            raise RuntimeError("harmonization_build_execution_fenced")
        now = datetime.now(timezone.utc)
        build.heartbeat_at = now
        build.lease_expires_at = _lease_deadline(now)
        session.add(build)
        session.commit()


def _sha256_file_with_lease(path: Path, build_id: str) -> str:
    """Hash a potentially large artifact without letting the execution lease lapse."""
    digest = hashlib.sha256()
    last_heartbeat = 0.0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            if (time.monotonic() - last_heartbeat
                    >= wsettings.harmonization_builder_heartbeat_s):
                _renew_build_lease(build_id)
                last_heartbeat = time.monotonic()
    return digest.hexdigest()


def _safe_workspace(root: str, identifier: str) -> Path:
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    path = base / identifier
    if path.exists():
        raise RuntimeError("immutable harmonization workspace already exists")
    path.mkdir(mode=0o700)
    return path


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")
    path.chmod(0o600)


def _workspace_size(root: Path) -> int:
    """Return allocated file bytes without following adapter-created links."""
    total = 0
    entries = 0
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            candidate = current_path / name
            if candidate.is_symlink():
                raise RuntimeError("harmonization_adapter_created_symlink")
        for name in files:
            entries += 1
            if entries > wsettings.harmonization_upload_max_files:
                raise RuntimeError("harmonization_workspace_file_limit_exceeded")
            metadata = (current_path / name).stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("harmonization_adapter_created_special_file")
            total += metadata.st_size
            if total > wsettings.harmonization_build_max_bytes:
                raise RuntimeError("harmonization_workspace_byte_limit_exceeded")
    return total


def _stop_process_group(process: subprocess.Popen) -> None:
    """Terminate the adapter and all non-detached descendants, escalating after the grace."""
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + wsettings.subprocess_stop_grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait()


def _artifact_locations(build: HarmonizationBuild, cohort: HarmonizationCohort) -> tuple[Path, Path]:
    root = Path(wsettings.harmonization_generated_root).resolve()
    pending = root / ".pending" / build.id
    destination = root / "profiles" / cohort.profile_code / f"v{cohort.profile_version}"
    return pending, destination


def _existing_published_candidate(
        build: HarmonizationBuild, cohort: HarmonizationCohort) -> Path | None:
    """Return an already durable exact candidate, or reject a conflicting destination."""
    _pending, destination_dir = _artifact_locations(build, cohort)
    if not destination_dir.exists() and not destination_dir.is_symlink():
        return None
    expected_name = f"MELD_{cohort.profile_code}combat_parameters.hdf5"
    expected = (build.artifact_manifest or {}).get("files", [{}])[0]
    if not destination_dir.is_dir() or destination_dir.is_symlink():
        raise RuntimeError("generated_profile_destination_conflict")
    candidate = destination_dir / expected_name
    if (candidate.is_file() and not candidate.is_symlink()
            and sha256_file(candidate) == expected.get("sha256")
            and candidate.stat().st_size == expected.get("size")):
        return candidate
    raise RuntimeError("generated_profile_destination_conflict")


def _discard_pending_publication(build: HarmonizationBuild, cohort: HarmonizationCohort) -> None:
    pending, _destination = _artifact_locations(build, cohort)
    try:
        removed = False
        if pending.is_symlink():
            pending.unlink()
            removed = True
        elif pending.exists():
            shutil.rmtree(pending)
            removed = True
        if removed and pending.parent.is_dir():
            _fsync_directory(pending.parent)
    except OSError:
        pass


def _ensure_private_directory(path: Path, mode: int) -> None:
    existed = path.exists()
    path.mkdir(mode=mode, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError("generated_profile_storage_path_invalid")
    if not existed:
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_published(build: HarmonizationBuild, cohort: HarmonizationCohort,
                      source: Path) -> Path:
    """Publish one build-owned directory atomically and reconcile a post-rename crash."""
    pending, destination_dir = _artifact_locations(build, cohort)
    expected_name = f"MELD_{cohort.profile_code}combat_parameters.hdf5"
    expected = (build.artifact_manifest or {}).get("files", [{}])[0]
    existing = _existing_published_candidate(build, cohort)
    if existing is not None:
        _fsync_directory(existing.parent.parent)
        return existing
    root = Path(wsettings.harmonization_generated_root).resolve()
    root_existed = root.exists()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("generated_profile_storage_path_invalid")
    if not root_existed:
        _fsync_directory(root.parent)
    _ensure_private_directory(root / ".pending", 0o700)
    _ensure_private_directory(root / "profiles", 0o750)
    _ensure_private_directory(root / "profiles" / cohort.profile_code, 0o750)
    _discard_pending_publication(build, cohort)
    pending.mkdir(mode=0o700)
    _fsync_directory(pending.parent)
    candidate = pending / expected_name
    shutil.copyfile(source, candidate)
    if (candidate.stat().st_size != expected.get("size")
            or sha256_file(candidate) != expected.get("sha256")):
        raise RuntimeError("generated_profile_source_changed_during_publication")
    candidate.chmod(0o440)
    with candidate.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(pending)
    pending.rename(destination_dir)
    _fsync_directory(pending.parent)
    _fsync_directory(destination_dir.parent)
    return destination_dir / expected_name


def _finish_candidate(session: Session, build: HarmonizationBuild,
                      cohort: HarmonizationCohort) -> None:
    if session.exec(select(HarmonizationProfile).where(
            HarmonizationProfile.code == cohort.profile_code,
            HarmonizationProfile.version == cohort.profile_version)).first():
        raise RuntimeError("generated_profile_database_conflict")
    relative_root = Path("profiles") / cohort.profile_code / f"v{cohort.profile_version}"
    subject_count = int((build.qc_report or {}).get("subject_count", 0))
    profile = HarmonizationProfile(
        code=cohort.profile_code, version=cohort.profile_version, name=cohort.name,
        method="meld_distributed_combat", detector_id=DetectorId.meld_fcd,
        selector=cohort.selector, artifact_manifest=build.artifact_manifest or {},
        parameters={
            "harmo_code": cohort.profile_code,
            "cohort_manifest_sha256": cohort.frozen_manifest["manifest_sha256"],
            "activation_eligible": True,
            "control_count": subject_count,
            "minimum_subjects": cohort.min_controls,
            "selector_canonical_sha256": canonical_json_sha256(cohort.selector),
            "build_images": {"meld": build.builder_image_digest},
            "builder_adapter_sha256": build.builder_adapter_sha256,
            "data_root": relative_root.as_posix(),
            "storage_scope": "generated",
            "internal_cv_report_sha256": (build.qc_report or {}).get("report_sha256"),
        },
        created_by="service:harmonization-builder",
    )
    session.add(profile)
    session.flush()
    build.profile_id = profile.id
    build.status, build.stage, build.progress = (
        HarmonizationBuildStatus.qc_review, "qc_review", 100)
    build.error_code = None
    build.heartbeat_at = None
    build.lease_expires_at = None
    session.add(build)
    audit.record(session, actor="service:harmonization-builder",
                 action="harmonization.build.qc_ready", entity_type="harmonization_build",
                 entity_id=build.id, payload={
                     "profile_id": profile.id,
                     "qc_report_sha256": (build.qc_report or {}).get("report_sha256"),
                     "artifact_manifest_sha256": canonical_sha256(build.artifact_manifest),
                     "builder_adapter_sha256": build.builder_adapter_sha256,
                 })
    session.commit()


def _publish_candidate(session: Session, build: HarmonizationBuild,
                       cohort: HarmonizationCohort, source: Path) -> None:
    """Fence cancellation against publication and the candidate database commit."""
    statement = select(HarmonizationBuild).where(HarmonizationBuild.id == build.id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    current = session.exec(statement.execution_options(populate_existing=True)).one()
    if current.status == HarmonizationBuildStatus.cancelled:
        raise BuildCancelled("harmonization_build_cancelled_before_publication")
    if (current.status != HarmonizationBuildStatus.building
            or current.stage != "publishing"):
        raise RuntimeError("harmonization_publication_state_changed")
    try:
        _ensure_published(current, cohort, source)
    except RuntimeError as exc:
        if str(exc) == "generated_profile_destination_conflict":
            _discard_pending_publication(current, cohort)
            raise
        try:
            durable = _existing_published_candidate(current, cohort)
        except RuntimeError:
            raise
        if durable is not None:
            raise PublicationPending(
                "harmonization_publication_finalization_pending") from exc
        _discard_pending_publication(current, cohort)
        raise
    except Exception as exc:
        try:
            durable = _existing_published_candidate(current, cohort)
        except RuntimeError:
            raise
        if durable is not None:
            raise PublicationPending(
                "harmonization_publication_finalization_pending") from exc
        _discard_pending_publication(current, cohort)
        raise
    try:
        _finish_candidate(session, current, cohort)
    except RuntimeError as exc:
        if str(exc) in {
                "generated_profile_database_conflict",
                "generated_profile_destination_conflict",
        }:
            raise
        raise PublicationPending("harmonization_publication_finalization_pending") from exc
    except Exception as exc:
        raise PublicationPending("harmonization_publication_finalization_pending") from exc


def _selected_source(row: HarmonizationCohortStudy) -> dict[str, Any]:
    selected = [item for item in row.series_manifest if item.get("selected_source") is True]
    if len(selected) != 1 or not isinstance(selected[0].get("instance_manifest"), list):
        raise RuntimeError("frozen_source_series_binding_invalid")
    return selected[0]


def _snapshot_sources(cohort: HarmonizationCohort, studies: list[HarmonizationCohortStudy],
                      workspace: Path, build_id: str) -> dict[str, list[dict[str, Any]]]:
    """Materialize a hash-verified, build-owned snapshot of every selected source instance."""
    root = workspace / "snapshot"
    root.mkdir(mode=0o700)
    class BoundedSession(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("allow_redirects", False)
            return super().request(method, url, **kwargs)

    client_session = BoundedSession()
    client_session.trust_env = False
    client_session.auth = (
        wsettings.harmonization_orthanc_user,
        wsettings.harmonization_orthanc_password.get_secret_value(),
    )
    dicomweb_root = wsettings.harmonization_orthanc_rest.rstrip("/") + "/dicom-web"
    dicomweb = DICOMwebClient(url=dicomweb_root, session=client_session)
    result: dict[str, list[dict[str, Any]]] = {}
    total = 0
    last_heartbeat = 0.0
    for study in sorted(studies, key=lambda value: value.subject_key_hmac):
        _renew_build_lease(build_id)
        last_heartbeat = time.monotonic()
        source = _selected_source(study)
        series_uid = str(source.get("series_uid", ""))
        expected = source["instance_manifest"]
        if (not expected or int(source.get("instance_count") or 0) != len(expected)
                or len({item.get("sop_instance_uid") for item in expected}) != len(expected)):
            raise RuntimeError("frozen_source_instance_manifest_invalid")
        current = dicomweb.search_for_instances(
            study_instance_uid=study.orthanc_study_uid,
            series_instance_uid=series_uid,
            fields=["00080018"],
            get_remaining=True,
        )
        current_uids = {
            str((item.get("00080018", {}).get("Value") or [""])[0]) for item in current
        }
        expected_uids = {str(item["sop_instance_uid"]) for item in expected}
        if current_uids != expected_uids:
            raise RuntimeError("frozen_source_instance_inventory_changed")
        subject_root = root / study.subject_key_hmac
        subject_root.mkdir(mode=0o700)
        materialized: list[dict[str, Any]] = []
        for index, item in enumerate(sorted(expected, key=lambda value: value["sop_instance_uid"])):
            sop_uid = str(item["sop_instance_uid"])
            destination = subject_root / f"{index:06d}.dcm"
            url = (
                f"{dicomweb_root}/studies/{quote(study.orthanc_study_uid, safe='')}/series/"
                f"{quote(series_uid, safe='')}/instances/{quote(sop_uid, safe='')}"
            )
            digest = hashlib.sha256()
            size = 0
            prefix = bytearray()
            with client_session.get(
                    url, headers={"Accept": "application/dicom"}, stream=True,
                    timeout=120, allow_redirects=False) as response:
                response.raise_for_status()
                with destination.open("xb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        total += len(chunk)
                        if len(prefix) < 132:
                            prefix.extend(chunk[:132 - len(prefix)])
                        if (size > wsettings.harmonization_max_instance_bytes
                                or total > wsettings.harmonization_build_max_bytes):
                            raise RuntimeError("harmonization_snapshot_byte_limit_exceeded")
                        digest.update(chunk)
                        output.write(chunk)
                        if (time.monotonic() - last_heartbeat
                                >= wsettings.harmonization_builder_heartbeat_s):
                            _renew_build_lease(build_id)
                            last_heartbeat = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
            destination.chmod(0o400)
            if (size != int(item.get("size", -1)) or digest.hexdigest() != item.get("sha256")
                    or bytes(prefix[128:132]) != b"DICM"):
                raise RuntimeError("frozen_source_instance_content_changed")
            materialized.append({
                "sha256": digest.hexdigest(),
                "size": size,
                "path": destination.relative_to(workspace).as_posix(),
            })
        result[study.subject_key_hmac] = materialized
    _workspace_size(workspace)
    return result


def _request_document(session: Session, build: HarmonizationBuild,
                      cohort: HarmonizationCohort,
                      snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    studies = session.exec(select(HarmonizationCohortStudy).where(
        HarmonizationCohortStudy.cohort_id == cohort.id,
        HarmonizationCohortStudy.included.is_(True))).all()
    demographics = session.exec(select(HarmonizationDemographic).where(
        HarmonizationDemographic.cohort_id == cohort.id)).all()
    demo = {row.subject_key_hmac: {"age": row.age, "sex": row.sex} for row in demographics}
    return {
        "schema_version": 1,
        "build_id": build.id,
        "profile": {"code": cohort.profile_code, "version": cohort.profile_version,
                    "name": cohort.name, "detector_id": "meld_fcd"},
        "source_role": cohort.source_role.value,
        "selector": cohort.selector,
        "frozen_manifest": cohort.frozen_manifest,
        "builder_image_digest": build.builder_image_digest,
        "builder_adapter_sha256": build.builder_adapter_sha256,
        "acceptance_criteria": build.acceptance_criteria,
        "snapshot_root": str((Path(wsettings.harmonization_build_root) / build.id).resolve()),
        "subjects": [{
            "subject_key_hmac": row.subject_key_hmac,
            "study_sha256": row.study_sha256,
            "dicom_instances": snapshot[row.subject_key_hmac],
            **demo[row.subject_key_hmac],
        } for row in sorted(studies, key=lambda value: value.subject_key_hmac)],
        "cv_plan": (build.cv_plan or {}).get("folds", []),
    }


def _verify_snapshot(snapshot: dict[str, list[dict[str, Any]]], workspace: Path,
                     build_id: str) -> None:
    last_heartbeat = 0.0
    for instances in snapshot.values():
        for item in instances:
            relative = Path(str(item.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("harmonization_snapshot_path_invalid")
            path = workspace / relative
            if (not path.is_file() or path.is_symlink()
                    or path.stat().st_size != int(item.get("size", -1))):
                raise RuntimeError("harmonization_snapshot_changed_by_adapter")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    if (time.monotonic() - last_heartbeat
                            >= wsettings.harmonization_builder_heartbeat_s):
                        _renew_build_lease(build_id)
                        last_heartbeat = time.monotonic()
            if digest.hexdigest() != item.get("sha256"):
                raise RuntimeError("harmonization_snapshot_changed_by_adapter")


def _run_adapter(request_path: Path, output: Path, *, mode: str,
                 build_id: str, fold_index: int | None = None) -> dict[str, Any]:
    configured = wsettings.harmonization_builder_adapter
    if not configured:
        raise RuntimeError("harmonization_builder_adapter_unconfigured")
    configured_path = Path(configured)
    if configured_path.is_symlink():
        raise RuntimeError("harmonization_builder_adapter_invalid")
    adapter = configured_path.resolve(strict=True)
    if not adapter.is_file() or not os.access(adapter, os.X_OK):
        raise RuntimeError("harmonization_builder_adapter_invalid")
    expected_adapter_sha = wsettings.harmonization_builder_adapter_sha256
    if not expected_adapter_sha or sha256_file(adapter) != expected_adapter_sha:
        raise RuntimeError("harmonization_builder_adapter_digest_mismatch")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    command = [str(adapter), "--request", str(request_path), "--output", str(output),
               "--mode", mode]
    if fold_index is not None:
        command.extend(["--fold", str(fold_index)])
    started = time.monotonic()
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    adapter_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "MELD7T_HARMONIZATION_SNAPSHOT_ROOT": str(request_path.parent / "snapshot"),
    }
    for name in ("HOME", "XDG_RUNTIME_DIR"):
        if os.environ.get(name):
            adapter_env[name] = os.environ[name]
    # Adapter output is discarded because an external scientific implementation cannot be trusted
    # to redact DICOM identifiers. Structured result.json and worker-owned error codes are retained.
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=adapter_env,
        start_new_session=True,
    )
    last_heartbeat = 0.0
    try:
        while process.poll() is None:
            if time.monotonic() - started > wsettings.subprocess_timeout_s:
                _stop_process_group(process)
                raise RuntimeError("harmonization_adapter_timeout")
            with Session(engine) as cancellation_session:
                current = cancellation_session.get(HarmonizationBuild, build_id)
                if current is None or current.status == HarmonizationBuildStatus.cancelled:
                    _stop_process_group(process)
                    raise BuildCancelled("harmonization_build_cancelled")
                if current.status != HarmonizationBuildStatus.building:
                    _stop_process_group(process)
                    raise RuntimeError("harmonization_build_execution_fenced")
                if time.monotonic() - last_heartbeat >= wsettings.harmonization_builder_heartbeat_s:
                    _workspace_size(request_path.parent)
                    _require_storage_capacity(wsettings.harmonization_build_root)
                    now = datetime.now(timezone.utc)
                    current.heartbeat_at = now
                    current.lease_expires_at = _lease_deadline(now)
                    cancellation_session.add(current)
                    cancellation_session.commit()
                    last_heartbeat = time.monotonic()
            time.sleep(0.5)
    except BaseException:
        _stop_process_group(process)
        raise
    return_code = process.returncode
    if return_code:
        raise RuntimeError(f"harmonization_adapter_exit_{return_code}")
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        pass
    else:
        _stop_process_group(process)
        raise RuntimeError("harmonization_adapter_left_background_processes")
    _workspace_size(request_path.parent)
    result_path = output / "result.json"
    if not result_path.is_file() or result_path.is_symlink() or result_path.stat().st_size > 2_000_000:
        raise RuntimeError("harmonization_adapter_result_missing_or_oversized")
    result = json.loads(result_path.read_text())
    if not isinstance(result, dict) or not isinstance(result.get("passed"), bool):
        raise RuntimeError("harmonization_adapter_result_invalid")
    metrics = result.get("metrics")
    if (not isinstance(metrics, dict) or len(metrics) > 256
            or any(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", str(key)) is None
                   for key in metrics)
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(float(value)) for value in metrics.values())):
        raise RuntimeError("harmonization_adapter_metrics_invalid")
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    result["resource_usage"] = {
        "wall_seconds": round(time.monotonic() - started, 3),
        "user_cpu_seconds": round(usage_after.ru_utime - usage_before.ru_utime, 3),
        "system_cpu_seconds": round(usage_after.ru_stime - usage_before.ru_stime, 3),
        "maximum_resident_kib": int(usage_after.ru_maxrss),
    }
    return result


def _criteria_pass(metrics: dict[str, Any], criteria: dict[str, Any], *,
                   field: str = "required_metrics") -> bool:
    required = criteria.get(field)
    if field == "final_required_metrics" and required is None:
        required = criteria.get("required_metrics")
    if not isinstance(required, dict) or not required:
        return False
    for name, bounds in required.items():
        if name not in metrics or isinstance(metrics[name], bool):
            return False
        try:
            value = float(metrics[name])
        except (TypeError, ValueError):
            return False
        if not isinstance(bounds, dict):
            return False
        if bounds.get("min") is not None and value < float(bounds["min"]):
            return False
        if bounds.get("max") is not None and value > float(bounds["max"]):
            return False
    return True


def _assert_no_pending_orthanc_rollback(session: Session) -> None:
    """Atomically fence the queued→building transition against receipt-governed deletion."""
    lock_harmonization_orthanc_mutation(session)
    pending = any(
        (row.import_result or {}).get("phase") in {
            "rollback_incomplete", "rollback_delete_approved"
        }
        for row in session.exec(select(HarmonizationUpload).where(
            HarmonizationUpload.status == HarmonizationUploadStatus.failed
        )).all()
    )
    if pending:
        raise BuildAdmissionDeferred("harmonization_orthanc_rollback_pending")


def _execute_build(build_id: str) -> None:
    workspace: Path | None = None
    try:
        _require_builder_credentials()
        with Session(engine) as session:
            statement = select(HarmonizationBuild).where(HarmonizationBuild.id == build_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            build = session.exec(
                statement.execution_options(populate_existing=True)
            ).first()
            if build is None:
                return
            cohort = session.get(HarmonizationCohort, build.cohort_id)
            if cohort is None or not cohort.frozen_manifest:
                raise RuntimeError("frozen_cohort_unavailable")
            if (build.status == HarmonizationBuildStatus.building
                    and build.stage == "publishing" and build.artifact_manifest
                    and build.qc_report):
                build.heartbeat_at = datetime.now(timezone.utc)
                build.lease_expires_at = _lease_deadline(build.heartbeat_at)
                session.add(build)
                session.commit()
                relative_source = Path(str(build.qc_report.get("final_artifact_relative", "")))
                if (relative_source.is_absolute() or ".." in relative_source.parts
                        or not relative_source.parts):
                    raise RuntimeError("published_artifact_resume_contract_invalid")
                source = Path(wsettings.harmonization_build_root) / build.id / relative_source
                _publish_candidate(session, build, cohort, source)
                return
            if build.status == HarmonizationBuildStatus.building:
                build.status, build.stage = HarmonizationBuildStatus.failed, "failed"
                build.error_code = "builder_interrupted_requires_new_attempt"
                build.completed_at = datetime.now(timezone.utc)
                build.heartbeat_at = None
                build.lease_expires_at = None
                session.add(build)
                audit.record(
                    session, actor="service:harmonization-builder",
                    action="harmonization.build.interrupted",
                    entity_type="harmonization_build", entity_id=build.id,
                    payload={"error_code": build.error_code},
                )
                session.commit()
                return
            if build.status != HarmonizationBuildStatus.queued:
                return
            # The API takes this same transaction-scoped lock before enqueueing. Recheck at the
            # actual state transition because an import can fail after a sampled healthy heartbeat
            # and after the build request was committed.
            _assert_no_pending_orthanc_rollback(session)
            if build.builder_image_digest != wsettings.meld_image:
                raise RuntimeError("builder_image_differs_from_worker_release")
            if (not wsettings.harmonization_builder_adapter_sha256
                    or build.builder_adapter_sha256
                    != wsettings.harmonization_builder_adapter_sha256
                    or not _adapter_ready()):
                raise RuntimeError("builder_adapter_differs_from_admitted_contract")
            build.status, build.stage, build.progress = (
                HarmonizationBuildStatus.building, "cross_validation", 1)
            build.started_at = datetime.now(timezone.utc)
            build.heartbeat_at = build.started_at
            build.lease_expires_at = _lease_deadline(build.started_at)
            session.add(build)
            audit.record(session, actor="service:harmonization-builder",
                         action="harmonization.build.start", entity_type="harmonization_build",
                         entity_id=build.id, payload={
                             "builder_image_digest": build.builder_image_digest,
                             "builder_adapter_sha256": build.builder_adapter_sha256,
                             "cohort_manifest_sha256": cohort.frozen_manifest["manifest_sha256"],
                         })
            session.commit()

            studies = list(session.exec(select(HarmonizationCohortStudy).where(
                HarmonizationCohortStudy.cohort_id == cohort.id,
                HarmonizationCohortStudy.included.is_(True))).all())
            snapshot_bytes = sum(
                int(instance.get("size", 0))
                for study in studies
                for instance in _selected_source(study)["instance_manifest"]
            )
            required_bytes = snapshot_bytes + wsettings.storage_output_headroom_bytes
            if required_bytes > wsettings.harmonization_build_max_bytes:
                raise RuntimeError("harmonization_build_reservation_exceeds_workspace_limit")
            _require_storage_capacity(
                wsettings.harmonization_build_root,
                incoming_bytes=required_bytes,
            )
            workspace = _safe_workspace(wsettings.harmonization_build_root, build.id)
            snapshot = _snapshot_sources(cohort, studies, workspace, build.id)
            request = _request_document(session, build, cohort, snapshot)
            request_path = workspace / "request.json"
            _write_json(request_path, request)
            fold_plan = request["cv_plan"]
            for position, fold in enumerate(fold_plan):
                session.refresh(build)
                if build.status == HarmonizationBuildStatus.cancelled:
                    return
                result = _run_adapter(request_path, workspace / f"fold-{position}",
                                      mode="cross-validation", build_id=build.id,
                                      fold_index=position)
                _verify_snapshot(snapshot, workspace, build.id)
                passed = result["passed"] and _criteria_pass(
                    result["metrics"], build.acceptance_criteria)
                session.add(HarmonizationFoldResult(
                    build_id=build.id, fold_index=position,
                    train_count=len(fold["train_subject_hmacs"]),
                    holdout_count=len(fold["holdout_subject_hmacs"]),
                    membership_hmac_sha256=fold["membership_hmac_sha256"],
                    status="passed" if passed else "failed", metrics=result["metrics"],
                    resource_usage=result["resource_usage"],
                ))
                build.progress = 5 + int(70 * (position + 1) / len(fold_plan))
                session.add(build)
                session.commit()
                if not passed:
                    raise RuntimeError("cross_validation_acceptance_failed")

            build.stage, build.progress = "final_fit", 80
            session.add(build)
            session.commit()
            final = workspace / "final"
            final_result = _run_adapter(request_path, final, mode="final", build_id=build.id)
            _verify_snapshot(snapshot, workspace, build.id)
            session.refresh(build)
            if build.status == HarmonizationBuildStatus.cancelled:
                return
            if (not final_result["passed"] or not _criteria_pass(
                    final_result["metrics"], build.acceptance_criteria,
                    field="final_required_metrics")):
                raise RuntimeError("final_fit_failed")
            expected_name = f"MELD_{cohort.profile_code}combat_parameters.hdf5"
            candidates = [path for path in final.rglob(expected_name)
                          if path.is_file() and not path.is_symlink()]
            if len(candidates) != 1:
                raise RuntimeError("final_fit_artifact_missing_or_ambiguous")

            relative_root = Path("profiles") / cohort.profile_code / f"v{cohort.profile_version}"
            artifact_manifest = {
                "schema_version": 1,
                "files": [{"path": (relative_root / expected_name).as_posix(),
                           "sha256": _sha256_file_with_lease(candidates[0], build.id),
                           "size": candidates[0].stat().st_size}],
                "cohort_manifest_sha256": cohort.frozen_manifest["manifest_sha256"],
                "builder_adapter_sha256": build.builder_adapter_sha256,
            }
            folds = session.exec(select(HarmonizationFoldResult).where(
                HarmonizationFoldResult.build_id == build.id
            ).order_by(HarmonizationFoldResult.fold_index)).all()
            report = qc_summary(folds, subject_count=len(request["subjects"]))
            report.update({
                "build_id": build.id,
                "cohort_manifest_sha256": cohort.frozen_manifest["manifest_sha256"],
                "acceptance_criteria_sha256": canonical_sha256(build.acceptance_criteria),
                "builder_image_digest": build.builder_image_digest,
                "builder_adapter_sha256": build.builder_adapter_sha256,
                "final_fit_metrics": final_result.get("metrics", {}),
                "final_fit_resource_usage": final_result.get("resource_usage", {}),
                "final_artifact_relative": candidates[0].relative_to(workspace).as_posix(),
            })
            report["report_sha256"] = canonical_sha256(report)
            _write_json(workspace / "qc-report.json", report)
            build.artifact_manifest = artifact_manifest
            build.qc_report = report
            build.stage, build.progress = "publishing", 95
            build.heartbeat_at = datetime.now(timezone.utc)
            build.lease_expires_at = _lease_deadline(build.heartbeat_at)
            session.add(build)
            session.commit()
            _publish_candidate(session, build, cohort, candidates[0])
    except Exception as exc:
        safe_error = _safe_worker_error_code(exc)
        if isinstance(exc, BuildAdmissionDeferred):
            raise
        try:
            with Session(engine) as session:
                build = session.get(HarmonizationBuild, build_id)
                if isinstance(exc, PublicationPending) and build is not None:
                    build.heartbeat_at = datetime.now(timezone.utc)
                    build.lease_expires_at = build.heartbeat_at
                    build.error_code = "publication_finalization_pending"
                    session.add(build)
                    try:
                        session.commit()
                    except Exception:
                        session.rollback()
                elif build is not None and build.status not in {
                        HarmonizationBuildStatus.cancelled, HarmonizationBuildStatus.qc_review,
                        HarmonizationBuildStatus.validated, HarmonizationBuildStatus.active}:
                    build.status, build.stage = HarmonizationBuildStatus.failed, "failed"
                    build.error_code = safe_error
                    build.completed_at = datetime.now(timezone.utc)
                    build.heartbeat_at = None
                    build.lease_expires_at = None
                    session.add(build)
                    audit.record(
                        session, actor="service:harmonization-builder",
                        action="harmonization.build.fail", entity_type="harmonization_build",
                        entity_id=build.id, payload={"error_code": build.error_code})
                    session.commit()
        except Exception:
            raise RuntimeError(
                "harmonization_build_failure_state_persistence_failed") from None
        # ARQ captures uncaught exception text and tracebacks in journald. Re-raise only a bounded
        # machine code so WADO URLs and their raw study/series/SOP identifiers cannot reach logs.
        raise RuntimeError(safe_error) from None
    finally:
        # Raw Study UIDs, demographics, DICOM snapshots, and untrusted adapter outputs are
        # temporary.  The database retains minimized manifests/QC/resource evidence; only an
        # interrupted atomic publication keeps its workspace for deterministic reconciliation.
        try:
            with Session(engine) as cleanup_session:
                current = cleanup_session.get(HarmonizationBuild, build_id)
                retain = bool(
                    current is not None
                    and current.status == HarmonizationBuildStatus.building
                    and current.stage == "publishing"
                )
            if not retain:
                shutil.rmtree(Path(wsettings.harmonization_build_root) / build_id,
                              ignore_errors=True)
        except Exception:
            pass


def _dicom_paths(upload: Path, work: Path) -> list[Path]:
    with upload.open("rb") as handle:
        signature = handle.read(4)
    if signature == b"PK\x03\x04":
        paths: list[Path] = []
        total = 0
        with zipfile.ZipFile(upload) as archive:
            infos = archive.infolist()
            if len(infos) > wsettings.harmonization_upload_max_files:
                raise ValueError("archive_file_limit_exceeded")
            for info in infos:
                relative = Path(info.filename)
                mode = info.external_attr >> 16
                if (relative.is_absolute() or ".." in relative.parts
                        or stat.S_ISLNK(mode) or info.flag_bits & 0x1):
                    raise ValueError("unsafe_archive_entry")
                if info.is_dir():
                    continue
                total += info.file_size
                if total > wsettings.harmonization_upload_max_expanded_bytes:
                    raise ValueError("archive_expanded_size_limit_exceeded")
                target = work / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as destination:
                    written = 0
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size:
                            raise ValueError("archive_entry_exceeds_declared_size")
                        destination.write(chunk)
                    if written != info.file_size:
                        raise ValueError("archive_entry_size_mismatch")
                    destination.flush()
                    os.fsync(destination.fileno())
                # Check the filesystem actually receiving this extraction.  Production callers
                # place ``work`` under harmonization_build_root; tests and recovery tooling may
                # supply a different bounded scratch root.
                _require_storage_capacity(work)
                paths.append(target)
        return paths
    target = work / "upload.dcm"
    shutil.copyfile(upload, target)
    return [target]


def _expanded_upload_bytes(upload: Path) -> int:
    """Validate the ZIP central directory and return exact declared extraction bytes."""
    with upload.open("rb") as handle:
        signature = handle.read(4)
    if signature != b"PK\x03\x04":
        return upload.stat().st_size
    total = 0
    with zipfile.ZipFile(upload) as archive:
        infos = archive.infolist()
        if len(infos) > wsettings.harmonization_upload_max_files:
            raise ValueError("archive_file_limit_exceeded")
        for info in infos:
            relative = Path(info.filename)
            mode = info.external_attr >> 16
            if (relative.is_absolute() or ".." in relative.parts
                    or stat.S_ISLNK(mode) or info.flag_bits & 0x1):
                raise ValueError("unsafe_archive_entry")
            if info.is_dir():
                continue
            total += info.file_size
            if total > wsettings.harmonization_upload_max_expanded_bytes:
                raise ValueError("archive_expanded_size_limit_exceeded")
    return total


_PROHIBITED_DIRECT_TAGS = PROHIBITED_DIRECT_KEYWORDS


def _validate_local_dicom(path: Path) -> dict[str, Any]:
    """Apply the enforceable portion of the deidentification admission contract pre-store."""
    if path.stat().st_size > wsettings.harmonization_max_instance_bytes:
        raise ValueError("dicom_instance_size_limit_exceeded")
    contract = validate_deidentified_part10(
        path,
        allowed_transfer_syntaxes=wsettings.harmonization_allowed_transfer_syntaxes,
        allowed_private_tags=wsettings.harmonization_allowed_private_tags,
    )
    return {
        **contract,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def _validate_imported_instance(client: requests.Session, instance_id: str) -> None:
    base = wsettings.harmonization_orthanc_rest.rstrip("/")
    tags_response = client.get(
        f"{base}/instances/{instance_id}/simplified-tags", timeout=30, allow_redirects=False)
    syntax_response = client.get(
        f"{base}/instances/{instance_id}/metadata/TransferSyntax",
        timeout=30, allow_redirects=False)
    if tags_response.status_code != 200 or syntax_response.status_code != 200:
        raise RuntimeError("harmonization_orthanc_instance_validation_failed")
    tags = tags_response.json()
    if (not isinstance(tags, dict)
            or any(str(tags.get(name, "")).strip() for name in _PROHIBITED_DIRECT_TAGS)):
        raise ValueError("dicom_direct_identifier_policy_failed")
    transfer_syntax = syntax_response.text.strip().strip('"')
    if transfer_syntax not in set(wsettings.harmonization_allowed_transfer_syntaxes):
        raise ValueError("dicom_transfer_syntax_not_allowed")


def _append_receipt(path: Path, event: dict[str, Any]) -> None:
    upload_receipt.append_receipt(path, event)


def _load_receipt(path: Path) -> tuple[
        dict[str, Any] | None, dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    return upload_receipt.load_receipt(path)


def _find_orthanc_instance(client: requests.Session, sop_instance_uid: str) -> list[str]:
    response = client.post(
        wsettings.harmonization_orthanc_rest.rstrip("/") + "/tools/find",
        json={"Level": "Instance", "Query": {"SOPInstanceUID": sop_instance_uid}},
        timeout=30, allow_redirects=False,
    )
    if response.status_code != 200:
        raise RuntimeError("harmonization_orthanc_preflight_failed")
    value = response.json()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError("harmonization_orthanc_preflight_response_invalid")
    return value


def _orthanc_instance_matches(client: requests.Session, instance_id: str,
                              expected_sha256: str, expected_size: int) -> bool:
    return _orthanc_instance_match_state(
        client, instance_id, expected_sha256, expected_size) == "match"


def _orthanc_instance_match_state(client: requests.Session, instance_id: str,
                                  expected_sha256: str, expected_size: int) -> str:
    response = client.get(
        wsettings.harmonization_orthanc_rest.rstrip("/") + f"/instances/{instance_id}/file",
        timeout=120, allow_redirects=False, stream=True,
    )
    if response.status_code != 200:
        status = response.status_code
        response.close()
        return "missing" if status == 404 else "different"
    digest = hashlib.sha256()
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                size += len(chunk)
                if size > expected_size:
                    return "different"
                digest.update(chunk)
    finally:
        response.close()
    return ("match" if size == expected_size and digest.hexdigest() == expected_sha256
            else "different")


def _rollback_instances(client: requests.Session, instance_ids: list[str]) -> list[str]:
    failed: list[str] = []
    for instance_id in reversed(list(dict.fromkeys(instance_ids))):
        try:
            response = client.delete(
                wsettings.harmonization_orthanc_rest.rstrip("/")
                + f"/instances/{instance_id}", timeout=30, allow_redirects=False)
            if response.status_code not in {200, 404}:
                failed.append(instance_id)
        except requests.RequestException:
            failed.append(instance_id)
    return failed


def _receipt_owned_instances(
        client: requests.Session, receipt: Path, *, include_ambiguous: bool = False
        ) -> tuple[list[str], int]:
    """Return proven-owned objects, or exact ambiguous objects after audited approval."""
    _header, intents, completed = _load_receipt(receipt)
    owned, ambiguous, verification = _receipt_instance_candidates(
        client, intents, completed, include_ambiguous=include_ambiguous)
    return owned, ambiguous + verification


def _receipt_instance_candidates(
        client: requests.Session, intents: dict[str, dict[str, Any]],
        completed: dict[str, dict[str, Any]], *, include_ambiguous: bool = False,
        proven_owned_ids: set[str] | None = None,
        ) -> tuple[list[str], int, int]:
    owned: list[str] = []
    ambiguous = 0
    verification_failures = 0
    proven_owned_ids = proven_owned_ids or set()
    for digest, record in completed.items():
        intent = intents.get(digest)
        # An explicit AlreadyStored response proves this upload did not create the object.  It is
        # never an exact-delete candidate, even when an administrator approves deletion of
        # response-lost (intent-only) objects elsewhere in the same receipt.
        if intent is not None and record["owned"]:
            state = _orthanc_instance_match_state(
                client, record["instance_id"], digest, int(intent["size"]))
            if state == "match":
                owned.append(record["instance_id"])
            elif state != "missing":
                verification_failures += 1
    for digest, intent in intents.items():
        if digest in completed:
            continue
        try:
            matches = _find_orthanc_instance(client, intent["sop_instance_uid"])
            exact = [instance_id for instance_id in matches if _orthanc_instance_matches(
                client, instance_id, digest, int(intent["size"]))]
            # Within the live process, a successful POST response proves ownership even if a
            # subsequent validation failed before its stored record was fsynced. Preserve the
            # ambiguity after a crash (where this volatile set is unavailable), but do not leave a
            # false rollback gate after deleting an ID this process can prove it created.
            known_exact = [item for item in exact if item in proven_owned_ids]
            owned.extend(known_exact)
            if known_exact:
                known = set(known_exact)
                matches = [item for item in matches if item not in known]
                exact = [item for item in exact if item not in known]
            if include_ambiguous:
                owned.extend(exact)
                ambiguous += len(matches) - len(exact)
            elif len(matches) == 1 and len(exact) == 1:
                # The POST response was lost, so an independent C-STORE or another controlled
                # import could own the exact object. Preserve it for explicit reconciliation.
                ambiguous += 1
            elif matches:
                ambiguous += len(matches)
        except Exception:
            verification_failures += 1
    return list(dict.fromkeys(owned)), ambiguous, verification_failures


def _rollback_upload_receipt(client: requests.Session,
                             receipt: Path,
                             known_instance_ids: list[str], *,
                             upload_sha256: str,
                             instance_manifest_sha256: str | None,
                             instance_count: int | None,
                             session: Session | None = None) -> dict[str, Any]:
    header, intents, completed = _load_receipt(receipt)
    upload_receipt.validate_header(
        header,
        upload_sha256=upload_sha256,
        instance_manifest_sha256=instance_manifest_sha256,
        instance_count=instance_count,
    )
    receipt_digest = upload_receipt.evidence_sha256(header, intents, completed)
    referenced = (_referenced_sop_count(
        session, {value["sop_instance_uid"] for value in intents.values()})
        if session is not None else 0)
    if referenced:
        return {
            "owned_delete_failures": 0,
            "ambiguous_instances": 0,
            "candidate_verification_failures": 0,
            "receipt_integrity_failures": 0,
            "referenced_instances": referenced,
            "receipt_evidence_sha256": receipt_digest,
        }
    receipt_owned, ambiguous, verification = _receipt_instance_candidates(
        client, intents, completed, proven_owned_ids=set(known_instance_ids))
    failed = _rollback_instances(client, receipt_owned)
    return {
        "owned_delete_failures": len(failed),
        "ambiguous_instances": ambiguous,
        "candidate_verification_failures": verification,
        "receipt_integrity_failures": 0,
        "referenced_instances": 0,
        "receipt_evidence_sha256": receipt_digest,
    }


def _rollback_pending(result: dict[str, Any]) -> int:
    return sum(int(result.get(field, 0)) for field in (
        "owned_delete_failures", "ambiguous_instances", "receipt_integrity_failures",
        "candidate_verification_failures", "referenced_instances"))


def _referenced_sop_count(session: Session, sop_uids: set[str]) -> int:
    if not sop_uids:
        return 0
    referenced: set[str] = set()
    for study in session.exec(select(HarmonizationCohortStudy)).all():
        for series in study.series_manifest:
            for instance in series.get("instance_manifest", []):
                uid = str(instance.get("sop_instance_uid", ""))
                if uid in sop_uids:
                    referenced.add(uid)
    return len(referenced)


def _safe_worker_error_code(exc: BaseException) -> str:
    """Return a bounded machine code that cannot retain paths, DICOM UIDs, or PHI."""
    kind = type(exc).__name__
    detail = str(exc)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_:-]{0,179}", detail):
        return f"{kind}:{detail}"
    return kind


@contextmanager
def _exclusive_ingestion_lock(*, blocking: bool = True):
    """Serialize Orthanc import/rollback effects across all host worker processes."""
    root = Path(wsettings.harmonization_build_root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".harmonization-ingestion.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    acquired = False
    try:
        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ingest_upload(upload_id: str) -> None:
    with _exclusive_ingestion_lock():
        if _rollback_pending_count():
            raise Retry(defer=60)
        _ingest_upload_locked(upload_id)


def _ingest_upload_locked(upload_id: str) -> None:
    source: Path | None = None
    receipt_path: Path | None = None
    client: requests.Session | None = None
    owned_instance_ids: list[str] = []
    manifest_sha256: str | None = None
    manifest_count: int | None = None
    upload_sha256: str | None = None
    committed = False
    try:
        _require_builder_credentials()
        with Session(engine) as session:
            statement = select(HarmonizationUpload).where(HarmonizationUpload.id == upload_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            row = session.exec(
                statement.execution_options(populate_existing=True)
            ).first()
            if row is None or row.status not in {
                    HarmonizationUploadStatus.staged,
                    HarmonizationUploadStatus.importing,
            }:
                return
            previous_result = dict(row.import_result or {})
            row.status = HarmonizationUploadStatus.importing
            row.import_result = {**previous_result, "phase": "importing"}
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
            session.commit()
            upload_sha256 = row.sha256
            source = Path(wsettings.harmonization_upload_root) / row.storage_key
            if (row.total_size > wsettings.harmonization_max_upload_bytes
                    or not source.is_file() or source.is_symlink()
                    or source.stat().st_size != row.total_size
                    or sha256_file(source) != row.sha256):
                raise ValueError("staged_upload_integrity_failed")
            expanded_bytes = _expanded_upload_bytes(source)
            _require_storage_capacity(
                wsettings.harmonization_build_root,
                incoming_bytes=expanded_bytes,
            )
            Path(wsettings.harmonization_build_root).mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                    dir=wsettings.harmonization_build_root, prefix=f"ingest-{row.id}-") as tmp:
                paths = _dicom_paths(source, Path(tmp))
                if not paths:
                    raise ValueError("upload_contains_no_dicom")
                prepared = [(path, _validate_local_dicom(path)) for path in paths]
                sop_uids = [item[1]["sop_instance_uid"] for item in prepared]
                if len(sop_uids) != len(set(sop_uids)):
                    raise ValueError("upload_contains_duplicate_sop_instance")
                study_subjects: dict[str, str] = {}
                for _path, metadata in prepared:
                    study_uid = metadata["study_instance_uid"]
                    patient_id = metadata["patient_id"]
                    if study_uid in study_subjects and study_subjects[study_uid] != patient_id:
                        raise ValueError("study_contains_inconsistent_pseudonymous_patient_id")
                    study_subjects[study_uid] = patient_id
                manifest = sorted(({
                    key: value for key, value in metadata.items()
                    if key != "transfer_syntax"
                } for _, metadata in prepared), key=lambda value: value["sop_instance_uid"])
                manifest_sha256 = canonical_sha256(manifest)
                manifest_count = len(manifest)
                receipt_path = source.with_name(source.name + ".receipt")
                header, intents, completed = _load_receipt(receipt_path)
                expected_header = upload_receipt.expected_header(
                    upload_sha256=row.sha256,
                    instance_manifest_sha256=manifest_sha256,
                    instance_count=manifest_count,
                )
                if header is None:
                    if previous_result.get("instance_manifest_sha256") is not None:
                        raise RuntimeError("harmonization_upload_receipt_missing")
                    _append_receipt(receipt_path, expected_header)
                    header = expected_header
                else:
                    upload_receipt.validate_header(
                        header,
                        upload_sha256=row.sha256,
                        instance_manifest_sha256=manifest_sha256,
                        instance_count=manifest_count,
                    )
                row.import_result = {
                    "phase": "importing", "instance_count": manifest_count,
                    "instance_manifest_sha256": manifest_sha256,
                    "receipt_evidence_sha256": upload_receipt.evidence_sha256(
                        header, intents, completed),
                }
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()

                client = requests.Session()
                client.trust_env = False
                client.auth = (wsettings.harmonization_orthanc_user,
                               wsettings.harmonization_orthanc_password.get_secret_value())
                for path, metadata in sorted(
                        prepared, key=lambda value: value[1]["sop_instance_uid"]):
                    file_sha256 = metadata["sha256"]
                    completed_record = completed.get(file_sha256)
                    if completed_record:
                        instance_id = completed_record["instance_id"]
                        if not _orthanc_instance_matches(
                                client, instance_id, file_sha256, metadata["size"]):
                            raise RuntimeError("harmonization_receipt_instance_changed")
                        if completed_record["owned"]:
                            owned_instance_ids.append(instance_id)
                        _validate_imported_instance(client, instance_id)
                        continue

                    existing = _find_orthanc_instance(client, metadata["sop_instance_uid"])
                    if len(existing) > 1:
                        raise RuntimeError("harmonization_orthanc_duplicate_instance_index")
                    if file_sha256 not in intents:
                        if existing:
                            raise ValueError("duplicate_sop_instance")
                        _append_receipt(receipt_path, {
                            "event": "intent", "file_sha256": file_sha256,
                            "sop_instance_uid": metadata["sop_instance_uid"],
                            "size": metadata["size"],
                        })
                        intents[file_sha256] = {
                            "sop_instance_uid": metadata["sop_instance_uid"],
                            "size": metadata["size"],
                        }
                    elif existing:
                        instance_id = existing[0]
                        if not _orthanc_instance_matches(
                                client, instance_id, file_sha256, metadata["size"]):
                            raise RuntimeError("harmonization_recovered_instance_changed")
                        _validate_imported_instance(client, instance_id)
                        _append_receipt(receipt_path, {
                            "event": "stored", "file_sha256": file_sha256,
                            "orthanc_instance_id": instance_id, "owned": False,
                        })
                        completed[file_sha256] = {
                            "instance_id": instance_id, "owned": False,
                        }
                        continue

                    with path.open("rb") as handle:
                        response = client.post(
                            wsettings.harmonization_orthanc_rest.rstrip("/") + "/instances",
                            data=handle, headers={"Content-Type": "application/dicom"},
                            timeout=120, allow_redirects=False,
                        )
                    if response.status_code not in {200, 201}:
                        raise RuntimeError(
                            f"harmonization_orthanc_import_{response.status_code}")
                    value = response.json()
                    instance_id = value.get("ID") if isinstance(value, dict) else None
                    parent_study = value.get("ParentStudy") if isinstance(value, dict) else None
                    status = value.get("Status") if isinstance(value, dict) else None
                    if (not isinstance(instance_id, str) or not instance_id
                            or not isinstance(parent_study, str) or not parent_study
                            or status not in {"Success", "Stored", "AlreadyStored"}):
                        raise RuntimeError("harmonization_orthanc_import_response_invalid")
                    owned = status != "AlreadyStored"
                    if owned:
                        owned_instance_ids.append(instance_id)
                    if not _orthanc_instance_matches(
                            client, instance_id, file_sha256, metadata["size"]):
                        raise RuntimeError("harmonization_stored_instance_changed")
                    _validate_imported_instance(client, instance_id)
                    _append_receipt(receipt_path, {
                        "event": "stored", "file_sha256": file_sha256,
                        "orthanc_instance_id": instance_id, "owned": owned,
                    })
                    completed[file_sha256] = {
                        "instance_id": instance_id, "owned": owned,
                    }

            study_uids = sorted({metadata["study_instance_uid"] for _, metadata in prepared})
            row.status, row.last_error = HarmonizationUploadStatus.imported, None
            row.import_result = {
                "phase": "imported", "instance_count": len(paths),
                "study_uids": study_uids, "instance_manifest_sha256": manifest_sha256,
                "studies": [{"study_uid": uid, "subject_key": study_subjects[uid]}
                            for uid in study_uids],
            }
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
            audit.record(session, actor="service:harmonization-builder",
                         action="harmonization.upload.import", entity_type="harmonization_upload",
                         entity_id=row.id, payload={
                             "file_count": len(paths), "study_count": len(set(study_uids)),
                             "sha256": row.sha256,
                         })
            try:
                session.commit()
            except Exception:
                session.rollback()
                raise
            committed = True
    except Exception as exc:
        safe_error = _safe_worker_error_code(exc)
        rollback_result: dict[str, Any] = {
            "owned_delete_failures": 0,
            "ambiguous_instances": 0,
            "candidate_verification_failures": 0,
            "receipt_integrity_failures": 0,
        }
        try:
            with Session(engine) as failure_session:
                if not committed and receipt_path is not None:
                    # Hold the same transaction-scoped fence used by cohort admission until the
                    # external deletion result and its durable failed-upload state commit together.
                    lock_harmonization_orthanc_mutation(failure_session)
                    try:
                        if client is None:
                            client = requests.Session()
                            client.trust_env = False
                            client.auth = (
                                wsettings.harmonization_orthanc_user,
                                wsettings.harmonization_orthanc_password.get_secret_value(),
                            )
                        if upload_sha256 is None:
                            raise RuntimeError("harmonization_upload_receipt_contract_missing")
                        rollback_result = _rollback_upload_receipt(
                            client, receipt_path, owned_instance_ids,
                            upload_sha256=upload_sha256,
                            instance_manifest_sha256=manifest_sha256,
                            instance_count=manifest_count,
                            session=failure_session,
                        )
                    except Exception:
                        rollback_result["receipt_integrity_failures"] = 1
                rollback_failed = _rollback_pending(rollback_result)
                row = failure_session.get(HarmonizationUpload, upload_id)
                if row is not None:
                    row.status = HarmonizationUploadStatus.failed
                    row.last_error = safe_error + (":rollback_pending" if rollback_failed else "")
                    row.import_result = {
                        **dict(row.import_result or {}),
                        "phase": "rollback_incomplete" if rollback_failed else "failed",
                        "rollback_pending_instances": rollback_failed,
                        **rollback_result,
                    }
                    row.updated_at = datetime.now(timezone.utc)
                    failure_session.add(row)
                    audit.record(
                        failure_session, actor="service:harmonization-builder",
                        action="harmonization.upload.fail", entity_type="harmonization_upload",
                        entity_id=row.id, payload={"error_code": row.last_error},
                    )
                    failure_session.commit()
        except Exception:
            # Never let an uploader-controlled original exception survive as chained context in
            # ARQ/journald when the durable failure/audit transaction itself is unavailable.
            raise RuntimeError(
                "harmonization_upload_failure_state_persistence_failed"
            ) from None
        try:
            # Failed uploads are terminal and never retried from their PHI-bearing source. Keep
            # only the small protected receipt when Orthanc rollback needs operator reconciliation.
            if source is not None:
                source.unlink(missing_ok=True)
            if not rollback_failed and receipt_path is not None:
                receipt_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(safe_error) from None
    else:
        # Final SQL/audit state is authoritative. A crash before this best-effort unlink is handled
        # by the terminal-upload staging reaper without touching Orthanc.
        try:
            if source is not None:
                source.unlink(missing_ok=True)
            if receipt_path is not None:
                receipt_path.unlink(missing_ok=True)
        except OSError:
            pass


def _assert_build_waitable(build_id: str) -> None:
    with Session(engine) as session:
        build = session.get(HarmonizationBuild, build_id)
        if build is None or build.status == HarmonizationBuildStatus.cancelled:
            raise BuildCancelled("harmonization_build_cancelled_while_waiting_for_gpu")
        if build.status not in {
                HarmonizationBuildStatus.queued, HarmonizationBuildStatus.building}:
            raise RuntimeError("harmonization_build_no_longer_waitable")


def _cancel_build_from_worker(build_id: str) -> None:
    with Session(engine) as session:
        statement = select(HarmonizationBuild).where(HarmonizationBuild.id == build_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        build = session.exec(statement).first()
        if build is None or build.status not in {
                HarmonizationBuildStatus.queued, HarmonizationBuildStatus.building}:
            return
        if build.status == HarmonizationBuildStatus.building and build.stage == "publishing":
            return
        build.status, build.stage = HarmonizationBuildStatus.cancelled, "cancelled"
        build.error_code = "worker_task_cancelled"
        build.completed_at = datetime.now(timezone.utc)
        build.heartbeat_at = None
        build.lease_expires_at = None
        session.add(build)
        audit.record(
            session, actor="service:harmonization-builder",
            action="harmonization.build.worker_cancel",
            entity_type="harmonization_build", entity_id=build.id,
            payload={"error_code": build.error_code},
        )
        session.commit()


async def run_harmonization_build(ctx: dict, build_id: str) -> None:
    await wait_if_paused(ctx["redis"])

    async def wait_check() -> None:
        await wait_if_paused(ctx["redis"])
        await asyncio.to_thread(_assert_build_waitable, build_id)

    # MELD estimation shares the one physical GPU with routine detector work. The short crash TTL
    # is renewed while owner-fencing prevents this job from deleting a successor's lease.
    async with gpu_lease(
            ctx["redis"], f"harmonization:{build_id}", "builder",
            ttl_s=max(300, wsettings.harmonization_builder_lease_s * 2),
            wait_check=wait_check):
        execution = asyncio.create_task(
            asyncio.to_thread(_execute_build, build_id),
            name=f"harmonization-build-thread:{build_id}",
        )
        try:
            await asyncio.shield(execution)
        except BuildAdmissionDeferred:
            # Keep the durable build queued and let the reconciler/admin resolve the rollback.
            # ARQ retries admission without consuming GPU compute or weakening the deletion fence.
            raise Retry(defer=30) from None
        except asyncio.CancelledError:
            # Do not release the shared GPU fence while the non-cancellable worker thread or its
            # process group is still active. SQL cancellation makes every long stage cooperate.
            try:
                await asyncio.to_thread(_cancel_build_from_worker, build_id)
            except Exception:
                _log.exception("failed to persist harmonization worker cancellation")
            try:
                await asyncio.shield(execution)
            except Exception:
                pass
            raise


async def ingest_harmonization_upload(_ctx: dict, upload_id: str) -> None:
    await asyncio.to_thread(_ingest_upload, upload_id)


def _adapter_ready() -> bool:
    configured = wsettings.harmonization_builder_adapter
    expected = wsettings.harmonization_builder_adapter_sha256
    if not configured or not expected:
        return False
    path = Path(configured)
    try:
        return bool(
            not path.is_symlink() and path.is_file() and os.access(path, os.X_OK)
            and sha256_file(path.resolve(strict=True)) == expected
        )
    except OSError:
        return False


def _cleanup_abandoned_workspaces() -> None:
    with _exclusive_ingestion_lock(blocking=False) as acquired:
        if acquired:
            _cleanup_abandoned_workspaces_locked()


def _cleanup_abandoned_workspaces_locked() -> None:
    root = Path(wsettings.harmonization_build_root)
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - wsettings.harmonization_failed_workspace_retention_hours * 3600
    with Session(engine) as session:
        builds = {row.id: row for row in session.exec(select(HarmonizationBuild)).all()}
    for path in root.iterdir():
        if path.is_symlink():
            path.unlink(missing_ok=True)
            continue
        if not path.is_dir() or path.stat().st_mtime > cutoff:
            continue
        build = builds.get(path.name)
        if build is not None and build.status == HarmonizationBuildStatus.building:
            continue
        if path.name.startswith("ingest-") or build is not None:
            shutil.rmtree(path, ignore_errors=True)


def _reconcile_upload_rollbacks() -> None:
    """Retry deletion receipts left by a crash or temporary Orthanc failure."""
    # Heartbeat publication must not stall behind a large active import. The next five-minute pass
    # retries reconciliation after the mutation lock becomes available.
    with _exclusive_ingestion_lock(blocking=False) as acquired:
        if acquired:
            _reconcile_upload_rollbacks_locked()


def _reconcile_upload_rollbacks_locked() -> None:
    client = requests.Session()
    client.trust_env = False
    client.auth = (wsettings.harmonization_orthanc_user,
                   wsettings.harmonization_orthanc_password.get_secret_value())
    root = Path(wsettings.harmonization_upload_root)
    with Session(engine) as session:
        row_ids = session.exec(select(HarmonizationUpload.id).where(
            HarmonizationUpload.status == HarmonizationUploadStatus.failed
        )).all()
    for row_id in row_ids:
        with Session(engine) as session:
            lock_harmonization_orthanc_mutation(session)
            statement = select(HarmonizationUpload).where(HarmonizationUpload.id == row_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = session.exec(statement).first()
            if row is None:
                continue
            result = dict(row.import_result or {})
            phase = result.get("phase")
            if phase not in {"rollback_incomplete", "rollback_delete_approved"}:
                continue
            force_delete = phase == "rollback_delete_approved"
            receipt = root / f"{row.storage_key}.receipt"
            try:
                header, intents, completed = _load_receipt(receipt)
                upload_receipt.validate_header(
                    header,
                    upload_sha256=row.sha256,
                    instance_manifest_sha256=result.get("instance_manifest_sha256"),
                    instance_count=result.get("instance_count"),
                )
                observed_digest = upload_receipt.evidence_sha256(header, intents, completed)
                if (re.fullmatch(r"[0-9a-f]{64}", str(
                        result.get("receipt_evidence_sha256", ""))) is None
                        or observed_digest != result["receipt_evidence_sha256"]):
                    raise RuntimeError("harmonization_upload_receipt_evidence_mismatch")
            except Exception:
                result["receipt_integrity_failures"] = 1
                result["rollback_pending_instances"] = _rollback_pending(result)
                row.import_result = result
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()
                continue
            result["receipt_integrity_failures"] = 0
            referenced = _referenced_sop_count(
                session, {value["sop_instance_uid"] for value in intents.values()})
            result["referenced_instances"] = referenced
            if referenced:
                result["rollback_pending_instances"] = _rollback_pending(result)
                row.import_result = result
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()
                continue
            owned, ambiguous, verification = _receipt_instance_candidates(
                client, intents, completed, include_ambiguous=force_delete)
            failed = _rollback_instances(client, owned)
            result["owned_delete_failures"] = len(failed)
            result["ambiguous_instances"] = ambiguous
            result["candidate_verification_failures"] = verification
            result["referenced_instances"] = 0
            pending = _rollback_pending(result)
            if pending:
                result["rollback_pending_instances"] = pending
                row.import_result = result
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()
                continue
            result["phase"] = "failed"
            result["rollback_pending_instances"] = 0
            row.import_result = result
            row.last_error = (row.last_error or "upload_failed")[:160] + ":rollback_reconciled"
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
            audit.record(
                session, actor="service:harmonization-builder",
                action="harmonization.upload.rollback_reconcile",
                entity_type="harmonization_upload", entity_id=row.id,
                payload={"deleted_instances": len(owned),
                         "ambiguous_delete_approved": force_delete},
            )
            session.commit()
            try:
                receipt.unlink(missing_ok=True)
            except OSError:
                pass


def _rollback_pending_count() -> int:
    with Session(engine) as session:
        return sum(
            (row.import_result or {}).get("phase") in {
                "rollback_incomplete", "rollback_delete_approved"}
            for row in session.exec(select(HarmonizationUpload).where(
                HarmonizationUpload.status == HarmonizationUploadStatus.failed)).all()
        )


async def _publish_builder_heartbeat(redis, boot_id: str) -> None:
    capacity = await asyncio.to_thread(
        storage_health,
        wsettings.harmonization_build_root,
        minimum_free_bytes=(wsettings.storage_min_free_bytes
                            + wsettings.storage_output_headroom_bytes),
        minimum_free_percent=wsettings.storage_min_free_percent,
    )
    rollback_pending = await asyncio.to_thread(_rollback_pending_count)
    adapter_ready = _adapter_ready()
    value = app_queue.make_worker_heartbeat(
        boot_id=boot_id,
        release_manifest_digest=wsettings.release_manifest_digest,
        git_sha=wsettings.git_sha,
        os_checksum=wsettings.os_checksum,
        images={"meld": wsettings.meld_image},
        capacity={
            "kind": "harmonization-builder",
            "max_jobs": wsettings.harmonization_builder_max_jobs, "max_gpu_jobs": 1,
            "ingest_jobs": 1,
            "storage_ready": bool(capacity["ready"] and rollback_pending == 0),
            "adapter_ready": adapter_ready,
            "adapter_sha256": (wsettings.harmonization_builder_adapter_sha256
                               if adapter_ready else None),
            "rollback_pending": rollback_pending,
        },
    )
    await redis.set(
        settings.harmonization_builder_heartbeat_key, value,
        ex=wsettings.worker_heartbeat_ttl_s,
    )


async def _builder_heartbeat_loop(redis, boot_id: str) -> None:
    last_reconcile = 0.0
    while True:
        try:
            if time.monotonic() - last_reconcile >= 300:
                await asyncio.to_thread(_reconcile_upload_rollbacks)
                await asyncio.to_thread(_cleanup_abandoned_workspaces)
                last_reconcile = time.monotonic()
            await _publish_builder_heartbeat(redis, boot_id)
        except Exception:
            _log.exception("harmonization builder heartbeat publication failed")
        await asyncio.sleep(wsettings.worker_heartbeat_interval_s)


async def _resume_interrupted_ingestions(redis) -> None:
    """Requeue durable importing claims left by a process/host interruption."""
    with Session(engine) as session:
        rows = session.exec(select(HarmonizationUpload).where(
            HarmonizationUpload.status == HarmonizationUploadStatus.importing
        )).all()
    for row in rows:
        token = int(row.updated_at.timestamp())
        await redis.enqueue_job(
            "ingest_harmonization_upload", row.id,
            _job_id=f"harmonization-upload:{row.id}:recovery:{token}",
            _queue_name=wsettings.harmonization_builder_queue,
        )


async def _builder_startup(ctx: dict) -> None:
    await asyncio.to_thread(_reconcile_upload_rollbacks)
    await asyncio.to_thread(_cleanup_abandoned_workspaces)
    await _resume_interrupted_ingestions(ctx["redis"])
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot_id = f"process-{os.getpid()}"
    await _publish_builder_heartbeat(ctx["redis"], boot_id)
    ctx["builder_heartbeat_task"] = asyncio.create_task(
        _builder_heartbeat_loop(ctx["redis"], boot_id),
        name="harmonization-builder-heartbeat",
    )


async def _builder_shutdown(ctx: dict) -> None:
    task = ctx.get("builder_heartbeat_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class BuilderWorkerSettings:
    functions = [run_harmonization_build, ingest_harmonization_upload]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = wsettings.harmonization_builder_queue
    max_jobs = wsettings.harmonization_builder_max_jobs
    job_timeout = wsettings.harmonization_builder_timeout_s
    keep_result = 3600
    health_check_interval = 30
    on_startup = _builder_startup
    on_shutdown = _builder_shutdown
