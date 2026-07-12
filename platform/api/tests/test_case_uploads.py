"""Routine browser DICOM ZIP intake API contracts."""
from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, select

from app import case_upload_routes, models, queue
from app.auth import Principal, Role
from app.config import settings
from app.db import engine
from app.main import app


def setup_module(_module):
    engine.dispose()
    Path("test_meld.db").unlink(missing_ok=True)
    SQLModel.metadata.create_all(engine)


def _zip_bytes() -> bytes:
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w") as archive:
        archive.writestr("nested/study/image.dcm", b"test-placeholder")
    return value.getvalue()


def _submitter() -> Principal:
    return Principal(
        subject="uploader", roles=frozenset({Role.submitter}),
        auth_method="trusted_proxy", request_id="case-upload-test",
    )


def test_case_upload_routes_are_registered():
    paths = set(app.openapi()["paths"])
    assert "/api/case-uploads" in paths
    assert "/api/case-uploads/{upload_id}" in paths
    assert "/api/case-uploads/{upload_id}/complete" in paths


def test_resumable_case_upload_queues_worker_without_creating_case(
        monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "case_upload_root", str(tmp_path))
    monkeypatch.setattr(settings, "storage_min_free_bytes", 1024**3)
    monkeypatch.setattr(settings, "storage_min_free_percent", 1.0)
    content = _zip_bytes()
    digest = hashlib.sha256(content).hexdigest()
    with Session(engine) as session:
        created = case_upload_routes.create_case_upload(
            case_upload_routes.CaseUploadCreate(
                pseudonym="HMRI-001", filename="patient-name-mrn-123.zip",
                total_size=len(content), sha256=digest, content_type="application/zip",
            ), principal=_submitter(), session=session)
        upload_id = created["id"]
        row = session.get(models.CaseUpload, upload_id)
        (tmp_path / row.storage_key).write_bytes(content)
        row.received_size = len(content)
        session.add(row)
        session.commit()
        completed = case_upload_routes.complete_case_upload(
            upload_id, principal=_submitter(), session=session)
        assert completed["status"] == models.CaseUploadStatus.staged
        assert completed["case_id"] is None
        assert completed["series_candidates"] == []
        assert case_upload_routes.complete_case_upload(
            upload_id, principal=_submitter(), session=session)["status"] \
            == models.CaseUploadStatus.staged
        row = session.get(models.CaseUpload, upload_id)
        assert row is not None
        assert row.filename == f"upload-{upload_id}.zip"
        assert "patient-name" not in row.filename
        assert row.status == models.CaseUploadStatus.staged
        assert session.exec(select(models.Case)).first() is None
        event = session.exec(select(models.OutboxEvent).where(
            models.OutboxEvent.dedupe_key == f"case.upload.ingest:{upload_id}"
        )).one()
        assert event.topic == "case.upload.ingest"
        assert event.payload == {"upload_id": upload_id}


def test_wrong_checksum_fails_closed_and_removes_source(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "case_upload_root", str(tmp_path))
    monkeypatch.setattr(settings, "storage_min_free_bytes", 1024**3)
    monkeypatch.setattr(settings, "storage_min_free_percent", 1.0)
    content = _zip_bytes()
    with Session(engine) as session:
        created = case_upload_routes.create_case_upload(
            case_upload_routes.CaseUploadCreate(
                pseudonym="HMRI-002", filename="study.zip", total_size=len(content),
                sha256="0" * 64,
            ), principal=_submitter(), session=session)
        upload_id = created["id"]
        row = session.get(models.CaseUpload, upload_id)
        (tmp_path / row.storage_key).write_bytes(content)
        row.received_size = len(content)
        session.add(row)
        session.commit()
        with pytest.raises(HTTPException) as rejected:
            case_upload_routes.complete_case_upload(
                upload_id, principal=_submitter(), session=session)
        assert rejected.value.status_code == 409
        row = session.get(models.CaseUpload, upload_id)
        assert row.status == models.CaseUploadStatus.failed
        assert row.last_error == "size_or_hash_mismatch"
        assert not (tmp_path / row.storage_key).exists()


def test_abandoned_case_upload_is_expired(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "case_upload_root", str(tmp_path))
    with Session(engine) as session:
        row = models.CaseUpload(
            pseudonym="HMRI-003", filename="upload.zip", total_size=10,
            received_size=3, sha256="a" * 64, created_by="user:test",
            updated_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        session.add(row)
        session.commit()
        (tmp_path / row.storage_key).write_bytes(b"abc")
        result = queue.reap_stale_case_uploads(session)
        assert result["expired"] == 1
        session.refresh(row)
        assert row.status == models.CaseUploadStatus.failed
        assert row.last_error == "upload_session_expired"
        assert not (tmp_path / row.storage_key).exists()


def test_terminal_case_upload_cleanup_rotates_an_undeletable_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "case_upload_root", str(tmp_path))
    blocked_path = tmp_path / "blocked-key"
    blocked_path.mkdir()
    removable_path = tmp_path / "removable-key"
    removable_path.write_bytes(b"archive")
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    with Session(engine) as session:
        blocked = models.CaseUpload(
            pseudonym="P1", filename="upload.zip", storage_key="blocked-key",
            total_size=1, received_size=1, sha256="a" * 64,
            status=models.CaseUploadStatus.failed, created_by="user:one",
            updated_at=old,
        )
        removable = models.CaseUpload(
            pseudonym="P2", filename="upload.zip", storage_key="removable-key",
            total_size=1, received_size=1, sha256="b" * 64,
            status=models.CaseUploadStatus.failed, created_by="user:two",
            updated_at=old + timedelta(minutes=1),
        )
        session.add(blocked)
        session.add(removable)
        session.commit()

        first = queue.reap_stale_case_uploads(session, limit=1)
        session.refresh(blocked)
        assert first["terminal_files_removed"] == 0
        assert blocked.staging_cleaned_at is None
        assert blocked.updated_at > removable.updated_at

        second = queue.reap_stale_case_uploads(session, limit=1)
        session.refresh(removable)
        assert second["terminal_files_removed"] == 1
        assert removable.staging_cleaned_at is not None
        assert not removable_path.exists()
