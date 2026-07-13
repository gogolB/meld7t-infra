import hashlib
import inspect
import json
import asyncio
from datetime import datetime, timedelta, timezone

from sqlmodel import SQLModel, Session, create_engine, select
import pytest

from app import audit, queue, routes
from app.auth import Principal, Role, require_reviewer
from app.models import (
    Adjudication, Case, CaseReport, CaseReportKind, CaseReportStatus, CaseStatus, Cluster,
    DetectorId, HarmonizationStatus, OutboxEvent, Provenance, Recipe, Result, Run, RunStatus,
    Series, SeriesRole, Workup,
)
from app.reporting import (
    ReportNotReadyError, UNHARMONIZED_WARNING, ensure_case_report, verified_derived_series,
)
from app.routes import (
    AdjudicationCreate, _case_report_abs, _recipe_public, _result_public, adjudicate,
    request_case_report,
)


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _fixture():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    case = Case(
        pseudonym="HMRI-001", created_by="user:researcher", orthanc_study_uid="1.2.3",
        status=CaseStatus.review_ready, workup=Workup.both,
        harmonization_status=HarmonizationStatus.unassigned,
    )
    session.add(case); session.flush()
    session.add(Series(
        case_id=case.id, orthanc_series_uid="1.2.3.4", series_description="MP2RAGE UNI",
        modality="MR", proposed_role=SeriesRole.t1_uni, confirmed_role=SeriesRole.t1_uni,
        instance_count=240,
    ))
    recipe = Recipe(
        case_id=case.id, workup=Workup.both, spec=[], version=1, spec_hash="a" * 64,
        confirmed_at=case.created_at,
    )
    session.add(recipe); session.flush()
    run = Run(
        case_id=case.id, recipe_id=recipe.id, detector_id=DetectorId.map,
        source_role=SeriesRole.t1_uni, source_series_uid="1.2.3.4",
        params={"harmonization": {"mode": "unharmonized", "reason": "confirmed"}},
        logical_key="reporting-map", status=RunStatus.review_ready,
    )
    session.add(run); session.flush()
    result = Result(run_id=run.id, n_clusters=1, output_manifest={
        "metric_schema": {"confidence": {"unit": "z"}}, "files": [],
    })
    session.add(result); session.flush()
    session.add(Cluster(
        result_id=result.id, index=1, hemi="left", location="frontal",
        size=0.6, confidence=5.1,
    ))
    session.add(Provenance(run_id=run.id, release_manifest_digest="b" * 64))
    session.commit()
    return session, case, recipe, run


def test_preliminary_report_snapshots_unharmonized_warning_and_outbox():
    session, case, recipe, run = _fixture()
    report, created = ensure_case_report(
        session, case, recipe, CaseReportKind.preliminary,
        requested_by="service:worker",
    )
    assert created is True
    assert report.snapshot["warnings"] == [UNHARMONIZED_WARNING]
    assert report.snapshot["runs"][0]["run"]["harmonization"]["mode"] == "unharmonized"
    assert len(report.snapshot_sha256) == 64
    assert report.snapshot["branding_sha256"]
    assert session.exec(select(OutboxEvent).where(
        OutboxEvent.aggregate_id == report.id)).one().topic == "case.report.generate"

    same, created_again = ensure_case_report(
        session, case, recipe, CaseReportKind.preliminary,
        requested_by="user:reviewer",
    )
    assert created_again is False and same.id == report.id

    run.status = RunStatus.adjudicated
    case.status = CaseStatus.adjudicated
    session.add(run); session.add(case)
    session.add(Adjudication(run_id=run.id, reviewer="user:reviewer", agree=True))
    session.commit()
    still_preliminary, created_after_review = ensure_case_report(
        session, case, recipe, CaseReportKind.preliminary,
        requested_by="user:reviewer",
    )
    assert created_after_review is False and still_preliminary.id == report.id
    assert still_preliminary.snapshot["adjudications"] == []

    report.status = CaseReportStatus.failed
    session.add(report); session.commit()
    retried, retry_created = ensure_case_report(
        session, case, recipe, CaseReportKind.preliminary,
        requested_by="user:reviewer",
    )
    assert retry_created is True and retried.version == 2
    session.close()


def test_final_report_requires_then_snapshots_adjudication():
    session, case, recipe, run = _fixture()
    with pytest.raises(ReportNotReadyError, match="adjudicated"):
        ensure_case_report(
            session, case, recipe, CaseReportKind.final,
            requested_by="user:reviewer",
        )
    run.status = RunStatus.adjudicated
    session.add(run)
    session.add(Adjudication(
        run_id=run.id, reviewer="user:reviewer", agree=True, confidence=4,
        notes="Research review complete",
    ))
    session.commit()
    report, created = ensure_case_report(
        session, case, recipe, CaseReportKind.final,
        requested_by="user:reviewer",
    )
    assert created is True
    assert report.snapshot["report_kind"] == "final"
    assert report.snapshot["adjudications"][0]["reviewer"] == "user:reviewer"
    session.close()


def test_last_adjudication_automatically_snapshots_final_report(monkeypatch):
    session, case, recipe, first = _fixture()
    second = Run(
        case_id=case.id, recipe_id=recipe.id, detector_id=DetectorId.meld_fcd,
        source_role=SeriesRole.t1_uni, source_series_uid="1.2.3.4",
        params={"harmonization": {"mode": "unharmonized"}},
        logical_key="reporting-meld", status=RunStatus.review_ready,
    )
    session.add(second); session.flush()
    session.add(Result(run_id=second.id, n_clusters=0, output_manifest={"files": []}))
    session.commit()
    monkeypatch.setattr(audit, "record", lambda *args, **kwargs: None)
    principal = Principal(
        subject="reviewer", roles=frozenset({Role.reviewer}),
        auth_method="development_bypass", request_id="report-final-test",
    )

    adjudicate(first.id, AdjudicationCreate(agree=True, confidence=4), principal, session)
    assert session.get(Case, case.id).status == CaseStatus.review_ready
    adjudicate(second.id, AdjudicationCreate(agree=True, confidence=4), principal, session)
    final = session.exec(select(CaseReport).where(
        CaseReport.case_id == case.id, CaseReport.kind == CaseReportKind.final)).one()
    assert session.get(Case, case.id).status == CaseStatus.adjudicated
    assert final.status == CaseReportStatus.queued
    assert len(final.snapshot["adjudications"]) == 2
    session.close()


def test_pending_plan_slot_is_not_reported_as_a_result_or_unharmonized_warning():
    session, case, recipe, run = _fixture()
    run.params = {"harmonization": {
        "profile_id": "profile-1", "code": "HMRI7T", "version": 1,
        "method": "map_normative",
    }}
    session.add(run)
    session.add(Run(
        case_id=case.id, recipe_id=recipe.id, detector_id=DetectorId.hippunfold,
        source_role=SeriesRole.t2, source_series_uid="1.2.3.9",
        params={"harmonization": {"mode": "unharmonized"}},
        logical_key="reporting-pending-hs", status=RunStatus.pending,
    ))
    session.commit()

    report, _ = ensure_case_report(
        session, case, recipe, CaseReportKind.preliminary,
        requested_by="service:worker",
    )
    pending = next(row for row in report.snapshot["runs"]
                   if row["run"]["status"] == "pending")
    assert report.snapshot["warnings"] == []
    assert pending["result"] is None
    assert pending["run"]["warnings"] == []
    session.close()


def test_derived_series_public_map_requires_matching_manifest_hash():
    session, _case, _recipe, run = _fixture()
    result = session.exec(select(Result).where(Result.run_id == run.id)).one()
    result.orthanc_study_uid = "2.25.100"
    document = {
        "schema_version": 1,
        "study_uid": "2.25.100",
        "series": [{
            "series_uid": "2.25.101", "role": "map_threshold_segmentation",
            "modality": "SEG", "description": "MAP threshold", "sop_count": 1,
        }],
    }
    result.output_manifest = {
        **(result.output_manifest or {}),
        "derived_series_manifest": document,
        "derived_series_manifest_sha256": _digest(document),
    }
    session.add(result); session.commit()
    public, integrity = verified_derived_series(result)
    assert integrity == "verified" and public[0]["role"] == "map_threshold_segmentation"

    result.output_manifest["derived_series_manifest"]["series"][0]["role"] = "tampered"
    public, integrity = verified_derived_series(result)
    assert public == [] and integrity == "failed"
    api_result = _result_public(result)
    assert api_result["derived_series_integrity"] == "failed"
    assert api_result["orthanc_study_uid"] is None
    assert "has_report" not in api_result
    session.close()


def test_manual_report_request_is_reviewer_gated():
    dependency = inspect.signature(request_case_report).parameters["principal"].default
    assert dependency.dependency is require_reviewer


def test_detector_native_pdf_has_no_public_route():
    assert all(route.path != "/runs/{run_id}/report" for route in routes.router.routes)


def test_public_recipe_binds_each_series_to_its_source_study():
    session, _case, recipe, _run = _fixture()
    recipe.spec = [{
        "detector_id": "map", "detector_label": "MAP", "status": "created",
        "params": {
            "series_uids": {"t1_uni": "1.2.3.4"},
            "acquisition_manifest": {"study_uid": "1.2.3"},
            "harmonization": {"mode": "unharmonized"},
        },
    }]
    assert _recipe_public(recipe)["spec"][0]["inputs"] == [{
        "study_uid": "1.2.3", "role": "t1_uni", "series_uid": "1.2.3.4",
    }]
    session.close()


def test_report_handoff_reconciler_recreates_missing_outbox_event():
    session, case, recipe, _run = _fixture()
    report, _ = ensure_case_report(
        session, case, recipe, CaseReportKind.preliminary,
        requested_by="service:worker",
    )
    event = session.exec(select(OutboxEvent).where(
        OutboxEvent.aggregate_id == report.id)).one()
    session.delete(event); session.commit()

    counts = asyncio.run(queue.reconcile_case_reports(session))
    recreated = session.exec(select(OutboxEvent).where(
        OutboxEvent.aggregate_id == report.id)).one()
    assert counts["created"] == 1
    assert recreated.topic == "case.report.generate"
    session.close()


def test_stale_report_reaper_fences_generation_and_sets_terminal_time(monkeypatch):
    session, case, recipe, _run = _fixture()
    report, _ = ensure_case_report(
        session, case, recipe, CaseReportKind.preliminary,
        requested_by="service:worker",
    )
    report.status = CaseReportStatus.generating
    report.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.add(report); session.commit()
    monkeypatch.setattr(audit, "record", lambda *args, **kwargs: None)

    counts = queue.reap_stale_case_reports(session)
    session.refresh(report)
    assert counts == {"reaped": 1}
    assert report.status == CaseReportStatus.failed
    assert report.last_error == "generation_timeout"
    assert report.completed_at is not None
    session.close()


def test_report_download_rejects_intermediate_symlink(tmp_path, monkeypatch):
    target = tmp_path / "real" / "case-1" / "report-1"
    target.mkdir(parents=True)
    artifact_path = target / "combined-report.pdf"
    artifact_path.write_bytes(b"%PDF test")
    (tmp_path / "reports").symlink_to(tmp_path / "real", target_is_directory=True)
    artifact = {
        "path": "reports/case-1/report-1/combined-report.pdf",
        "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "size": artifact_path.stat().st_size,
        "page_count": 1,
        "media_type": "application/pdf",
    }
    artifact["manifest_sha256"] = _digest(artifact)
    report = CaseReport(
        case_id="case-1", recipe_id="recipe-1", kind=CaseReportKind.preliminary,
        status=CaseReportStatus.ready, snapshot={}, snapshot_sha256="a" * 64,
        branding={}, requested_by="service:test", report_path=artifact["path"],
        artifact_manifest=artifact,
    )
    monkeypatch.setattr(routes.settings, "meld_data", str(tmp_path))
    assert _case_report_abs(report) is None
