"""Worker-side verification of immutable harmonization profile contracts."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import wsettings

_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


@dataclass(frozen=True)
class ResolvedHarmonization:
    code: str
    version: int
    method: str
    host_data_root: str
    metadata: dict[str, Any]
    parameters: dict[str, Any]
    applied: bool = True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_harmonization(raw: Any) -> ResolvedHarmonization | None:
    """Fail closed unless every requested profile artifact exists under the configured root."""
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError("run.params.harmonization must be an object")
    if raw.get("mode") in {"unharmonized", "not_applicable"}:
        mode = str(raw["mode"])
        reason = str(raw.get("reason", "")).strip() or (
            "explicitly confirmed without a harmonization profile"
        )
        return ResolvedHarmonization(
            code="none", version=0, method=mode, host_data_root="",
            metadata={"mode": mode, "reason": reason, "applied": False},
            parameters={}, applied=False,
        )
    code, method = str(raw.get("code", "")), str(raw.get("method", ""))
    name = str(raw.get("name", "")).strip()
    if not _CODE_RE.fullmatch(code):
        raise ValueError("harmonization code must be 1-64 safe characters")
    try:
        version = int(raw["version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("harmonization profile requires a positive integer version") from exc
    if version < 1 or not method or not name:
        raise ValueError("harmonization profile requires name, version >= 1, and method")
    profile_document = {
        "code": code,
        "version": version,
        "name": name,
        "method": method,
        "detector_id": raw.get("detector_id"),
        "selector": raw.get("selector"),
        "artifact_manifest": raw.get("artifact_manifest"),
        "parameters": raw.get("parameters"),
    }
    expected_profile_hash = str(raw.get("profile_document_sha256", ""))
    actual_profile_hash = hashlib.sha256(json.dumps(
        profile_document, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()
    if (re.fullmatch(r"[0-9a-f]{64}", expected_profile_hash) is None
            or expected_profile_hash != actual_profile_hash):
        raise ValueError("harmonization profile document hash is missing or inconsistent")
    if wsettings.deployment_mode in {"research", "production"}:
        identifier = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z",
            re.IGNORECASE,
        )
        if (identifier.fullmatch(str(raw.get("profile_id", ""))) is None
                or identifier.fullmatch(str(raw.get("assignment_id", ""))) is None):
            raise ValueError("server harmonization contract requires profile and assignment IDs")
        if re.fullmatch(r"[0-9a-f]{64}", str(raw.get("acquisition_fingerprint", ""))) is None:
            raise ValueError("server harmonization contract requires an acquisition fingerprint")
        if bool(raw.get("selector_override")) != bool(raw.get("override_reason_present")):
            raise ValueError("harmonization selector override provenance is inconsistent")
    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("harmonization parameters must be an object")
    if wsettings.deployment_mode in {"research", "production"}:
        expected_build_images = {
            "meld_distributed_combat": {"meld": wsettings.meld_image},
            "map_normative": {"spm": wsettings.map_image, "pkg": wsettings.pkg_image},
        }.get(method)
        if expected_build_images is None:
            raise ValueError(f"unsupported server harmonization method {method!r}")
        if parameters.get("build_images") != expected_build_images:
            raise ValueError(
                "harmonization profile build images differ from this worker release"
            )
    manifest = raw.get("artifact_manifest") or {}
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        raise ValueError("harmonization artifact_manifest.files must be a list")
    if not files:
        raise ValueError("applied harmonization profiles require pinned artifacts")

    try:
        scope = parameters.get("storage_scope")
        if scope not in {None, "release", "generated"}:
            raise ValueError("unknown harmonization artifact storage scope")
        configured_root = (wsettings.harmonization_generated_root
                           if scope == "generated" else wsettings.harmonization_root)
        root = Path(configured_root).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"harmonization root is unavailable: {exc}") from exc
    verified: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            raise ValueError("each harmonization artifact requires path and sha256")
        relative = Path(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe harmonization artifact path: {relative}")
        candidate = root / relative
        components = [root / Path(*relative.parts[:index])
                      for index in range(1, len(relative.parts) + 1)]
        if any(component.is_symlink() for component in components):
            raise ValueError(f"harmonization artifact path contains a symlink: {relative}")
        try:
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"harmonization artifact is unavailable: {relative}") from exc
        if root not in path.parents or not path.is_file():
            raise ValueError(f"harmonization artifact escapes root or is not regular: {relative}")
        expected = str(item["sha256"]).lower()
        actual = _sha256(path)
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
            raise ValueError(f"harmonization artifact hash mismatch: {relative}")
        size = path.stat().st_size
        if item.get("size") is not None and size != int(item["size"]):
            raise ValueError(f"harmonization artifact size mismatch: {relative}")
        verified.append({"path": relative.as_posix(), "sha256": actual, "size": size})

    data_root_rel = Path(str(parameters.get("data_root", ".")))
    if data_root_rel.is_absolute() or ".." in data_root_rel.parts:
        raise ValueError("harmonization parameters.data_root must be relative")
    try:
        data_root = (root / data_root_rel).resolve(strict=True)
    except OSError as exc:
        raise ValueError("harmonization data_root is unavailable") from exc
    if data_root != root and root not in data_root.parents:
        raise ValueError("harmonization data_root escapes configured root")
    if not data_root.is_dir():
        raise ValueError("harmonization data_root is not a directory")
    for item in verified:
        artifact = (root / item["path"]).resolve()
        if artifact.parent != data_root and data_root not in artifact.parents:
            raise ValueError(f"artifact {item['path']} is outside profile data_root")

    # The mounted directory is the detector's complete view of the profile.  Reject unlisted files
    # so a valid hash list cannot coexist with a second, silently consumed parameter set.
    listed_under_root = {
        (root / item["path"]).resolve().relative_to(data_root).as_posix()
        for item in verified
    }
    actual_under_root: set[str] = set()
    for candidate in data_root.rglob("*"):
        if candidate.is_symlink():
            raise ValueError("harmonization data_root must not contain symlinks")
        if candidate.is_file():
            actual_under_root.add(candidate.relative_to(data_root).as_posix())
    if actual_under_root != listed_under_root:
        raise ValueError("harmonization data_root contains unlisted or missing files")

    if method == "meld_distributed_combat":
        expected = {f"MELD_{code}combat_parameters.hdf5"}
        if listed_under_root != expected:
            raise ValueError(f"MELD profile must contain exactly {sorted(expected)}")
    elif method == "map_normative":
        required = {
            f"normative/map/{feature}_{stat}.nii.gz"
            for feature in ("junction", "extension") for stat in ("mean", "std")
        }
        if not required.issubset(listed_under_root):
            raise ValueError("MAP profile is missing canonical normative/map artifacts")

    canonical = json.dumps(verified, sort_keys=True, separators=(",", ":")).encode()
    metadata = {
        "profile_id": raw.get("profile_id"),
        "code": code,
        "version": version,
        "method": method,
        "parameters": parameters,
        "artifacts": verified,
        "artifact_manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "assignment_id": raw.get("assignment_id"),
        "acquisition_fingerprint": raw.get("acquisition_fingerprint"),
        "selector_override": raw.get("selector_override") is True,
        "override_reason_present": raw.get("override_reason_present") is True,
    }
    return ResolvedHarmonization(code, version, method, str(data_root), metadata, parameters, True)
