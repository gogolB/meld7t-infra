"""Pure contracts for deidentified MELD cohort freezing and cross-validation.

Scientific metric calculation is performed by the pinned builder image.  These helpers define the
deterministic subject split and immutable manifests shared by the API, worker, and tests.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections import defaultdict
from typing import Any, Iterable

from sqlalchemy import text

from .config import settings


HARMONIZATION_ORTHANC_MUTATION_LOCK_KEY = 5567949153544167254


def lock_harmonization_orthanc_mutation(session: Any) -> None:
    """Fence cohort admission against receipt-governed Orthanc deletion."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": HARMONIZATION_ORTHANC_MUTATION_LOCK_KEY},
        )


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def subject_key_hmac(cohort_id: str, subject_key: str) -> str:
    normalized = subject_key.strip()
    if not normalized or len(normalized) > 128 or any(c in normalized for c in "\r\n\0"):
        raise ValueError("subject key must be one non-empty line of at most 128 characters")
    return hmac.new(
        settings.audit_hmac_key.get_secret_value().encode(),
        f"harmonization-cohort:{cohort_id}:{normalized}".encode(),
        hashlib.sha256,
    ).hexdigest()


def deterministic_folds(subjects: Iterable[dict[str, Any]], folds: int) -> list[dict[str, Any]]:
    """Return stable, sex-stratified folds; each subject is held out exactly once."""
    rows = list(subjects)
    if not 2 <= folds <= 10:
        raise ValueError("cross-validation folds must be between 2 and 10")
    if len(rows) < folds:
        raise ValueError("cross-validation needs at least one holdout per fold")
    keys = [str(row["subject_key_hmac"]) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("cross-validation subjects must be unique")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("sex", "")).strip().lower()].append(row)
    buckets: list[list[str]] = [[] for _ in range(folds)]
    offset = 0
    for sex in sorted(groups):
        ordered = sorted(
            groups[sex],
            key=lambda row: (float(row.get("age", 0)), str(row["subject_key_hmac"])),
        )
        for index, row in enumerate(ordered):
            buckets[(offset + index) % folds].append(str(row["subject_key_hmac"]))
        offset = (offset + len(ordered)) % folds

    all_keys = sorted(keys)
    result = []
    for index, holdout in enumerate(buckets):
        holdout = sorted(holdout)
        training = sorted(set(all_keys) - set(holdout))
        membership = {"fold": index, "training": training, "holdout": holdout}
        result.append({
            "fold_index": index,
            "train_subject_hmacs": training,
            "holdout_subject_hmacs": holdout,
            "membership_hmac_sha256": hmac.new(
                settings.audit_hmac_key.get_secret_value().encode(),
                json.dumps(membership, sort_keys=True, separators=(",", ":")).encode(),
                hashlib.sha256,
            ).hexdigest(),
        })
    if sorted(key for fold in result for key in fold["holdout_subject_hmacs"]) != all_keys:
        raise ValueError("cross-validation plan does not cover every subject exactly once")
    return result


def frozen_manifest(*, cohort: Any, studies: Iterable[Any], demographics: Iterable[Any]) -> dict:
    included = sorted(
        ({
            "subject_key_hmac": row.subject_key_hmac,
            "orthanc_study_uid_hmac": hmac.new(
                settings.audit_hmac_key.get_secret_value().encode(),
                row.orthanc_study_uid.encode(), hashlib.sha256,
            ).hexdigest(),
            "study_sha256": row.study_sha256,
            "acquisition_fingerprint": row.acquisition_fingerprint,
            "series_manifest_sha256": canonical_sha256(row.series_manifest),
        } for row in studies if row.included),
        key=lambda row: row["subject_key_hmac"],
    )
    demo = sorted(
        ({"subject_key_hmac": row.subject_key_hmac, "age": row.age, "sex": row.sex}
         for row in demographics),
        key=lambda row: row["subject_key_hmac"],
    )
    body = {
        "schema_version": 1,
        "cohort_id": cohort.id,
        "profile": {"code": cohort.profile_code, "version": cohort.profile_version,
                    "detector_id": "meld_fcd"},
        "source_role": getattr(cohort.source_role, "value", cohort.source_role),
        "selector": cohort.selector,
        "minimum_controls": cohort.min_controls,
        "cv_folds": cohort.cv_folds,
        "studies": included,
        "demographics_sha256": canonical_sha256(demo),
    }
    body["manifest_sha256"] = canonical_sha256(body)
    return body


def qc_summary(fold_results: Iterable[Any], *, subject_count: int) -> dict[str, Any]:
    rows = list(fold_results)
    statuses = [row.get("status") if isinstance(row, dict) else row.status for row in rows]
    metrics = [row.get("metrics", {}) if isinstance(row, dict) else row.metrics for row in rows]
    resources = [
        row.get("resource_usage", {}) if isinstance(row, dict) else row.resource_usage
        for row in rows
    ]
    return {
        "schema_version": 1,
        "internal_validation": "deterministic_k_fold",
        "folds": len(rows),
        "subject_count": subject_count,
        "all_folds_succeeded": bool(rows) and all(value == "passed" for value in statuses),
        "metrics": metrics,
        "resource_usage": resources,
        "scientific_caveat": (
            "Internal cross-validation assesses cohort stability; it does not replace independent "
            "golden-case or external scientific validation."
        ),
    }
