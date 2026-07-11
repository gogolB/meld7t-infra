"""Release-controlled bootstrap/update of expected active harmonization profiles.

This runs after Alembic in the isolated migration job, before normal API readiness is required.
The signed release inventory, artifact closure, scientific evidence, and current database state are
validated completely before the one database/audit transaction mutates profile status.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from . import audit
from .config import ExpectedHarmonizationProfile, settings
from .db import engine
from .harmonization import (
    profile_document_sha256,
    selectors_may_overlap,
    validate_profile_semantics,
    validate_selector,
    verify_artifact_manifest,
)
from .models import DetectorId, HarmonizationProfile, HarmonizationProfileStatus


def _load_json(path: Path):
    value = json.loads(path.read_text())
    return value


def import_expected_profiles() -> dict[str, int]:
    root = Path(settings.harmonization_root).resolve(strict=True)
    inventory_path = root / "expected-active-profiles.json"
    raw_inventory = _load_json(inventory_path)
    if not isinstance(raw_inventory, list) or not raw_inventory:
        raise ValueError("signed expected-active-profiles.json must be a non-empty array")
    inventory = [ExpectedHarmonizationProfile.model_validate(item) for item in raw_inventory]
    expected_keys = [(item.code, item.version) for item in inventory]
    if len(expected_keys) != len(set(expected_keys)) or len({item.code for item in inventory}) != len(
            inventory):
        raise ValueError("signed expected profile inventory contains duplicates/multiple versions")
    configured = {
        (item.code, item.version, item.detector_id, item.document_sha256)
        for item in settings.harmonization_expected_profiles
    }
    signed = {
        (item.code, item.version, item.detector_id, item.document_sha256)
        for item in inventory
    }
    if configured != signed:
        raise ValueError("API expected-profile configuration differs from the signed release")

    documents: dict[tuple[str, int], dict] = {}
    verified: dict[tuple[str, int], dict] = {}
    profile_dir = root / "profiles"
    for path in sorted(profile_dir.glob("*.json")):
        document = _load_json(path)
        if not isinstance(document, dict):
            raise ValueError(f"profile is not an object: {path.name}")
        key = (str(document.get("code", "")), document.get("version"))
        if key in documents:
            raise ValueError(f"duplicate profile document {key!r}")
        documents[key] = document
    for item in inventory:
        key = (item.code, item.version)
        document = documents.get(key)
        if document is None or profile_document_sha256(document) != item.document_sha256:
            raise ValueError(f"signed expected profile differs or is absent: {item.code} v{item.version}")
        if document.get("detector_id") != item.detector_id:
            raise ValueError(f"signed expected profile detector differs: {item.code}")
        validate_selector(document.get("selector"))
        candidate = HarmonizationProfile(
            code=item.code,
            version=item.version,
            name=document.get("name"),
            method=document.get("method"),
            detector_id=DetectorId(item.detector_id),
            selector=document.get("selector"),
            artifact_manifest=document.get("artifact_manifest"),
            parameters=document.get("parameters"),
            created_by=f"release:{settings.release_manifest_digest}",
        )
        artifact_result = verify_artifact_manifest(candidate.artifact_manifest, root)
        validate_profile_semantics(candidate, artifact_result)
        verified[key] = artifact_result

    # The release-controlled importer is an alternate activation path and must enforce the same
    # ambiguity guard as the interactive API.  A conservative overlap result is intentional: if
    # disjointness cannot be proven from exact/numeric/role predicates, the release author must
    # narrow the selectors or retain only one profile for that detector.
    expected_documents = [documents[key] for key in expected_keys]
    for index, left in enumerate(expected_documents):
        for right in expected_documents[index + 1:]:
            if (left.get("detector_id") == right.get("detector_id")
                    and selectors_may_overlap(left.get("selector"), right.get("selector"))):
                raise ValueError(
                    "signed expected profiles have overlapping selectors for detector "
                    f"{left.get('detector_id')}: {left.get('code')} and {right.get('code')}"
                )

    created = activated = retired = 0
    with Session(engine) as session:
        rows = session.exec(select(HarmonizationProfile)).all()
        by_key = {(row.code, row.version): row for row in rows}
        for row in rows:
            if (row.status == HarmonizationProfileStatus.active
                    and (row.code, row.version) not in set(expected_keys)):
                row.status = HarmonizationProfileStatus.retired
                session.add(row)
                retired += 1
        session.flush()
        for item in inventory:
            key = (item.code, item.version)
            document = documents[key]
            row = by_key.get(key)
            if row is not None:
                if profile_document_sha256(row) != item.document_sha256:
                    raise ValueError(f"database profile differs from signed release: {item.code}")
                if row.status == HarmonizationProfileStatus.retired:
                    raise ValueError(
                        f"retired profile cannot be reactivated; increment version for {item.code}"
                    )
            else:
                row = HarmonizationProfile(
                    code=item.code, version=item.version, name=document["name"],
                    method=document["method"], detector_id=DetectorId(item.detector_id),
                    selector=document["selector"], artifact_manifest=document["artifact_manifest"],
                    parameters=document["parameters"],
                    created_by=f"release:{settings.release_manifest_digest}",
                )
                session.add(row)
                created += 1
            report = document["parameters"]["scientific_validation"]
            row.status = HarmonizationProfileStatus.active
            row.validated_by = f"validation:{report['independent_reviewer']}"
            row.validated_at = datetime.now(timezone.utc)
            row.validation_summary = {
                "artifact_manifest": verified[key],
                "scientific": {
                    "approval_id": report["approval_id"],
                    "approved_at": report["approved_at"],
                    "methodology_sha256": report["methodology_sha256"],
                    "golden_case_evidence_sha256": report["golden_case_evidence_sha256"],
                },
            }
            session.add(row)
            activated += 1
        session.flush()
        audit.record(
            session,
            actor=f"release:{settings.release_manifest_digest}",
            action="harmonization.release_inventory.activate",
            entity_type="harmonization_profile_inventory",
            entity_id=settings.release_manifest_digest or "missing-release",
            payload={
                "profiles": [{
                    "code": item.code, "version": item.version,
                    "detector_id": item.detector_id,
                    "document_sha256": item.document_sha256,
                } for item in inventory],
                "created": created, "activated": activated, "retired": retired,
            },
        )
        session.commit()
    return {"created": created, "activated": activated, "retired": retired}


def main() -> None:
    print(json.dumps(import_expected_profiles(), sort_keys=True))


if __name__ == "__main__":
    main()
