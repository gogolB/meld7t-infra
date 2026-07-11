"""Authenticated research workflow, idempotent confirmation, and audit verification."""
import os
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["MELD7T_DB_URL"] = "sqlite:///./test_meld.db"
os.environ["MELD7T_DEPLOYMENT_MODE"] = "test"
os.environ["MELD7T_AUTH_DEV_BYPASS"] = "true"
os.environ["MELD7T_AUDIT_REQUIRE_IMMUDB"] = "false"
os.environ["MELD7T_HARMONIZATION_REQUIRED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from fastapi import HTTPException  # noqa: E402
import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app import audit, models, queue  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.harmonization import case_harmonization_coverage, run_harmonization_contract  # noqa: E402
from app.models import (  # noqa: E402
    CaseStatus, DetectorId, HarmonizationProfileStatus, HarmonizationStatus, OutboxEvent, Result,
    RunStatus, SeriesRole, Workup,
)
from app.orthanc import propose_role  # noqa: E402
from app.recipe import build_recipe, recipe_summary  # noqa: E402
from app.routes import (  # noqa: E402
    _SERIES_MUTABLE_CASE_STATES, _artifact_matches_manifest,
    _assert_series_mutation_has_no_active_runs, _public_gpu_owner,
    _validate_recipe_harmonization_contract,
)


def setup_module(_m):
    if os.path.exists("test_meld.db"):
        os.remove("test_meld.db")
    SQLModel.metadata.create_all(engine)


def test_propose_role():
    assert propose_role("SAG T1 MP2RAGE UNI") == SeriesRole.t1_uni
    assert propose_role("SAG T1 MP2RAGE INV1") == SeriesRole.t1_inv1
    assert propose_role("AX_T1_MPRAGE") == SeriesRole.t1_mprage
    assert propose_role("SAG_DARKFLUID") == SeriesRole.flair
    assert propose_role("SAG_T2SPACE") == SeriesRole.t2


def test_recipe_tandem():
    roles = {"u1": "t1_uni", "i1": "t1_inv1", "i2": "t1_inv2",
             "m1": "t1_mprage"}
    entries = build_recipe(Workup.fcd, roles, require_harmonization=False,
                           unharmonized_reason="test deployment")
    meld = [e for e in entries if e["detector_id"] == "meld_fcd" and e["status"] == "created"]
    assert len(meld) == 2                      # tandem: MELD on both UNI and MPRAGE
    mapd = [e for e in entries if e["detector_id"] == "map" and e["status"] == "created"]
    assert len(mapd) == 2                      # MAP is built too → also tandem on both T1 sources
    s = recipe_summary(entries)
    assert s["will_run"] == 4 and s["tandem"] is True
    # the un-built HS detectors still surface as declared-pending slots in a full 'both' workup
    both = build_recipe(Workup.both, roles, require_harmonization=False,
                        unharmonized_reason="test deployment")
    assert any(e["detector_id"] in ("qt2", "aid_hs") and e["status"] == "pending" for e in both)


def test_case_harmonization_is_partial_until_every_current_target_is_confirmed():
    with Session(engine) as session:
        case = models.Case(
            pseudonym="COVERAGE", created_by="test", orthanc_study_uid="1.2.840.777",
            status=CaseStatus.series_confirmed,
        )
        session.add(case)
        session.flush()
        source = models.Series(
            case_id=case.id, orthanc_series_uid="1.2.840.777.1",
            proposed_role=SeriesRole.t1_mprage, confirmed_role=SeriesRole.t1_mprage,
            fingerprint="a" * 64, acquisition={"model": "terra"}, active=True,
        )
        session.add(source)
        session.flush()
        session.add(models.HarmonizationAssignment(
            case_id=case.id, profile_id="profile-meld", detector_id=DetectorId.meld_fcd,
            source_series_uid=source.orthanc_series_uid,
            acquisition_fingerprint=source.fingerprint, status=HarmonizationStatus.confirmed,
        ))
        session.flush()
        partial = case_harmonization_coverage(session, case.id)
        assert partial["coverage"] == "partial"
        assert partial["confirmed"] == 1 and partial["required"] == 2
        session.add(models.HarmonizationAssignment(
            case_id=case.id, profile_id="profile-map", detector_id=DetectorId.map,
            source_series_uid=source.orthanc_series_uid,
            acquisition_fingerprint=source.fingerprint, status=HarmonizationStatus.confirmed,
        ))
        session.flush()
        assert case_harmonization_coverage(session, case.id)["coverage"] == "complete"
        session.rollback()


def test_terminal_case_series_recovery_requires_every_run_to_be_terminal():
    assert {CaseStatus.failed, CaseStatus.review_ready, CaseStatus.adjudicated}.issubset(
        _SERIES_MUTABLE_CASE_STATES
    )
    with Session(engine) as session:
        case = models.Case(
            pseudonym="RECOVERY", created_by="user:test", orthanc_study_uid="1.2.840.778",
            status=CaseStatus.failed,
        )
        session.add(case)
        session.flush()
        recipe = models.Recipe(case_id=case.id, workup=Workup.fcd, spec=[], version=1)
        session.add(recipe)
        session.flush()
        _assert_series_mutation_has_no_active_runs(session, case.id)
        session.add(models.Run(
            case_id=case.id, recipe_id=recipe.id, detector_id=DetectorId.map,
            logical_key="recovery-active-run", status=RunStatus.packaging,
        ))
        session.flush()
        with pytest.raises(HTTPException, match="current run is active"):
            _assert_series_mutation_has_no_active_runs(session, case.id)
        session.rollback()


class _MemoryLedger:
    def __init__(self, *, first_tx_id=1):
        self.values = {}
        self.next_tx_id = first_tx_id

    def verified_set(self, key, value):
        tx_id = self.next_tx_id
        self.next_tx_id += 1
        self.values[(key, tx_id)] = value
        return tx_id

    def verify_entry(self, key, value, tx_id):
        status = "verified" if self.values.get((key, tx_id)) == value else "mismatch"
        return audit.LedgerVerification(status, tx_id)


def test_full_workflow(monkeypatch):
    async def dispatch_without_redis(session, *, limit=50):
        return {"published": 0, "failed": 0}

    monkeypatch.setattr(queue, "dispatch_outbox_events", dispatch_without_redis)
    monkeypatch.setattr(audit, "_immu", _MemoryLedger())
    with TestClient(app) as c:
        created = c.post(
            "/api/cases", json={"pseudonym": "P01", "orthanc_study_uid": "1.2.840.1"})
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        with Session(engine) as s:
            case = s.get(models.Case, cid)
            case.status = CaseStatus.series_pending
            for uid, description, role in (
                ("1.2.1", "SAG T1 MP2RAGE UNI", SeriesRole.t1_uni),
                ("1.2.2", "SAG T1 MP2RAGE INV1", SeriesRole.t1_inv1),
                ("1.2.3", "SAG T1 MP2RAGE INV2", SeriesRole.t1_inv2),
                ("1.2.4", "AX_T1_MPRAGE", SeriesRole.t1_mprage),
            ):
                s.add(models.Series(case_id=cid, orthanc_series_uid=uid,
                                    series_description=description, proposed_role=role))
            s.add(case)
            s.commit()

        roles = {"1.2.1": "t1_uni", "1.2.2": "t1_inv1", "1.2.3": "t1_inv2",
                 "1.2.4": "t1_mprage"}
        r = c.post(f"/api/cases/{cid}/series/confirm", json={"roles": roles})
        assert r.status_code == 200, r.text

        r = c.post(f"/api/cases/{cid}/recipe", json={"workup": "fcd"})
        assert r.status_code == 200, r.text
        assert r.json()["summary"]["will_run"] == 4

        confirmed = c.post(f"/api/cases/{cid}/recipe/confirm")
        assert confirmed.status_code == 200, confirmed.text
        built = [x for x in confirmed.json() if x["status"] == "queued"]
        assert len(built) == 4
        repeated = c.post(f"/api/cases/{cid}/recipe/confirm")
        assert repeated.status_code == 200
        assert {row["id"] for row in repeated.json()} == {row["id"] for row in confirmed.json()}
        with Session(engine) as s:
            run_events = s.exec(select(OutboxEvent).where(
                OutboxEvent.topic == "run.enqueue")).all()
            assert len(run_events) == 4
            run = s.get(models.Run, built[0]["id"])
            run.status = RunStatus.review_ready
            s.add(run)
            s.add(Result(run_id=run.id, n_clusters=0,
                         output_manifest={"schema_version": 1, "files": []}))
            failed = s.get(models.Run, built[1]["id"])
            failed.status = RunStatus.failed
            failed.attempt = 1
            failed.status_reason = "controlled test failure"
            s.add(failed)
            s.commit()

        retried = c.post(f"/api/admin/runs/{built[1]['id']}/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "queued"
        with Session(engine) as s:
            run_events = s.exec(select(OutboxEvent).where(
                OutboxEvent.topic == "run.enqueue")).all()
            assert len(run_events) == 5

        r = c.post(f"/api/runs/{built[0]['id']}/adjudication",
                   json={"agree": True, "confidence": 4})
        assert r.status_code == 200, r.text
        assert r.json()["reviewer"] == "user:development"

        v = c.post("/api/audit/verify").json()
        assert v["ok"] is True and v["fully_verified"] is True
        assert v["count"] >= 5


def test_expired_worker_claim_is_failed_and_unlocked(monkeypatch):
    # This module intentionally preserves the database between workflow tests, so
    # the fake ledger must also preserve immudb's globally monotonic transaction IDs.
    monkeypatch.setattr(audit, "_immu", _MemoryLedger(first_tx_id=100_000))
    with Session(engine) as session:
        case = models.Case(
            pseudonym="LEASE01", created_by="user:test", orthanc_study_uid="9.9.9001",
            status=CaseStatus.running,
        )
        session.add(case)
        session.flush()
        recipe = models.Recipe(case_id=case.id, workup=Workup.fcd, spec=[], version=1)
        session.add(recipe)
        session.flush()
        run = models.Run(
            case_id=case.id, recipe_id=recipe.id, detector_id="map", logical_key="lease-test",
            status=RunStatus.preprocessing, claim_token="expired-token",
            heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        session.add(run)
        session.add(models.Job(run_id=run.id, stage="run", status="preprocessing"))
        session.commit()

        result = queue.reap_stale_runs(session)
        session.refresh(run)
        session.refresh(case)
        assert result == {"reaped": 1, "cases_failed": 1}
        assert run.status == RunStatus.failed
        assert run.status_reason == "worker_lease_expired"
        assert run.claim_token is None and run.lease_expires_at is None
        assert case.status == CaseStatus.failed


def test_recipe_confirmation_rejects_changed_harmonization_assignment():
    with Session(engine) as session:
        case = models.Case(
            pseudonym="HARMLEASE", created_by="user:test", orthanc_study_uid="9.9.9101",
            status=CaseStatus.recipe_pending,
        )
        session.add(case)
        source = models.Series(
            case_id=case.id, orthanc_series_uid="9.9.9101.1",
            confirmed_role=SeriesRole.t1_mprage, fingerprint="f" * 64,
            acquisition={"manufacturer": "test"},
        )
        profile = models.HarmonizationProfile(
            code="MAPTEST", version=1, name="MAP test", method="map_normative",
            detector_id=DetectorId.map, selector={"manufacturer": "test"},
            artifact_manifest={"files": [{"path": "placeholder", "sha256": "0" * 64}]},
            parameters={}, status=HarmonizationProfileStatus.active, created_by="user:test",
        )
        session.add(source)
        session.add(profile)
        session.flush()
        assignment = models.HarmonizationAssignment(
            case_id=case.id, profile_id=profile.id, detector_id=DetectorId.map,
            source_series_uid=source.orthanc_series_uid,
            acquisition_fingerprint=source.fingerprint,
            status=HarmonizationStatus.confirmed, confirmed_by="user:test",
        )
        session.add(assignment)
        session.commit()
        entry = {
            "detector_id": "map", "source_series_uid": source.orthanc_series_uid,
            "params": {"harmonization": run_harmonization_contract(profile, assignment)},
        }
        _validate_recipe_harmonization_contract(session, case.id, entry)

        assignment.override_reason = "approval changed after the recipe was built"
        session.add(assignment)
        session.commit()
        with pytest.raises(HTTPException, match="assignment/profile changed"):
            _validate_recipe_harmonization_contract(session, case.id, entry)


def test_served_artifact_must_still_match_provenance(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"signed research result")
    result = Result(
        run_id="artifact-contract",
        output_manifest={"files": [{
            "path": "report.pdf",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }]},
    )
    assert _artifact_matches_manifest(result, Path("report.pdf"), path)
    path.write_bytes(b"modified result")
    assert not _artifact_matches_manifest(result, Path("report.pdf"), path)


def test_gpu_status_never_exposes_claim_fencing_token():
    run_id = "12345678-1234-4abc-8def-123456789abc"
    assert _public_gpu_owner(f"{run_id}:secret-claim-token") == run_id
    assert _public_gpu_owner("secret-claim-token") is None
