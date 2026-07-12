"""Resumable browser intake for one routine research DICOM study.

This is a separate resource from harmonization cohort uploads: it targets the main Orthanc and
creates a normal ``Case``.  Completion only queues validation/import.  The worker proposes roles
for every discovered series but deliberately leaves confirmation and recipe submission to a human.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field as PydanticField, field_validator
from sqlalchemy import func, text
from sqlmodel import Session, select

from . import audit
from .auth import Principal, Role, get_principal, require_submitter
from .config import settings
from .db import get_session
from .harmonization import sha256_file
from .models import Case, CaseUpload, CaseUploadStatus, CaseStatus, OutboxEvent, Series
from .storage import storage_health


router = APIRouter(prefix="/api/case-uploads", tags=["case-uploads"])


class CaseUploadCreate(BaseModel):
    pseudonym: str = PydanticField(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    filename: str = PydanticField(min_length=1, max_length=255)
    total_size: int = PydanticField(gt=0)
    sha256: str = PydanticField(pattern=r"^[0-9a-fA-F]{64}$")
    content_type: str = PydanticField(default="application/zip", max_length=128)

    @field_validator("filename")
    @classmethod
    def safe_zip_basename(cls, value: str) -> str:
        if (Path(value).name != value or value in {".", ".."}
                or any(char in value for char in "\r\n\0")):
            raise ValueError("filename must be a safe basename")
        if not value.lower().endswith(".zip"):
            raise ValueError("routine browser intake requires a DICOM ZIP archive")
        return value


def _authorize_upload(principal: Principal, row: CaseUpload, *, mutate: bool = False) -> None:
    if principal.has_role(Role.admin) or row.created_by == principal.actor:
        return
    if not mutate and principal.has_role(Role.reviewer):
        return
    raise HTTPException(403, "case upload access denied")


def _upload_path(row: CaseUpload) -> Path:
    root = Path(settings.case_upload_root).resolve()
    candidate = root / row.storage_key
    if candidate.parent != root:
        raise RuntimeError("invalid_case_upload_storage_key")
    return candidate


def _series_candidates(session: Session, row: CaseUpload) -> list[dict]:
    if row.case_id is None:
        return []
    rows = session.exec(select(Series).where(
        Series.case_id == row.case_id, Series.active.is_(True)
    ).order_by(Series.series_description, Series.orthanc_series_uid)).all()
    return [{
        "id": series.id,
        "orthanc_series_uid": series.orthanc_series_uid,
        "series_description": series.series_description,
        "modality": series.modality,
        "proposed_role": series.proposed_role,
        "confirmed_role": series.confirmed_role,
        "fingerprint": series.fingerprint,
        "instance_count": series.instance_count,
    } for series in rows]


def _public_upload(session: Session, row: CaseUpload) -> dict:
    candidates = _series_candidates(session, row)
    case = session.get(Case, row.case_id) if row.case_id else None
    if row.status == CaseUploadStatus.ready and case is not None:
        confirmation_status = (
            "confirmed" if case.status != CaseStatus.series_pending
            else "awaiting_series_confirmation"
        )
    else:
        confirmation_status = None
    return {
        "id": row.id,
        "case_id": row.case_id,
        "pseudonym": row.pseudonym,
        "status": row.status,
        "received_size": row.received_size,
        "total_size": row.total_size,
        "max_chunk_size": settings.case_upload_chunk_bytes,
        "import_result": row.import_result,
        "last_error": row.last_error,
        "series_candidates": candidates,
        "proposed_roles": {
            item["orthanc_series_uid"]: item["proposed_role"] for item in candidates
        },
        "confirmation_status": confirmation_status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.post("", status_code=201)
def create_case_upload(
        body: CaseUploadCreate,
        principal: Principal = Depends(require_submitter),
        session: Session = Depends(get_session)) -> dict:
    if body.total_size > settings.case_upload_max_bytes:
        raise HTTPException(413, "upload exceeds configured routine case limit")
    # Serialize reservations so simultaneous API workers cannot admit the same free capacity.
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(5577497352164671721)"))
    live = (
        CaseUploadStatus.receiving,
        CaseUploadStatus.staged,
        CaseUploadStatus.importing,
    )
    used = int(session.exec(select(func.coalesce(func.sum(CaseUpload.total_size), 0)).where(
        CaseUpload.status.in_(live))).one())
    if used + body.total_size > settings.case_upload_quota_bytes:
        raise HTTPException(413, "routine case upload storage quota would be exceeded")

    root = Path(settings.case_upload_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    reserved_remaining = int(session.exec(select(func.coalesce(func.sum(
        CaseUpload.total_size - CaseUpload.received_size), 0)).where(
            CaseUpload.status == CaseUploadStatus.receiving)).one())
    capacity = storage_health(
        str(root),
        minimum_free_bytes=(settings.storage_min_free_bytes
                            + reserved_remaining + body.total_size),
        minimum_free_percent=settings.storage_min_free_percent,
    )
    if not capacity["ready"]:
        raise HTTPException(507, "routine case upload storage is below its admission watermark")

    row = CaseUpload(
        pseudonym=body.pseudonym,
        filename="pending.zip",
        content_type="application/zip",
        total_size=body.total_size,
        sha256=body.sha256.lower(),
        created_by=principal.actor,
    )
    # Workstation filenames frequently carry names/MRNs; retain only an opaque server name.
    row.filename = f"upload-{row.id}.zip"
    path = root.resolve() / row.storage_key
    path.touch(mode=0o600, exist_ok=False)
    try:
        session.add(row)
        audit.record_authenticated(
            session, principal=principal, action="case_upload.create",
            entity_type="case_upload", entity_id=row.id,
            payload={"total_size": row.total_size, "sha256": row.sha256},
        )
        session.commit()
    except Exception:
        session.rollback()
        path.unlink(missing_ok=True)
        raise
    return _public_upload(session, row)


@router.put("/{upload_id}")
async def upload_case_chunk(
        upload_id: str, request: Request, offset: int = Query(ge=0),
        principal: Principal = Depends(require_submitter),
        session: Session = Depends(get_session)) -> dict:
    statement = select(CaseUpload).where(CaseUpload.id == upload_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = session.exec(statement).first()
    if row is None:
        raise HTTPException(404, "case upload not found")
    _authorize_upload(principal, row, mutate=True)
    if row.status != CaseUploadStatus.receiving or offset != row.received_size:
        raise HTTPException(409, "upload offset/status mismatch")
    path = _upload_path(row)
    if not path.is_file() or path.is_symlink():
        raise HTTPException(409, "upload staging file does not match durable offset")
    durable_size = path.stat().st_size
    if durable_size < offset:
        raise HTTPException(409, "upload staging file is shorter than its durable offset")
    # An fsync can survive a crash before its SQL offset.  Discard only that uncommitted suffix.
    if durable_size > offset:
        with path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            os.fsync(handle.fileno())

    size = 0
    with path.open("r+b") as handle:
        handle.seek(offset)
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if (size > settings.case_upload_chunk_bytes
                        or row.received_size + size > row.total_size):
                    raise HTTPException(413, "chunk exceeds configured upload limits")
                handle.write(chunk)
            if size == 0:
                raise HTTPException(422, "upload chunk cannot be empty")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.truncate(offset)
            handle.flush()
            os.fsync(handle.fileno())
            raise
    row.received_size += size
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        with path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            os.fsync(handle.fileno())
        raise
    return _public_upload(session, row)


@router.post("/{upload_id}/complete")
def complete_case_upload(
        upload_id: str, principal: Principal = Depends(require_submitter),
        session: Session = Depends(get_session)) -> dict:
    statement = select(CaseUpload).where(CaseUpload.id == upload_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = session.exec(statement).first()
    if row is None:
        raise HTTPException(404, "case upload not found")
    _authorize_upload(principal, row, mutate=True)
    if row.status in {
            CaseUploadStatus.staged, CaseUploadStatus.importing, CaseUploadStatus.ready}:
        return _public_upload(session, row)
    if row.status != CaseUploadStatus.receiving:
        raise HTTPException(409, "failed upload sessions cannot be completed")

    path = _upload_path(row)
    regular = path.is_file() and not path.is_symlink()
    staged_size = path.stat().st_size if regular else -1
    actual = sha256_file(path) if regular else ""
    if row.received_size != row.total_size or staged_size != row.total_size or actual != row.sha256:
        row.status = CaseUploadStatus.failed
        row.last_error = "size_or_hash_mismatch"
        row.updated_at = datetime.now(timezone.utc)
        try:
            if regular:
                path.unlink()
                row.staging_cleaned_at = row.updated_at
        except OSError:
            pass
        session.add(row)
        audit.record_authenticated(
            session, principal=principal, action="case_upload.reject",
            entity_type="case_upload", entity_id=row.id,
            payload={"reason": row.last_error, "received_size": row.received_size},
        )
        session.commit()
        raise HTTPException(409, "completed upload size or checksum differs")

    now = datetime.now(timezone.utc)
    row.status = CaseUploadStatus.staged
    row.completed_at = now
    row.updated_at = now
    session.add(row)
    session.add(OutboxEvent(
        dedupe_key=f"case.upload.ingest:{row.id}",
        topic="case.upload.ingest",
        aggregate_type="case_upload",
        aggregate_id=row.id,
        payload={"upload_id": row.id},
    ))
    audit.record_authenticated(
        session, principal=principal, action="case_upload.complete",
        entity_type="case_upload", entity_id=row.id,
        payload={"size": row.total_size, "sha256": row.sha256},
    )
    session.commit()
    return _public_upload(session, row)


@router.get("/{upload_id}")
def get_case_upload(
        upload_id: str, principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session)) -> dict:
    row = session.get(CaseUpload, upload_id)
    if row is None:
        raise HTTPException(404, "case upload not found")
    _authorize_upload(principal, row)
    audit.record_access(
        session, principal=principal, entity_type="case_upload", entity_id=row.id,
        detail={"status": row.status.value},
    )
    session.commit()
    return _public_upload(session, row)


@router.get("")
def list_case_uploads(
        limit: int = Query(default=50, ge=1, le=200),
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session)) -> list[dict]:
    statement = select(CaseUpload).order_by(CaseUpload.created_at.desc()).limit(limit)
    if not (principal.has_role(Role.admin) or principal.has_role(Role.reviewer)):
        statement = statement.where(CaseUpload.created_by == principal.actor)
    rows = session.exec(statement).all()
    audit.record_access_coalesced(
        session, principal=principal, entity_type="case_upload_collection",
        entity_id="list", detail={"count": len(rows)},
    )
    session.commit()
    return [_public_upload(session, row) for row in rows]
