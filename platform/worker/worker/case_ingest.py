"""Bounded routine DICOM ZIP validation, main-Orthanc import, and role proposal."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

import pydicom
import requests
from pydicom.uid import MediaStorageDirectoryStorage
from sqlalchemy import text
from sqlmodel import Session, select

from app import audit, upload_receipt
from app.config import settings as app_settings
from app.db import engine
from app.dicom_policy import _exact_acquisition
from app.harmonization import acquisition_fingerprint, canonical_acquisition, sha256_file
from app.models import (
    Case, CaseStatus, CaseUpload, CaseUploadStatus, HarmonizationStatus, Series,
)
from app.orthanc import propose_role
from app.storage import storage_health

from .config import wsettings


_UID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
_SAFE_ERROR_CODES = {
    "archive_entry_count_limit_exceeded",
    "archive_expanded_size_limit_exceeded",
    "archive_entry_size_mismatch",
    "archive_contains_duplicate_path",
    "archive_is_not_zip",
    "archive_is_unsafe",
    "archive_is_encrypted",
    "case_upload_storage_unavailable",
    "dicom_instance_size_limit_exceeded",
    "dicom_uid_contract_invalid",
    "dicom_file_meta_mismatch",
    "duplicate_sop_instance_uid",
    "inconsistent_series_metadata",
    "mixed_patient_upload",
    "mixed_study_upload",
    "orthanc_duplicate_instance_index",
    "orthanc_import_failed",
    "orthanc_import_response_invalid",
    "orthanc_instance_changed",
    "orthanc_study_closure_mismatch",
    "orthanc_unavailable",
    "patient_identifier_missing",
    "receipt_contract_invalid",
    "sop_instance_already_exists",
    "staged_upload_integrity_failed",
    "study_already_registered",
    "study_instance_already_exists",
    "upload_contains_no_dicom_instances",
    "upload_contains_non_dicom_file",
}


def _fail(code: str) -> None:
    raise ValueError(code)


def _safe_error_code(exc: BaseException) -> str:
    value = str(exc)
    return value if value in _SAFE_ERROR_CODES else "case_upload_worker_error"


def _orthanc_rest_root() -> str:
    """Derive the authenticated host-facing Orthanc REST root from DICOMweb config."""
    parts = urlsplit(app_settings.orthanc_dicomweb)
    path = parts.path.rstrip("/")
    if not parts.scheme or not parts.netloc or not path.endswith("/dicom-web"):
        raise RuntimeError("orthanc_unavailable")
    return urlunsplit((parts.scheme, parts.netloc, path[:-len("/dicom-web")], "", ""))


def _declared_expanded_size(upload: Path) -> int:
    if not zipfile.is_zipfile(upload):
        _fail("archive_is_not_zip")
    total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(upload) as archive:
            infos = archive.infolist()
            if len(infos) > wsettings.case_upload_max_files:
                _fail("archive_entry_count_limit_exceeded")
            for info in infos:
                name = info.filename
                relative = PurePosixPath(name)
                mode = info.external_attr >> 16
                if (not name or "\\" in name or "\0" in name or relative.is_absolute()
                        or ".." in relative.parts or stat.S_ISLNK(mode)
                        or info.flag_bits & 0x1):
                    _fail("archive_is_unsafe" if not info.flag_bits & 0x1
                          else "archive_is_encrypted")
                normalized = relative.as_posix()
                if normalized in seen:
                    _fail("archive_contains_duplicate_path")
                seen.add(normalized)
                if info.is_dir():
                    continue
                total += info.file_size
                if total > wsettings.case_upload_max_expanded_bytes:
                    _fail("archive_expanded_size_limit_exceeded")
    except zipfile.BadZipFile:
        _fail("archive_is_not_zip")
    return total


def _extract_archive(upload: Path, work: Path) -> list[Path]:
    """Extract only regular entries under ``work`` after central-directory validation."""
    _declared_expanded_size(upload)
    paths: list[Path] = []
    try:
        with zipfile.ZipFile(upload) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = PurePosixPath(info.filename)
                target = work.joinpath(*relative.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    with archive.open(info) as source, target.open("xb") as destination:
                        written = 0
                        while chunk := source.read(1024 * 1024):
                            written += len(chunk)
                            if written > info.file_size:
                                _fail("archive_entry_size_mismatch")
                            destination.write(chunk)
                        if written != info.file_size:
                            _fail("archive_entry_size_mismatch")
                        destination.flush()
                        os.fsync(destination.fileno())
                except (RuntimeError, zipfile.BadZipFile):
                    _fail("archive_entry_size_mismatch")
                paths.append(target)
    except zipfile.BadZipFile:
        _fail("archive_is_not_zip")
    return paths


def _clean_text(value, *, maximum: int) -> str | None:
    if value is None:
        return None
    result = " ".join(str(value).split())
    return result[:maximum] if result else None


def _inspect_dicom(path: Path) -> dict:
    size = path.stat().st_size
    if size <= 0 or size > wsettings.case_upload_max_instance_bytes:
        _fail("dicom_instance_size_limit_exceeded")
    try:
        dataset = pydicom.dcmread(path, stop_before_pixels=True, force=False)
    except Exception:
        _fail("upload_contains_non_dicom_file")
    sop_class_uid = str(getattr(dataset, "SOPClassUID", "") or
                        getattr(dataset.file_meta, "MediaStorageSOPClassUID", ""))
    if sop_class_uid == str(MediaStorageDirectoryStorage):
        return {"dicomdir": True}

    identifiers = {
        "sop_instance_uid": str(getattr(dataset, "SOPInstanceUID", "")),
        "series_instance_uid": str(getattr(dataset, "SeriesInstanceUID", "")),
        "study_instance_uid": str(getattr(dataset, "StudyInstanceUID", "")),
    }
    transfer_syntax = str(getattr(dataset.file_meta, "TransferSyntaxUID", ""))
    if (any(_UID_RE.fullmatch(value) is None or len(value) > 64
            for value in (*identifiers.values(), sop_class_uid, transfer_syntax))):
        _fail("dicom_uid_contract_invalid")
    meta_sop = str(getattr(dataset.file_meta, "MediaStorageSOPInstanceUID", ""))
    meta_class = str(getattr(dataset.file_meta, "MediaStorageSOPClassUID", ""))
    if ((meta_sop and meta_sop != identifiers["sop_instance_uid"])
            or (meta_class and meta_class != sop_class_uid)):
        _fail("dicom_file_meta_mismatch")
    patient_id = str(getattr(dataset, "PatientID", "")).strip()
    issuer = str(getattr(dataset, "IssuerOfPatientID", "")).strip()
    if (not patient_id or len(patient_id) > 256 or len(issuer) > 256
            or any(char in patient_id + issuer for char in "\r\n\0")):
        _fail("patient_identifier_missing")
    acquisition = _exact_acquisition(dataset)
    return {
        **identifiers,
        "dicomdir": False,
        "patient_key": (patient_id, issuer),
        "sop_class_uid": sop_class_uid,
        "transfer_syntax": transfer_syntax,
        "modality": _clean_text(getattr(dataset, "Modality", None), maximum=16),
        "series_description": _clean_text(
            getattr(dataset, "SeriesDescription", None), maximum=256),
        "image_type": [str(value)[:128] for value in (
            getattr(dataset, "ImageType", None) or [])],
        "acquisition": acquisition,
        "sha256": sha256_file(path),
        "size": size,
        "path": path,
    }


def _validate_archive(upload: Path, work: Path) -> tuple[list[dict], list[dict], int]:
    paths = _extract_archive(upload, work)
    prepared: list[dict] = []
    dicomdir_count = 0
    for path in paths:
        metadata = _inspect_dicom(path)
        if metadata["dicomdir"]:
            dicomdir_count += 1
        else:
            prepared.append(metadata)
    if not prepared:
        _fail("upload_contains_no_dicom_instances")
    if len({item["study_instance_uid"] for item in prepared}) != 1:
        _fail("mixed_study_upload")
    if len({item["patient_key"] for item in prepared}) != 1:
        _fail("mixed_patient_upload")
    sop_uids = [item["sop_instance_uid"] for item in prepared]
    if len(sop_uids) != len(set(sop_uids)):
        _fail("duplicate_sop_instance_uid")

    summaries: list[dict] = []
    series_uids = sorted({item["series_instance_uid"] for item in prepared})
    for series_uid in series_uids:
        members = [item for item in prepared if item["series_instance_uid"] == series_uid]
        descriptions = sorted({item["series_description"] for item in members
                               if item["series_description"]})
        modalities = sorted({item["modality"] for item in members if item["modality"]})
        modality_values = {item["modality"] for item in members}
        acquisitions = [canonical_acquisition(item["acquisition"]) for item in members]
        if (len(descriptions) > 1 or len(modality_values) != 1 or not modalities
                or len({item["sop_class_uid"] for item in members}) != 1
                or not acquisitions[0]
                or any(value != acquisitions[0] for value in acquisitions[1:])):
            _fail("inconsistent_series_metadata")
        representative = members[0]
        acquisition = acquisitions[0]
        summaries.append({
            "series_uid": series_uid,
            "description": descriptions[0] if descriptions else None,
            "modality": modalities[0] if len(modalities) == 1 else None,
            "image_type": representative["image_type"],
            "acquisition": acquisition,
            "fingerprint": acquisition_fingerprint(acquisition) if acquisition else None,
            "instances": len(members),
        })
    return prepared, summaries, dicomdir_count


def _manifest(prepared: list[dict]) -> tuple[list[dict], str]:
    manifest = [{
        "sop_instance_uid": item["sop_instance_uid"],
        "series_instance_uid": item["series_instance_uid"],
        "study_instance_uid": item["study_instance_uid"],
        "sop_class_uid": item["sop_class_uid"],
        "transfer_syntax": item["transfer_syntax"],
        "sha256": item["sha256"],
        "size": item["size"],
    } for item in sorted(prepared, key=lambda value: value["sop_instance_uid"])]
    digest = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return manifest, digest


def _client() -> requests.Session:
    client = requests.Session()
    client.trust_env = False
    return client


def _find_instance(client: requests.Session, base: str, sop_uid: str) -> list[str]:
    try:
        response = client.post(
            base + "/tools/find",
            json={"Level": "Instance", "Query": {"SOPInstanceUID": sop_uid}},
            timeout=30, allow_redirects=False,
        )
        response.raise_for_status()
        value = response.json()
    except requests.RequestException:
        raise RuntimeError("orthanc_unavailable") from None
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)):
        raise RuntimeError("orthanc_import_response_invalid")
    return value


def _find_study(client: requests.Session, base: str, study_uid: str) -> list[str]:
    """Return Orthanc IDs for an exact Study UID without following redirects."""
    try:
        response = client.post(
            base + "/tools/find",
            json={"Level": "Study", "Query": {"StudyInstanceUID": study_uid}},
            timeout=30, allow_redirects=False,
        )
        response.raise_for_status()
        value = response.json()
    except requests.RequestException:
        raise RuntimeError("orthanc_unavailable") from None
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)):
        raise RuntimeError("orthanc_import_response_invalid")
    return value


def _instance_matches(client: requests.Session, base: str, instance_id: str,
                      digest: str, expected_size: int) -> bool:
    try:
        with client.get(
                f"{base}/instances/{instance_id}/file", stream=True, timeout=120,
                allow_redirects=False) as response:
            if response.status_code != 200:
                return False
            actual = hashlib.sha256()
            size = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > expected_size:
                    return False
                actual.update(chunk)
    except requests.RequestException:
        raise RuntimeError("orthanc_unavailable") from None
    return size == expected_size and actual.hexdigest() == digest


def _verify_study_closure(client: requests.Session, base: str, study_uid: str,
                          completed: dict[str, dict]) -> None:
    """Prove the exact Orthanc study contains only this upload's instances.

    The SOP collision checks close races for individual instances, but Orthanc groups every SOP
    with the same StudyInstanceUID.  Re-read that grouping after all POSTs while the caller still
    holds the global mutation fence so a non-overlapping SOP cannot be silently exposed through
    the newly registered case.
    """
    study_ids = _find_study(client, base, study_uid)
    if len(study_ids) != 1:
        _fail("orthanc_study_closure_mismatch")
    try:
        response = client.get(
            f"{base}/studies/{study_ids[0]}/instances", timeout=30,
            allow_redirects=False)
        response.raise_for_status()
        value = response.json()
    except requests.RequestException:
        raise RuntimeError("orthanc_unavailable") from None
    except (requests.JSONDecodeError, ValueError):
        raise RuntimeError("orthanc_import_response_invalid") from None
    instances = value if isinstance(value, list) else None
    instance_ids = [] if instances is not None else None
    if instances is not None:
        for item in instances:
            instance_id = item if isinstance(item, str) else (
                item.get("ID") if isinstance(item, dict) else None)
            if not isinstance(instance_id, str) or not instance_id:
                instance_ids = None
                break
            instance_ids.append(instance_id)
    expected = {
        record.get("instance_id") for record in completed.values()
        if isinstance(record, dict)
    }
    if (instance_ids is None
            or len(instance_ids) != len(set(instance_ids))
            or None in expected
            or len(expected) != len(completed)
            or set(instance_ids) != expected):
        _fail("orthanc_study_closure_mismatch")


def _import_instances(client: requests.Session, base: str, prepared: list[dict],
                      receipt_path: Path, upload_sha256: str,
                      manifest_sha256: str) -> dict[str, dict]:
    try:
        header, intents, completed = upload_receipt.load_receipt(receipt_path)
    except RuntimeError:
        raise RuntimeError("receipt_contract_invalid") from None
    expected_header = upload_receipt.expected_header(
        upload_sha256=upload_sha256,
        instance_manifest_sha256=manifest_sha256,
        instance_count=len(prepared),
        study_instance_uid=prepared[0]["study_instance_uid"],
    )
    if header is None:
        # SOP-only collision checks are insufficient: Orthanc groups non-overlapping instances by
        # StudyInstanceUID. Without this fence, an upload could silently join an orphaned study and
        # expose unrelated series through the case viewer. Persist the successful preflight in the
        # receipt before the first import so a crash retry can distinguish its own partial study.
        if _find_study(client, base, prepared[0]["study_instance_uid"]):
            _fail("study_instance_already_exists")
        upload_receipt.append_receipt(receipt_path, expected_header)
        header = expected_header
    try:
        upload_receipt.validate_header(
            header, upload_sha256=upload_sha256,
            instance_manifest_sha256=manifest_sha256,
            instance_count=len(prepared),
            study_instance_uid=prepared[0]["study_instance_uid"],
        )
    except RuntimeError:
        raise RuntimeError("receipt_contract_invalid") from None

    # Reject collisions before mutating Orthanc.  Only an intent in this exact upload receipt can
    # recover a prior crash between POST and receipt completion.
    for item in prepared:
        digest = item["sha256"]
        if digest in completed:
            record = completed[digest]
            if not _instance_matches(
                    client, base, record["instance_id"], digest, item["size"]):
                raise RuntimeError("orthanc_instance_changed")
            continue
        existing = _find_instance(client, base, item["sop_instance_uid"])
        if len(existing) > 1:
            raise RuntimeError("orthanc_duplicate_instance_index")
        if existing:
            if digest not in intents or not _instance_matches(
                    client, base, existing[0], digest, item["size"]):
                _fail("sop_instance_already_exists")
            # A durable intent was written only after the absent-StudyUID header and before this
            # worker's POST, all under the exclusive Research Orthanc mutation fence. If the
            # process died after POST but before `stored`, the exact matching instance is therefore
            # worker-owned and must remain rollback-eligible. Treating it as external would delete
            # the receipt as "clean" while leaving an unregistered orphan study behind.
            upload_receipt.append_receipt(receipt_path, {
                "event": "stored", "file_sha256": digest,
                "orthanc_instance_id": existing[0], "owned": True,
            })
            completed[digest] = {"instance_id": existing[0], "owned": True}

    for item in sorted(prepared, key=lambda value: value["sop_instance_uid"]):
        digest = item["sha256"]
        if digest in completed:
            continue
        if digest not in intents:
            upload_receipt.append_receipt(receipt_path, {
                "event": "intent", "file_sha256": digest,
                "sop_instance_uid": item["sop_instance_uid"], "size": item["size"],
            })
            intents[digest] = {
                "sop_instance_uid": item["sop_instance_uid"], "size": item["size"],
            }
        # Close the preflight/POST race without ever replacing an existing SOP.
        existing = _find_instance(client, base, item["sop_instance_uid"])
        if existing:
            if len(existing) > 1 or not _instance_matches(
                    client, base, existing[0], digest, item["size"]):
                _fail("sop_instance_already_exists")
            upload_receipt.append_receipt(receipt_path, {
                "event": "stored", "file_sha256": digest,
                "orthanc_instance_id": existing[0], "owned": False,
            })
            completed[digest] = {"instance_id": existing[0], "owned": False}
            continue
        try:
            with item["path"].open("rb") as handle:
                response = client.post(
                    base + "/instances", data=handle,
                    headers={"Content-Type": "application/dicom"},
                    timeout=120, allow_redirects=False,
                )
        except requests.RequestException:
            raise RuntimeError("orthanc_unavailable") from None
        if response.status_code not in {200, 201}:
            raise RuntimeError("orthanc_import_failed")
        try:
            value = response.json()
        except (requests.JSONDecodeError, ValueError):
            raise RuntimeError("orthanc_import_response_invalid") from None
        instance_id = value.get("ID") if isinstance(value, dict) else None
        status_value = value.get("Status") if isinstance(value, dict) else None
        if (not isinstance(instance_id, str) or not instance_id
                or status_value not in {"Success", "Stored", "AlreadyStored"}):
            raise RuntimeError("orthanc_import_response_invalid")
        if not _instance_matches(client, base, instance_id, digest, item["size"]):
            raise RuntimeError("orthanc_instance_changed")
        owned = status_value != "AlreadyStored"
        upload_receipt.append_receipt(receipt_path, {
            "event": "stored", "file_sha256": digest,
            "orthanc_instance_id": instance_id, "owned": owned,
        })
        completed[digest] = {"instance_id": instance_id, "owned": owned}
    return completed


def _rollback_owned(client: requests.Session | None, base: str | None,
                    completed: dict[str, dict], prepared: list[dict]) -> int:
    if client is None or base is None:
        return sum(bool(record.get("owned")) for record in completed.values())
    by_digest = {item["sha256"]: item for item in prepared}
    failures = 0
    for digest, record in completed.items():
        if not record.get("owned"):
            continue
        item = by_digest.get(digest)
        try:
            if item is None or not _instance_matches(
                    client, base, record["instance_id"], digest, item["size"]):
                failures += 1
                continue
            response = client.delete(
                f"{base}/instances/{record['instance_id']}", timeout=30,
                allow_redirects=False)
            if response.status_code not in {200, 204, 404}:
                failures += 1
        except (requests.RequestException, RuntimeError):
            failures += 1
    return failures


async def ingest_case_upload(_ctx: dict, upload_id: str) -> None:
    """Validate/import one completed upload; never confirm mappings or enqueue detectors."""
    source: Path | None = None
    receipt_path: Path | None = None
    prepared: list[dict] = []
    completed: dict[str, dict] = {}
    client: requests.Session | None = None
    base: str | None = None
    upload_sha256: str | None = None
    committed = False
    try:
        with Session(engine) as claim_session:
            statement = select(CaseUpload).where(CaseUpload.id == upload_id)
            if claim_session.bind is not None and claim_session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            row = claim_session.exec(statement).first()
            if row is None or row.status in {CaseUploadStatus.ready, CaseUploadStatus.failed}:
                return
            if row.status not in {CaseUploadStatus.staged, CaseUploadStatus.importing}:
                return
            row.status = CaseUploadStatus.importing
            row.import_result = {"phase": "validating"}
            row.updated_at = datetime.now(timezone.utc)
            claim_session.add(row)
            claim_session.commit()
            storage_key = row.storage_key
            total_size = row.total_size
            upload_sha256 = row.sha256

        root = Path(wsettings.case_upload_root).resolve()
        source = root / storage_key
        if (source.parent != root or total_size > wsettings.case_upload_max_bytes
                or not source.is_file() or source.is_symlink()
                or source.stat().st_size != total_size
                or sha256_file(source) != upload_sha256):
            _fail("staged_upload_integrity_failed")
        expanded = _declared_expanded_size(source)
        capacity = storage_health(
            str(root),
            minimum_free_bytes=wsettings.storage_min_free_bytes + expanded,
            minimum_free_percent=wsettings.storage_min_free_percent,
        )
        if not capacity["ready"]:
            _fail("case_upload_storage_unavailable")
        with tempfile.TemporaryDirectory(dir=root, prefix=f"case-ingest-{upload_id}-") as tmp:
            prepared, summaries, dicomdir_count = _validate_archive(source, Path(tmp))
            manifest, manifest_sha256 = _manifest(prepared)
            study_uid = prepared[0]["study_instance_uid"]
            receipt_path = source.with_name(source.name + ".receipt")

            with Session(engine) as session:
                # Serialize app-controlled main-Orthanc mutations across worker processes and
                # recheck durable state after waiting for the fence.
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    session.execute(text("SELECT pg_advisory_xact_lock(4769117494662682451)"))
                statement = select(CaseUpload).where(CaseUpload.id == upload_id)
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    statement = statement.with_for_update()
                row = session.exec(statement).first()
                if row is None or row.status == CaseUploadStatus.ready:
                    return
                if row.status != CaseUploadStatus.importing:
                    return
                if session.exec(select(Case.id).where(
                        Case.orthanc_study_uid == study_uid)).first() is not None:
                    _fail("study_already_registered")
                row.import_result = {
                    "phase": "importing", "instance_count": len(prepared),
                    "series_count": len(summaries),
                    "instance_manifest_sha256": manifest_sha256,
                }
                session.add(row)
                session.flush()

                client = _client()
                base = _orthanc_rest_root()
                completed = _import_instances(
                    client, base, prepared, receipt_path, upload_sha256, manifest_sha256)
                _verify_study_closure(client, base, study_uid, completed)

                case = Case(
                    pseudonym=row.pseudonym,
                    created_by=row.created_by,
                    assigned_to=row.created_by,
                    orthanc_study_uid=study_uid,
                    status=CaseStatus.series_pending,
                    harmonization_status=HarmonizationStatus.unassigned,
                )
                session.add(case)
                session.flush()
                fingerprints: list[str] = []
                roles = Counter()
                for item in summaries:
                    role = propose_role(item["description"])
                    roles[role.value] += 1
                    if item["fingerprint"]:
                        fingerprints.append(item["fingerprint"])
                    session.add(Series(
                        case_id=case.id,
                        orthanc_series_uid=item["series_uid"],
                        series_description=item["description"],
                        modality=item["modality"],
                        proposed_role=role,
                        confirmed_role=None,
                        image_type=item["image_type"],
                        acquisition=item["acquisition"],
                        fingerprint=item["fingerprint"],
                        instance_count=item["instances"],
                        active=True,
                    ))
                case.scanner_fingerprint = (
                    hashlib.sha256("|".join(sorted(fingerprints)).encode()).hexdigest()
                    if fingerprints else None
                )
                session.add(case)
                row.case_id = case.id
                row.status = CaseUploadStatus.ready
                row.last_error = None
                row.import_result = {
                    "phase": "ready_for_confirmation",
                    "study_uid": study_uid,
                    "instance_count": len(prepared),
                    "series_count": len(summaries),
                    "dicomdir_count": dicomdir_count,
                    "instance_manifest_sha256": manifest_sha256,
                    "proposed_role_counts": dict(sorted(roles.items())),
                }
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                audit.record(
                    session, actor="service:case-ingest", action="case_upload.import",
                    entity_type="case_upload", entity_id=row.id,
                    payload={
                        "case_id": case.id, "study_uid": study_uid,
                        "instance_count": len(manifest), "series_count": len(summaries),
                        "instance_manifest_sha256": manifest_sha256,
                    },
                )
                session.commit()
                committed = True
    except Exception as exc:
        error_code = _safe_error_code(exc)
        receipt_invalid = False
        if receipt_path is not None:
            try:
                _header, _intents, receipt_completed = upload_receipt.load_receipt(receipt_path)
                completed = receipt_completed
            except RuntimeError:
                # A damaged receipt is itself protected evidence; rollback cannot safely guess
                # which externally stored instances belong to this upload.
                completed = {}
                receipt_invalid = True
        rollback_failures = _rollback_owned(client, base, completed, prepared)
        if receipt_invalid:
            rollback_failures += 1
        try:
            with Session(engine) as failure_session:
                row = failure_session.get(CaseUpload, upload_id)
                if row is not None and row.status != CaseUploadStatus.ready:
                    row.status = CaseUploadStatus.failed
                    row.last_error = error_code + (
                        ":rollback_pending" if rollback_failures else "")
                    row.import_result = {
                        **dict(row.import_result or {}),
                        "phase": "rollback_incomplete" if rollback_failures else "failed",
                        "rollback_pending_instances": rollback_failures,
                    }
                    row.updated_at = datetime.now(timezone.utc)
                    failure_session.add(row)
                    audit.record(
                        failure_session, actor="service:case-ingest",
                        action="case_upload.fail", entity_type="case_upload",
                        entity_id=row.id, payload={
                            "error_code": row.last_error,
                            "rollback_pending_instances": rollback_failures,
                        },
                    )
                    failure_session.commit()
        except Exception:
            raise RuntimeError("case_upload_failure_state_persistence_failed") from None
        if not rollback_failures:
            for path in (source, receipt_path):
                try:
                    if path is not None:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise RuntimeError(error_code) from None
    else:
        if committed:
            for path in (source, receipt_path):
                try:
                    if path is not None:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                with Session(engine) as cleanup_session:
                    row = cleanup_session.get(CaseUpload, upload_id)
                    if row is not None and all(
                            path is None or not path.exists() for path in (source, receipt_path)):
                        row.staging_cleaned_at = datetime.now(timezone.utc)
                        cleanup_session.add(row)
                        cleanup_session.commit()
            except Exception:
                pass
