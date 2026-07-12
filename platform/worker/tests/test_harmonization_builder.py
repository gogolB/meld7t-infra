"""Fail-closed contracts for the dedicated harmonization builder worker."""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from pydantic import SecretStr
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid
from sqlmodel import Session, SQLModel, create_engine

from app.models import (
    HarmonizationBuild,
    HarmonizationCohort,
    HarmonizationCohortStudy,
    HarmonizationUpload,
)
from worker.config import wsettings
from worker.harmonization_builder import (
    BuildAdmissionDeferred,
    _adapter_ready, _append_receipt, _criteria_pass, _dicom_paths, _ensure_published,
    _expanded_upload_bytes, _load_receipt, _validate_imported_instance,
    _publish_candidate, _receipt_instance_candidates, _receipt_owned_instances,
    _rollback_upload_receipt, _safe_worker_error_code, _validate_local_dicom,
    _assert_no_pending_orthanc_rollback, _require_builder_credentials,
)


ADAPTER_SHA256 = "7" * 64


def test_dedicated_builder_still_requires_its_orthanc_credential(monkeypatch):
    monkeypatch.setattr(wsettings, "deployment_mode", "production")
    monkeypatch.setattr(
        wsettings, "harmonization_orthanc_password", SecretStr("change-me"))
    with pytest.raises(RuntimeError, match="credential_invalid"):
        _require_builder_credentials()
    monkeypatch.setattr(
        wsettings, "harmonization_orthanc_password", SecretStr("h" * 40))
    _require_builder_credentials()


def test_adapter_readiness_requires_exact_regular_executable(tmp_path, monkeypatch):
    adapter = tmp_path / "adapter"
    adapter.write_bytes(b"#!/bin/sh\nexit 0\n")
    adapter.chmod(0o700)
    monkeypatch.setattr(wsettings, "harmonization_builder_adapter", str(adapter))
    monkeypatch.setattr(
        wsettings, "harmonization_builder_adapter_sha256",
        hashlib.sha256(adapter.read_bytes()).hexdigest(),
    )
    assert _adapter_ready() is True
    monkeypatch.setattr(wsettings, "harmonization_builder_adapter_sha256", "0" * 64)
    assert _adapter_ready() is False
    link = tmp_path / "adapter-link"
    link.symlink_to(adapter)
    monkeypatch.setattr(wsettings, "harmonization_builder_adapter", str(link))
    assert _adapter_ready() is False


def test_metric_acceptance_requires_every_versioned_bound():
    criteria = {"required_metrics": {
        "residual_site_effect": {"max": 0.1},
        "stability": {"min": 0.9, "max": 1.0},
    }}
    assert _criteria_pass({"residual_site_effect": 0.05, "stability": 0.95}, criteria)
    assert not _criteria_pass({"residual_site_effect": 0.2, "stability": 0.95}, criteria)
    assert not _criteria_pass({"residual_site_effect": 0.05}, criteria)
    assert not _criteria_pass({"residual_site_effect": False, "stability": 0.95}, criteria)
    assert not _criteria_pass({"residual_site_effect": 0.05}, {})
    final = {**criteria, "final_required_metrics": {"fit_stability": {"min": 0.8}}}
    assert _criteria_pass({"fit_stability": 0.9}, final, field="final_required_metrics")
    assert not _criteria_pass({"stability": 0.9}, final, field="final_required_metrics")


def test_build_claim_is_fenced_by_durable_pending_rollback():
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    with Session(isolated) as session:
        cohort = HarmonizationCohort(
            id="rollback-cohort", name="rollback", site_code="SITE",
            profile_code="HROLLBACK", profile_version=1, source_role="t1_uni",
            selector={"roles": ["t1_uni"]}, created_by="user:admin",
        )
        session.add(cohort)
        session.add(HarmonizationUpload(
            cohort_id=cohort.id, filename="control.dcm", total_size=1,
            received_size=1, sha256="a" * 64, created_by="user:admin",
            status="failed", import_result={"phase": "rollback_incomplete"},
        ))
        session.commit()
        with pytest.raises(BuildAdmissionDeferred, match="rollback_pending"):
            _assert_no_pending_orthanc_rollback(session)


def test_upload_archive_rejects_traversal_and_extracts_regular_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(wsettings, "harmonization_upload_max_files", 10)
    monkeypatch.setattr(wsettings, "harmonization_upload_max_expanded_bytes", 1024 * 1024)
    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.dcm", b"x")
    with pytest.raises(ValueError, match="unsafe_archive_entry"):
        _dicom_paths(unsafe, tmp_path / "unsafe-output")

    safe = tmp_path / "safe.zip"
    payload = b"\0" * 128 + b"DICM" + b"payload"
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("site/control-01.dcm", payload)
    output = tmp_path / "safe-output"
    output.mkdir()
    paths = _dicom_paths(safe, output)
    assert [path.relative_to(output).as_posix() for path in paths] == ["site/control-01.dcm"]
    assert paths[0].read_bytes() == payload
    assert _expanded_upload_bytes(safe) == len(payload)


def test_local_dicom_requires_confidentiality_attestations(tmp_path, monkeypatch):
    file_meta = FileMetaDataset()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.MediaStorageSOPClassUID = MRImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    path = tmp_path / "deidentified.dcm"
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = MRImageStorage
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.PatientID = "CONTROL-001"
    dataset.PatientIdentityRemoved = "YES"
    dataset.DeidentificationMethod = "Site DICOM confidentiality profile v1"
    dataset.BurnedInAnnotation = "NO"
    dataset.Modality = "MR"
    dataset.Manufacturer = "Siemens Healthineers"
    dataset.ManufacturerModelName = "Magnetom Terra"
    dataset.Rows = 320
    dataset.Columns = 320
    dataset.PixelSpacing = [0.7, 0.7]
    dataset.save_as(path, enforce_file_format=True)
    contract = _validate_local_dicom(path)
    assert contract["sop_instance_uid"] == dataset.SOPInstanceUID
    assert contract["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert contract["modality"] == "MR"
    assert contract["acquisition"]["model"] == "Magnetom Terra"
    assert contract["acquisition"]["voxel_spacing_mm"] == [0.7, 0.7]

    dataset.PatientName = "Identified^Person"
    dataset.save_as(path, enforce_file_format=True)
    with pytest.raises(ValueError, match="identifier"):
        _validate_local_dicom(path)

    del dataset.PatientName
    dataset.add_new(0x00190010, "LO", "PRIVATE")
    dataset.save_as(path, enforce_file_format=True)
    monkeypatch.setattr(wsettings, "harmonization_allowed_private_tags", [])
    with pytest.raises(ValueError, match="private_tag"):
        _validate_local_dicom(path)


def test_upload_receipt_is_append_only_and_recoverable(tmp_path):
    receipt = tmp_path / "upload.receipt"
    header = {"event": "header", "schema_version": 1, "upload_sha256": "a" * 64,
              "instance_manifest_sha256": "b" * 64, "instance_count": 1}
    _append_receipt(receipt, header)
    _append_receipt(receipt, {"event": "intent", "file_sha256": "c" * 64,
                              "sop_instance_uid": "1.2.3.4", "size": 512})
    _append_receipt(receipt, {"event": "stored", "file_sha256": "c" * 64,
                              "orthanc_instance_id": "orthanc-id", "owned": True})
    loaded, intents, completed = _load_receipt(receipt)
    assert loaded == header
    assert intents == {"c" * 64: {"sop_instance_uid": "1.2.3.4", "size": 512}}
    assert completed == {
        "c" * 64: {"instance_id": "orthanc-id", "owned": True},
    }


def test_upload_receipt_discards_only_torn_final_record(tmp_path):
    receipt = tmp_path / "torn.receipt"
    header = {"event": "header", "schema_version": 1, "upload_sha256": "a" * 64,
              "instance_manifest_sha256": "b" * 64, "instance_count": 1}
    _append_receipt(receipt, header)
    with receipt.open("ab") as handle:
        handle.write(b'{"event":"stored","file_sha256":"')
    loaded, intents, completed = _load_receipt(receipt)
    assert loaded == header
    assert intents == {}
    assert completed == {}
    assert receipt.read_bytes().endswith(b"\n")


def test_intent_only_receipt_preserves_ambiguous_orthanc_object(tmp_path, monkeypatch):
    payload = b"exact-dicom-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    receipt = tmp_path / "lost-response.receipt"
    _append_receipt(receipt, {"event": "header", "schema_version": 1,
                              "upload_sha256": "a" * 64,
                              "instance_manifest_sha256": "b" * 64,
                              "instance_count": 1})
    _append_receipt(receipt, {"event": "intent", "file_sha256": digest,
                              "sop_instance_uid": "1.2.840.1", "size": len(payload)})

    class Response:
        status_code = 200

        def __init__(self, value=None):
            self.value = value

        def json(self):
            return self.value

        def iter_content(self, chunk_size):
            del chunk_size
            yield payload

        def close(self):
            pass

    class Client:
        def post(self, *_args, **_kwargs):
            return Response(["orthanc-instance"])

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(wsettings, "harmonization_orthanc_rest", "http://orthanc")
    owned, unresolved = _receipt_owned_instances(Client(), receipt)
    assert owned == []
    assert unresolved == 1
    approved, unresolved_after_approval = _receipt_owned_instances(
        Client(), receipt, include_ambiguous=True)
    assert approved == ["orthanc-instance"]
    assert unresolved_after_approval == 0
    _header, intents, completed = _load_receipt(receipt)
    live_owned, live_ambiguous, live_verification = _receipt_instance_candidates(
        Client(), intents, completed, proven_owned_ids={"orthanc-instance"})
    assert live_owned == ["orthanc-instance"]
    assert live_ambiguous == 0
    assert live_verification == 0


def test_exact_delete_never_selects_explicitly_preexisting_object(monkeypatch):
    external = b"preexisting"
    lost = b"response-lost"
    external_digest = hashlib.sha256(external).hexdigest()
    lost_digest = hashlib.sha256(lost).hexdigest()
    intents = {
        external_digest: {"sop_instance_uid": "1.2.840.1", "size": len(external)},
        lost_digest: {"sop_instance_uid": "1.2.840.2", "size": len(lost)},
    }
    completed = {
        external_digest: {"instance_id": "external-instance", "owned": False},
    }

    class Response:
        status_code = 200

        def __init__(self, value=None, payload=b""):
            self.value, self.payload = value, payload

        def json(self):
            return self.value

        def iter_content(self, chunk_size):
            del chunk_size
            yield self.payload

        def close(self):
            pass

    class Client:
        def post(self, *_args, **_kwargs):
            return Response(["lost-instance"])

        def get(self, url, **_kwargs):
            assert "external-instance" not in url
            return Response(payload=lost)

    monkeypatch.setattr(wsettings, "harmonization_orthanc_rest", "http://orthanc")
    selected, ambiguous, verification = _receipt_instance_candidates(
        Client(), intents, completed, include_ambiguous=True)
    assert selected == ["lost-instance"]
    assert ambiguous == 0
    assert verification == 0


def test_receipt_contract_fails_closed_and_referenced_objects_are_not_deleted(
        tmp_path, monkeypatch):
    receipt = tmp_path / "protected.receipt"
    payload = b"exact"
    digest = hashlib.sha256(payload).hexdigest()
    header = {"event": "header", "schema_version": 1, "upload_sha256": "a" * 64,
              "instance_manifest_sha256": "b" * 64, "instance_count": 1}
    _append_receipt(receipt, header)
    _append_receipt(receipt, {"event": "intent", "file_sha256": digest,
                              "sop_instance_uid": "1.2.840.7", "size": len(payload)})
    _append_receipt(receipt, {"event": "stored", "file_sha256": digest,
                              "orthanc_instance_id": "owned-instance", "owned": True})

    class Client:
        def __init__(self):
            self.deleted = []

        def delete(self, url, **_kwargs):
            self.deleted.append(url)
            raise AssertionError("referenced object must not be deleted")

    monkeypatch.setattr(wsettings, "harmonization_orthanc_rest", "http://orthanc")
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    cohort = HarmonizationCohort(
        id="receipt-cohort", name="site", site_code="SITE", profile_code="HRECEIPT",
        profile_version=1, source_role="t1_uni",
        selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
        created_by="user:builder",
    )
    study = HarmonizationCohortStudy(
        cohort_id=cohort.id, orthanc_study_uid="1.2.840", subject_key_hmac="c" * 64,
        acquisition_fingerprint="d" * 64, acquisition={"model": "terra"},
        series_manifest=[{"instance_manifest": [{"sop_instance_uid": "1.2.840.7"}]}],
        study_sha256="e" * 64,
    )
    client = Client()
    with Session(isolated) as session:
        session.add(cohort)
        session.add(study)
        session.commit()
        result = _rollback_upload_receipt(
            client, receipt, [], upload_sha256="a" * 64,
            instance_manifest_sha256="b" * 64, instance_count=1, session=session)
    assert result["referenced_instances"] == 1
    assert client.deleted == []

    with pytest.raises(RuntimeError, match="header_mismatch"):
        _rollback_upload_receipt(
            client, receipt, [], upload_sha256="f" * 64,
            instance_manifest_sha256="b" * 64, instance_count=1)
    with pytest.raises(RuntimeError, match="header_mismatch"):
        _rollback_upload_receipt(
            client, tmp_path / "missing.receipt", [], upload_sha256="a" * 64,
            instance_manifest_sha256="b" * 64, instance_count=1)


def test_ingest_error_codes_never_retain_uploader_paths():
    leaked = FileExistsError(
        "[Errno 17] File exists: 'MRN-12345/control-study.dcm'")
    assert _safe_worker_error_code(leaked) == "FileExistsError"
    assert _safe_worker_error_code(
        ValueError("archive_file_limit_exceeded")) == (
            "ValueError:archive_file_limit_exceeded")
    wado = RuntimeError(
        "404 Client Error for http://orthanc/dicom-web/studies/1.2.3/series/4.5.6")
    assert _safe_worker_error_code(wado) == "RuntimeError"


def test_generated_profile_publication_is_atomic_and_reconcilable(tmp_path, monkeypatch):
    generated = tmp_path / "generated"
    monkeypatch.setattr(wsettings, "harmonization_generated_root", str(generated))
    source = tmp_path / "MELD_HSITEcombat_parameters.hdf5"
    source.write_bytes(b"immutable-combat")
    cohort = HarmonizationCohort(
        id="cohort", name="site", site_code="SITE", profile_code="HSITE",
        profile_version=1, source_role="t1_uni",
        selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
        created_by="user:builder",
    )
    build = HarmonizationBuild(
        id="build", cohort_id=cohort.id, initiated_by="user:builder",
        builder_image_digest="example/meld@sha256:" + "a" * 64,
        builder_adapter_sha256=ADAPTER_SHA256,
        artifact_manifest={"files": [{
            "path": "profiles/HSITE/v1/MELD_HSITEcombat_parameters.hdf5",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size": source.stat().st_size,
        }]},
    )
    published = _ensure_published(build, cohort, source)
    assert published.read_bytes() == source.read_bytes()
    assert not (generated / ".pending" / build.id).exists()
    # A retry after rename but before the database commit verifies and reuses the exact directory.
    assert _ensure_published(build, cohort, source) == published
    published.chmod(0o640)
    published.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="destination_conflict"):
        _ensure_published(build, cohort, source)


def test_prepublication_copy_failure_is_not_marked_pending(tmp_path, monkeypatch):
    generated = tmp_path / "generated"
    monkeypatch.setattr(wsettings, "harmonization_generated_root", str(generated))
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    cohort = HarmonizationCohort(
        id="prepublish-cohort", name="site", site_code="SITE", profile_code="HPRE",
        profile_version=1, source_role="t1_uni",
        selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
        created_by="user:builder", frozen_manifest={"manifest_sha256": "a" * 64},
        status="frozen",
    )
    build = HarmonizationBuild(
        id="prepublish-build", cohort_id=cohort.id, initiated_by="user:builder",
        status="building", stage="publishing",
        builder_image_digest="example/meld@sha256:" + "a" * 64,
        builder_adapter_sha256=ADAPTER_SHA256,
        artifact_manifest={"files": [{
            "path": "profiles/HPRE/v1/MELD_HPREcombat_parameters.hdf5",
            "sha256": "b" * 64, "size": 42,
        }]},
    )
    with Session(isolated) as session:
        session.add(cohort)
        session.add(build)
        session.commit()
        with pytest.raises(FileNotFoundError):
            _publish_candidate(session, build, cohort, tmp_path / "missing.hdf5")


def test_imported_dicom_policy_rejects_direct_identifiers_and_unknown_syntax(monkeypatch):
    class Response:
        def __init__(self, value, text=""):
            self.status_code = 200
            self._value = value
            self.text = text

        def json(self):
            return self._value

    class Client:
        def __init__(self, tags, syntax):
            self.tags, self.syntax = tags, syntax

        def get(self, url, **_kwargs):
            return Response(self.tags) if url.endswith("simplified-tags") else Response(
                None, self.syntax)

    monkeypatch.setattr(wsettings, "harmonization_orthanc_rest", "http://orthanc")
    _validate_imported_instance(Client({"PatientID": "DEID-001"}, "1.2.840.10008.1.2.1"), "i")
    with pytest.raises(ValueError, match="identifier"):
        _validate_imported_instance(
            Client({"PatientName": "Identified^Person"}, "1.2.840.10008.1.2.1"), "i")
    with pytest.raises(ValueError, match="transfer_syntax"):
        _validate_imported_instance(Client({}, "9.9.9.unknown"), "i")
