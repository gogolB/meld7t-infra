"""Scanner/protocol harmonization matching and artifact verification.

Harmonization is deliberately explicit: acquisition metadata produces ranked profile proposals,
but a researcher must confirm one before a harmonized run can be queued.  Profiles and artifacts
are versioned and hash-addressed so an air-gapped release can reproduce the exact transform.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from .config import settings


FINGERPRINT_KEYS = (
    "manufacturer",
    "model",
    "station_name",
    "field_strength_t",
    "software_versions",
    "protocol_name",
    "sequence_name",
    "scanning_sequence",
    "repetition_time_ms",
    "echo_time_ms",
    "inversion_time_ms",
    "flip_angle_deg",
    "voxel_spacing_mm",
    "rows",
    "columns",
    "slice_thickness_mm",
    "spacing_between_slices_mm",
    "receive_coil_name",
    "transmit_coil_name",
    "imaged_nucleus",
    "pixel_bandwidth_hz",
    "percent_phase_fov",
    "acquisition_matrix",
    "phase_encoding_direction",
    "phase_encoding_steps",
    "parallel_acquisition",
    "parallel_technique",
    "acceleration_factor_in_plane",
    "acceleration_factor_out_of_plane",
    "reconstruction_diameter_mm",
    "echo_train_length",
    "number_of_averages",
    "mr_acquisition_type",
    "complex_image_component",
    "acquisition_contrast",
    "image_type",
    "rescale_slope",
    "rescale_intercept",
    "bits_stored",
    "pixel_representation",
)

_PROFILE_ACTIVATION_LOCK_KEY = 0x6D656C6468  # "meldh"


def lock_profile_activation(session: Any) -> None:
    """Serialize selector-overlap checks and activation across all API processes."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _PROFILE_ACTIVATION_LOCK_KEY},
        )
FINGERPRINT_SCHEMA_VERSION = 2

SERIES_ROLES = frozenset({
    "t1_uni", "t1_inv1", "t1_inv2", "t1_mprage", "flair", "t2", "unknown",
})
PROFILE_METHOD_BY_DETECTOR = {
    "meld_fcd": "meld_distributed_combat",
    "map": "map_normative",
}

_SELECTOR_OPERATORS = frozenset({"eq", "in", "regex", "min", "max", "target", "tolerance"})
_NUMERIC_OPERATORS = frozenset({"min", "max", "target", "tolerance"})
_integrity_generation = 0


def mark_profile_integrity_dirty() -> None:
    """Invalidate the cached readiness result after an active-profile state change."""
    global _integrity_generation
    _integrity_generation += 1


def profile_integrity_generation() -> int:
    return _integrity_generation


def case_harmonization_coverage(session: Any, case_id: str) -> dict[str, Any]:
    """Derive complete/partial/blocked state across every current detector/source target.

    A case-level flag must never become ``confirmed`` merely because the first of several
    scanner/protocol assignments was approved.
    """
    from sqlmodel import select

    from .detectors import REGISTRY
    from .models import (
        HarmonizationAssignment,
        HarmonizationProfile,
        HarmonizationProfileStatus,
        HarmonizationStatus,
        Series,
    )

    rows = session.exec(select(Series).where(
        Series.case_id == case_id, Series.active.is_(True)
    )).all()
    by_uid = {row.orthanc_series_uid: row for row in rows}
    targets: set[tuple[str, str]] = set()
    for row in rows:
        if row.confirmed_role is None or row.confirmed_role.value == "unknown":
            continue
        for detector in REGISTRY.values():
            if (detector.status == "built"
                    and detector.id.value in PROFILE_METHOD_BY_DETECTOR
                    and row.confirmed_role in detector.source_roles):
                targets.add((detector.id.value, row.orthanc_series_uid))
    assignments = session.exec(select(HarmonizationAssignment).where(
        HarmonizationAssignment.case_id == case_id
    )).all()
    assignment_by_target = {
        (assignment.detector_id.value, assignment.source_series_uid): assignment
        for assignment in assignments
    }
    confirmed = 0
    blocked = 0
    for target in targets:
        assignment = assignment_by_target.get(target)
        source = by_uid.get(target[1])
        profile = (session.get(HarmonizationProfile, assignment.profile_id)
                   if assignment is not None else None)
        current = bool(
            assignment is not None and source is not None
            and assignment.status == HarmonizationStatus.confirmed
            and source.fingerprint
            and assignment.acquisition_fingerprint == source.fingerprint
            and (not settings.is_server_mode
                 or (profile is not None
                     and profile.status == HarmonizationProfileStatus.active
                     and runtime_profile_trusted(session, profile)))
        )
        if current:
            confirmed += 1
        elif assignment is not None and assignment.status == HarmonizationStatus.blocked:
            blocked += 1
    if not targets:
        state = HarmonizationStatus.not_required
        coverage = "not_required"
    elif confirmed == len(targets):
        state = HarmonizationStatus.confirmed
        coverage = "complete"
    elif blocked:
        state = HarmonizationStatus.blocked
        coverage = "blocked"
    elif confirmed:
        state = HarmonizationStatus.proposed
        coverage = "partial"
    else:
        state = HarmonizationStatus.unassigned
        coverage = "unassigned"
    return {
        "status": state,
        "coverage": coverage,
        "required": len(targets),
        "confirmed": confirmed,
        "blocked": blocked,
        "missing": len(targets) - confirmed,
    }


def _normal(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return " ".join(value.strip().lower().split())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return round(number, 6)
    if isinstance(value, (list, tuple, set)):
        values = [_normal(v) for v in value]
        return sorted(values, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, dict):
        return {str(k): _normal(v) for k, v in sorted(value.items())}
    return _normal(str(value))


def canonical_acquisition(acquisition: dict[str, Any]) -> dict[str, Any]:
    """Return minimized acquisition metadata used for matching and keyed hashing.

    Scanner station/protocol fields and DICOM UIDs can be identifying. Callers must treat this
    structure as protected research metadata even though direct patient demographics are excluded.
    """
    return {key: _normal(acquisition.get(key)) for key in FINGERPRINT_KEYS
            if acquisition.get(key) is not None}


def acquisition_fingerprint(acquisition: dict[str, Any]) -> str:
    body = json.dumps({
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "acquisition": canonical_acquisition(acquisition),
    }, sort_keys=True, separators=(",", ":")).encode()
    # A plain hash of low-entropy site/protocol metadata is dictionary-correlatable. The site audit
    # key makes fingerprints stable within one deployment without making them portable identifiers.
    return hmac.new(
        settings.audit_hmac_key.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def profile_document_sha256(profile: Any) -> str:
    get = profile.get if isinstance(profile, dict) else lambda key, default=None: getattr(
        profile, key, default)
    raw_detector = get("detector_id")
    detector = getattr(raw_detector, "value", raw_detector)
    return canonical_json_sha256({
        "code": get("code"),
        "version": get("version"),
        "name": get("name"),
        "method": get("method"),
        "detector_id": detector,
        "selector": get("selector"),
        "artifact_manifest": get("artifact_manifest"),
        "parameters": get("parameters"),
    })


def runtime_profile_trusted(session: Any, profile: Any) -> bool:
    """Require a signed release identity or the complete local activation proof chain.

    Readiness reports profile-integrity failures, but request paths cannot depend on an operator
    noticing a red health check.  This inexpensive database proof keeps an unexpected legacy row
    out of candidate ranking, assignment, recipe creation, and final recipe confirmation.
    Artifact bytes are independently re-hashed by readiness and again by the execution worker.
    """
    from sqlmodel import select

    from .models import (
        HarmonizationBuild,
        HarmonizationBuildStatus,
        HarmonizationCohort,
        HarmonizationCohortStatus,
        HarmonizationProfileStatus,
    )

    if profile is None or profile.status != HarmonizationProfileStatus.active:
        return False
    detector = getattr(profile.detector_id, "value", profile.detector_id)
    for expected in settings.harmonization_expected_profiles:
        if ((profile.code, profile.version) == (expected.code, expected.version)
                and detector == expected.detector_id
                and profile_document_sha256(profile) == expected.document_sha256):
            return True

    parameters = profile.parameters if isinstance(profile.parameters, dict) else {}
    if parameters.get("storage_scope") != "generated":
        return False
    build = session.exec(select(HarmonizationBuild).where(
        HarmonizationBuild.profile_id == profile.id,
        HarmonizationBuild.status == HarmonizationBuildStatus.active,
    )).first()
    if build is None:
        return False
    cohort = session.get(HarmonizationCohort, build.cohort_id)
    qc = build.qc_report if isinstance(build.qc_report, dict) else {}
    artifact_manifest = (build.artifact_manifest
                         if isinstance(build.artifact_manifest, dict) else {})
    scientific_validation = parameters.get("scientific_validation")
    scientific_validation = (scientific_validation
                             if isinstance(scientific_validation, dict) else {})
    frozen = (cohort.frozen_manifest
              if cohort is not None and isinstance(cohort.frozen_manifest, dict) else {})
    actors = (build.initiated_by, build.validated_by, build.activated_by)
    return bool(
        cohort is not None
        and cohort.status == HarmonizationCohortStatus.frozen
        and cohort.approved_by == build.initiated_by
        and all(isinstance(actor, str) and actor for actor in actors)
        and profile.validated_by == build.validated_by
        and (profile.code, profile.version)
        == (cohort.profile_code, cohort.profile_version)
        and profile.selector == cohort.selector
        and build.artifact_manifest == profile.artifact_manifest
        and qc.get("all_folds_succeeded") is True
        and qc.get("report_sha256") == canonical_json_sha256({
            key: value for key, value in qc.items() if key != "report_sha256"
        })
        and frozen.get("manifest_sha256") == canonical_json_sha256({
            key: value for key, value in frozen.items() if key != "manifest_sha256"
        })
        and qc.get("cohort_manifest_sha256") == frozen.get("manifest_sha256")
        and parameters.get("cohort_manifest_sha256") == frozen.get("manifest_sha256")
        and parameters.get("internal_cv_report_sha256") == qc.get("report_sha256")
        and (parameters.get("build_images") or {}).get("meld")
        == build.builder_image_digest
        and qc.get("builder_image_digest") == build.builder_image_digest
        and re.fullmatch(
            r"[0-9a-f]{64}", str(build.builder_adapter_sha256 or "")
        ) is not None
        and qc.get("builder_adapter_sha256") == build.builder_adapter_sha256
        and artifact_manifest.get("builder_adapter_sha256")
        == build.builder_adapter_sha256
        and parameters.get("builder_adapter_sha256") == build.builder_adapter_sha256
        and scientific_validation.get("builder_adapter_sha256")
        == build.builder_adapter_sha256
    )


def validate_scientific_validation(profile: Any) -> None:
    """Validate the signed, profile-bound site acceptance summary.

    This is an engineering gate, not a claim that the method is clinically or scientifically
    valid. The referenced evidence remains an external acceptance deliverable.
    """
    parameters = profile.parameters if isinstance(profile.parameters, dict) else {}
    report = parameters.get("scientific_validation")
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError("profile lacks a schema-v1 scientific validation summary")
    detector = getattr(profile.detector_id, "value", profile.detector_id)
    artifact_manifest = (profile.artifact_manifest
                         if isinstance(profile.artifact_manifest, dict) else {})
    adapter_bound = detector == "meld_fcd" and (
        parameters.get("storage_scope") == "generated"
        or "builder_adapter_sha256" in parameters
        or "builder_adapter_sha256" in artifact_manifest
        or "builder_adapter_sha256" in report
    )
    expected_report_fields = {
        "schema_version", "profile", "approval_id", "independent_reviewer", "approved_at",
        "acquisition_fingerprints", "qc", "holdout", "metrics_sha256",
        "golden_case_evidence_sha256", "methodology_sha256", "image_digests",
    }
    if adapter_bound:
        if "builder_adapter_sha256" not in report:
            raise ValueError(
                "scientific validation lacks the builder adapter digest"
            )
        expected_report_fields.add("builder_adapter_sha256")
    if set(report) != expected_report_fields:
        raise ValueError("scientific validation summary must use the exact minimized schema")
    binding = report.get("profile")
    if binding != {
        "code": profile.code, "version": profile.version, "detector_id": detector,
    }:
        raise ValueError("scientific validation summary is bound to a different profile")
    for field in ("approval_id", "independent_reviewer", "approved_at"):
        value = report.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(f"scientific validation {field} is missing or invalid")
    try:
        approved_at = datetime.fromisoformat(str(report["approved_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scientific validation approved_at must be ISO-8601") from exc
    if approved_at.tzinfo is None:
        raise ValueError("scientific validation approved_at must include a timezone")
    fingerprints = report.get("acquisition_fingerprints")
    if (not isinstance(fingerprints, list) or not fingerprints
            or len(fingerprints) != len(set(fingerprints))
            or any(re.fullmatch(r"[0-9a-f]{64}", str(value)) is None
                   for value in fingerprints)):
        raise ValueError("scientific validation needs unique acquisition fingerprints")
    qc = report.get("qc")
    if (not isinstance(qc, dict)
            or set(qc) != {"included", "excluded"}
            or isinstance(qc.get("included"), bool) or not isinstance(qc.get("included"), int)
            or qc["included"] < 20
            or isinstance(qc.get("excluded"), bool) or not isinstance(qc.get("excluded"), int)
            or qc["excluded"] < 0):
        raise ValueError("scientific validation QC counts are incomplete")
    holdout = report.get("holdout")
    counts = ("positive_cases", "negative_cases", "control_cases")
    if (not isinstance(holdout, dict)
            or set(holdout) != {"case_count", *counts}
            or any(isinstance(holdout.get(key), bool) or not isinstance(holdout.get(key), int)
                   or holdout[key] < 1 for key in counts)
            or holdout.get("case_count") != sum(holdout[key] for key in counts)):
        raise ValueError("scientific validation needs positive, negative, and control holdouts")
    for field in ("metrics_sha256", "golden_case_evidence_sha256", "methodology_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(report.get(field, ""))) is None:
            raise ValueError(f"scientific validation {field} must be a SHA-256 digest")
    images = report.get("image_digests")
    expected_images = parameters.get("build_images")
    if (not isinstance(images, dict) or not images or images != expected_images
            or any(re.search(r"@sha256:[0-9a-f]{64}$", str(value)) is None
                   for value in images.values())):
        raise ValueError("scientific validation build images do not match the profile")
    if adapter_bound:
        adapter_sha256 = str(parameters.get("builder_adapter_sha256", "")).lower()
        if (re.fullmatch(r"[0-9a-f]{64}", adapter_sha256) is None
                or report.get("builder_adapter_sha256") != adapter_sha256):
            raise ValueError(
                "scientific validation builder adapter does not match the profile"
            )


def validate_selector(selector: dict[str, Any]) -> None:
    """Validate the complete selector grammar before a profile can be stored.

    Matching remains fail-closed as a second line of defence because existing databases may
    contain profiles created by an older release.
    """
    if not isinstance(selector, dict) or not selector:
        raise ValueError("selector must be a non-empty object")
    allowed_top_level = {"roles", "acquisition"} if "acquisition" in selector else (
        set(FINGERPRINT_KEYS) | {"roles"})
    unknown_top_level = set(selector) - allowed_top_level
    if unknown_top_level:
        raise ValueError(f"unsupported selector key {sorted(unknown_top_level)[0]}")
    roles = selector.get("roles")
    if roles is not None:
        if (not isinstance(roles, list) or not roles
                or any(not isinstance(role, str) or not role.strip() for role in roles)):
            raise ValueError("selector.roles must be a non-empty list of role names")
        if len(roles) != len(set(roles)):
            raise ValueError("selector.roles must not contain duplicates")
        unknown_roles = set(roles) - SERIES_ROLES
        if unknown_roles:
            raise ValueError(f"selector.roles contains unsupported role {sorted(unknown_roles)[0]}")
    rules = selector.get("acquisition", {k: v for k, v in selector.items() if k != "roles"})
    if not isinstance(rules, dict) or not rules:
        raise ValueError("selector.acquisition must be a non-empty object")
    for key, rule in rules.items():
        if key not in FINGERPRINT_KEYS:
            raise ValueError(f"unsupported selector key {key}")
        if isinstance(rule, list):
            if not rule:
                raise ValueError(f"selector rule {key}.in must not be empty")
            continue
        if not isinstance(rule, dict):
            continue
        if not rule:
            raise ValueError(f"selector rule {key} must not be empty")
        unknown = set(rule) - _SELECTOR_OPERATORS
        if unknown:
            raise ValueError(f"unsupported operator {sorted(unknown)[0]} for {key}")
        modes = sum(name in rule for name in ("eq", "in", "regex"))
        has_numeric = bool(set(rule) & _NUMERIC_OPERATORS)
        if modes + int(has_numeric) != 1:
            raise ValueError(f"selector rule {key} must use exactly one comparison mode")
        if "in" in rule and (not isinstance(rule["in"], list) or not rule["in"]):
            raise ValueError(f"selector rule {key}.in must be a non-empty list")
        if "regex" in rule:
            pattern = rule["regex"]
            if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
                raise ValueError(f"selector rule {key}.regex must be 1-256 characters")
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid regex for {key}: {exc}") from exc
        if has_numeric:
            if not set(rule) & {"min", "max", "target"}:
                raise ValueError(f"numeric selector rule {key} needs min, max, or target")
            try:
                values = {name: float(value) for name, value in rule.items()}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"numeric selector rule {key} contains a non-number") from exc
            if any(not math.isfinite(value) for value in values.values()):
                raise ValueError(f"numeric selector rule {key} must contain finite values")
            if values.get("tolerance", 0.0) < 0:
                raise ValueError(f"numeric selector rule {key}.tolerance must be non-negative")
            if "min" in values and "max" in values and values["min"] > values["max"]:
                raise ValueError(f"numeric selector rule {key}.min must not exceed max")


@dataclass(frozen=True)
class ProfileMatch:
    profile_id: str
    matched: bool
    score: int
    reasons: tuple[str, ...]


def _match_rule(actual: Any, rule: Any) -> tuple[bool, int, str]:
    actual = _normal(actual)
    if isinstance(rule, list):
        allowed = [_normal(v) for v in rule]
        ok = actual in allowed
        return ok, 2 if ok else 0, f"in={allowed!r}"
    if not isinstance(rule, dict):
        expected = _normal(rule)
        ok = actual == expected
        return ok, 3 if ok else 0, f"eq={expected!r}"

    if "eq" in rule:
        expected = _normal(rule["eq"])
        ok = actual == expected
        return ok, 3 if ok else 0, f"eq={expected!r}"
    if "in" in rule:
        if not isinstance(rule["in"], list):
            return False, 0, "invalid in rule"
        allowed = [_normal(v) for v in rule["in"]]
        ok = actual in allowed
        return ok, 2 if ok else 0, f"in={allowed!r}"
    if "regex" in rule:
        pattern = str(rule["regex"])
        if len(pattern) > 256:
            return False, 0, "regex too long"
        try:
            ok = isinstance(actual, str) and re.search(pattern, actual, re.IGNORECASE) is not None
        except re.error:
            return False, 0, "invalid regex"
        return ok, 1 if ok else 0, f"regex={pattern!r}"

    try:
        number = float(actual)
    except (TypeError, ValueError):
        return False, 0, "not numeric"
    minimum = rule.get("min")
    maximum = rule.get("max")
    tolerance = float(rule.get("tolerance", 0.0))
    if minimum is not None and number < float(minimum) - tolerance:
        return False, 0, f"below min={minimum}"
    if maximum is not None and number > float(maximum) + tolerance:
        return False, 0, f"above max={maximum}"
    if "target" in rule and abs(number - float(rule["target"])) > tolerance:
        return False, 0, f"outside target={rule['target']}±{tolerance}"
    return True, 2, "numeric range"


def match_selector(profile_id: str, selector: dict[str, Any], acquisition: dict[str, Any],
                   *, role: str | None = None) -> ProfileMatch:
    """Evaluate one profile selector against minimized protected acquisition metadata.

    Selector keys under ``acquisition`` are all required. Optional ``roles`` limits the profile to
    named confirmed series roles. Unknown selector fields fail closed.
    """
    reasons: list[str] = []
    score = 0
    try:
        validate_selector(selector)
    except ValueError as exc:
        return ProfileMatch(profile_id, False, 0, (str(exc),))
    roles = selector.get("roles")
    if roles is not None and role not in set(roles):
        return ProfileMatch(profile_id, False, 0, (f"role {role!r} not allowed",))
    rules = selector.get("acquisition", selector)
    if not isinstance(rules, dict):
        return ProfileMatch(profile_id, False, 0, ("selector must be an object",))
    for key, rule in rules.items():
        if key == "roles":
            continue
        if key not in FINGERPRINT_KEYS:
            return ProfileMatch(profile_id, False, 0, (f"unsupported selector key {key}",))
        ok, points, why = _match_rule(acquisition.get(key), rule)
        reasons.append(f"{key}: {'match' if ok else 'no match'} ({why})")
        if not ok:
            return ProfileMatch(profile_id, False, 0, tuple(reasons))
        score += points
    return ProfileMatch(profile_id, True, score, tuple(reasons))


def rank_profiles(profiles: Iterable[Any], acquisition: dict[str, Any], *, role: str | None = None,
                  detector_id: str | None = None) -> list[ProfileMatch]:
    matches: list[ProfileMatch] = []
    for profile in profiles:
        profile_detector = getattr(profile, "detector_id", None)
        profile_detector = getattr(profile_detector, "value", profile_detector)
        if detector_id and profile_detector and profile_detector != detector_id:
            continue
        result = match_selector(str(profile.id), profile.selector, acquisition, role=role)
        if result.matched:
            matches.append(result)
    return sorted(matches, key=lambda m: (-m.score, m.profile_id))


def _selector_values(rule: Any) -> set[Any] | None:
    if isinstance(rule, list):
        return {_normal(value) for value in rule}
    if not isinstance(rule, dict):
        return {_normal(rule)}
    if "eq" in rule:
        return {_normal(rule["eq"])}
    if "in" in rule and isinstance(rule["in"], list):
        return {_normal(value) for value in rule["in"]}
    return None


def _selector_interval(rule: Any) -> tuple[float, float] | None:
    if not isinstance(rule, dict) or not set(rule) & _NUMERIC_OPERATORS:
        return None
    tolerance = float(rule.get("tolerance", 0.0))
    low = float(rule.get("min", -math.inf)) - tolerance
    high = float(rule.get("max", math.inf)) + tolerance
    if "target" in rule:
        target = float(rule["target"])
        low, high = max(low, target - tolerance), min(high, target + tolerance)
    return low, high


def selectors_may_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Conservatively reject active selectors unless their domains are provably disjoint."""
    validate_selector(first)
    validate_selector(second)
    first_roles = set(first.get("roles", SERIES_ROLES))
    second_roles = set(second.get("roles", SERIES_ROLES))
    if not first_roles.intersection(second_roles):
        return False
    a_rules = first.get("acquisition", {k: v for k, v in first.items() if k != "roles"})
    b_rules = second.get("acquisition", {k: v for k, v in second.items() if k != "roles"})
    for key in set(a_rules).intersection(b_rules):
        a_values, b_values = _selector_values(a_rules[key]), _selector_values(b_rules[key])
        if a_values is not None and b_values is not None and not a_values.intersection(b_values):
            return False
        a_interval, b_interval = _selector_interval(a_rules[key]), _selector_interval(b_rules[key])
        if a_interval is not None and b_interval is not None:
            if a_interval[1] < b_interval[0] or b_interval[1] < a_interval[0]:
                return False
        # Prove a finite value-set and regex disjoint when possible. Two regexes remain
        # conservatively overlapping and should be narrowed with an eq/in discriminator.
        for values, rule in ((a_values, b_rules[key]), (b_values, a_rules[key])):
            if values is not None and isinstance(rule, dict) and "regex" in rule:
                pattern = re.compile(str(rule["regex"]), re.IGNORECASE)
                if not any(isinstance(value, str) and pattern.search(value) for value in values):
                    return False
    return True


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def profile_artifact_root(profile: Any) -> str:
    """Select the immutable signed-release or locally generated artifact trust root."""
    parameters = (profile.get("parameters", {}) if isinstance(profile, dict)
                  else getattr(profile, "parameters", {}))
    scope = parameters.get("storage_scope") if isinstance(parameters, dict) else None
    if scope == "generated":
        return settings.harmonization_generated_root
    if scope not in {None, "release"}:
        raise ValueError("unknown harmonization artifact storage scope")
    return settings.harmonization_root


def verify_artifact_manifest(manifest: dict[str, Any], root: str | Path) -> dict[str, Any]:
    """Verify a profile's local artifact manifest and return a resolved immutable contract."""
    root_path = Path(root).resolve(strict=True)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("artifact_manifest.files must be a non-empty list")
    verified = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            raise ValueError("each artifact requires path and sha256")
        relative = Path(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"artifact path must be relative: {relative}")
        normalized = relative.as_posix()
        if normalized in seen:
            raise ValueError(f"duplicate artifact path: {relative}")
        seen.add(normalized)
        candidate = root_path / relative
        components = [root_path / Path(*relative.parts[:index])
                      for index in range(1, len(relative.parts) + 1)]
        if any(component.is_symlink() for component in components):
            raise ValueError(f"artifact path contains a symlink: {relative}")
        path = candidate.resolve(strict=True)
        if path != root_path and root_path not in path.parents:
            raise ValueError(f"artifact escapes harmonization root: {relative}")
        if not path.is_file():
            raise ValueError(f"artifact is not a regular file: {relative}")
        expected = str(item["sha256"]).lower()
        actual = sha256_file(path)
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
            raise ValueError(f"artifact hash mismatch: {relative}")
        if item.get("size") is not None and path.stat().st_size != int(item["size"]):
            raise ValueError(f"artifact size mismatch: {relative}")
        verified.append({"path": normalized, "sha256": actual, "size": path.stat().st_size})
    return {"root": str(root_path), "files": verified,
            "manifest_sha256": hashlib.sha256(json.dumps(verified, sort_keys=True,
                                                            separators=(",", ":")).encode()).hexdigest()}


def run_harmonization_contract(profile: Any, assignment: Any | None = None) -> dict[str, Any]:
    detector = getattr(getattr(profile, "detector_id", None), "value",
                       getattr(profile, "detector_id", None))
    contract = {
        "profile_id": str(profile.id),
        "code": profile.code,
        "version": profile.version,
        "name": profile.name,
        "method": profile.method,
        "detector_id": detector,
        "selector": profile.selector,
        "profile_document_sha256": profile_document_sha256(profile),
        "artifact_manifest": profile.artifact_manifest,
        "parameters": profile.parameters,
    }
    if assignment is not None:
        contract.update({
            "assignment_id": str(assignment.id),
            "acquisition_fingerprint": assignment.acquisition_fingerprint,
            # An override is deliberately retained as a scientific eligibility marker without
            # copying free text into run parameters or immutable audit payloads.
            "selector_override": bool(assignment.override_reason),
            "override_reason_present": bool(assignment.override_reason),
        })
    return contract


def validate_profile_semantics(profile: Any, verified_manifest: dict[str, Any]) -> None:
    """Validate detector/method-specific scientific activation requirements."""
    detector = getattr(profile.detector_id, "value", profile.detector_id)
    method = str(profile.method)
    expected_detector = next(
        (name for name, expected in PROFILE_METHOD_BY_DETECTOR.items() if expected == method),
        None,
    )
    if expected_detector is None:
        raise ValueError(f"unsupported harmonization method {method!r}")
    if detector is not None and detector != expected_detector:
        raise ValueError(
            f"method {method!r} is only valid for detector {expected_detector!r}"
        )
    parameters = profile.parameters if isinstance(profile.parameters, dict) else {}
    validate_scientific_validation(profile)
    files = verified_manifest.get("files", [])
    paths = {str(item.get("path", "")) for item in files}
    if method == "meld_distributed_combat":
        if re.fullmatch(r"H[A-Za-z0-9][A-Za-z0-9_-]{0,31}", str(profile.code)) is None:
            raise ValueError("MELD harmonization code must use the documented H-prefixed format")
        if parameters.get("activation_eligible") is not True:
            raise ValueError("MELD profile cohort is not marked activation-eligible")
        minimum = parameters.get("minimum_subjects")
        count = parameters.get("control_count")
        if (isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 20
                or isinstance(count, bool) or not isinstance(count, int) or count < minimum):
            raise ValueError("MELD profile control cohort must contain at least 20 subjects")
        if parameters.get("harmo_code") != profile.code:
            raise ValueError("MELD profile harmo_code must equal the immutable profile code")
        images = parameters.get("build_images")
        if (not isinstance(images, dict) or set(images) != {"meld"}
                or re.search(r"@sha256:[0-9a-f]{64}$", str(images.get("meld"))) is None):
            raise ValueError("MELD profile requires its immutable build image")
        report = parameters.get("scientific_validation")
        report = report if isinstance(report, dict) else {}
        manifest = (profile.artifact_manifest
                    if isinstance(profile.artifact_manifest, dict) else {})
        adapter_bound = (
            parameters.get("storage_scope") == "generated"
            or "builder_adapter_sha256" in parameters
            or "builder_adapter_sha256" in manifest
            or "builder_adapter_sha256" in report
        )
        if adapter_bound:
            adapter_sha256 = str(parameters.get("builder_adapter_sha256", ""))
            if (re.fullmatch(r"[0-9a-f]{64}", adapter_sha256) is None
                    or manifest.get("builder_adapter_sha256") != adapter_sha256
                    or report.get("builder_adapter_sha256") != adapter_sha256):
                raise ValueError(
                    "MELD profile builder adapter digest is missing or inconsistent"
                )
        if parameters.get("selector_canonical_sha256") != canonical_json_sha256(profile.selector):
            raise ValueError("MELD profile selector differs from its cohort preparation contract")
        cohort_hash = str(parameters.get("cohort_manifest_sha256", "")).lower()
        manifest_cohort_hash = str(
            (profile.artifact_manifest or {}).get("cohort_manifest_sha256", "")
        ).lower()
        if (re.fullmatch(r"[0-9a-f]{64}", cohort_hash) is None
                or cohort_hash != manifest_cohort_hash):
            raise ValueError("MELD profile cohort manifest hashes are missing or inconsistent")
        data_root = Path(str(parameters.get("data_root", ".")))
        if data_root.is_absolute() or ".." in data_root.parts:
            raise ValueError("MELD profile data_root must be a safe relative path")
        if data_root != Path(".") and any(
                Path(path) != data_root and data_root not in Path(path).parents for path in paths):
            raise ValueError("MELD profile artifacts must all be contained by parameters.data_root")
        expected_name = f"MELD_{profile.code}combat_parameters.hdf5"
        relative_to_data = {
            Path(path).relative_to(data_root).as_posix() for path in paths
            if Path(path) == data_root or data_root in Path(path).parents
        }
        if relative_to_data != {expected_name}:
            raise ValueError(
                f"MELD profile data_root must contain only {expected_name!r}"
            )
    elif method == "map_normative":
        required = {
            "junction_mean.nii.gz", "junction_std.nii.gz",
            "extension_mean.nii.gz", "extension_std.nii.gz",
        }
        normative = {
            Path(path).name for path in paths
            if "/normative/map/" in f"/{path}"
        }
        missing = sorted(required - normative)
        if missing:
            raise ValueError(f"MAP normative profile is incomplete; missing {missing}")
        data_root = Path(str(parameters.get("data_root", ".")))
        if data_root.is_absolute() or ".." in data_root.parts:
            raise ValueError("MAP profile data_root must be a safe relative path")
        if data_root != Path(".") and any(
                Path(path) != data_root and data_root not in Path(path).parents for path in paths):
            raise ValueError("MAP profile artifacts must all be contained by parameters.data_root")
        exact = {(data_root / "normative" / "map" / name).as_posix() for name in required}
        if not exact.issubset(paths):
            raise ValueError("MAP normative artifacts must use the canonical normative/map paths")
        if parameters.get("selector_canonical_sha256") != canonical_json_sha256(profile.selector):
            raise ValueError("MAP profile selector differs from its cohort preparation contract")
        minimum = parameters.get("minimum_subjects")
        count = parameters.get("control_count")
        if (parameters.get("activation_eligible") is not True
                or isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 20
                or isinstance(count, bool) or not isinstance(count, int) or count < minimum):
            raise ValueError("MAP profile control cohort is not activation-eligible")
        cohort_hash = str(parameters.get("cohort_manifest_sha256", "")).lower()
        manifest_cohort_hash = str(
            (profile.artifact_manifest or {}).get("cohort_manifest_sha256", "")
        ).lower()
        if (re.fullmatch(r"[0-9a-f]{64}", cohort_hash) is None
                or cohort_hash != manifest_cohort_hash):
            raise ValueError("MAP profile cohort manifest hashes are missing or inconsistent")
        images = parameters.get("build_images")
        if (not isinstance(images, dict) or set(images) != {"spm", "pkg"}
                or any(re.search(r"@sha256:[0-9a-f]{64}$", str(value)) is None
                       for value in images.values())):
            raise ValueError("MAP profile requires immutable SPM and pkg build images")

    verified_root = verified_manifest.get("root")
    if verified_root is not None:
        root = Path(str(verified_root)).resolve(strict=True)
        data_directory = (root / data_root).resolve(strict=True)
        if (data_directory != root and root not in data_directory.parents) \
                or not data_directory.is_dir():
            raise ValueError("harmonization profile data_root is unavailable or outside root")
        listed = {
            (root / str(item["path"])).resolve().relative_to(data_directory).as_posix()
            for item in files
        }
        actual: set[str] = set()
        for candidate in data_directory.rglob("*"):
            if candidate.is_symlink():
                raise ValueError("harmonization profile data_root contains a symlink")
            if candidate.is_file():
                actual.add(candidate.relative_to(data_directory).as_posix())
        if actual != listed:
            raise ValueError("harmonization profile data_root contains unlisted or missing files")
