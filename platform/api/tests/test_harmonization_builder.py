"""Cohort-builder API and deterministic validation contracts."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import (
    audit,
    cohort_routes,
    harmonization_routes,
    main,
    models,
    profile_import,
    queue,
    upload_receipt,
)
from app.auth import Principal, Role
from app.cohort_builder import canonical_sha256, deterministic_folds
from app.config import settings
from app.db import engine
from app.harmonization import (
    canonical_json_sha256,
    profile_document_sha256,
    runtime_profile_trusted,
)
from app.main import app


ADAPTER_SHA256 = "7" * 64


def setup_module(_module):
    engine.dispose()
    Path("test_meld.db").unlink(missing_ok=True)
    SQLModel.metadata.create_all(engine)


def _series(_study_uid: str, **_kwargs):
    acquisition = {
        "manufacturer": "siemens healthineers", "model": "magnetom terra",
        "station_name": "research-7t", "field_strength_t": 7.0,
        "protocol_name": "mp2rage research v1", "rows": 320, "columns": 320,
        "voxel_spacing_mm": [0.7, 0.7],
    }
    return [{
        "series_uid": f"{_study_uid}.1", "description": "MP2RAGE UNI", "instances": 1,
        "modality": "MR",
        "acquisition": acquisition, "fingerprint": "a" * 64,
    }]


def _instances(study_uid: str, series_uid: str, **_kwargs):
    patient_id = f"control-{int(study_uid.rsplit('.', 1)[-1]):03d}"
    return {
        "instances": [{
            "sop_instance_uid": f"{series_uid}.1",
            "sha256": hashlib.sha256(study_uid.encode()).hexdigest(),
            "size": 1024,
        }],
        "series": {
            "modality": "MR", "description": "MP2RAGE UNI",
            "patient_id": patient_id,
            "acquisition": _series(study_uid)[0]["acquisition"],
            "fingerprint": "a" * 64,
        },
    }


def test_deterministic_stratified_folds_cover_every_subject_once():
    subjects = [{
        "subject_key_hmac": f"{index:064x}", "age": 20 + index,
        "sex": "female" if index % 2 else "male",
    } for index in range(25)]
    first = deterministic_folds(subjects, 5)
    assert first == deterministic_folds(reversed(subjects), 5)
    assert {len(row["holdout_subject_hmacs"]) for row in first} == {5}
    holdouts = [key for row in first for key in row["holdout_subject_hmacs"]]
    assert sorted(holdouts) == sorted(row["subject_key_hmac"] for row in subjects)
    assert all(set(row["train_subject_hmacs"]).isdisjoint(row["holdout_subject_hmacs"])
               for row in first)


def test_build_acceptance_contract_requires_finite_nonempty_metric_bounds():
    image = "registry.local/meld@sha256:" + "a" * 64
    with pytest.raises(ValueError):
        cohort_routes.BuildCreate(
            builder_image_digest=image,
            acceptance_criteria={"methodology_sha256": "b" * 64},
        )
    with pytest.raises(ValueError):
        cohort_routes.BuildCreate(
            builder_image_digest=image,
            acceptance_criteria={
                "methodology_sha256": "b" * 64,
                "required_metrics": {"stability": {"min": float("nan")}},
            },
        )
    accepted = cohort_routes.BuildCreate(
        builder_image_digest=image,
        acceptance_criteria={
            "methodology_sha256": "b" * 64,
            "required_metrics": {"stability": {"min": 0.9, "max": 1.0}},
        },
    )
    assert accepted.acceptance_criteria["required_metrics"]["stability"]["min"] == 0.9
    with pytest.raises(ValueError):
        cohort_routes.BuildRejection(reason=" " * 20, evidence_sha256="c" * 64)


def test_server_blocks_ad_hoc_profile_create_validate_and_activate(monkeypatch):
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    principal = Principal(
        subject="admin", roles=frozenset({Role.admin}),
        auth_method="trusted_proxy", request_id="profile-mutation",
    )
    body = harmonization_routes.ProfileCreate(
        code="HADHOC", version=1, name="ad hoc",
        method="meld_distributed_combat", detector_id="meld_fcd",
        selector={"roles": ["t1_uni"]}, artifact_manifest={"files": []},
        parameters={"storage_scope": "generated"},
    )
    monkeypatch.setattr(settings, "deployment_mode", "research")
    with Session(isolated) as session:
        with pytest.raises(HTTPException, match="signed release import or the linked cohort"):
            harmonization_routes.create_profile(body, principal=principal, session=session)
        with pytest.raises(HTTPException, match="signed release import or the linked cohort"):
            harmonization_routes.validate_profile(
                "missing", principal=principal, session=session)
        with pytest.raises(HTTPException, match="signed release import or the linked cohort"):
            harmonization_routes.activate_profile(
                "missing", principal=principal, session=session)


def test_development_profile_workflow_allows_one_admin_for_every_transition(monkeypatch):
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    principal = Principal(
        subject="operator", roles=frozenset({Role.admin}),
        auth_method="trusted_proxy", request_id="single-admin-profile",
    )
    monkeypatch.setattr(settings, "deployment_mode", "development")
    monkeypatch.setattr(
        harmonization_routes, "verify_artifact_manifest",
        lambda *_args, **_kwargs: {"files": [], "manifest_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        harmonization_routes, "validate_profile_semantics",
        lambda *_args, **_kwargs: None,
    )
    body = harmonization_routes.ProfileCreate(
        code="HDEV", version=1, name="development profile", method="identity",
        selector={"acquisition": {"model": {"eq": "terra"}}},
        artifact_manifest={"files": []},
        parameters={"scientific_validation": {"independent_reviewer": "another-user"}},
    )
    with Session(isolated) as session:
        created = harmonization_routes.create_profile(
            body, principal=principal, session=session)
        with pytest.raises(HTTPException, match="does not match"):
            harmonization_routes.validate_profile(
                created["id"], principal=principal, session=session)
        profile = session.get(models.HarmonizationProfile, created["id"])
        profile.parameters = {
            **profile.parameters,
            "scientific_validation": {"independent_reviewer": principal.subject},
        }
        session.add(profile)
        session.commit()

        validated = harmonization_routes.validate_profile(
            created["id"], principal=principal, session=session)
        assert validated["status"] == "validated"
        active = harmonization_routes.activate_profile(
            created["id"], principal=principal, session=session)
        assert active["status"] == "active"
        session.refresh(profile)
        assert profile.created_by == profile.validated_by == principal.actor


def test_runtime_profile_trust_requires_exact_signed_document(monkeypatch):
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    with Session(isolated) as session:
        profile = models.HarmonizationProfile(
            code="HSIGNED", version=1, name="signed",
            method="meld_distributed_combat", detector_id="meld_fcd",
            selector={"roles": ["t1_uni"]},
            artifact_manifest={"files": [{"path": "profile.bin", "sha256": "a" * 64}]},
            parameters={"storage_scope": "release"}, status="active",
            created_by="release:test",
        )
        session.add(profile)
        session.commit()
        monkeypatch.setattr(settings, "harmonization_expected_profiles", [SimpleNamespace(
            code=profile.code, version=profile.version, detector_id="meld_fcd",
            document_sha256=profile_document_sha256(profile),
        )])
        assert runtime_profile_trusted(session, profile) is True
        profile.parameters = {**profile.parameters, "unexpected": True}
        assert runtime_profile_trusted(session, profile) is False


def test_cohort_api_freezes_exact_controls_and_queues_build(monkeypatch):
    async def dispatch_without_redis(_session, *, limit=50):
        return {"published": 0, "failed": 0}

    monkeypatch.setattr(queue, "dispatch_outbox_events", dispatch_without_redis)
    monkeypatch.setattr(cohort_routes, "get_study_series", _series)
    monkeypatch.setattr(cohort_routes, "get_series_instance_manifest", _instances)
    monkeypatch.setattr(
        settings, "harmonization_builder_adapter_sha256", ADAPTER_SHA256
    )
    with TestClient(app) as client:
        created = client.post("/api/harmonization/cohorts", json={
            "name": "Site 7T MP2RAGE", "site_code": "SITE7T",
            "profile_code": "HSITE7T", "profile_version": 91,
            "source_role": "t1_uni", "min_controls": 20, "cv_folds": 5,
            "selector": {"roles": ["t1_uni"], "acquisition": {
                "model": {"eq": "magnetom terra"},
                "protocol_name": {"regex": "mp2rage"},
            }},
        })
        assert created.status_code == 201, created.text
        cohort_id = created.json()["id"]
        studies = [{
            "study_uid": f"1.2.826.0.1.3680043.10.91.{index}",
            "subject_key": f"control-{index:03d}", "included": True,
        } for index in range(20)]
        imported = client.post(
            f"/api/harmonization/cohorts/{cohort_id}/studies/import",
            json={"studies": studies},
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["counts"]["included"] == 20
        assert imported.json()["studies"][0]["series_manifest"][0]["selected_source"] is True
        assert "instance_manifest" not in imported.json()["studies"][0]["series_manifest"][0]
        csv_rows = ["ID,Age,Sex"] + [
            f"control-{index:03d},{20 + index},{'female' if index % 2 else 'male'}"
            for index in range(20)
        ]
        extra_column = list(csv_rows)
        extra_column[1] += ",unapproved-extra-value"
        rejected_csv = client.post(
            f"/api/harmonization/cohorts/{cohort_id}/demographics",
            content="\n".join(extra_column) + "\n",
            headers={"Content-Type": "text/csv"},
        )
        assert rejected_csv.status_code == 422, rejected_csv.text
        demographics = client.post(
            f"/api/harmonization/cohorts/{cohort_id}/demographics",
            content="\n".join(csv_rows) + "\n", headers={"Content-Type": "text/csv"},
        )
        assert demographics.status_code == 200, demographics.text
        assert demographics.json()["status"] == "cohort_ready"
        frozen = client.post(f"/api/harmonization/cohorts/{cohort_id}/freeze")
        assert frozen.status_code == 200, frozen.text
        manifest = frozen.json()["frozen_manifest"]
        assert frozen.json()["status"] == "frozen"
        assert len(manifest["studies"]) == 20
        assert all("subject_key_hmac" in row and "control-" not in repr(row)
                   for row in manifest["studies"])
        build = client.post(f"/api/harmonization/cohorts/{cohort_id}/builds", json={
            "builder_image_digest": "registry.local/meld@sha256:" + "b" * 64,
            "acceptance_criteria": {
                "methodology_sha256": "c" * 64,
                "required_metrics": {"residual_site_effect": {"max": 0.1}},
            },
        })
        assert build.status_code == 201, build.text
        assert build.json()["status"] == "queued"
        assert len(build.json()["cv_plan"]["folds"]) == 5
        with Session(engine) as session:
            event = session.exec(select(models.OutboxEvent).where(
                models.OutboxEvent.aggregate_id == build.json()["id"],
                models.OutboxEvent.topic == "harmonization.build.enqueue",
            )).one()
            assert event.payload["attempt"] == 1


def test_resumable_upload_rejects_wrong_checksum(monkeypatch, tmp_path):
    async def dispatch_without_redis(_session, *, limit=50):
        return {"published": 0, "failed": 0}

    monkeypatch.setattr(queue, "dispatch_outbox_events", dispatch_without_redis)
    monkeypatch.setattr(settings, "harmonization_upload_root", str(tmp_path))
    monkeypatch.setattr(settings, "storage_min_free_bytes", 1024**3)
    monkeypatch.setattr(settings, "storage_min_free_percent", 1.0)
    with Session(engine) as session:
        cohort = models.HarmonizationCohort(
            name="Upload cohort", site_code="UPLOAD", profile_code="HUPLOAD",
            profile_version=92, source_role="t1_uni",
            selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
            created_by="user:test",
        )
        session.add(cohort)
        session.commit()
        cohort_id = cohort.id
    content = b"not-the-declared-content"
    with TestClient(app) as client:
        created = client.post(f"/api/harmonization/cohorts/{cohort_id}/uploads", json={
            "filename": "controls.dcm", "total_size": len(content),
            "sha256": hashlib.sha256(b"different").hexdigest(),
            "content_type": "application/dicom",
        })
        assert created.status_code == 201, created.text
        upload_id = created.json()["id"]
        chunk = client.put(
            f"/api/harmonization/cohorts/{cohort_id}/uploads/{upload_id}?offset=0",
            content=content, headers={"Content-Type": "application/octet-stream"},
        )
        assert chunk.status_code == 200, chunk.text
        completed = client.post(
            f"/api/harmonization/cohorts/{cohort_id}/uploads/{upload_id}/complete")
        assert completed.status_code == 409
        with Session(engine) as session:
            row = session.get(models.HarmonizationUpload, upload_id)
            assert row.status == models.HarmonizationUploadStatus.failed
            assert row.last_error == "size_or_hash_mismatch"


def test_resumable_upload_recovers_uncommitted_suffix_and_completion_is_idempotent(
        monkeypatch, tmp_path):
    async def dispatch_without_redis(_session, *, limit=50):
        return {"published": 0, "failed": 0}

    monkeypatch.setattr(queue, "dispatch_outbox_events", dispatch_without_redis)
    monkeypatch.setattr(settings, "harmonization_upload_root", str(tmp_path))
    monkeypatch.setattr(settings, "storage_min_free_bytes", 1024**3)
    monkeypatch.setattr(settings, "storage_min_free_percent", 1.0)
    with Session(engine) as session:
        cohort = models.HarmonizationCohort(
            name="Crash-safe upload", site_code="UPLOADSAFE", profile_code="HUPLOADSAFE",
            profile_version=1, source_role="t1_uni",
            selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
            created_by="user:test",
        )
        session.add(cohort)
        session.commit()
        cohort_id = cohort.id
    content = b"authoritative-retry"
    with TestClient(app) as client:
        created = client.post(f"/api/harmonization/cohorts/{cohort_id}/uploads", json={
            "filename": "control.dcm", "total_size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
        upload_id = created.json()["id"]
        with Session(engine) as session:
            row = session.get(models.HarmonizationUpload, upload_id)
            (tmp_path / row.storage_key).write_bytes(b"uncommitted-crash-suffix")
        uploaded = client.put(
            f"/api/harmonization/cohorts/{cohort_id}/uploads/{upload_id}?offset=0",
            content=content, headers={"Content-Type": "application/octet-stream"})
        assert uploaded.status_code == 200, uploaded.text
        first = client.post(
            f"/api/harmonization/cohorts/{cohort_id}/uploads/{upload_id}/complete")
        second = client.post(
            f"/api/harmonization/cohorts/{cohort_id}/uploads/{upload_id}/complete")
        assert first.status_code == second.status_code == 200
        assert first.json()["status"] == second.json()["status"] == "staged"


def test_coverage_reports_uncovered_and_matched_protocols(monkeypatch):
    async def dispatch_without_redis(_session, *, limit=50):
        return {"published": 0, "failed": 0}

    monkeypatch.setattr(queue, "dispatch_outbox_events", dispatch_without_redis)
    with Session(engine) as session:
        profile = models.HarmonizationProfile(
            code="HCOVERAGE", version=1, name="coverage", method="meld_distributed_combat",
            detector_id="meld_fcd", selector={
                "roles": ["t1_uni"], "acquisition": {"model": {"eq": "terra"}},
            }, artifact_manifest={"files": []}, parameters={},
            status="active", created_by="user:test",
        )
        session.add(profile)
        for index, model in enumerate(("terra", "unknown-scanner"), 1):
            case = models.Case(
                pseudonym=f"COVER-{index}", created_by="user:test",
                orthanc_study_uid=f"1.2.840.991.{index}")
            session.add(case)
            session.flush()
            session.add(models.Series(
                case_id=case.id, orthanc_series_uid=f"1.2.840.991.{index}.1",
                confirmed_role="t1_uni", proposed_role="t1_uni",
                fingerprint=("d" if model == "terra" else "e") * 64,
                acquisition={"model": model}, active=True,
            ))
        session.commit()
    with TestClient(app) as client:
        response = client.get("/api/harmonization/coverage")
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["covered"] >= 1
        assert response.json()["summary"]["uncovered"] >= 1


def test_server_coverage_excludes_runtime_untrusted_active_profiles(monkeypatch):
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    principal = Principal(
        subject="auditor", roles=frozenset({Role.auditor}),
        auth_method="trusted_proxy", request_id="coverage-trust",
    )
    monkeypatch.setattr(settings, "deployment_mode", "production")
    monkeypatch.setattr(settings, "harmonization_expected_profiles", [])
    with Session(isolated) as session:
        profile = models.HarmonizationProfile(
            code="HUNTRUSTED", version=1, name="untrusted",
            method="meld_distributed_combat", detector_id="meld_fcd",
            selector={
                "roles": ["t1_uni"], "acquisition": {"model": {"eq": "terra"}},
            },
            artifact_manifest={"files": []}, parameters={},
            status="active", created_by="legacy:test",
        )
        case = models.Case(
            pseudonym="UNTRUSTED-COVERAGE", created_by="user:test",
            orthanc_study_uid="1.2.840.991.99",
        )
        session.add(profile)
        session.add(case)
        session.flush()
        session.add(models.Series(
            case_id=case.id, orthanc_series_uid="1.2.840.991.99.1",
            confirmed_role="t1_uni", proposed_role="t1_uni",
            fingerprint="9" * 64, acquisition={"model": "terra"}, active=True,
        ))
        session.commit()

        result = cohort_routes.harmonization_coverage(
            principal=principal, session=session)

    assert result["summary"]["covered"] == 0
    assert result["summary"]["uncovered"] == 1
    assert result["observations"][0]["candidate_profile_ids"] == []


def test_empty_release_bootstrap_refuses_established_release_history(monkeypatch, tmp_path):
    root = tmp_path / "harmonization"
    (root / "profiles").mkdir(parents=True)
    (root / "expected-active-profiles.json").write_text("[]\n")
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    with Session(isolated) as session:
        session.add(models.HarmonizationProfile(
            code="HGENERATED", version=1, name="generated",
            method="meld_distributed_combat", detector_id="meld_fcd",
            selector={"acquisition": {"model": "terra"}}, artifact_manifest={"files": []},
            parameters={"storage_scope": "generated"}, status="active",
            created_by="service:harmonization-builder",
        ))
        session.add(models.HarmonizationProfile(
            code="HOLDRELEASE", version=1, name="release",
            method="meld_distributed_combat", detector_id="meld_fcd",
            selector={"acquisition": {"model": "old"}}, artifact_manifest={"files": []},
            parameters={}, status="active", created_by="release:old",
        ))
        session.commit()
    monkeypatch.setattr(profile_import, "engine", isolated)
    monkeypatch.setattr(profile_import.settings, "harmonization_root", str(root))
    monkeypatch.setattr(profile_import.settings, "harmonization_expected_profiles", [])
    monkeypatch.setattr(profile_import.settings, "harmonization_cohort_bootstrap_allowed", True)
    with pytest.raises(ValueError, match="established signed profile history"):
        profile_import.import_expected_profiles()
    with Session(isolated) as session:
        generated = session.exec(select(models.HarmonizationProfile).where(
            models.HarmonizationProfile.code == "HGENERATED")).one()
        old = session.exec(select(models.HarmonizationProfile).where(
            models.HarmonizationProfile.code == "HOLDRELEASE")).one()
        assert generated.status == models.HarmonizationProfileStatus.active
        assert old.status == models.HarmonizationProfileStatus.active


def test_empty_release_bootstrap_preserves_generated_only_profile(monkeypatch, tmp_path):
    root = tmp_path / "harmonization"
    (root / "profiles").mkdir(parents=True)
    (root / "expected-active-profiles.json").write_text("[]\n")
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    with Session(isolated) as session:
        session.add(models.HarmonizationProfile(
            code="HGENERATED", version=1, name="generated",
            method="meld_distributed_combat", detector_id="meld_fcd",
            selector={"acquisition": {"model": "terra"}}, artifact_manifest={"files": []},
            parameters={"storage_scope": "generated"}, status="active",
            created_by="service:harmonization-builder",
        ))
        session.commit()
    monkeypatch.setattr(profile_import, "engine", isolated)
    monkeypatch.setattr(profile_import.settings, "harmonization_root", str(root))
    monkeypatch.setattr(profile_import.settings, "harmonization_expected_profiles", [])
    monkeypatch.setattr(profile_import.settings, "harmonization_cohort_bootstrap_allowed", True)
    assert profile_import.import_expected_profiles() == {
        "created": 0, "activated": 0, "retired": 0, "promoted": 0,
    }


def test_readiness_allows_only_explicit_empty_inventory_bootstrap(monkeypatch):
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    monkeypatch.setattr(main, "engine", isolated)
    monkeypatch.setattr(main.settings, "harmonization_expected_profiles", [])
    monkeypatch.setattr(main.settings, "harmonization_cohort_bootstrap_allowed", True)
    assert main._scan_harmonization_profiles_off_loop()["ready"] is True
    monkeypatch.setattr(main.settings, "harmonization_cohort_bootstrap_allowed", False)
    result = main._scan_harmonization_profiles_off_loop()
    assert result["ready"] is False
    assert result["failures"] == [{"error": "signed_expected_inventory_missing"}]


def test_abandoned_upload_chunks_are_expired_and_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "harmonization_upload_root", str(tmp_path))
    monkeypatch.setattr(settings, "harmonization_upload_expiry_hours", 1)
    with Session(engine) as session:
        cohort = models.HarmonizationCohort(
            name="Expiry cohort", site_code="EXPIRY", profile_code="HEXPIRY",
            profile_version=93, source_role="t1_uni",
            selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
            created_by="user:test",
        )
        session.add(cohort)
        session.flush()
        upload = models.HarmonizationUpload(
            cohort_id=cohort.id, filename="abandoned.dcm", total_size=10,
            received_size=5, sha256="f" * 64, created_by="user:test",
            content_type="application/dicom",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        session.add(upload)
        session.commit()
        path = tmp_path / upload.storage_key
        path.write_bytes(b"12345")
        assert queue.reap_stale_harmonization_uploads(session) == {
            "expired": 1, "files_removed": 1, "terminal_files_removed": 0,
            "subject_mappings_redacted": 0}
        session.refresh(upload)
        assert upload.status == models.HarmonizationUploadStatus.failed
        assert upload.last_error == "upload_session_expired"
        assert not path.exists()


def test_imported_upload_subject_mapping_is_redacted_after_expiry(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "harmonization_upload_root", str(tmp_path))
    monkeypatch.setattr(settings, "harmonization_upload_expiry_hours", 1)
    with Session(engine) as session:
        cohort = models.HarmonizationCohort(
            name="Mapping expiry", site_code="MAPEXP", profile_code="HMAPEXP",
            profile_version=94, source_role="t1_uni",
            selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
            created_by="user:test",
        )
        session.add(cohort)
        session.flush()
        upload = models.HarmonizationUpload(
            cohort_id=cohort.id, filename="controls.zip", total_size=10,
            received_size=10, sha256="e" * 64, created_by="user:test",
            status="imported", import_result={
                "phase": "imported",
                "study_uids": ["1.2.3"],
                "studies": [{"study_uid": "1.2.3", "subject_key": "CONTROL-001"}],
            },
            updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        session.add(upload)
        session.commit()
        result = queue.reap_stale_harmonization_uploads(session)
        assert result["subject_mappings_redacted"] == 1
        session.refresh(upload)
        assert upload.import_result["studies"] == [{"study_uid": "1.2.3"}]
        assert upload.import_result["subject_mappings_redacted"] is True
        assert upload.mapping_redacted_at is not None


def test_upload_completion_retry_and_ambiguous_rollback_resolution(monkeypatch, tmp_path):
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    monkeypatch.setattr(settings, "harmonization_upload_root", str(tmp_path))
    admin = Principal(
        subject="operator", roles=frozenset({Role.admin}),
        auth_method="trusted_proxy", request_id="rollback-resolution",
    )
    with Session(isolated) as session:
        cohort = models.HarmonizationCohort(
            id="upload-resolution-cohort", name="resolution", site_code="RESOLVE",
            profile_code="HRESOLVE", profile_version=1, source_role="t1_uni",
            selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
            created_by=admin.actor,
        )
        importing = models.HarmonizationUpload(
            id="upload-importing", cohort_id=cohort.id, filename="controls.zip",
            total_size=10, received_size=10, sha256="a" * 64,
            created_by=admin.actor, status="importing",
            import_result={"phase": "importing"},
        )
        ambiguous = models.HarmonizationUpload(
            id="upload-ambiguous", cohort_id=cohort.id, filename="ambiguous.zip",
            total_size=10, received_size=10, sha256="b" * 64,
            created_by=admin.actor, status="failed",
            import_result={"phase": "rollback_incomplete",
                           "rollback_pending_instances": 1,
                           "ambiguous_instances": 1,
                           "owned_delete_failures": 0,
                           "candidate_verification_failures": 0,
                           "receipt_integrity_failures": 0},
        )
        session.add(cohort)
        session.add(importing)
        session.add(ambiguous)
        session.commit()
        retried = cohort_routes.complete_upload(
            cohort.id, importing.id, principal=admin, session=session)
        assert retried["status"] == models.HarmonizationUploadStatus.importing
        with pytest.raises(HTTPException) as fenced:
            cohort_routes._assert_no_pending_orthanc_rollback(session)
        assert fenced.value.status_code == 409
        session.rollback()
        resolved = cohort_routes.resolve_upload_rollback(
            cohort.id, ambiguous.id,
            cohort_routes.UploadRollbackResolution(
                action="preserve",
                reason="Independent C-STORE ownership was verified by the site operator.",
                evidence_sha256="c" * 64,
            ),
            principal=admin, session=session,
        )
        assert resolved["import_result"]["phase"] == "failed"
        assert resolved["import_result"]["resolution"]["action"] == "preserve"
        cohort_routes._assert_no_pending_orthanc_rollback(session)


def test_rollback_evidence_is_canonical_and_subject_mapping_is_admin_only(
        monkeypatch, tmp_path):
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    monkeypatch.setattr(settings, "harmonization_upload_root", str(tmp_path))
    admin = Principal(
        subject="operator", roles=frozenset({Role.admin}),
        auth_method="trusted_proxy", request_id="rollback-evidence",
    )
    cohort = models.HarmonizationCohort(
        id="evidence-cohort", name="evidence", site_code="EVIDENCE",
        profile_code="HEVIDENCE", profile_version=1, source_role="t1_uni",
        selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
        created_by=admin.actor,
    )
    upload = models.HarmonizationUpload(
        id="evidence-upload", cohort_id=cohort.id, filename="controls.zip",
        storage_key="evidence-storage", total_size=10, received_size=10,
        sha256="a" * 64, created_by=admin.actor, status="failed",
    )
    header = upload_receipt.expected_header(
        upload_sha256=upload.sha256, instance_manifest_sha256="b" * 64,
        instance_count=1,
    )
    receipt = tmp_path / f"{upload.storage_key}.receipt"
    upload_receipt.append_receipt(receipt, header)
    upload_receipt.append_receipt(receipt, {
        "event": "intent", "file_sha256": "c" * 64,
        "sop_instance_uid": "1.2.840.9", "size": 512,
    })
    loaded_header, intents, completed = upload_receipt.load_receipt(receipt)
    digest = upload_receipt.evidence_sha256(loaded_header, intents, completed)
    upload.import_result = {
        "phase": "rollback_incomplete", "instance_manifest_sha256": "b" * 64,
        "instance_count": 1, "receipt_evidence_sha256": digest,
        "rollback_pending_instances": 1, "ambiguous_instances": 1,
        "studies": [{"study_uid": "1.2.840", "subject_key": "CONTROL-001"}],
    }
    with Session(isolated) as session:
        session.add(cohort)
        session.add(upload)
        session.commit()
        public = cohort_routes._cohort_public(
            session, cohort, detail=True, include_upload_subjects=False)
        private = cohort_routes._cohort_public(
            session, cohort, detail=True, include_upload_subjects=True)
        assert public["uploads"][0]["import_result"]["studies"] == [
            {"study_uid": "1.2.840"}]
        assert private["uploads"][0]["import_result"]["studies"][0][
            "subject_key"] == "CONTROL-001"

        evidence = cohort_routes.get_upload_rollback_evidence(
            cohort.id, upload.id, principal=admin, session=session)
        assert evidence["receipt_evidence_sha256"] == digest
        assert evidence["pending_counts"]["ambiguous_instances"] == 1
        assert evidence["instances"] == [{
            "file_sha256": "c" * 64, "sop_instance_uid": "1.2.840.9",
            "size": 512, "orthanc_instance_id": None, "worker_owned": None,
            "response_recorded": False,
        }]
        with pytest.raises(HTTPException) as rejected:
            cohort_routes.resolve_upload_rollback(
                cohort.id, upload.id,
                cohort_routes.UploadRollbackResolution(
                    action="delete",
                    reason="Operator checked a different non-canonical receipt digest.",
                    evidence_sha256="d" * 64,
                ),
                principal=admin, session=session,
            )
        assert rejected.value.status_code == 422


def test_study_admission_rechecks_exact_closure_under_mutation_fence(monkeypatch):
    async def dispatch_without_redis(_session, *, limit=50):
        return {"published": 0, "failed": 0}

    calls = 0

    def changing_instances(study_uid: str, series_uid: str, **kwargs):
        nonlocal calls
        calls += 1
        value = _instances(study_uid, series_uid, **kwargs)
        if calls == 2:
            value["instances"][0]["sha256"] = "f" * 64
        return value

    monkeypatch.setattr(queue, "dispatch_outbox_events", dispatch_without_redis)
    monkeypatch.setattr(cohort_routes, "get_study_series", _series)
    monkeypatch.setattr(cohort_routes, "get_series_instance_manifest", changing_instances)
    with TestClient(app) as client:
        created = client.post("/api/harmonization/cohorts", json={
            "name": "Admission race", "site_code": "RACE",
            "profile_code": "HRACE", "profile_version": 96,
            "source_role": "t1_uni",
            "selector": {"roles": ["t1_uni"], "acquisition": {
                "model": {"eq": "magnetom terra"},
                "protocol_name": {"regex": "mp2rage"},
            }},
        })
        assert created.status_code == 201, created.text
        cohort_id = created.json()["id"]
        response = client.post(
            f"/api/harmonization/cohorts/{cohort_id}/studies/import",
            json={"studies": [{
                "study_uid": "1.2.826.0.1.3680043.10.96.1",
                "subject_key": "control-001", "included": True,
            }]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "exact cohort source closure changed before admission"
    with Session(engine) as session:
        assert session.exec(select(models.HarmonizationCohortStudy).where(
            models.HarmonizationCohortStudy.cohort_id == cohort_id)).all() == []


def test_stale_build_lease_fails_compute_but_resumes_atomic_publication():
    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    with Session(engine) as session:
        cohorts = []
        for index in (1, 2):
            cohort = models.HarmonizationCohort(
                name=f"Lease {index}", site_code=f"LEASE{index}",
                profile_code=f"HLEASE{index}", profile_version=1, source_role="t1_uni",
                selector={"roles": ["t1_uni"], "acquisition": {"model": f"terra-{index}"}},
                created_by="user:test", status="frozen",
            )
            session.add(cohort)
            session.flush()
            cohorts.append(cohort)
        compute = models.HarmonizationBuild(
            cohort_id=cohorts[0].id, status="building", stage="cross_validation",
            initiated_by="user:test", builder_image_digest="example/meld@sha256:" + "1" * 64,
            builder_adapter_sha256=ADAPTER_SHA256,
            acceptance_criteria={}, heartbeat_at=expired, lease_expires_at=expired,
        )
        publication = models.HarmonizationBuild(
            cohort_id=cohorts[1].id, status="building", stage="publishing",
            initiated_by="user:test", builder_image_digest="example/meld@sha256:" + "2" * 64,
            builder_adapter_sha256=ADAPTER_SHA256,
            acceptance_criteria={}, heartbeat_at=expired, lease_expires_at=expired,
            artifact_manifest={"files": [{"path": "x", "sha256": "a" * 64}]},
            qc_report={"all_folds_succeeded": True},
        )
        session.add(compute)
        session.add(publication)
        session.commit()
        result = queue.reap_stale_harmonization_builds(session)
        assert result == {"failed": 1, "publication_resumed": 1}
        session.refresh(compute)
        session.refresh(publication)
        assert compute.status == models.HarmonizationBuildStatus.failed
        assert compute.error_code == "builder_lease_expired"
        assert publication.status == models.HarmonizationBuildStatus.building
        event = session.exec(select(models.OutboxEvent).where(
            models.OutboxEvent.aggregate_id == publication.id,
            models.OutboxEvent.topic == "harmonization.build.enqueue")).one()
        assert event.payload["dispatch_token"] == "resume-publication"


def test_durable_publication_stage_cannot_be_cancelled():
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    with Session(isolated) as session:
        cohort = models.HarmonizationCohort(
            id="publishing-cohort", name="publishing", site_code="PUB",
            profile_code="HPUB", profile_version=1, source_role="t1_uni",
            selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
            created_by="user:initiator", status="frozen",
        )
        build = models.HarmonizationBuild(
            id="publishing-build", cohort_id=cohort.id, status="building",
            stage="publishing", initiated_by="user:initiator",
            builder_image_digest="example/meld@sha256:" + "a" * 64,
            builder_adapter_sha256=ADAPTER_SHA256,
            acceptance_criteria={}, artifact_manifest={"files": []},
            qc_report={"all_folds_succeeded": True},
        )
        session.add(cohort)
        session.add(build)
        session.commit()
        admin = Principal(
            subject="admin", roles=frozenset({Role.admin}),
            auth_method="trusted_proxy", request_id="cancel-publication",
        )
        with pytest.raises(HTTPException, match="must finish deterministic reconciliation"):
            cohort_routes.cancel_build(build.id, principal=admin, session=session)
        session.refresh(build)
        assert build.status == models.HarmonizationBuildStatus.building
        assert build.stage == "publishing"


@pytest.mark.asyncio
async def test_missing_harmonization_broker_job_reopens_durable_handoff(monkeypatch):
    class MissingJob:
        def __init__(self, _job_id, _pool):
            pass

        async def status(self):
            return queue.JobStatus.not_found

    async def fake_pool():
        return object()

    monkeypatch.setattr(queue, "Job", MissingJob)
    monkeypatch.setattr(queue, "get_pool", fake_pool)
    with Session(engine) as session:
        cohort = models.HarmonizationCohort(
            name="reconcile", site_code="RECON", profile_code="HRECON", profile_version=1,
            source_role="t1_uni", selector={"roles": ["t1_uni"],
                                             "acquisition": {"model": "terra"}},
            created_by="user:test",
        )
        session.add(cohort)
        session.flush()
        build = models.HarmonizationBuild(
            cohort_id=cohort.id, initiated_by="user:test", attempt=1,
            builder_image_digest="example/meld@sha256:" + "a" * 64,
            builder_adapter_sha256=ADAPTER_SHA256,
            acceptance_criteria={}, status="queued",
        )
        session.add(build)
        session.flush()
        event = models.OutboxEvent(
            dedupe_key=f"harmonization.build.enqueue:{build.id}:attempt:1",
            topic="harmonization.build.enqueue", aggregate_type="harmonization_build",
            aggregate_id=build.id, payload={"build_id": build.id, "attempt": 1},
            status="published", attempts=1,
            published_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        session.add(event)
        session.commit()
        result = await queue.reconcile_harmonization_jobs(session)
        assert result["reopened"] == 1
        session.refresh(event)
        assert event.status == models.OutboxStatus.pending
        assert event.payload["dispatch_token"] == "reconcile-2"


def test_generated_candidate_accepts_single_admin_with_bound_evidence(
        monkeypatch, tmp_path):
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    monkeypatch.setattr(settings, "harmonization_generated_root", str(tmp_path))
    selector = {"roles": ["t1_uni"], "acquisition": {"model": {"eq": "terra"}}}
    frozen = {
        "schema_version": 1, "cohort_id": "cohort", "profile": {
            "code": "HSINGLEADMIN", "version": 1, "detector_id": "meld_fcd"},
        "source_role": "t1_uni", "selector": selector, "minimum_controls": 20,
        "cv_folds": 5, "studies": [], "demographics_sha256": "d" * 64,
        "cv_plan_sha256": "e" * 64,
    }
    frozen["manifest_sha256"] = canonical_sha256(frozen)
    data_root = tmp_path / "profiles" / "HSINGLEADMIN" / "v1"
    data_root.mkdir(parents=True)
    artifact = data_root / "MELD_HSINGLEADMINcombat_parameters.hdf5"
    artifact.write_bytes(b"combat")
    artifact_manifest = {
        "schema_version": 1,
        "files": [{"path": "profiles/HSINGLEADMIN/v1/" + artifact.name,
                   "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                   "size": artifact.stat().st_size}],
        "cohort_manifest_sha256": frozen["manifest_sha256"],
    }
    plan = [{
        "fold_index": index, "train_subject_hmacs": [f"train-{index}-{n}" for n in range(16)],
        "holdout_subject_hmacs": [f"hold-{index}-{n}" for n in range(4)],
        "membership_hmac_sha256": f"{index + 1:064x}",
    } for index in range(5)]
    demographic_rows = [{
        "subject_key_hmac": f"{index + 100:064x}", "age": float(20 + index),
        "sex": "female" if index % 2 else "male",
    } for index in range(20)]
    frozen["studies"] = [{
        "subject_key_hmac": f"{index + 100:064x}",
        "orthanc_study_uid_hmac": f"{index + 300:064x}",
        "study_sha256": f"{index + 200:064x}",
        "acquisition_fingerprint": "a" * 64,
        "series_manifest_sha256": canonical_sha256([]),
    } for index in range(20)]
    frozen["demographics_sha256"] = canonical_sha256(demographic_rows)
    frozen["cv_plan_sha256"] = canonical_sha256(plan)
    frozen["manifest_sha256"] = canonical_sha256({
        key: value for key, value in frozen.items() if key != "manifest_sha256"})
    artifact_manifest["cohort_manifest_sha256"] = frozen["manifest_sha256"]
    metrics = [{"residual_site_effect": 0.01 + index / 1000} for index in range(5)]
    resources = [{"wall_seconds": 1.0} for _ in range(5)]
    qc = {
        "schema_version": 1, "internal_validation": "deterministic_k_fold",
        "folds": 5, "subject_count": 20, "all_folds_succeeded": True,
        "metrics": metrics, "resource_usage": resources,
        "scientific_caveat": "internal only", "build_id": "build",
        "cohort_manifest_sha256": frozen["manifest_sha256"],
        "acceptance_criteria_sha256": canonical_sha256({
            "methodology_sha256": "9" * 64}),
        "builder_image_digest": "example/meld@sha256:" + "8" * 64,
        "builder_adapter_sha256": ADAPTER_SHA256,
        "final_fit_metrics": {}, "final_fit_resource_usage": {},
        "final_artifact_relative": "final/" + artifact.name,
    }
    qc["report_sha256"] = canonical_sha256(qc)
    artifact_manifest["builder_adapter_sha256"] = ADAPTER_SHA256
    with Session(isolated) as session:
        cohort = models.HarmonizationCohort(
            id="cohort", name="single admin", site_code="SITE",
            profile_code="HSINGLEADMIN", profile_version=1, source_role="t1_uni",
            selector=selector, min_controls=20, cv_folds=5, status="frozen",
            frozen_manifest=frozen, created_by="user:initiator", approved_by="user:initiator",
        )
        session.add(cohort)
        for index in range(20):
            session.add(models.HarmonizationCohortStudy(
                cohort_id=cohort.id, orthanc_study_uid=f"1.2.840.555.{index}",
                subject_key_hmac=f"{index + 100:064x}", acquisition_fingerprint="a" * 64,
                acquisition={"model": "terra"}, series_manifest=[],
                study_sha256=f"{index + 200:064x}", included=True,
            ))
            session.add(models.HarmonizationDemographic(
                cohort_id=cohort.id, subject_key_hmac=f"{index + 100:064x}",
                age=20 + index, sex="female" if index % 2 else "male",
            ))
        profile = models.HarmonizationProfile(
            code=cohort.profile_code, version=1, name=cohort.name,
            method="meld_distributed_combat", detector_id="meld_fcd", selector=selector,
            artifact_manifest=artifact_manifest, status="draft",
            parameters={
                "harmo_code": cohort.profile_code,
                "cohort_manifest_sha256": frozen["manifest_sha256"],
                "activation_eligible": True, "control_count": 20, "minimum_subjects": 20,
                "selector_canonical_sha256": canonical_json_sha256(selector),
                "build_images": {"meld": qc["builder_image_digest"]},
                "builder_adapter_sha256": ADAPTER_SHA256,
                "data_root": "profiles/HSINGLEADMIN/v1", "storage_scope": "generated",
                "internal_cv_report_sha256": qc["report_sha256"],
            }, created_by="service:harmonization-builder",
        )
        session.add(profile)
        session.flush()
        build = models.HarmonizationBuild(
            id="build", cohort_id=cohort.id, status="qc_review", stage="qc_review",
            initiated_by="user:initiator", builder_image_digest=qc["builder_image_digest"],
            builder_adapter_sha256=ADAPTER_SHA256,
            acceptance_criteria={"methodology_sha256": "9" * 64},
            cv_plan={"folds": plan}, qc_report=qc, artifact_manifest=artifact_manifest,
            profile_id=profile.id,
        )
        session.add(build)
        for index in range(5):
            session.add(models.HarmonizationFoldResult(
                build_id=build.id, fold_index=index, train_count=16, holdout_count=4,
                membership_hmac_sha256=plan[index]["membership_hmac_sha256"],
                status="passed", metrics=metrics[index], resource_usage=resources[index],
            ))
        session.commit()
        report = {
            "schema_version": 1,
            "profile": {"code": cohort.profile_code, "version": 1,
                        "detector_id": "meld_fcd"},
            "approval_id": "GOLDEN-001", "independent_reviewer": "initiator",
            "approved_at": "2026-07-11T12:00:00Z",
            "acquisition_fingerprints": ["a" * 64],
            "qc": {"included": 20, "excluded": 0},
            "holdout": {"case_count": 3, "positive_cases": 1,
                        "negative_cases": 1, "control_cases": 1},
            "metrics_sha256": "b" * 64, "golden_case_evidence_sha256": "c" * 64,
            "methodology_sha256": "9" * 64,
            "image_digests": {"meld": qc["builder_image_digest"]},
            "builder_adapter_sha256": ADAPTER_SHA256,
        }
        administrator = Principal(
            subject="initiator", roles=frozenset({Role.admin}),
            auth_method="trusted_proxy", request_id="validate")
        with pytest.raises(HTTPException, match="adapter differs"):
            cohort_routes.validate_build(
                build.id,
                cohort_routes.BuildValidation(scientific_validation={
                    **report, "builder_adapter_sha256": "0" * 64,
                }),
                principal=administrator, session=session,
            )
        with pytest.raises(HTTPException, match="methodology"):
            cohort_routes.validate_build(
                build.id,
                cohort_routes.BuildValidation(scientific_validation={
                    **report, "methodology_sha256": "0" * 64,
                }),
                principal=administrator, session=session,
            )
        validated = cohort_routes.validate_build(
            build.id, cohort_routes.BuildValidation(scientific_validation=report),
            principal=administrator, session=session)
        assert validated["status"] == "validated"
        active = cohort_routes.activate_build(
            build.id, principal=administrator, session=session)
        assert active["status"] == "active"
        session.refresh(profile)
        assert profile.status == models.HarmonizationProfileStatus.active
        session.refresh(build)
        assert (build.initiated_by == build.validated_by == build.activated_by
                == administrator.actor)
        exported = cohort_routes.export_build_for_release(
            build.id, principal=administrator, session=session)
        assert exported["profile_document"]["parameters"]["storage_scope"] == "release"
        assert exported["builder_adapter_sha256"] == ADAPTER_SHA256
        assert exported["profile_document"]["artifact_manifest"][
            "builder_adapter_sha256"] == ADAPTER_SHA256
        assert exported["expected_inventory_entry"]["document_sha256"] \
            == profile_import._release_promotion_sha256(profile)
        assert runtime_profile_trusted(session, profile) is True
        monkeypatch.setattr(main, "engine", isolated)
        monkeypatch.setattr(main.settings, "harmonization_expected_profiles", [])
        assert main._scan_harmonization_profiles_off_loop()["ready"] is True
        build.activated_by = None
        session.add(build)
        session.commit()
        assert runtime_profile_trusted(session, profile) is False
        build.activated_by = administrator.actor
        session.add(build)
        session.commit()
        profile.parameters = {
            **profile.parameters, "builder_adapter_sha256": "8" * 64,
        }
        assert runtime_profile_trusted(session, profile) is False
        profile.parameters = {
            **profile.parameters, "builder_adapter_sha256": ADAPTER_SHA256,
        }
        profile.status = models.HarmonizationProfileStatus.retired
        session.add(profile)
        session.commit()
        with pytest.raises(HTTPException, match="active generated profile"):
            cohort_routes.export_build_for_release(
                build.id, principal=administrator, session=session)


def test_single_admin_rejection_archives_candidate_and_requires_new_version():
    isolated = create_engine("sqlite://")
    SQLModel.metadata.create_all(isolated)
    with Session(isolated) as session:
        cohort = models.HarmonizationCohort(
            id="reject-cohort", name="reject", site_code="REJECT", profile_code="HREJECT",
            profile_version=1, source_role="t1_uni", status="frozen",
            selector={"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
            created_by="user:initiator", approved_by="user:initiator",
        )
        profile = models.HarmonizationProfile(
            id="reject-profile", code="HREJECT", version=1, name="reject",
            method="meld_distributed_combat", detector_id="meld_fcd",
            selector=cohort.selector, artifact_manifest={"files": []}, parameters={},
            status="draft", created_by="service:harmonization-builder",
        )
        session.add(cohort)
        session.add(profile)
        session.flush()
        build = models.HarmonizationBuild(
            id="reject-build", cohort_id=cohort.id, status="qc_review", stage="qc_review",
            profile_id=profile.id, initiated_by="user:initiator",
            builder_image_digest="example/meld@sha256:" + "a" * 64,
            builder_adapter_sha256=ADAPTER_SHA256,
            acceptance_criteria={},
        )
        session.add(build)
        session.commit()
        reviewer = Principal(
            subject="initiator", roles=frozenset({Role.admin}),
            auth_method="trusted_proxy", request_id="reject",
        )
        result = cohort_routes.reject_build(
            build.id,
            cohort_routes.BuildRejection(
                reason="External golden cases failed the approved residual-effect threshold.",
                evidence_sha256="b" * 64,
            ),
            principal=reviewer, session=session,
        )
        assert result["stage"] == "rejected"
        assert result["rejection_summary"]["requires_new_profile_version"] is True
        session.refresh(cohort)
        session.refresh(profile)
        assert cohort.status == models.HarmonizationCohortStatus.archived
        assert profile.status == models.HarmonizationProfileStatus.retired
