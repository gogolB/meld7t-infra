import hashlib
import json
import asyncio
import sys
from pathlib import Path

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "platform" / "api"), str(REPO / "platform" / "worker")]

from app.models import (
    Case, CaseReport, CaseReportKind, CaseReportStatus, CaseStatus, DetectorId, Recipe, Result,
    Run, RunStatus, Workup,
)
from worker import report_tasks, tasks


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _store_report(engine, *, branding=None):
    with Session(engine) as session:
        case = Case(pseudonym="HMRI-002", created_by="user:test", status=CaseStatus.review_ready)
        session.add(case); session.flush()
        recipe = Recipe(case_id=case.id, workup=Workup.both, spec=[], spec_hash="a" * 64)
        session.add(recipe); session.flush()
        branding = branding or {
            "product_name": "MELD 7T", "institution_name": "Houston Methodist",
            "department_name": "Houston Methodist Research Institute",
            "primary_color": "#124A7E", "secondary_color": "#749ABB",
            "footer_text": "HMRI research use only",
        }
        snapshot = {
            "report_kind": "preliminary", "version": 1,
            "created_at": "2026-07-12T12:00:00+00:00", "evidence_sha256": "b" * 64,
            "branding_sha256": _digest(branding),
            "case": {"id": case.id, "pseudonym": case.pseudonym, "workup": "both"},
            "recipe": {"id": recipe.id, "spec_hash": recipe.spec_hash},
            "source_series": [], "runs": [], "adjudications": [], "warnings": [],
            "release_manifest_digest": "c" * 64,
        }
        snapshot_sha256 = _digest(snapshot)
        snapshot["snapshot_sha256"] = snapshot_sha256
        report = CaseReport(
            case_id=case.id, recipe_id=recipe.id, kind=CaseReportKind.preliminary,
            status=CaseReportStatus.queued, snapshot=snapshot,
            snapshot_sha256=snapshot_sha256, branding=branding, requested_by="service:test",
        )
        session.add(report); session.commit()
        return report.id, case.id


def _configure(tmp_path: Path, monkeypatch, name="reports.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(report_tasks, "engine", engine)
    monkeypatch.setattr(report_tasks.wsettings, "meld_data", str(tmp_path))
    monkeypatch.setattr(report_tasks.wsettings, "branding_logo_path", None)
    monkeypatch.setattr(report_tasks.audit, "record", lambda *args, **kwargs: None)
    return engine


def test_queued_report_is_rendered_and_manifested(tmp_path: Path, monkeypatch):
    engine = _configure(tmp_path, monkeypatch)
    report_id, _case_id = _store_report(engine)

    result = asyncio.run(report_tasks.generate_case_report(None, report_id))
    assert result["status"] == "ready"
    with Session(engine) as session:
        stored = session.get(CaseReport, report_id)
        assert stored.status == CaseReportStatus.ready
        path = tmp_path / stored.report_path
        assert path.read_bytes().startswith(b"%PDF")
        assert stored.artifact_manifest["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert stored.artifact_manifest["manifest_sha256"]


def test_report_claim_rejects_branding_not_bound_to_snapshot(tmp_path: Path, monkeypatch):
    engine = _configure(tmp_path, monkeypatch, "tamper.db")
    report_id, _case_id = _store_report(engine)
    with Session(engine) as session:
        row = session.get(CaseReport, report_id)
        row.branding = {**row.branding, "institution_name": "Tampered Institution"}
        session.add(row); session.commit()

    result = asyncio.run(report_tasks.generate_case_report(None, report_id))
    assert result["status"] == "failed"
    with Session(engine) as session:
        row = session.get(CaseReport, report_id)
        assert row.status == CaseReportStatus.failed
        assert row.last_error == "report_snapshot_integrity_failed"
        assert row.completed_at is not None


def test_report_rejects_logo_bytes_changed_after_snapshot(tmp_path: Path, monkeypatch):
    engine = _configure(tmp_path, monkeypatch, "logo-tamper.db")
    logo = tmp_path / "report-logo.png"
    original = b"approved-logo-bytes"
    logo.write_bytes(original)
    branding = {
        "product_name": "MELD 7T", "institution_name": "Houston Methodist",
        "department_name": "Houston Methodist Research Institute",
        "primary_color": "#124A7E", "secondary_color": "#749ABB",
        "footer_text": "HMRI research use only",
        "logo_sha256": hashlib.sha256(original).hexdigest(), "logo_size": len(original),
    }
    report_id, case_id = _store_report(engine, branding=branding)
    monkeypatch.setattr(report_tasks.wsettings, "branding_logo_path", str(logo))
    logo.write_bytes(b"replacement-logo")

    result = asyncio.run(report_tasks.generate_case_report(None, report_id))
    assert result["status"] == "failed"
    assert not (tmp_path / "reports" / case_id / report_id / "combined-report.pdf").exists()
    with Session(engine) as session:
        row = session.get(CaseReport, report_id)
        assert row.status == CaseReportStatus.failed
        assert "logo differs" in row.last_error


def test_fenced_renderer_removes_its_unpublished_pdf(tmp_path: Path, monkeypatch):
    engine = _configure(tmp_path, monkeypatch, "fenced.db")
    report_id, case_id = _store_report(engine)

    def render_then_reap(_snapshot, _branding, output):
        output.write_bytes(b"%PDF orphan")
        with Session(engine) as session:
            row = session.get(CaseReport, report_id)
            row.status = CaseReportStatus.failed
            row.last_error = "generation_timeout"
            session.add(row); session.commit()
        return {"sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "size": output.stat().st_size, "page_count": 1}

    monkeypatch.setattr(report_tasks, "render_case_report", render_then_reap)
    result = asyncio.run(report_tasks.generate_case_report(None, report_id))
    assert result["status"] == "failed"
    assert not (tmp_path / "reports" / case_id / report_id / "combined-report.pdf").exists()


def test_report_frames_reject_intermediate_symlinks(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(report_tasks.wsettings, "meld_data", str(tmp_path))
    real = tmp_path / "real"
    real.mkdir()
    frame = real / "frame.png"
    frame.write_bytes(b"not-an-image-but-hash-valid")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    snapshot = {"runs": [{"frame_artifacts": [{
        "path": "linked/frame.png", "sha256": hashlib.sha256(frame.read_bytes()).hexdigest(),
        "size": frame.stat().st_size,
    }]}]}

    verified = report_tasks._verified_frame_paths(snapshot)
    assert verified["runs"][0]["frame_paths"] == []


def test_report_frames_reject_oversized_manifest_artifacts(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(report_tasks.wsettings, "meld_data", str(tmp_path))
    frame = tmp_path / "oversized.png"
    with frame.open("wb") as handle:
        handle.truncate(report_tasks.MAX_FRAME_BYTES + 1)
    snapshot = {"runs": [{"frame_artifacts": [{
        "path": frame.name, "sha256": "a" * 64, "size": frame.stat().st_size,
    }]}]}
    verified = report_tasks._verified_frame_paths(snapshot)
    assert verified["runs"][0]["frame_paths"] == []


def test_terminal_case_transition_creates_automatic_preliminary_snapshot(
        tmp_path: Path, monkeypatch):
    engine = _configure(tmp_path, monkeypatch, "automatic.db")
    monkeypatch.setattr(tasks, "engine", engine)
    with Session(engine) as session:
        case = Case(pseudonym="HMRI-AUTO", created_by="service:test", status=CaseStatus.running)
        session.add(case); session.flush()
        recipe = Recipe(
            case_id=case.id, workup=Workup.both, spec=[], spec_hash="a" * 64,
            confirmed_at=case.created_at,
        )
        session.add(recipe); session.flush()
        for detector in (DetectorId.map, DetectorId.meld_fcd):
            run = Run(
                case_id=case.id, recipe_id=recipe.id, detector_id=detector,
                logical_key=f"automatic-{detector.value}", status=RunStatus.review_ready,
                params={"harmonization": {"mode": "unharmonized"}},
            )
            session.add(run); session.flush()
            session.add(Result(run_id=run.id, n_clusters=0, output_manifest={"files": []}))
        session.flush()

        tasks._maybe_finish_case(session, case.id)
        session.commit()
        assert session.get(Case, case.id).status == CaseStatus.review_ready
        reports = session.exec(select(CaseReport)).all()
        assert len(reports) == 1
        assert reports[0].kind == CaseReportKind.preliminary
        assert reports[0].status == CaseReportStatus.queued
