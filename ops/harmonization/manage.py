#!/usr/bin/env python3
"""Prepare and finalize versioned MELD Distributed ComBat harmonization profiles.

The tool never embeds subject rows or demographics in its output manifest. It records only counts,
variance checks, acquisition selector metadata, source-file hashes, the exact container command, and
hashes for the resulting parameters. Run it in the project's development container.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import math
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUBJECT_RE = re.compile(r"^sub-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
CODE_RE = re.compile(r"^H[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
PROFILE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
OCI_DIGEST_RE = re.compile(r"^[^\s@]+/[^\s@]+@sha256:[0-9a-f]{64}$")
MAP_FILES = tuple(
    f"{feature}_{stat}.nii.gz"
    for feature in ("junction", "extension") for stat in ("mean", "std")
)
SEX_CATEGORIES = {
    "f": "female", "female": "female",
    "m": "male", "male": "male",
    "o": "other", "other": "other", "nonbinary": "other", "non-binary": "other",
    "intersex": "intersex",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _column(fieldnames: list[str], needle: str) -> str:
    exact = [name for name in fieldnames if name.strip().lower() == needle]
    if len(exact) == 1:
        return exact[0]
    matches = [name for name in fieldnames if needle in name.lower()]
    if len(matches) != 1:
        raise ValueError(f"expected one demographics column containing {needle!r}; found {matches}")
    return matches[0]


def _evidence_key(path: Path) -> bytes:
    key = path.read_bytes().strip()
    if len(key) < 32:
        raise ValueError("cohort evidence HMAC key must contain at least 32 bytes")
    return key


def _hmac_file(path: Path, key: bytes) -> str:
    digest = hmac.new(key, digestmod=hashlib.sha256)
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_cohort(subjects_path: Path, demographics_path: Path,
                evidence_key: bytes) -> dict[str, Any]:
    subjects = [line.strip() for line in subjects_path.read_text().splitlines() if line.strip()]
    if not subjects or len(subjects) != len(set(subjects)):
        raise ValueError("subject list must be non-empty and contain no duplicates")
    invalid = [subject for subject in subjects if not SUBJECT_RE.fullmatch(subject)]
    if invalid:
        raise ValueError(f"invalid BIDS subject IDs: {invalid[:5]}")

    with demographics_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        id_col = _column(fields, "id")
        age_col = _column(fields, "age")
        sex_col = _column(fields, "sex")
        rows = list(reader)
    ids = [str(row[id_col]).strip() for row in rows]
    if any(not value for value in ids):
        raise ValueError("demographics contains an empty subject ID")
    if len(ids) != len(set(ids)):
        raise ValueError("demographics contains duplicate subject IDs")
    by_id = {str(row[id_col]).strip(): row for row in rows}
    missing = sorted(set(subjects) - set(by_id))
    if missing:
        raise ValueError(f"demographics missing {len(missing)} listed subjects: {missing[:5]}")
    ages, sexes = [], []
    for subject in subjects:
        row = by_id[subject]
        try:
            age = float(row[age_col])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid age for {subject}") from exc
        if not math.isfinite(age) or not 0 <= age <= 120:
            raise ValueError(f"age outside supported range for {subject}")
        ages.append(age)
        raw_sex = str(row[sex_col]).strip().lower()
        sex = SEX_CATEGORIES.get(raw_sex)
        if sex is None:
            raise ValueError(
                f"unsupported sex category for {subject}; use female, male, other, or intersex"
            )
        sexes.append(sex)
    return {
        "subject_count": len(subjects),
        "age_has_variance": len(set(ages)) > 1,
        "sex_has_variance": len(set(sexes)) > 1,
        "demographic_rows": len(rows),
        # Subject lists and small demographic tables are dictionary-correlatable under plain
        # SHA-256. A release-workstation-only site key produces non-portable evidence digests.
        "subjects_hmac_sha256": _hmac_file(subjects_path, evidence_key),
        "demographics_hmac_sha256": _hmac_file(demographics_path, evidence_key),
    }


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def profile_document_sha256(profile: dict[str, Any]) -> str:
    """Hash exactly the immutable profile fields enforced by API/worker contracts."""
    return canonical_json_sha256({
        key: profile.get(key) for key in (
            "code", "version", "name", "method", "detector_id", "selector",
            "artifact_manifest", "parameters",
        )
    })


def _safe_relative(value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or path == Path("."):
        raise ValueError(f"{label} must be a non-empty relative path without '..'")
    return path


def _scientific_validation(path: Path, *, code: str, version: int,
                           detector_id: str, build_images: dict[str, str]) -> dict[str, Any]:
    """Validate a minimized site acceptance summary before binding it into a profile."""
    report = _json(path)
    if report.get("schema_version") != 1 or report.get("profile") != {
            "code": code, "version": version, "detector_id": detector_id}:
        raise ValueError("validation report is not bound to this exact profile")
    for field in ("approval_id", "independent_reviewer", "approved_at"):
        if not isinstance(report.get(field), str) or not report[field].strip():
            raise ValueError(f"validation report {field} is missing")
    try:
        approved_at = datetime.fromisoformat(report["approved_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("validation report approved_at must be ISO-8601") from exc
    if approved_at.tzinfo is None:
        raise ValueError("validation report approved_at must include a timezone")
    fingerprints = report.get("acquisition_fingerprints")
    if (not isinstance(fingerprints, list) or not fingerprints
            or len(fingerprints) != len(set(fingerprints))
            or any(re.fullmatch(r"[0-9a-f]{64}", str(value)) is None
                   for value in fingerprints)):
        raise ValueError("validation report needs unique SHA-256 acquisition fingerprints")
    qc = report.get("qc")
    if (not isinstance(qc, dict) or isinstance(qc.get("included"), bool)
            or not isinstance(qc.get("included"), int) or qc["included"] < 20
            or isinstance(qc.get("excluded"), bool)
            or not isinstance(qc.get("excluded"), int) or qc["excluded"] < 0):
        raise ValueError("validation report QC counts are incomplete")
    holdout = report.get("holdout")
    count_fields = ("positive_cases", "negative_cases", "control_cases")
    if (not isinstance(holdout, dict)
            or any(isinstance(holdout.get(field), bool)
                   or not isinstance(holdout.get(field), int) or holdout[field] < 1
                   for field in count_fields)
            or holdout.get("case_count") != sum(holdout[field] for field in count_fields)):
        raise ValueError("validation report needs positive, negative, and control holdouts")
    for field in ("metrics_sha256", "golden_case_evidence_sha256", "methodology_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(report.get(field, ""))) is None:
            raise ValueError(f"validation report {field} must be a SHA-256 digest")
    if (not isinstance(build_images, dict) or not build_images
            or any(OCI_DIGEST_RE.fullmatch(str(value)) is None
                   for value in build_images.values())
            or report.get("image_digests") != build_images):
        raise ValueError("validation report build images differ from profile build images")
    return report


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def build_expected_inventory(args: argparse.Namespace) -> int:
    """Create the exact active-profile inventory consumed by server readiness."""
    if args.output.exists():
        raise ValueError("expected-profile inventory already exists; write a new release input")
    inventory = []
    seen_codes: set[str] = set()
    for path in args.profile:
        profile = _json(path)
        code = str(profile.get("code", ""))
        version = profile.get("version")
        detector = profile.get("detector_id")
        if not PROFILE_CODE_RE.fullmatch(code):
            raise ValueError(f"{path}: invalid profile code")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError(f"{path}: invalid profile version")
        if detector not in {"meld_fcd", "map"}:
            raise ValueError(f"{path}: only MELD/MAP profiles belong in the active inventory")
        if code in seen_codes:
            raise ValueError(f"two active versions use profile code {code!r}")
        seen_codes.add(code)
        inventory.append({
            "code": code,
            "version": version,
            "detector_id": detector,
            "document_sha256": profile_document_sha256(profile),
        })
    if not inventory:
        raise ValueError("expected-profile inventory needs at least one profile")
    inventory.sort(key=lambda item: (item["code"], item["version"]))
    _write_json(args.output, inventory)
    print(json.dumps({"inventory": str(args.output), "profiles": len(inventory)}, indent=2))
    return 0


def _read_inventory(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text())
    if not isinstance(value, list) or not value:
        raise ValueError("expected-profile inventory must be a non-empty JSON array")
    required = {"code", "version", "detector_id", "document_sha256"}
    seen_codes: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("expected-profile inventory entry has an invalid schema")
        if (not PROFILE_CODE_RE.fullmatch(str(item["code"]))
                or not isinstance(item["version"], int) or isinstance(item["version"], bool)
                or item["version"] < 1 or item["detector_id"] not in {"meld_fcd", "map"}
                or re.fullmatch(r"[0-9a-f]{64}", str(item["document_sha256"])) is None):
            raise ValueError("expected-profile inventory entry contains an invalid value")
        if item["code"] in seen_codes:
            raise ValueError("expected-profile inventory contains two versions of one code")
        seen_codes.add(item["code"])
    return value


def verify_expected_inventory(args: argparse.Namespace) -> int:
    inventory = _read_inventory(args.inventory)
    profiles: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for path in sorted(args.profiles.glob("*.json")):
        profile = _json(path)
        key = (str(profile.get("code", "")), profile.get("version"))
        profiles.setdefault(key, []).append(profile)
    for item in inventory:
        matches = profiles.get((item["code"], item["version"]), [])
        if len(matches) != 1 or profile_document_sha256(matches[0]) != item["document_sha256"]:
            raise ValueError(
                f"expected profile {item['code']} v{item['version']} is absent or differs"
            )
        if matches[0].get("detector_id") != item["detector_id"]:
            raise ValueError(f"expected profile {item['code']} has the wrong detector")
    print(json.dumps({"ok": True, "expected_profiles": len(inventory)}, indent=2))
    return 0


def _image_lock(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_no, raw in enumerate(path.read_text().splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 2 or parts[0] in result:
            raise ValueError(f"{path}:{line_no}: malformed or duplicate image-lock row")
        role, reference = parts
        if OCI_DIGEST_RE.fullmatch(reference) is None:
            raise ValueError(f"{path}:{line_no}: image {role!r} is not digest-pinned")
        result[role] = reference
    return result


def verify_runtime_images(args: argparse.Namespace) -> int:
    """Prove profile-building images equal the detector images in this release lock."""
    profile = _json(args.profile)
    images = _image_lock(args.image_lock)
    method = profile.get("method")
    if method == "meld_distributed_combat":
        expected = {"meld": images.get("meld_graph")}
    elif method == "map_normative":
        expected = {"spm": images.get("spm"), "pkg": images.get("pkg")}
    else:
        raise ValueError(f"unsupported harmonization method {method!r}")
    if any(value is None for value in expected.values()):
        raise ValueError("release image lock lacks a profile build-image role")
    actual = (profile.get("parameters") or {}).get("build_images")
    if actual != expected:
        raise ValueError(
            "profile build images differ from the release detector/runtime image lock"
        )
    print(json.dumps({"ok": True, "profile": str(args.profile), "images": expected}, indent=2))
    return 0


def prepare(args: argparse.Namespace) -> int:
    if not CODE_RE.fullmatch(args.code):
        raise ValueError("harmonization code must start with H and contain safe identifier characters")
    cohort = load_cohort(
        args.subjects, args.demographics, _evidence_key(args.evidence_hmac_key_file))
    eligible = (cohort["subject_count"] >= args.minimum_subjects
                and cohort["age_has_variance"] and cohort["sex_has_variance"])
    if not eligible and not args.allow_ineligible_draft:
        raise ValueError(
            f"cohort is not activation-eligible: need >= {args.minimum_subjects} subjects and "
            "non-zero age/sex variance (use --allow-ineligible-draft only for method development)"
        )
    if OCI_DIGEST_RE.fullmatch(args.image) is None:
        raise ValueError("--image must be a fully qualified immutable OCI digest reference")
    if args.version < 1 or args.minimum_subjects < 20:
        raise ValueError("version must be positive and minimum-subjects must be at least 20")
    selector = _json(args.selector)
    if not selector:
        raise ValueError("selector must not be empty")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    command = [
        "python", "scripts/new_patient_pipeline/new_pt_pipeline.py",
        "-harmo_code", args.code,
        "-ids", f"/data/{args.subjects.name}",
        "-demos", f"/data/{args.demographics.name}",
        "--harmo_only",
    ]
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code": args.code,
        "version": args.version,
        "method": "meld_distributed_combat",
        "minimum_subjects": args.minimum_subjects,
        "activation_eligible": eligible,
        "cohort": cohort,
        "container_image": args.image,
        "container_command": command,
        "selector_sha256": sha256_file(args.selector),
        "selector_canonical_sha256": canonical_json_sha256(selector),
    }
    manifest_path = output / "cohort-manifest.json"
    _write_json(manifest_path, manifest)
    profile = {
        "code": args.code,
        "version": args.version,
        "name": args.name,
        "method": "meld_distributed_combat",
        "detector_id": "meld_fcd",
        "selector": selector,
        "artifact_manifest": {"files": []},
        "parameters": {
            "harmo_code": args.code,
            "cohort_manifest_sha256": sha256_file(manifest_path),
            "activation_eligible": eligible,
            "control_count": cohort["subject_count"],
            "minimum_subjects": args.minimum_subjects,
            "selector_canonical_sha256": canonical_json_sha256(selector),
            "build_images": {"meld": args.image},
        },
    }
    _write_json(output / "profile-draft.json", profile)
    print(json.dumps({"manifest": str(manifest_path), "profile": str(output / "profile-draft.json"),
                      "container_command": command, "activation_eligible": eligible}, indent=2))
    return 0


def finalize(args: argparse.Namespace) -> int:
    draft = _json(args.profile_draft)
    code = str(draft.get("code", ""))
    if not CODE_RE.fullmatch(code):
        raise ValueError("profile draft has invalid harmonization code")
    cohort_manifest = args.profile_draft.parent / "cohort-manifest.json"
    cohort = _json(cohort_manifest)
    if not cohort.get("activation_eligible"):
        raise ValueError("cohort manifest is not activation-eligible; do not finalize this draft")
    expected_cohort_hash = draft.get("parameters", {}).get("cohort_manifest_sha256")
    if expected_cohort_hash != sha256_file(cohort_manifest):
        raise ValueError("profile draft does not match its cohort manifest")
    selector_hash = canonical_json_sha256(draft.get("selector"))
    if (selector_hash != cohort.get("selector_canonical_sha256")
            or selector_hash != draft.get("parameters", {}).get(
                "selector_canonical_sha256")):
        raise ValueError("profile selector changed after cohort preparation")
    draft.setdefault("parameters", {})["scientific_validation"] = _scientific_validation(
        args.validation_report,
        code=code,
        version=int(draft.get("version", 0)),
        detector_id="meld_fcd",
        build_images=draft["parameters"].get("build_images", {}),
    )
    source_root = args.artifact_source.resolve(strict=True)
    output = args.output.resolve()
    if output == source_root or source_root in output.parents:
        raise ValueError("artifact output must not be inside the artifact source tree")
    if output.exists():
        raise ValueError("artifact output already exists; use a new versioned directory")
    if args.final_profile.exists():
        raise ValueError("final profile already exists; profiles are immutable")
    manifest_prefix = _safe_relative(args.manifest_prefix, "manifest-prefix")
    expected_name = f"MELD_{code}combat_parameters.hdf5"
    candidates = sorted(path for path in source_root.rglob(expected_name)
                        if path.is_file() and not path.is_symlink())
    if len(candidates) != 1:
        raise ValueError(
            f"expected exactly one {expected_name!r} under {source_root}; found {len(candidates)}"
        )
    output.mkdir(parents=True, exist_ok=True)
    files = []
    for source in candidates:
        destination = output / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        manifest_path = manifest_prefix / source.name
        files.append({"path": manifest_path.as_posix(), "sha256": sha256_file(destination),
                      "size": destination.stat().st_size})
    draft["artifact_manifest"] = {
        "schema_version": 1,
        "files": files,
        "cohort_manifest_sha256": sha256_file(cohort_manifest),
    }
    draft.setdefault("parameters", {})["data_root"] = manifest_prefix.as_posix()
    final_path = args.final_profile
    _write_json(final_path, draft)
    print(json.dumps({"profile": str(final_path), "artifacts": len(files),
                      "artifact_root": str(output)}, indent=2))
    return 0


def _map_artifacts(source_root: Path) -> dict[str, Path]:
    """Validate a complete MAP control set on one finite, consistent MNI grid."""
    try:
        import nibabel as nib
        import numpy as np
    except ImportError as exc:
        raise ValueError("MAP finalization requires nibabel and numpy in meld-dev") from exc

    normative = source_root / "normative" / "map"
    paths = {name: normative / name for name in MAP_FILES}
    missing = [name for name, path in paths.items() if not path.is_file() or path.is_symlink()]
    if missing:
        raise ValueError(f"MAP artifact source is incomplete; missing {missing}")
    reference_shape = None
    reference_affine = None
    for name, path in paths.items():
        image = nib.load(path)
        data = np.asanyarray(image.dataobj, dtype=np.float32)
        affine = np.asarray(image.affine, dtype=np.float64)
        if data.ndim != 3 or 0 in data.shape or not np.all(np.isfinite(data)):
            raise ValueError(f"MAP artifact is not a finite non-empty 3D NIfTI: {name}")
        if (not np.all(np.isfinite(affine))
                or abs(float(np.linalg.det(affine[:3, :3]))) < 1e-8):
            raise ValueError(f"MAP artifact has an invalid affine: {name}")
        if reference_shape is None:
            reference_shape, reference_affine = data.shape, affine
        elif data.shape != reference_shape or not np.allclose(
                affine, reference_affine, atol=1e-4, rtol=1e-6):
            raise ValueError(f"MAP artifacts do not share one exact analysis grid: {name}")
        if name.endswith("_std.nii.gz"):
            if np.any(data < 0) or not np.any(data > 1e-6):
                raise ValueError(f"MAP standard-deviation artifact is invalid: {name}")
    return paths


def map_finalize(args: argparse.Namespace) -> int:
    """Package precomputed MAP control statistics as one scanner/protocol profile."""
    if not PROFILE_CODE_RE.fullmatch(args.code):
        raise ValueError("profile code must contain only safe identifier characters")
    if args.version < 1 or args.minimum_subjects < 20:
        raise ValueError("version must be positive and minimum-subjects must be at least 20")
    for label, image in (("spm-image", args.spm_image), ("pkg-image", args.pkg_image)):
        if OCI_DIGEST_RE.fullmatch(image) is None:
            raise ValueError(f"--{label} must be a fully qualified immutable OCI digest reference")
    cohort = load_cohort(
        args.subjects, args.demographics, _evidence_key(args.evidence_hmac_key_file))
    eligible = (cohort["subject_count"] >= args.minimum_subjects
                and cohort["age_has_variance"] and cohort["sex_has_variance"])
    if not eligible:
        raise ValueError(
            f"MAP cohort is not activation-eligible: need >= {args.minimum_subjects} controls "
            "and non-zero age/sex variance"
        )
    selector = _json(args.selector)
    if not selector:
        raise ValueError("selector must not be empty")
    source_root = args.artifact_source.resolve(strict=True)
    sources = _map_artifacts(source_root)
    output = args.output.resolve()
    if output == source_root or source_root in output.parents:
        raise ValueError("artifact output must not be inside the artifact source tree")
    for path, label in ((output, "artifact output"), (args.final_profile, "final profile"),
                        (args.cohort_manifest, "cohort manifest")):
        if path.exists():
            raise ValueError(f"{label} already exists; use a new versioned path")
    prefix = _safe_relative(args.manifest_prefix, "manifest-prefix")

    cohort_document = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code": args.code,
        "version": args.version,
        "method": "map_normative",
        "minimum_subjects": args.minimum_subjects,
        "activation_eligible": True,
        "cohort": cohort,
        "selector_sha256": sha256_file(args.selector),
        "selector_canonical_sha256": canonical_json_sha256(selector),
        "build_images": {"spm": args.spm_image, "pkg": args.pkg_image},
    }
    _write_json(args.cohort_manifest, cohort_document)
    cohort_hash = sha256_file(args.cohort_manifest)
    build_images = {"spm": args.spm_image, "pkg": args.pkg_image}
    scientific_validation = _scientific_validation(
        args.validation_report,
        code=args.code,
        version=args.version,
        detector_id="map",
        build_images=build_images,
    )

    destination_root = output / "normative" / "map"
    destination_root.mkdir(parents=True)
    files = []
    for name in MAP_FILES:
        destination = destination_root / name
        shutil.copy2(sources[name], destination)
        relative = prefix / "normative" / "map" / name
        files.append({"path": relative.as_posix(), "sha256": sha256_file(destination),
                      "size": destination.stat().st_size})
    profile = {
        "code": args.code,
        "version": args.version,
        "name": args.name,
        "method": "map_normative",
        "detector_id": "map",
        "selector": selector,
        "artifact_manifest": {
            "schema_version": 1,
            "files": files,
            "cohort_manifest_sha256": cohort_hash,
            "selector_canonical_sha256": canonical_json_sha256(selector),
        },
        "parameters": {
            "activation_eligible": True,
            "control_count": cohort["subject_count"],
            "minimum_subjects": args.minimum_subjects,
            "cohort_manifest_sha256": cohort_hash,
            "selector_canonical_sha256": canonical_json_sha256(selector),
            "data_root": prefix.as_posix(),
            "build_images": build_images,
            "scientific_validation": scientific_validation,
        },
    }
    _write_json(args.final_profile, profile)
    print(json.dumps({"profile": str(args.final_profile), "cohort_manifest": str(
        args.cohort_manifest), "artifacts": len(files), "artifact_root": str(output)}, indent=2))
    return 0


def verify(args: argparse.Namespace) -> int:
    profile = _json(args.profile)
    root = args.harmonization_root.resolve(strict=True)
    failures = []
    files = profile.get("artifact_manifest", {}).get("files", [])
    if not isinstance(files, list) or not files:
        print(json.dumps({"ok": False, "failures": ["profile has no artifacts"]}, indent=2))
        return 1
    seen: set[str] = set()
    for item in files:
        try:
            relative = _safe_relative(str(item["path"]), "artifact path")
        except (KeyError, ValueError) as exc:
            failures.append(str(exc))
            continue
        normalized = relative.as_posix()
        if normalized in seen:
            failures.append(f"duplicate artifact path: {relative}")
            continue
        seen.add(normalized)
        candidate = root / relative
        components = [root / Path(*relative.parts[:index])
                      for index in range(1, len(relative.parts) + 1)]
        if any(component.is_symlink() for component in components):
            failures.append(f"symlink not allowed: {relative}")
            continue
        path = candidate.resolve()
        if root not in path.parents or not path.is_file():
            failures.append(f"missing/outside root: {relative}")
        elif not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")).lower()):
            failures.append(f"invalid hash: {relative}")
        elif sha256_file(path) != str(item["sha256"]).lower():
            failures.append(f"hash mismatch: {relative}")
        elif item.get("size") is not None and path.stat().st_size != int(item["size"]):
            failures.append(f"size mismatch: {relative}")
    if failures:
        print(json.dumps({"ok": False, "failures": failures}, indent=2))
        return 1
    print(json.dumps({"ok": True, "artifacts": len(files)}, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--code", required=True)
    prep.add_argument("--version", type=int, default=1)
    prep.add_argument("--name", required=True)
    prep.add_argument("--subjects", required=True, type=Path)
    prep.add_argument("--demographics", required=True, type=Path)
    prep.add_argument("--selector", required=True, type=Path)
    prep.add_argument("--image", required=True)
    prep.add_argument("--output", required=True, type=Path)
    prep.add_argument("--minimum-subjects", type=int, default=20)
    prep.add_argument("--allow-ineligible-draft", action="store_true")
    prep.add_argument("--evidence-hmac-key-file", required=True, type=Path)
    prep.set_defaults(func=prepare)
    final = sub.add_parser("finalize")
    final.add_argument("--profile-draft", required=True, type=Path)
    final.add_argument("--artifact-source", required=True, type=Path)
    final.add_argument("--output", required=True, type=Path)
    final.add_argument("--manifest-prefix", required=True)
    final.add_argument("--final-profile", required=True, type=Path)
    final.add_argument("--validation-report", required=True, type=Path)
    final.set_defaults(func=finalize)
    map_final = sub.add_parser("map-finalize")
    map_final.add_argument("--code", required=True)
    map_final.add_argument("--version", type=int, default=1)
    map_final.add_argument("--name", required=True)
    map_final.add_argument("--subjects", required=True, type=Path)
    map_final.add_argument("--demographics", required=True, type=Path)
    map_final.add_argument("--selector", required=True, type=Path)
    map_final.add_argument("--artifact-source", required=True, type=Path)
    map_final.add_argument("--output", required=True, type=Path)
    map_final.add_argument("--manifest-prefix", required=True)
    map_final.add_argument("--final-profile", required=True, type=Path)
    map_final.add_argument("--cohort-manifest", required=True, type=Path)
    map_final.add_argument("--spm-image", required=True)
    map_final.add_argument("--pkg-image", required=True)
    map_final.add_argument("--minimum-subjects", type=int, default=20)
    map_final.add_argument("--validation-report", required=True, type=Path)
    map_final.add_argument("--evidence-hmac-key-file", required=True, type=Path)
    map_final.set_defaults(func=map_finalize)
    check = sub.add_parser("verify")
    check.add_argument("--profile", required=True, type=Path)
    check.add_argument("--harmonization-root", required=True, type=Path)
    check.set_defaults(func=verify)
    inventory = sub.add_parser("expected-inventory")
    inventory.add_argument("--profile", required=True, action="append", type=Path)
    inventory.add_argument("--output", required=True, type=Path)
    inventory.set_defaults(func=build_expected_inventory)
    inventory_check = sub.add_parser("verify-expected-inventory")
    inventory_check.add_argument("--inventory", required=True, type=Path)
    inventory_check.add_argument("--profiles", required=True, type=Path)
    inventory_check.set_defaults(func=verify_expected_inventory)
    runtime_check = sub.add_parser("verify-runtime-images")
    runtime_check.add_argument("--profile", required=True, type=Path)
    runtime_check.add_argument("--image-lock", required=True, type=Path)
    runtime_check.set_defaults(func=verify_runtime_images)
    return ap


def main() -> int:
    try:
        args = parser().parse_args()
        return args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
