from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from arq import Retry
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "platform" / "api"), str(REPO / "platform" / "worker")]

from worker import dicom, ingest, pipeline, tasks  # noqa: E402
from worker.config import WorkerSettings, wsettings  # noqa: E402
from worker.detectors.base import CompletionValidationError, DetectorRunner  # noqa: E402
from worker.detectors.meld import MeldRunner  # noqa: E402
from worker.detectors.map import MapRunner  # noqa: E402
from worker.gpu import gpu_lease, wait_if_paused  # noqa: E402
from worker.harmonization import resolve_harmonization  # noqa: E402
from worker.process import run_process  # noqa: E402
from worker.tasks import completion_bundle_sha256  # noqa: E402


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
