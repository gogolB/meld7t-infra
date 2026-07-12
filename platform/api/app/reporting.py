"""Versioned combined-report snapshots and durable queue handoff."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from .config import settings
from .models import (
    Adjudication, Case, CaseReport, CaseReportKind, CaseReportStatus, Cluster, OutboxEvent,
    Provenance, Recipe, Result, Run, RunStatus, Series,
)


UNHARMONIZED_WARNING = (
    "UNHARMONIZED RESEARCH RESULT: no scanner/protocol harmonization profile was applied. "
    "Findings may reflect acquisition differences and must not be compared with harmonized "
    "results."
)
MAX_REPORT_LOGO_BYTES = 5 * 1024 * 1024


class ReportNotReadyError(ValueError):
    """The requested snapshot cannot yet be produced from current workflow state."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def branding_snapshot() -> dict[str, Any]:
    logo_sha256: str | None = None
    logo_size: int | None = None
    if settings.branding_logo_path:
        path = Path(settings.branding_logo_path)
        try:
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or path.stat().st_size < 1
                    or path.stat().st_size > MAX_REPORT_LOGO_BYTES):
                raise OSError("configured report logo is not a bounded regular file")
            logo_size = path.stat().st_size
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            logo_sha256 = digest.hexdigest()
        except OSError as exc:
            raise ReportNotReadyError(
                "configured report branding logo is unavailable or unsafe") from exc
    return {
        "product_name": settings.branding_product_name,
        "institution_name": settings.branding_institution_name,
        "department_name": settings.branding_department_name,
        # Absolute mount paths differ between the API container and host worker and are not part
        # of deployment identity. The content digest is stable across both boundaries.
        "logo_sha256": logo_sha256,
        "logo_size": logo_size,
        "primary_color": settings.branding_primary_color,
        "secondary_color": settings.branding_secondary_color,
        "footer_text": settings.branding_footer_text,
    }


def harmonization_public(run: Run) -> tuple[dict[str, Any], list[str]]:
    raw = (run.params or {}).get("harmonization") or {}
    if raw.get("profile_id"):
        return ({
            "mode": "harmonized",
            "applied": True,
            "profile": {key: raw.get(key) for key in ("code", "version", "method")},
        }, [])
    if raw.get("mode") == "not_applicable":
        return ({"mode": "not_applicable", "applied": False, "profile": None}, [])
    return ({"mode": "unharmonized", "applied": False, "profile": None},
            [UNHARMONIZED_WARNING])


def verified_derived_series(result: Result | None) -> tuple[list[dict[str, Any]], str]:
    """Return a small public series map only when its persisted digest still verifies.

    Historical results created before the generalized derived-DICOM contract legitimately have
    no manifest.  A partial or hash-mismatched new contract is different: callers must not present
    unverified database JSON as an authoritative DICOM publication map.
    """
    if result is None:
        return [], "unavailable"
    output = result.output_manifest if isinstance(result.output_manifest, dict) else {}
    document = output.get("derived_series_manifest")
    expected = output.get("derived_series_manifest_sha256")
    if document is None and expected is None:
        return [], "unavailable"
    if (not isinstance(document, dict)
            or re.fullmatch(r"[0-9a-f]{64}", str(expected or "")) is None
            or _canonical_sha256(document) != expected
            or document.get("schema_version") != 1
            or document.get("study_uid") != result.orthanc_study_uid
            or not isinstance(document.get("series"), list)):
        return [], "failed"

    public: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in document["series"]:
        if not isinstance(item, dict):
            return [], "failed"
        uid = item.get("series_uid")
        count = item.get("sop_count")
        if (not isinstance(uid, str) or len(uid) > 64
                or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", uid) is None
                or uid in seen or isinstance(count, bool) or not isinstance(count, int)
                or count < 1):
            return [], "failed"
        role, modality, description = (item.get(key) for key in (
            "role", "modality", "description"))
        if (not isinstance(role, str) or not role or len(role) > 128
                or not isinstance(modality, str) or len(modality) > 16
                or not isinstance(description, str) or len(description) > 256):
            return [], "failed"
        seen.add(uid)
        public.append({
            "series_uid": uid,
            "role": role,
            "modality": modality,
            "description": description,
            "sop_count": count,
        })
    if not public:
        return [], "failed"
    return public, "verified"


def _series(row: Series) -> dict[str, Any]:
    return {
        "id": row.id,
        "orthanc_series_uid": row.orthanc_series_uid,
        "series_description": row.series_description,
        "modality": row.modality,
        "proposed_role": _value(row.proposed_role),
        "confirmed_role": _value(row.confirmed_role),
        "instance_count": row.instance_count,
    }


def _cluster(row: Cluster) -> dict[str, Any]:
    return {
        "id": row.id, "index": row.index, "hemi": row.hemi, "location": row.location,
        "size": row.size, "confidence": row.confidence, "saliency": row.saliency,
    }


def _adjudication(row: Adjudication, detector_id: str) -> dict[str, Any]:
    return {
        "id": row.id, "run_id": row.run_id, "detector_id": detector_id,
        "reviewer": row.reviewer, "ts": row.ts.isoformat(), "agree": row.agree,
        "confidence": row.confidence, "ground_truth": row.ground_truth,
        "notes": row.notes, "supersedes": row.supersedes,
    }


def _frame_artifacts(result: Result | None) -> list[dict[str, Any]]:
    manifest = result.output_manifest if result and isinstance(result.output_manifest, dict) else {}
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        return []
    frames = []
    for item in files:
        if (isinstance(item, dict) and str(item.get("path", "")).lower().endswith(".png")
                and isinstance(item.get("sha256"), str)):
            frames.append({key: item.get(key) for key in ("path", "sha256", "size")})
    return frames[:12]


def report_evidence(session: Session, case: Case, recipe: Recipe,
                    kind: CaseReportKind) -> dict[str, Any]:
    runs = session.exec(select(Run).where(
        Run.recipe_id == recipe.id, Run.superseded_by.is_(None),
    ).order_by(Run.created_at, Run.id)).all()
    runnable = [run for run in runs if run.status != RunStatus.pending]
    if not runnable:
        raise ReportNotReadyError("case has no runnable detector results")
    if kind == CaseReportKind.preliminary:
        if any(run.status not in {RunStatus.review_ready, RunStatus.adjudicated}
               for run in runnable):
            raise ReportNotReadyError(
                "preliminary report requires every runnable result to be ready")
    elif any(run.status != RunStatus.adjudicated for run in runnable):
        raise ReportNotReadyError(
            "final report requires every runnable result to be adjudicated")

    all_warnings: list[str] = []
    out_runs: list[dict[str, Any]] = []
    out_adjudications: list[dict[str, Any]] = []
    release_digests: set[str] = set()
    for run in runs:
        result = session.exec(select(Result).where(Result.run_id == run.id)).first()
        if run.status != RunStatus.pending and result is None:
            raise ReportNotReadyError(f"ready run {run.id} has no result")
        clusters = (session.exec(select(Cluster).where(
            Cluster.result_id == result.id).order_by(Cluster.index)).all() if result else [])
        harmonization, warnings = harmonization_public(run)
        # A declared pending slot is part of the processing plan, not an unharmonized result.
        # Only warnings attached to an actual validated result belong in the report banner.
        if result is None:
            warnings = []
        all_warnings.extend(warnings)
        manifest = result.output_manifest if result and isinstance(
            result.output_manifest, dict) else {}
        derived_series, derived_integrity = verified_derived_series(result)
        if derived_integrity == "failed":
            raise ReportNotReadyError(
                f"derived DICOM series manifest integrity failed for run {run.id}")
        out_runs.append({
            "run": {
                "id": run.id, "detector_id": _value(run.detector_id),
                "detector_version": run.detector_version, "source_role": _value(run.source_role),
                "source_series_uid": run.source_series_uid,
                "status": (RunStatus.review_ready.value
                           if kind == CaseReportKind.preliminary
                           and run.status == RunStatus.adjudicated else _value(run.status)),
                "harmonization": harmonization, "warnings": warnings,
            },
            "result": ({
                "id": result.id, "n_clusters": result.n_clusters,
                "harmo_code": result.harmo_code,
                "metric_schema": manifest.get("metric_schema"),
                "detector_summary": manifest.get("detector_summary"),
                "orthanc_study_uid": result.orthanc_study_uid,
                "derived_series": derived_series,
                "derived_series_integrity": derived_integrity,
            } if result else None),
            "clusters": [_cluster(cluster) for cluster in clusters],
            "frame_artifacts": _frame_artifacts(result),
        })
        if kind == CaseReportKind.final:
            for adjudication in session.exec(select(Adjudication).where(
                    Adjudication.run_id == run.id).order_by(
                        Adjudication.ts, Adjudication.id)).all():
                out_adjudications.append(_adjudication(
                    adjudication, str(_value(run.detector_id))))
        provenance = session.exec(select(Provenance).where(Provenance.run_id == run.id)).first()
        if provenance and provenance.release_manifest_digest:
            release_digests.add(provenance.release_manifest_digest)

    return {
        "report_kind": kind.value,
        "case": {
            "id": case.id, "pseudonym": case.pseudonym, "workup": _value(case.workup),
            "status": ("review_ready" if kind == CaseReportKind.preliminary
                       and _value(case.status) == "adjudicated" else _value(case.status)),
            "orthanc_study_uid": case.orthanc_study_uid,
            "harmonization_status": _value(case.harmonization_status),
        },
        "recipe": {
            "id": recipe.id, "version": recipe.version, "spec_hash": recipe.spec_hash,
            "workup": _value(recipe.workup),
        },
        "source_series": [_series(row) for row in session.exec(select(Series).where(
            Series.case_id == case.id, Series.active.is_(True)
        ).order_by(Series.series_description, Series.orthanc_series_uid)).all()],
        "runs": out_runs,
        "adjudications": out_adjudications,
        "warnings": sorted(set(all_warnings)),
        "release_manifest_digest": (next(iter(release_digests))
                                    if len(release_digests) == 1 else None),
    }


def ensure_case_report(session: Session, case: Case, recipe: Recipe, kind: CaseReportKind,
                       *, requested_by: str) -> tuple[CaseReport, bool]:
    """Create a report snapshot/outbox exactly once per distinct evidence state."""
    evidence = report_evidence(session, case, recipe, kind)
    brand = branding_snapshot()
    branding_sha256 = _canonical_sha256(brand)
    evidence_sha256 = _canonical_sha256({"evidence": evidence, "branding": brand})
    latest = session.exec(select(CaseReport).where(
        CaseReport.case_id == case.id, CaseReport.kind == kind,
    ).order_by(CaseReport.version.desc(), CaseReport.created_at.desc())).first()
    if (latest is not None and latest.status != CaseReportStatus.failed
            and isinstance(latest.snapshot, dict)
            and latest.snapshot.get("evidence_sha256") == evidence_sha256):
        return latest, False
    version = (latest.version + 1) if latest else 1
    snapshot = {
        **evidence,
        "version": version,
        "created_at": _now().isoformat(),
        "evidence_sha256": evidence_sha256,
        "branding_sha256": branding_sha256,
    }
    snapshot_sha256 = _canonical_sha256(snapshot)
    snapshot["snapshot_sha256"] = snapshot_sha256
    row = CaseReport(
        case_id=case.id, recipe_id=recipe.id, kind=kind, version=version,
        status=CaseReportStatus.queued, snapshot=snapshot,
        snapshot_sha256=snapshot_sha256, branding=brand, requested_by=requested_by,
    )
    session.add(row)
    session.flush()
    session.add(OutboxEvent(
        dedupe_key=f"case.report.generate:{row.id}", topic="case.report.generate",
        aggregate_type="case_report", aggregate_id=row.id,
        payload={"report_id": row.id},
    ))
    return row, True


def report_public(row: CaseReport) -> dict[str, Any]:
    return {
        "id": row.id, "case_id": row.case_id, "recipe_id": row.recipe_id,
        "kind": _value(row.kind), "version": row.version, "status": _value(row.status),
        "has_report": bool(row.report_path and row.status == CaseReportStatus.ready),
        "download_url": (f"/api/cases/{row.case_id}/reports/{row.id}/pdf"
                         if row.report_path and row.status == CaseReportStatus.ready else None),
        "snapshot_sha256": row.snapshot_sha256,
        "artifact_sha256": ((row.artifact_manifest or {}).get("sha256")
                            if isinstance(row.artifact_manifest, dict) else None),
        "created_at": row.created_at, "completed_at": row.completed_at,
        "has_error": bool(row.last_error),
    }
