"""Crash-safe, canonical receipt contract for harmonization Orthanc imports."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any


def append_receipt(path: Path, event: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(path, flags)
    try:
        payload = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with os.fdopen(descriptor, "ab", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    if created:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def load_receipt(path: Path) -> tuple[
        dict[str, Any] | None, dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, {}, {}
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024 * 1024:
            raise RuntimeError("harmonization_upload_receipt_invalid")
        with os.fdopen(descriptor, "r+b", closefd=False) as handle:
            payload = handle.read()
        # Writers append one newline-terminated fsynced record. Discard only a torn final suffix.
        if payload and not payload.endswith(b"\n"):
            complete_length = payload.rfind(b"\n") + 1
            os.ftruncate(descriptor, complete_length)
            os.fsync(descriptor)
            payload = payload[:complete_length]
    finally:
        os.close(descriptor)

    header = None
    intents: dict[str, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    for raw in payload.splitlines():
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("harmonization_upload_receipt_invalid") from exc
        if not isinstance(value, dict):
            raise RuntimeError("harmonization_upload_receipt_invalid")
        if value.get("event") == "header" and header is None:
            header = value
        elif value.get("event") == "intent":
            digest = str(value.get("file_sha256", ""))
            sop_uid = str(value.get("sop_instance_uid", ""))
            size = value.get("size")
            if (re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", sop_uid) is None
                    or isinstance(size, bool) or not isinstance(size, int) or size <= 0
                    or digest in intents):
                raise RuntimeError("harmonization_upload_receipt_invalid")
            intents[digest] = {"sop_instance_uid": sop_uid, "size": size}
        elif value.get("event") == "stored":
            digest = str(value.get("file_sha256", ""))
            instance_id = str(value.get("orthanc_instance_id", ""))
            owned = value.get("owned")
            if (re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or not instance_id or len(instance_id) > 128
                    or any(char in instance_id for char in "\r\n\0/")
                    or not isinstance(owned, bool) or digest in completed):
                raise RuntimeError("harmonization_upload_receipt_invalid")
            completed[digest] = {"instance_id": instance_id, "owned": owned}
        else:
            raise RuntimeError("harmonization_upload_receipt_invalid")
    if payload and (header is None or set(completed) - set(intents)):
        raise RuntimeError("harmonization_upload_receipt_invalid")
    return header, intents, completed


def expected_header(*, upload_sha256: str, instance_manifest_sha256: str,
                    instance_count: int,
                    study_instance_uid: str | None = None) -> dict[str, Any]:
    header: dict[str, Any] = {
        "event": "header", "schema_version": 1,
        "upload_sha256": upload_sha256,
        "instance_manifest_sha256": instance_manifest_sha256,
        "instance_count": instance_count,
    }
    if study_instance_uid is not None:
        if (len(study_instance_uid) > 64
                or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", study_instance_uid) is None):
            raise RuntimeError("harmonization_upload_receipt_contract_invalid")
        # Routine-case imports use this durable marker to prove that the target Study UID was
        # absent before their first Orthanc mutation. Harmonization receipts omit it and retain
        # their existing schema-1 document exactly.
        header["orthanc_study_preflight"] = {
            "study_instance_uid": study_instance_uid,
            "status": "absent",
        }
    return header


def validate_header(header: dict[str, Any] | None, *, upload_sha256: str,
                    instance_manifest_sha256: str | None,
                    instance_count: int | None,
                    study_instance_uid: str | None = None) -> dict[str, Any]:
    if instance_manifest_sha256 is None or instance_count is None:
        raise RuntimeError("harmonization_upload_receipt_contract_missing")
    expected = expected_header(
        upload_sha256=upload_sha256,
        instance_manifest_sha256=instance_manifest_sha256,
        instance_count=instance_count,
        study_instance_uid=study_instance_uid,
    )
    if header != expected:
        raise RuntimeError("harmonization_upload_receipt_header_mismatch")
    return expected


def evidence_sha256(header: dict[str, Any], intents: dict[str, dict[str, Any]],
                    completed: dict[str, dict[str, Any]]) -> str:
    document = {
        "schema_version": 1,
        "header": header,
        "intents": [{"file_sha256": digest, **intents[digest]}
                    for digest in sorted(intents)],
        "stored": [{"file_sha256": digest, **completed[digest]}
                   for digest in sorted(completed)],
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
