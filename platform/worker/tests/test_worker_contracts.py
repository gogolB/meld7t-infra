from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from arq import Retry
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, MediaStorageDirectoryStorage
from sqlmodel import Session, SQLModel, create_engine, select

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "platform" / "api"), str(REPO / "platform" / "worker")]

from worker import case_ingest, dicom, ingest, pipeline, tasks  # noqa: E402
from worker.config import WorkerSettings, wsettings  # noqa: E402
from worker.detectors.base import CompletionValidationError, DetectorRunner  # noqa: E402
from worker.detectors.meld import MeldRunner  # noqa: E402
from worker.detectors.map import MapRunner  # noqa: E402
from worker.gpu import gpu_lease, wait_if_paused  # noqa: E402
from worker.harmonization import resolve_harmonization  # noqa: E402
from worker.process import run_process  # noqa: E402
from worker.tasks import completion_bundle_sha256  # noqa: E402
from app import models as app_models  # noqa: E402


def test_worker_capacity_heartbeat_recovers_after_transient_failure(monkeypatch):
    calls = 0

    async def publish(_redis, _boot_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary broker failure")

    async def exercise():
        monkeypatch.setattr(tasks, "_publish_worker_heartbeat", publish)
        monkeypatch.setattr(wsettings, "worker_heartbeat_interval_s", 0.001)
        heartbeat = asyncio.create_task(tasks._worker_heartbeat_loop(object(), "boot"))
        try:
            for _ in range(100):
                if calls >= 2:
                    break
                await asyncio.sleep(0.001)
            assert calls >= 2
        finally:
            heartbeat.cancel()
            with pytest.raises(asyncio.CancelledError):
                await heartbeat

    asyncio.run(exercise())


def _dcm(path: Path, *, study: str, series: str, sop: str, patient: str = "R001") -> FileDataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = series
    ds.PatientID = patient
    ds.PatientName = f"Research^{patient}"
    ds.Modality = "MR"
    ds.Manufacturer = "Research Scanner Co"
    ds.ManufacturerModelName = "Model 7T"
    ds.MagneticFieldStrength = 7.0
    ds.ProtocolName = "test protocol"
    ds.Rows = 2
    ds.Columns = 2
    ds.PixelSpacing = [1.0, 1.0]
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = b"\0" * 8
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(path, enforce_file_format=True)
    return ds


def _dicomdir(path: Path) -> None:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MediaStorageDirectoryStorage
    meta.MediaStorageSOPInstanceUID = "1.2.840.10008.1.3.10.1"
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = MediaStorageDirectoryStorage
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.FileSetID = "HMRI"
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.save_as(path, enforce_file_format=True)


def test_routine_case_ingest_is_registered_with_bounded_worker():
    assert case_ingest.ingest_case_upload in tasks.WorkerSettings.functions


def test_case_zip_accepts_nested_dicomdir_and_proposes_every_series(tmp_path, monkeypatch):
    monkeypatch.setattr(wsettings, "case_upload_max_files", 20)
    monkeypatch.setattr(wsettings, "case_upload_max_expanded_bytes", 10 * 1024 * 1024)
    monkeypatch.setattr(wsettings, "case_upload_max_instance_bytes", 5 * 1024 * 1024)
    source = tmp_path / "source"
    first = _dcm(
        source / "one.dcm", study="1.2.840.7", series="1.2.840.7.1",
        sop="1.2.840.7.1.1")
    first.SeriesDescription = "SAG T1 MP2RAGE UNI"
    first.save_as(source / "one.dcm", enforce_file_format=True)
    second = _dcm(
        source / "two.dcm", study="1.2.840.7", series="1.2.840.7.2",
        sop="1.2.840.7.2.1")
    second.SeriesDescription = "SAG T2 SPACE"
    second.save_as(source / "two.dcm", enforce_file_format=True)
    _dicomdir(source / "DICOMDIR")
    archive_path = tmp_path / "study.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(source / "DICOMDIR", "DICOMDIR")
        archive.write(source / "one.dcm", "SCANS/T1/0001")
        archive.write(source / "two.dcm", "SCANS/T2/0001")

    prepared, summaries, dicomdir_count = case_ingest._validate_archive(
        archive_path, tmp_path / "expanded")
    assert len(prepared) == 2
    assert dicomdir_count == 1
    assert {item["series_uid"] for item in summaries} == {"1.2.840.7.1", "1.2.840.7.2"}
    assert {case_ingest.propose_role(item["description"]).value for item in summaries} == {
        "t1_uni", "t2"}


def test_case_zip_rejects_traversal_junk_and_mixed_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(wsettings, "case_upload_max_files", 20)
    monkeypatch.setattr(wsettings, "case_upload_max_expanded_bytes", 10 * 1024 * 1024)
    monkeypatch.setattr(wsettings, "case_upload_max_instance_bytes", 5 * 1024 * 1024)
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.dcm", b"payload")
    with pytest.raises(ValueError, match="archive_is_unsafe"):
        case_ingest._declared_expanded_size(traversal)

    junk = tmp_path / "junk.zip"
    with zipfile.ZipFile(junk, "w") as archive:
        archive.writestr("README.txt", b"not dicom")
    with pytest.raises(ValueError, match="upload_contains_non_dicom_file"):
        case_ingest._validate_archive(junk, tmp_path / "junk-expanded")

    mixed = tmp_path / "mixed.zip"
    first = tmp_path / "first.dcm"
    second = tmp_path / "second.dcm"
    _dcm(first, study="1.2.10", series="1.2.10.1", sop="1.2.10.1.1", patient="A")
    _dcm(second, study="1.2.10", series="1.2.10.2", sop="1.2.10.2.1", patient="B")
    with zipfile.ZipFile(mixed, "w") as archive:
        archive.write(first, "a/1")
        archive.write(second, "b/1")
    with pytest.raises(ValueError, match="mixed_patient_upload"):
        case_ingest._validate_archive(mixed, tmp_path / "mixed-expanded")

    other_study = tmp_path / "other-study.zip"
    _dcm(second, study="1.2.11", series="1.2.11.1", sop="1.2.11.1.1", patient="A")
    with zipfile.ZipFile(other_study, "w") as archive:
        archive.write(first, "a/1")
        archive.write(second, "b/1")
    with pytest.raises(ValueError, match="mixed_study_upload"):
        case_ingest._validate_archive(other_study, tmp_path / "study-expanded")

    inconsistent = tmp_path / "inconsistent-series.zip"
    _dcm(first, study="1.2.12", series="1.2.12.1", sop="1.2.12.1.1", patient="A")
    changed = _dcm(
        second, study="1.2.12", series="1.2.12.1", sop="1.2.12.1.2", patient="A")
    changed.ProtocolName = "different protocol under reused Series UID"
    changed.save_as(second, enforce_file_format=True)
    with zipfile.ZipFile(inconsistent, "w") as archive:
        archive.write(first, "series/1")
        archive.write(second, "series/2")
    with pytest.raises(ValueError, match="inconsistent_series_metadata"):
        case_ingest._validate_archive(inconsistent, tmp_path / "inconsistent-expanded")


def test_case_upload_worker_creates_pending_case_and_never_queues_detectors(
        tmp_path, monkeypatch):
    isolated = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    SQLModel.metadata.create_all(isolated)
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    source = tmp_path / "source"
    first = _dcm(
        source / "one.dcm", study="1.2.840.70", series="1.2.840.70.1",
        sop="1.2.840.70.1.1", patient="HMRI001")
    first.SeriesDescription = "SAG T1 MP2RAGE UNI"
    first.save_as(source / "one.dcm", enforce_file_format=True)
    second = _dcm(
        source / "two.dcm", study="1.2.840.70", series="1.2.840.70.2",
        sop="1.2.840.70.2.1", patient="HMRI001")
    second.SeriesDescription = "SAG T2 SPACE"
    second.save_as(source / "two.dcm", enforce_file_format=True)
    archive_path = upload_root / "archive-key"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(source / "one.dcm", "nested/t1/1")
        archive.write(source / "two.dcm", "nested/t2/1")
    payload_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with Session(isolated) as session:
        upload = app_models.CaseUpload(
            pseudonym="HMRI-CASE-001", filename="upload.zip", storage_key="archive-key",
            total_size=archive_path.stat().st_size, received_size=archive_path.stat().st_size,
            sha256=payload_sha256, status=app_models.CaseUploadStatus.staged,
            created_by="user:uploader",
        )
        session.add(upload)
        session.commit()
        upload_id = upload.id

    monkeypatch.setattr(case_ingest, "engine", isolated)
    monkeypatch.setattr(wsettings, "case_upload_root", str(upload_root))
    monkeypatch.setattr(wsettings, "case_upload_max_files", 20)
    monkeypatch.setattr(wsettings, "case_upload_max_bytes", 10 * 1024 * 1024)
    monkeypatch.setattr(wsettings, "case_upload_max_expanded_bytes", 10 * 1024 * 1024)
    monkeypatch.setattr(wsettings, "case_upload_max_instance_bytes", 5 * 1024 * 1024)
    monkeypatch.setattr(wsettings, "storage_min_free_bytes", 1)
    monkeypatch.setattr(wsettings, "storage_min_free_percent", 1.0)
    monkeypatch.setattr(case_ingest, "_client", lambda: object())
    monkeypatch.setattr(case_ingest, "_orthanc_rest_root", lambda: "http://orthanc")

    imported = []

    def import_instances(_client, _base, prepared, _receipt, _upload_sha, _manifest_sha):
        imported.extend(item["sop_instance_uid"] for item in prepared)
        return {}

    monkeypatch.setattr(case_ingest, "_import_instances", import_instances)
    monkeypatch.setattr(case_ingest, "_verify_study_closure", lambda *_args: None)
    asyncio.run(case_ingest.ingest_case_upload({}, upload_id))

    with Session(isolated) as session:
        upload = session.get(app_models.CaseUpload, upload_id)
        assert upload.status == app_models.CaseUploadStatus.ready
        assert upload.import_result["phase"] == "ready_for_confirmation"
        case = session.get(app_models.Case, upload.case_id)
        assert case.status == app_models.CaseStatus.series_pending
        assert case.orthanc_study_uid == "1.2.840.70"
        series = session.exec(select(app_models.Series).where(
            app_models.Series.case_id == case.id)).all()
        assert {row.proposed_role.value for row in series} == {"t1_uni", "t2"}
        assert all(row.confirmed_role is None for row in series)
        assert session.exec(select(app_models.Run)).first() is None
    assert sorted(imported) == ["1.2.840.70.1.1", "1.2.840.70.2.1"]
    assert not archive_path.exists()


def test_case_upload_failure_reloads_receipt_before_rollback(tmp_path, monkeypatch):
    isolated = create_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    SQLModel.metadata.create_all(isolated)
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    dicom_path = tmp_path / "one.dcm"
    _dcm(dicom_path, study="1.2.840.80", series="1.2.840.80.1",
         sop="1.2.840.80.1.1", patient="HMRI002")
    archive_path = upload_root / "archive-key"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(dicom_path, "study/one")
    payload_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with Session(isolated) as session:
        upload = app_models.CaseUpload(
            pseudonym="HMRI-ROLLBACK", filename="upload.zip", storage_key="archive-key",
            total_size=archive_path.stat().st_size, received_size=archive_path.stat().st_size,
            sha256=payload_sha256, status=app_models.CaseUploadStatus.staged,
            created_by="user:uploader",
        )
        session.add(upload)
        session.commit()
        upload_id = upload.id

    monkeypatch.setattr(case_ingest, "engine", isolated)
    monkeypatch.setattr(wsettings, "case_upload_root", str(upload_root))
    monkeypatch.setattr(wsettings, "case_upload_max_files", 20)
    monkeypatch.setattr(wsettings, "case_upload_max_bytes", 10 * 1024 * 1024)
    monkeypatch.setattr(wsettings, "case_upload_max_expanded_bytes", 10 * 1024 * 1024)
    monkeypatch.setattr(wsettings, "case_upload_max_instance_bytes", 5 * 1024 * 1024)
    monkeypatch.setattr(wsettings, "storage_min_free_bytes", 1)
    monkeypatch.setattr(wsettings, "storage_min_free_percent", 1.0)
    monkeypatch.setattr(case_ingest, "_client", lambda: object())
    monkeypatch.setattr(case_ingest, "_orthanc_rest_root", lambda: "http://orthanc")

    def fail_after_receipt(_client, _base, prepared, receipt, upload_sha, manifest_sha):
        item = prepared[0]
        case_ingest.upload_receipt.append_receipt(
            receipt, case_ingest.upload_receipt.expected_header(
                upload_sha256=upload_sha,
                instance_manifest_sha256=manifest_sha,
                instance_count=1,
            ))
        case_ingest.upload_receipt.append_receipt(receipt, {
            "event": "intent", "file_sha256": item["sha256"],
            "sop_instance_uid": item["sop_instance_uid"], "size": item["size"],
        })
        case_ingest.upload_receipt.append_receipt(receipt, {
            "event": "stored", "file_sha256": item["sha256"],
            "orthanc_instance_id": "orthanc-owned", "owned": True,
        })
        raise RuntimeError("orthanc_import_failed")

    observed = {}

    def rollback(_client, _base, completed, _prepared):
        observed.update(completed)
        return 0

    monkeypatch.setattr(case_ingest, "_import_instances", fail_after_receipt)
    monkeypatch.setattr(case_ingest, "_rollback_owned", rollback)
    with pytest.raises(RuntimeError, match="orthanc_import_failed"):
        asyncio.run(case_ingest.ingest_case_upload({}, upload_id))
    assert len(observed) == 1
    assert next(iter(observed.values())) == {
        "instance_id": "orthanc-owned", "owned": True}
    with Session(isolated) as session:
        upload = session.get(app_models.CaseUpload, upload_id)
        assert upload.status == app_models.CaseUploadStatus.failed
        assert upload.import_result["rollback_pending_instances"] == 0


def test_case_upload_rejects_preexisting_orthanc_study_and_receipt_fences_retry(
        tmp_path, monkeypatch):
    prepared = [{
        "study_instance_uid": "1.2.840.90",
        "sop_instance_uid": "1.2.840.90.1.1",
        "sha256": "a" * 64,
        "size": 128,
    }]
    receipt = tmp_path / "upload.receipt"
    monkeypatch.setattr(case_ingest, "_find_study", lambda *_args: ["orphan-study"])
    with pytest.raises(ValueError, match="study_instance_already_exists"):
        case_ingest._import_instances(
            object(), "http://orthanc", prepared, receipt, "b" * 64, "c" * 64)
    assert not receipt.exists()

    header = case_ingest.upload_receipt.expected_header(
        upload_sha256="b" * 64,
        instance_manifest_sha256="c" * 64,
        instance_count=1,
        study_instance_uid="1.2.840.90",
    )
    case_ingest.upload_receipt.append_receipt(receipt, header)
    case_ingest.upload_receipt.append_receipt(receipt, {
        "event": "intent", "file_sha256": "a" * 64,
        "sop_instance_uid": "1.2.840.90.1.1", "size": 128,
    })
    case_ingest.upload_receipt.append_receipt(receipt, {
        "event": "stored", "file_sha256": "a" * 64,
        "orthanc_instance_id": "owned-on-retry", "owned": True,
    })
    monkeypatch.setattr(
        case_ingest, "_find_study",
        lambda *_args: pytest.fail("a receipt-fenced retry must not repeat first-import preflight"),
    )
    monkeypatch.setattr(case_ingest, "_instance_matches", lambda *_args: True)
    completed = case_ingest._import_instances(
        object(), "http://orthanc", prepared, receipt, "b" * 64, "c" * 64)
    assert completed == {
        "a" * 64: {"instance_id": "owned-on-retry", "owned": True},
    }


def test_case_upload_requires_exact_post_import_study_closure(monkeypatch):
    class Response:
        def __init__(self, instances):
            self.instances = instances

        def raise_for_status(self):
            return None

        def json(self):
            return self.instances

    class Client:
        def __init__(self, instances):
            self.instances = instances
            self.requested = None

        def get(self, url, **_kwargs):
            self.requested = url
            return Response(self.instances)

    completed = {
        "a" * 64: {"instance_id": "owned-1", "owned": True},
        "b" * 64: {"instance_id": "owned-2", "owned": True},
    }
    monkeypatch.setattr(case_ingest, "_find_study", lambda *_args: ["study-1"])
    client = Client([{"ID": "owned-1"}, {"ID": "owned-2"}])
    case_ingest._verify_study_closure(
        client, "http://orthanc", "1.2.840.90", completed)
    assert client.requested == "http://orthanc/studies/study-1/instances"

    with pytest.raises(ValueError, match="orthanc_study_closure_mismatch"):
        case_ingest._verify_study_closure(
            Client([{"ID": "owned-1"}, "owned-2", {"ID": "external-race"}]),
            "http://orthanc", "1.2.840.90", completed,
        )


def test_case_upload_crash_after_post_keeps_recovered_instance_rollback_owned(
        tmp_path, monkeypatch):
    digest = "a" * 64
    prepared = [{
        "study_instance_uid": "1.2.840.91",
        "sop_instance_uid": "1.2.840.91.1.1",
        "sha256": digest,
        "size": 128,
    }]
    receipt = tmp_path / "upload.receipt"
    case_ingest.upload_receipt.append_receipt(
        receipt,
        case_ingest.upload_receipt.expected_header(
            upload_sha256="b" * 64,
            instance_manifest_sha256="c" * 64,
            instance_count=1,
            study_instance_uid="1.2.840.91",
        ),
    )
    case_ingest.upload_receipt.append_receipt(receipt, {
        "event": "intent", "file_sha256": digest,
        "sop_instance_uid": "1.2.840.91.1.1", "size": 128,
    })
    monkeypatch.setattr(case_ingest, "_find_instance", lambda *_args: ["posted-before-crash"])
    monkeypatch.setattr(case_ingest, "_instance_matches", lambda *_args: True)
    completed = case_ingest._import_instances(
        object(), "http://orthanc", prepared, receipt, "b" * 64, "c" * 64)
    assert completed == {
        digest: {"instance_id": "posted-before-crash", "owned": True},
    }

    class Client:
        def __init__(self):
            self.deleted = []

        def delete(self, url, **_kwargs):
            self.deleted.append(url)
            return SimpleNamespace(status_code=204)

    client = Client()
    assert case_ingest._rollback_owned(
        client, "http://orthanc", completed, prepared) == 0
    assert client.deleted == ["http://orthanc/instances/posted-before-crash"]


def test_local_staging_is_exact_grouped_atomic_and_reusable(tmp_path, monkeypatch):
    imports, staging = tmp_path / "imports", tmp_path / "staging"
    study, t1, t2 = "1.2.3", "1.2.3.1", "1.2.3.2"
    _dcm(imports / "mixed" / "one.dcm", study=study, series=t1, sop="1.2.3.1.1")
    _dcm(imports / "mixed" / "two.dcm", study=study, series=t2, sop="1.2.3.2.1")
    # An unrequested series must never enter the compute snapshot.
    _dcm(imports / "mixed" / "other.dcm", study=study, series="1.2.3.9", sop="1.2.3.9.1")
    monkeypatch.setattr(wsettings, "dicom_import_root", str(imports))
    monkeypatch.setattr(wsettings, "dicom_staging", str(staging))
    request = dicom.AcquisitionRequest(
        "12345678-1234-1234-1234-123456789abc",
        {"t1_mprage": t1, "t2": t2}, study, {t1: 1, t2: 1},
    )
    case = SimpleNamespace(dicom_path=str(imports / "mixed"), orthanc_study_uid=None)

    root, manifest = dicom.dicom_root_for(case, request)
    assert Path(root, ".complete.json").is_file()
    assert manifest["series_by_role"] == request.series_by_role
    assert {p.parent.name for p in Path(root, "series").glob("*/*.dcm")} == {t1, t2}
    assert not any("1.2.3.9" in str(p) for p in Path(root).rglob("*.dcm"))
    assert not list(staging.glob(".*"))

    cached_root, cached = dicom.dicom_root_for(case, request)
    assert cached_root == root
    assert cached == manifest
    Path(root, "series", t1, "unlisted.dcm").write_bytes(b"not part of the manifest")
    with pytest.raises(dicom.DicomStagingError, match="unlisted or missing"):
        dicom.dicom_root_for(case, request)


def test_local_staging_rejects_mixed_patient_and_leaves_no_final(tmp_path, monkeypatch):
    imports, staging = tmp_path / "imports", tmp_path / "staging"
    study, a, b = "2.3.4", "2.3.4.1", "2.3.4.2"
    _dcm(imports / "a.dcm", study=study, series=a, sop="2.3.4.1.1", patient="A")
    _dcm(imports / "b.dcm", study=study, series=b, sop="2.3.4.2.1", patient="B")
    monkeypatch.setattr(wsettings, "dicom_import_root", str(imports))
    monkeypatch.setattr(wsettings, "dicom_staging", str(staging))
    run_id = "22345678-1234-1234-1234-123456789abc"
    request = dicom.AcquisitionRequest(run_id, {"t1_mprage": a, "t2": b}, study)
    with pytest.raises(dicom.DicomStagingError, match="patient"):
        dicom.dicom_root_for(SimpleNamespace(dicom_path=str(imports)), request)
    assert not (staging / run_id).exists()


def test_wado_uses_qido_manifest_and_exact_series(tmp_path):
    study, series = "3.4.5", "3.4.5.1"
    datasets = [
        _dcm(tmp_path / f"{i}.dcm", study=study, series=series, sop=f"3.4.5.1.{i}")
        for i in (1, 2)
    ]

    class Client:
        def __init__(self):
            self.retrieved = []

        def search_for_instances(self, **kwargs):
            assert kwargs["study_instance_uid"] == study
            assert kwargs["series_instance_uid"] == series
            return datasets

        def retrieve_series(self, study_uid, series_uid):
            self.retrieved.append((study_uid, series_uid))
            return datasets

    client = Client()
    request = dicom.AcquisitionRequest(
        "32345678-1234-1234-1234-123456789abc", {"t1_mprage": series}, study)
    manifest = dicom._stage_wado(client, tmp_path / "out", request)
    assert client.retrieved == [(study, series)]
    assert manifest["series"][0]["instance_count"] == 2


def test_full_uuid_subject_and_typed_completion_contract():
    assert pipeline.subject_id("12345678-1234-5678-9abc-def012345678") == (
        "sub-r12345678123456789abcdef012345678")
    first = pipeline.subject_id(
        "12345678-1234-5678-9abc-def012345678", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    second = pipeline.subject_id(
        "12345678-1234-5678-9abc-def012345678", "ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    assert first != second and first.startswith("sub-r12345678123456789abcdef012345678a")
    runner = DetectorRunner()
    valid = {"result": {"n_clusters": 1}, "clusters": [
        {"index": 1, "hemi": "left", "location": "frontal", "size": 1.2,
         "confidence": 3.4, "saliency": {}},
    ]}
    assert runner.validate_completion(valid, {}).result["n_clusters"] == 1
    valid["result"]["n_clusters"] = 0
    with pytest.raises(CompletionValidationError, match="does not match"):
        runner.validate_completion(valid, {})
    valid["result"]["n_clusters"] = 1
    valid["clusters"][0]["saliency"] = {"bad": float("nan")}
    with pytest.raises(CompletionValidationError, match="finite JSON"):
        runner.validate_completion(valid, {})


def test_package_stdout_returns_structured_derived_series_manifest():
    payload = {
        "schema_version": 1,
        "study_uid": "1.2.3",
        "series": [{"series_uid": "1.2.4", "role": "map_candidate_segmentation",
                    "modality": "SEG", "description": "MAP", "sop_count": 1}],
    }
    parsed = pipeline._package_uids(
        ("study_uid=1.2.3\n"
         "derived_series_manifest_json=" + json.dumps(payload, separators=(",", ":")) + "\n"
         "probmap_series_uids_json=[\"1.2.5\",\"1.2.6\"]\n").encode())
    assert parsed["study_uid"] == "1.2.3"
    assert parsed["derived_series_manifest"] == payload
    assert parsed["probmap_series_uids"] == ["1.2.5", "1.2.6"]


def test_harmonization_cli_marks_unharmonized_and_applied_outputs():
    marker = resolve_harmonization({
        "mode": "unharmonized", "reason": "no matching scanner profile"})
    assert pipeline._harmonization_cli(marker) == [
        "--harmonization-status", "unharmonized"]
    applied = SimpleNamespace(
        applied=True, code="HMRI7T", version=2, method="map_normative")
    args = pipeline._harmonization_cli(applied)
    assert args == [
        "--harmonization-status", "applied", "--harmonization-code", "HMRI7T",
        "--harmonization-version", "2", "--harmonization-method", "map_normative",
    ]


def test_generalized_dicom_completion_validates_series_and_per_sop_contract(
        tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    manifest_path = data_root / "work" / "dicom-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    study_uid, reference_uid, seg_uid = "1.2.3", "1.2.4", "1.2.5"
    series = {
        "schema_version": 1, "study_uid": study_uid,
        "harmonization": {"status": "unharmonized", "code": "none", "version": 0,
                          "method": "unharmonized"},
        "series": [
            {"series_uid": reference_uid, "role": "map_native_t1_reference",
             "modality": "MR", "description": "reference", "sop_count": 2},
            {"series_uid": seg_uid, "role": "map_candidate_segmentation",
             "modality": "SEG", "description": "seg", "sop_count": 1},
        ],
    }
    files = [
        {"sop_instance_uid": f"1.2.10.{index}", "series_instance_uid": uid,
         "sha256": f"{index}" * 64, "size": 100 + index}
        for index, uid in ((1, reference_uid), (2, reference_uid), (3, seg_uid))
    ]
    manifest_hash = hashlib.sha256(json.dumps(
        files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    series_hash = hashlib.sha256(json.dumps(
        series, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {
        "schema_version": 1, "study_instance_uid": study_uid,
        "sop_count": len(files), "files": files, "manifest_sha256": manifest_hash,
        "derived_series": series, "derived_series_manifest_sha256": series_hash,
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(wsettings, "meld_data", str(data_root))
    runner = DetectorRunner()
    runner.required_uid_keys = ("study_uid", "t1_series_uid", "seg_series_uid")
    uids = {
        "study_uid": study_uid, "t1_series_uid": reference_uid, "seg_series_uid": seg_uid,
        "dicom_sop_count": str(len(files)), "dicom_manifest_sha256": manifest_hash,
        "derived_series_manifest": series, "derived_series_manifest_sha256": series_hash,
        "dicom_manifest_path": "work/dicom-manifest.json",
    }
    ingested = {"result": {"n_clusters": 0}, "clusters": []}
    completed = runner.validate_completion(ingested, uids)
    assert completed.artifacts == ("work/dicom-manifest.json",)

    manifest["derived_series"]["series"][0]["sop_count"] = 1
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CompletionValidationError, match="contract is inconsistent|hash"):
        runner.validate_completion(ingested, uids)


def test_map_packaging_uses_recipe_for_study_and_run_for_series_uids(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_process(cmd, _log, **_kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = _kwargs
        return SimpleNamespace(returncode=1, stdout=b"")

    monkeypatch.setattr(pipeline, "run_process", fake_run_process)
    monkeypatch.setattr(wsettings, "meld_data", str(tmp_path))
    monkeypatch.setattr(
        wsettings, "orthanc_innet", "http://internal-user:super-secret@orthanc/dicom-web")
    workdir = tmp_path / "work" / "run" / "attempt"
    workdir.mkdir(parents=True)
    asyncio.run(pipeline.run_map_package(
        "sub-test", "P001", str(workdir), "run-uid-seed", "recipe-study-seed", 2))
    cmd = captured["cmd"]
    assert cmd[cmd.index("--uid-seed") + 1] == "run-uid-seed"
    assert cmd[cmd.index("--study-uid-seed") + 1] == "recipe-study-seed"
    assert "super-secret" not in " ".join(cmd)
    assert captured["kwargs"]["env"]["MELD7T_ORTHANC_INNET"].endswith(
        "super-secret@orthanc/dicom-web")


def test_meld_csv_rejects_nonintegral_and_nonfinite_values(tmp_path):
    subject = "sub-rtest"
    reports = tmp_path / "output" / "predictions_reports" / subject / "reports"
    reports.mkdir(parents=True)
    csv_path = reports / f"info_clusters_{subject}.csv"
    csv_path.write_text("cluster,size,hemi,location,confidence,bad saliency\n"
                        "1.5,2.0,left,frontal,3.0,1.0\n")
    with pytest.raises(ValueError, match="cluster index"):
        ingest.parse_clusters(str(tmp_path), subject)
    csv_path.write_text("cluster,size,hemi,location,confidence,bad saliency\n"
                        "1,2.0,left,frontal,3.0,nan\n")
    with pytest.raises(ValueError, match="non-finite"):
        ingest.parse_clusters(str(tmp_path), subject)


def test_completion_bundle_hash_is_reconstructable_after_self_hash_is_stored():
    run_fields = {"run_id": "run", "recipe_id": "recipe", "logical_key": "key",
                  "attempt": 1, "execution_contract": {"release": "signed"}}
    record = {"result": {"n_clusters": 0}, "clusters": [], "uids": {}}
    output = {"files": [], "manifest_sha256": "a" * 64}
    provenance = {"output_hashes": {"record_contract": "b" * 64},
                  "release_manifest_digest": "c" * 64}
    digest = completion_bundle_sha256(
        run_fields=run_fields, record_contract=record,
        output_manifest=output, provenance_contract=provenance)
    output["completion_bundle_sha256"] = digest
    provenance["output_hashes"]["completion_bundle"] = digest
    assert completion_bundle_sha256(
        run_fields=run_fields, record_contract=record,
        output_manifest=output, provenance_contract=provenance) == digest


def test_harmonization_contract_hash_and_containment(tmp_path, monkeypatch):
    root = tmp_path / "harmonization"
    artifact_dir = root / "profile" / "normative" / "map"
    artifact_dir.mkdir(parents=True)
    manifest = []
    for feature in ("junction", "extension"):
        for stat in ("mean", "std"):
            artifact = artifact_dir / f"{feature}_{stat}.nii.gz"
            artifact.write_bytes(f"immutable-{feature}-{stat}".encode())
            manifest.append({
                "path": f"profile/normative/map/{artifact.name}",
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            })
    monkeypatch.setattr(wsettings, "harmonization_root", str(root))
    contract = {
        "profile_id": "p1", "code": "SITE7T-v1", "version": 1, "name": "site",
        "method": "map_normative", "detector_id": "map",
        "selector": {"acquisition": {"model": "test"}},
        "parameters": {
            "data_root": "profile",
            "build_images": {"spm": wsettings.map_image, "pkg": wsettings.pkg_image},
        },
        "artifact_manifest": {"files": manifest},
    }
    contract["profile_document_sha256"] = hashlib.sha256(json.dumps({
        key: contract.get(key) for key in (
            "code", "version", "name", "method", "detector_id", "selector",
            "artifact_manifest", "parameters")
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    resolved = resolve_harmonization(contract)
    assert resolved.code == "SITE7T-v1"
    assert len(resolved.metadata["artifacts"]) == 4
    (artifact_dir / "junction_mean.nii.gz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        resolve_harmonization(contract)


def test_detector_rejects_profile_for_wrong_method():
    profile = SimpleNamespace(applied=True, method="map_normative")
    with pytest.raises(CompletionValidationError, match="does not support"):
        MeldRunner().validate_harmonization(profile)
    marker = resolve_harmonization({
        "mode": "not_applicable", "reason": "no validated transform for this detector"})
    DetectorRunner().validate_harmonization(marker)


def test_server_worker_rejects_missing_or_false_not_applicable_harmonization(monkeypatch):
    monkeypatch.setattr(wsettings, "deployment_mode", "production")
    with pytest.raises(CompletionValidationError, match="explicit harmonization contract"):
        MeldRunner().validate_harmonization(None)
    marker = resolve_harmonization({
        "mode": "not_applicable", "reason": "incorrect bypass for supported detector"})
    with pytest.raises(CompletionValidationError, match="cannot mark supported"):
        MeldRunner().validate_harmonization(marker)


def test_server_harmonization_requires_assignment_provenance(tmp_path, monkeypatch):
    root = tmp_path / "harmonization"
    data_root = root / "profile"
    data_root.mkdir(parents=True)
    artifact = data_root / "MELD_HSITEcombat_parameters.hdf5"
    artifact.write_bytes(b"immutable-combat-parameters")
    contract = {
        "profile_id": "d7dd2090-4df1-4f42-a7ec-9f5cf75720cb",
        "assignment_id": "2e943f9a-6e0f-4ea1-a712-97d793a66842",
        "acquisition_fingerprint": "a" * 64,
        "selector_override": False,
        "override_reason_present": False,
        "code": "HSITE", "version": 1, "name": "site", "method": "meld_distributed_combat",
        "detector_id": "meld_fcd", "selector": {"acquisition": {"model": "test"}},
        "parameters": {
            "data_root": "profile",
            "build_images": {"meld": wsettings.meld_image},
        },
        "artifact_manifest": {"files": [{
            "path": "profile/MELD_HSITEcombat_parameters.hdf5",
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }]},
    }
    contract["profile_document_sha256"] = hashlib.sha256(json.dumps({
        key: contract.get(key) for key in (
            "code", "version", "name", "method", "detector_id", "selector",
            "artifact_manifest", "parameters")
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    monkeypatch.setattr(wsettings, "deployment_mode", "production")
    monkeypatch.setattr(wsettings, "harmonization_root", str(root))
    assert resolve_harmonization(contract).metadata["assignment_id"] == contract["assignment_id"]
    contract["parameters"]["build_images"]["meld"] = "different/image@sha256:" + "f" * 64
    contract["profile_document_sha256"] = hashlib.sha256(json.dumps({
        key: contract.get(key) for key in (
            "code", "version", "name", "method", "detector_id", "selector",
            "artifact_manifest", "parameters")
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ValueError, match="build images differ"):
        resolve_harmonization(contract)
    contract["parameters"]["build_images"]["meld"] = wsettings.meld_image
    contract["profile_document_sha256"] = hashlib.sha256(json.dumps({
        key: contract.get(key) for key in (
            "code", "version", "name", "method", "detector_id", "selector",
            "artifact_manifest", "parameters")
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    contract.pop("assignment_id")
    with pytest.raises(ValueError, match="profile and assignment IDs"):
        resolve_harmonization(contract)


def test_meld_harmonization_requires_observed_exact_invocation(tmp_path):
    profile = SimpleNamespace(
        code="HSITE", host_data_root="/signed/profile", applied=True)
    log = tmp_path / "meld.log"
    log.write_text(
        "$ podman run -v /signed/profile:/data/meld_params/distributed_combat:ro,z "
        "image python pipeline.py -harmo_code HSITE\n")
    MeldRunner._verify_harmonization_invocation(str(log), profile)
    log.write_text(
        "$ podman run -v /signed/other:/data/meld_params/distributed_combat:ro,z "
        "image python pipeline.py -harmo_code HSITE\n")
    with pytest.raises(CompletionValidationError, match="does not match"):
        MeldRunner._verify_harmonization_invocation(str(log), profile)


def test_gpu_release_is_one_atomic_eval():
    class Redis:
        def __init__(self):
            self.calls = []

        async def set(self, *args, **kwargs):
            return True

        async def eval(self, *args):
            self.calls.append(args)
            return 1

    redis = Redis()

    async def exercise():
        async with gpu_lease(redis, "run-1", "claim-1"):
            pass

    asyncio.run(exercise())
    assert len(redis.calls) == 1
    assert redis.calls[0][1:] == (1, wsettings.gpu_lock_key, "run-1:claim-1")


def test_pause_defers_without_consuming_detector_timeout():
    class Redis:
        async def get(self, _key):
            return "1"

    with pytest.raises(Retry):
        asyncio.run(wait_if_paused(Redis()))


def test_server_worker_rejects_mutable_images_and_incomplete_provenance():
    common = {
        "deployment_mode": "research",
        "release_manifest_digest": "a" * 64,
        "git_sha": "b" * 40,
        "os_checksum": "c" * 64,
        "map_script_sha256": "e" * 64,
        "hippunfold_cache_sha256": "f" * 64,
    }
    with pytest.raises(ValueError, match="manifest digest"):
        WorkerSettings(_env_file=None, **common)
    immutable = "localhost/example@sha256:" + "d" * 64
    settings = WorkerSettings(
        _env_file=None, **common,
        pkg_image=immutable, meld_image=immutable,
        hippunfold_image=immutable, map_image=immutable,
    )
    assert settings.release_manifest_digest == "a" * 64
    with pytest.raises(ValueError, match="configured together"):
        WorkerSettings(
            _env_file=None, **common,
            pkg_image=immutable, meld_image=immutable,
            hippunfold_image=immutable, map_image=immutable,
            harmonization_builder_adapter="/opt/meld7t/bin/adapter",
        )


def test_map_rejects_runtime_script_drift(tmp_path, monkeypatch):
    script = tmp_path / "containers" / "map" / "segment.m"
    script.parent.mkdir(parents=True)
    script.write_text("disp('changed')")
    monkeypatch.setattr(wsettings, "repo_dir", str(tmp_path))
    monkeypatch.setattr(wsettings, "map_script_sha256", "0" * 64)
    with pytest.raises(CompletionValidationError, match="differs from signed release"):
        asyncio.run(MapRunner().compute("sub-test", str(tmp_path / "work")))


def test_subprocess_timeout_terminates_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(wsettings, "subprocess_stop_grace_s", 1)
    async def exercise():
        with pytest.raises(TimeoutError):
            await run_process(
                ["bash", "-c", "sleep 30"], str(tmp_path / "timeout.log"), timeout_s=0.05)

    asyncio.run(exercise())
    assert "bash" in (tmp_path / "timeout.log").read_text()
