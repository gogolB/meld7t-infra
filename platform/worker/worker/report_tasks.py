"""Queued combined-report generation from immutable CaseReport snapshots."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app import audit
from app.db import engine
from app.models import CaseReport, CaseReportStatus

from .config import wsettings
from .report_pdf import MAX_FRAME_BYTES, MAX_LOGO_BYTES, render_case_report


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_frame_paths(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Copy the snapshot and resolve only hash-matching image artifacts under meld-data."""
    root = Path(wsettings.meld_data).resolve()
    document = json.loads(json.dumps(snapshot))
    for row in document.get("runs") or []:
        verified: list[str] = []
        for item in row.pop("frame_artifacts", []) or []:
            if not isinstance(item, dict):
                continue
            relative = Path(str(item.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            unresolved = root / relative
            components = [root / Path(*relative.parts[:index])
                          for index in range(1, len(relative.parts) + 1)]
            try:
                if any(component.is_symlink() for component in components):
                    continue
                candidate = unresolved.resolve(strict=True)
            except (OSError, ValueError):
                continue
            if candidate == root or root not in candidate.parents or not candidate.is_file():
                continue
            expected = str(item.get("sha256", ""))
            try:
                raw_size = item.get("size", -1)
                if isinstance(raw_size, bool):
                    raise TypeError
                expected_size = int(raw_size)
                invalid = (
                    re.fullmatch(r"[0-9a-f]{64}", expected) is None or expected_size < 1
                    or expected_size > MAX_FRAME_BYTES
                    or candidate.stat().st_size != expected_size
                    or _sha256(candidate) != expected
                )
            except (OSError, TypeError, ValueError):
                invalid = True
            if invalid:
                continue
            verified.append({"path": str(candidate), "sha256": expected, "size": expected_size})
        row["frame_paths"] = verified
    return document


def _verified_branding(branding: dict[str, Any]) -> dict[str, Any]:
    """Bind the worker-visible logo bytes to the deployment snapshot before rendering."""
    document = dict(branding)
    expected = document.get("logo_sha256")
    expected_size = document.get("logo_size")
    declared_path = document.get("logo_path")
    if expected is None and expected_size is None and not declared_path:
        document["logo_path"] = None
        return document
    if (re.fullmatch(r"[0-9a-f]{64}", str(expected or "")) is None
            or isinstance(expected_size, bool) or not isinstance(expected_size, int)
            or expected_size < 1 or expected_size > MAX_LOGO_BYTES
            or not wsettings.branding_logo_path):
        raise ValueError("report branding logo snapshot is incomplete")
    candidate = Path(wsettings.branding_logo_path)
    if (not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file()
            or candidate.stat().st_size != expected_size or _sha256(candidate) != expected):
        raise ValueError("report branding logo differs from its immutable snapshot")
    document["logo_path"] = str(candidate.resolve(strict=True))
    return document


def _claim(report_id: str) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    with Session(engine) as session:
        statement = select(CaseReport).where(CaseReport.id == report_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        row = session.exec(statement).first()
        if row is None or row.status != CaseReportStatus.queued:
            return None
        snapshot = dict(row.snapshot or {})
        supplied = snapshot.pop("snapshot_sha256", None)
        branding = dict(row.branding or {})
        branding_sha256 = snapshot.get("branding_sha256")
        if (supplied != row.snapshot_sha256
                or _canonical_sha256(snapshot) != row.snapshot_sha256
                or branding_sha256 != _canonical_sha256(branding)):
            row.status = CaseReportStatus.failed
            row.last_error = "report_snapshot_integrity_failed"
            row.updated_at = _now()
            row.completed_at = row.updated_at
            session.add(row)
            audit.record(
                session, actor="service:worker", action="case_report.fail",
                entity_type="case_report", entity_id=row.id,
                payload={"error_code": "snapshot_integrity_failed"},
            )
            session.commit()
            return None
        row.status = CaseReportStatus.generating
        row.updated_at = _now()
        session.add(row)
        audit.record(
            session, actor="service:worker", action="case_report.generating",
            entity_type="case_report", entity_id=row.id,
            payload={"kind": row.kind.value, "version": row.version,
                     "snapshot_sha256": row.snapshot_sha256},
        )
        session.commit()
        return dict(row.snapshot), branding, row.case_id


_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _report_output(case_id: str, report_id: str) -> Path:
    """Create and validate the task-owned directory without following nested symlinks."""
    if (_SAFE_COMPONENT.fullmatch(case_id) is None
            or _SAFE_COMPONENT.fullmatch(report_id) is None):
        raise ValueError("report identifiers are unsafe path components")
    root = Path(wsettings.meld_data).resolve(strict=True)
    current = root
    for component in ("reports", case_id, report_id):
        candidate = current / component
        if candidate.is_symlink():
            raise ValueError("report output path contains a symlink")
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("report output parent is not a regular directory")
        resolved = candidate.resolve(strict=True)
        if root not in resolved.parents:
            raise ValueError("report output escapes meld-data")
        current = resolved
    output = current / "combined-report.pdf"
    if output.exists() or output.is_symlink():
        raise ValueError("report output already exists")
    return output


def _remove_unpublished_output(output: Path) -> None:
    """Remove only this report task's fenced, unreferenced artifact and empty directories."""
    try:
        root = Path(wsettings.meld_data).resolve(strict=True)
        relative = output.relative_to(root)
        components = [root / Path(*relative.parts[:index])
                      for index in range(1, len(relative.parts) + 1)]
        if any(component.is_symlink() for component in components):
            return
        candidate = output.resolve(strict=True)
        if root not in candidate.parents or not candidate.is_file():
            return
        candidate.unlink()
        reports_root = (root / "reports").resolve(strict=True)
        parent = candidate.parent
        while parent != reports_root and reports_root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    except (OSError, RuntimeError, ValueError):
        return


async def generate_case_report(_ctx, report_id: str) -> dict[str, Any]:
    claimed = _claim(report_id)
    if claimed is None:
        with Session(engine) as session:
            row = session.get(CaseReport, report_id)
            return {"report_id": report_id, "status": (row.status.value if row else "not_found")}
    snapshot, branding, case_id = claimed
    output = Path(wsettings.meld_data, "reports", case_id, report_id, "combined-report.pdf")
    try:
        output = _report_output(case_id, report_id)
        # API and host worker may mount the same approved asset at different absolute paths. Bind
        # the local bytes, not either path string, before handing it to the offline renderer.
        branding = _verified_branding(branding)
        rendered_snapshot = _verified_frame_paths(snapshot)
        manifest = await __import__("asyncio").to_thread(
            render_case_report, rendered_snapshot, branding, output,
        )
        root = Path(wsettings.meld_data).resolve()
        relative = output.resolve().relative_to(root).as_posix()
        artifact = {
            "path": relative,
            "sha256": manifest["sha256"],
            "size": manifest["size"],
            "page_count": manifest["page_count"],
            "media_type": "application/pdf",
        }
        artifact["manifest_sha256"] = _canonical_sha256(artifact)
        with Session(engine) as session:
            statement = select(CaseReport).where(CaseReport.id == report_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            row = session.exec(statement).one()
            if row.status != CaseReportStatus.generating:
                raise RuntimeError("report left generating state before completion")
            row.status = CaseReportStatus.ready
            row.report_path = relative
            row.artifact_manifest = artifact
            row.last_error = None
            row.updated_at = _now()
            row.completed_at = row.updated_at
            session.add(row)
            audit.record(
                session, actor="service:worker", action="case_report.complete",
                entity_type="case_report", entity_id=row.id,
                payload={"kind": row.kind.value, "version": row.version,
                         "snapshot_sha256": row.snapshot_sha256,
                         "artifact_sha256": artifact["sha256"],
                         "manifest_sha256": artifact["manifest_sha256"],
                         "page_count": artifact["page_count"]},
            )
            session.commit()
        return {"report_id": report_id, "status": "ready", **artifact}
    except Exception as exc:
        remove_output = False
        with Session(engine) as session:
            row = session.get(CaseReport, report_id)
            if row is not None and row.status == CaseReportStatus.generating:
                row.status = CaseReportStatus.failed
                row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                row.updated_at = _now()
                row.completed_at = row.updated_at
                session.add(row)
                audit.record(
                    session, actor="service:worker", action="case_report.fail",
                    entity_type="case_report", entity_id=report_id,
                    payload={"error_code": type(exc).__name__},
                )
                session.commit()
            # A timeout reaper can fence the row while Pillow is rendering.  Once the database
            # confirms this task did not publish a ready row, its deterministic new file is an
            # orphan and can be removed safely.
            remove_output = row is not None and row.status != CaseReportStatus.ready
        if remove_output:
            _remove_unpublished_output(output)
        return {"report_id": report_id, "status": "failed", "error": type(exc).__name__}
