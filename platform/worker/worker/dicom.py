"""Exact-series DICOM acquisition into an immutable local staging directory.

Only the SeriesInstanceUIDs confirmed in a run contract are acquired.  WADO retrieval and local
imports share the same validation path and are published with an atomic rename only after every
requested series, instance identity, expected count, patient/study identity, and file hash has
been checked.  Compute containers therefore see only a complete local snapshot, never a caller
supplied path or a partially downloaded study.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app import audit
from app.harmonization import acquisition_fingerprint, canonical_acquisition

from .config import wsettings

_UID_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_MARKER = ".complete.json"
_MANIFEST = "manifest.json"


class DicomStagingError(RuntimeError):
    """Input identity/completeness could not be proven."""


@dataclass(frozen=True)
class AcquisitionRequest:
    run_id: str
    series_by_role: dict[str, str]
    study_uid: str | None = None
    expected_counts: dict[str, int] | None = None
    expected_fingerprints: dict[str, str] | None = None
    expected_acquisitions: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.series_by_role:
            raise DicomStagingError("at least one confirmed SeriesInstanceUID is required")
        for role, uid in self.series_by_role.items():
            if not role or not _valid_uid(uid):
                raise DicomStagingError(f"invalid series acquisition entry {role!r}={uid!r}")
        if len(set(self.series_by_role.values())) != len(self.series_by_role):
            raise DicomStagingError("one SeriesInstanceUID cannot satisfy multiple acquisition roles")
        if self.study_uid and not _valid_uid(self.study_uid):
            raise DicomStagingError(f"invalid StudyInstanceUID: {self.study_uid!r}")
        try:
            counts = [int(value) for value in (self.expected_counts or {}).values()]
        except (TypeError, ValueError) as exc:
            raise DicomStagingError("expected instance counts must be integers") from exc
        if any(value < 1 for value in counts):
            raise DicomStagingError("expected instance counts must be positive")
        expected_total = sum(counts)
        if expected_total > wsettings.dicom_max_instances_per_run:
            raise DicomStagingError(
                f"requested instance count {expected_total} exceeds configured limit")
        fingerprints = self.expected_fingerprints or {}
        if (set(fingerprints) - set(self.series_by_role.values())
                or any(re.fullmatch(r"[0-9a-f]{64}", str(value)) is None
                       for value in fingerprints.values())):
            raise DicomStagingError("expected acquisition fingerprints are invalid")
        acquisitions = self.expected_acquisitions or {}
        if (set(acquisitions) != set(fingerprints)
                or any(not isinstance(value, dict) or not canonical_acquisition(value)
                       for value in acquisitions.values())):
            raise DicomStagingError("expected acquisition metadata is missing or invalid")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", self.run_id):
            raise DicomStagingError("run id is not safe for local staging")


def _valid_uid(value: Any) -> bool:
    value = str(value or "")
    return len(value) <= 64 and _UID_RE.fullmatch(value) is not None


def _dataset_value(ds: Any, keyword: str, tag: str) -> str:
    """Read a UID/patient identifier from pydicom datasets or DICOM JSON mappings."""
    if isinstance(ds, dict):
        value = ds.get(keyword, ds.get(tag))
        if isinstance(value, dict):
            vals = value.get("Value") or []
            value = vals[0] if vals else ""
        return str(value or "")
    return str(getattr(ds, keyword, "") or "")


def _identities(ds: Any) -> dict[str, str]:
    return {
        "study_uid": _dataset_value(ds, "StudyInstanceUID", "0020000D"),
        "series_uid": _dataset_value(ds, "SeriesInstanceUID", "0020000E"),
        "sop_uid": _dataset_value(ds, "SOPInstanceUID", "00080018"),
        "patient_id": _dataset_value(ds, "PatientID", "00100020"),
        "issuer": _dataset_value(ds, "IssuerOfPatientID", "00100021"),
        "patient_name": _dataset_value(ds, "PatientName", "00100010"),
    }


def _dataset_values(ds: Any, keyword: str, tag: str) -> list[Any]:
    if isinstance(ds, dict):
        value = ds.get(keyword, ds.get(tag, {}))
        if isinstance(value, dict):
            return list(value.get("Value") or [])
        return [] if value in (None, "") else [value]
    value = getattr(ds, keyword, None)
    if value in (None, ""):
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _first(ds: Any, keyword: str, tag: str) -> Any:
    values = _dataset_values(ds, keyword, tag)
    return values[0] if values else None


def _number(value: Any, cast=float) -> Any:
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _acquisition(ds: Any) -> dict[str, Any]:
    """Extract the same minimized protected fields used by API QIDO fingerprinting."""
    return {
        "manufacturer": _first(ds, "Manufacturer", "00080070"),
        "model": _first(ds, "ManufacturerModelName", "00081090"),
        "station_name": _first(ds, "StationName", "00081010"),
        "field_strength_t": _number(_first(ds, "MagneticFieldStrength", "00180087")),
        "software_versions": _dataset_values(ds, "SoftwareVersions", "00181020"),
        "protocol_name": _first(ds, "ProtocolName", "00181030"),
        "sequence_name": _first(ds, "SequenceName", "00180024"),
        "scanning_sequence": _dataset_values(ds, "ScanningSequence", "00180020"),
        "repetition_time_ms": _number(_first(ds, "RepetitionTime", "00180080")),
        "echo_time_ms": _number(_first(ds, "EchoTime", "00180081")),
        "inversion_time_ms": _number(_first(ds, "InversionTime", "00180082")),
        "flip_angle_deg": _number(_first(ds, "FlipAngle", "00181314")),
        "slice_thickness_mm": _number(_first(ds, "SliceThickness", "00180050")),
        "spacing_between_slices_mm": _number(
            _first(ds, "SpacingBetweenSlices", "00180088")),
        "receive_coil_name": _first(ds, "ReceiveCoilName", "00181250"),
        "transmit_coil_name": _first(ds, "TransmitCoilName", "00181251"),
        "imaged_nucleus": _first(ds, "ImagedNucleus", "00180085"),
        "pixel_bandwidth_hz": _number(_first(ds, "PixelBandwidth", "00180095")),
        "percent_phase_fov": _number(_first(ds, "PercentPhaseFieldOfView", "00180094")),
        "acquisition_matrix": [
            value for value in (
                _number(raw, int) for raw in _dataset_values(
                    ds, "AcquisitionMatrix", "00181310"))
            if value is not None
        ],
        "phase_encoding_direction": _first(
            ds, "InPlanePhaseEncodingDirection", "00181312"),
        "phase_encoding_steps": _number(
            _first(ds, "NumberOfPhaseEncodingSteps", "00180089"), int),
        "parallel_acquisition": _first(
            ds, "ParallelAcquisitionTechnique", "00189077"),
        "parallel_technique": _first(
            ds, "ParallelAcquisitionTechniqueDescription", "00189078"),
        "acceleration_factor_in_plane": _number(
            _first(ds, "ParallelReductionFactorInPlane", "00189069")),
        "acceleration_factor_out_of_plane": _number(
            _first(ds, "ParallelReductionFactorOutOfPlane", "00189155")),
        "reconstruction_diameter_mm": _number(
            _first(ds, "ReconstructionDiameter", "00181100")),
        "echo_train_length": _number(_first(ds, "EchoTrainLength", "00180091"), int),
        "number_of_averages": _number(_first(ds, "NumberOfAverages", "00180083")),
        "mr_acquisition_type": _first(ds, "MRAcquisitionType", "00180023"),
        "complex_image_component": _first(ds, "ComplexImageComponent", "00089208"),
        "acquisition_contrast": _first(ds, "AcquisitionContrast", "00089209"),
        "image_type": _dataset_values(ds, "ImageType", "00080008"),
        "rescale_slope": _number(_first(ds, "RescaleSlope", "00281053")),
        "rescale_intercept": _number(_first(ds, "RescaleIntercept", "00281052")),
        "bits_stored": _number(_first(ds, "BitsStored", "00280101"), int),
        "pixel_representation": _number(
            _first(ds, "PixelRepresentation", "00280103"), int),
        "rows": _number(_first(ds, "Rows", "00280010"), int),
        "columns": _number(_first(ds, "Columns", "00280011"), int),
        "voxel_spacing_mm": [
            value for value in (
                _number(raw) for raw in _dataset_values(ds, "PixelSpacing", "00280030"))
            if value is not None
        ],
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _patient_fingerprint(ids: dict[str, str]) -> str:
    # The manifest proves all series belong to one patient without persisting a direct identifier.
    return audit.sensitive_digest({
        "patient_id": ids["patient_id"],
        "issuer": ids["issuer"],
        "patient_name": ids["patient_name"],
    })


def _allowed_import(path: str) -> Path:
    try:
        candidate = Path(path).resolve(strict=True)
        root = Path(wsettings.dicom_import_root).resolve(strict=True)
    except OSError as exc:
        raise DicomStagingError(f"local DICOM import path is unavailable: {exc}") from exc
    if not candidate.is_dir() or (candidate != root and root not in candidate.parents):
        raise DicomStagingError(
            f"local DICOM path must be a directory below configured import root {root}")
    return candidate


def _manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _load_completed(dest: Path, request: AcquisitionRequest) -> dict | None:
    manifest_path, marker_path = dest / _MANIFEST, dest / _MARKER
    if not dest.is_dir() or not manifest_path.is_file() or not marker_path.is_file():
        return None
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        marker = json.loads(marker_path.read_text())
        if marker.get("manifest_sha256") != hashlib.sha256(raw).hexdigest():
            raise DicomStagingError("staging completion marker does not match manifest")
        if manifest.get("schema_version") != 1:
            raise DicomStagingError("unsupported DICOM staging manifest")
        if manifest.get("series_by_role") != request.series_by_role:
            raise DicomStagingError("staged series do not match the immutable run contract")
        if request.study_uid and manifest.get("study_uid") != request.study_uid:
            raise DicomStagingError("staged study does not match the immutable run contract")
        if (manifest.get("source") not in {"local-import", "orthanc-wado-rs"}
                or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get(
                    "patient_fingerprint", "")))
                or not _valid_uid(manifest.get("study_uid"))):
            raise DicomStagingError("staging manifest has invalid source/study/patient identity")
        series_rows = manifest.get("series")
        if not isinstance(series_rows, list) or not series_rows:
            raise DicomStagingError("staging manifest has no series")
        expected_series = set(request.series_by_role.values())
        seen_series: set[str] = set()
        seen_sops: set[str] = set()
        listed_files: set[str] = set()
        total_bytes = 0
        for series in series_rows:
            if not isinstance(series, dict):
                raise DicomStagingError("invalid series entry in staging manifest")
            series_uid = str(series.get("series_uid", ""))
            instances = series.get("instances")
            series_fingerprint = str(series.get("acquisition_fingerprint", ""))
            if (series_uid not in expected_series or series_uid in seen_series
                    or not isinstance(instances, list) or not instances
                    or series.get("instance_count") != len(instances)
                    or re.fullmatch(r"[0-9a-f]{64}", series_fingerprint) is None):
                raise DicomStagingError("staging manifest series identity/count is inconsistent")
            expected_fingerprint = (request.expected_fingerprints or {}).get(series_uid)
            if expected_fingerprint and series_fingerprint != expected_fingerprint:
                raise DicomStagingError("staged acquisition fingerprint differs from run contract")
            expected_count = (request.expected_counts or {}).get(series_uid)
            if expected_count is not None and len(instances) != int(expected_count):
                raise DicomStagingError("staging manifest no longer matches expected instance count")
            seen_series.add(series_uid)
            for instance in instances:
                if not isinstance(instance, dict):
                    raise DicomStagingError("invalid instance entry in staging manifest")
                sop_uid = str(instance.get("sop_uid", ""))
                relative = Path(str(instance.get("path", "")))
                expected_relative = Path("series") / series_uid / f"{sop_uid}.dcm"
                size = instance.get("size")
                digest = str(instance.get("sha256", ""))
                if (not _valid_uid(sop_uid) or sop_uid in seen_sops
                        or relative != expected_relative or relative.is_absolute()
                        or ".." in relative.parts
                        or isinstance(size, bool) or not isinstance(size, int) or size <= 0
                        or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                    raise DicomStagingError("staging manifest instance contract is invalid")
                path = dest / relative
                components = [dest / Path(*relative.parts[:index])
                              for index in range(1, len(relative.parts) + 1)]
                if (any(component.is_symlink() for component in components)
                        or not path.is_file() or path.stat().st_size != size):
                    raise DicomStagingError(f"staged instance missing/changed: {relative}")
                if _hash_file(path) != digest:
                    raise DicomStagingError(f"staged instance hash mismatch: {relative}")
                seen_sops.add(sop_uid)
                listed_files.add(relative.as_posix())
                total_bytes += size
        if seen_series != expected_series:
            raise DicomStagingError("staging manifest does not contain every requested series")
        if (len(seen_sops) > wsettings.dicom_max_instances_per_run
                or total_bytes > wsettings.dicom_max_bytes_per_run):
            raise DicomStagingError("completed DICOM staging exceeds configured limits")
        actual_files: set[str] = set()
        for dirpath, dirs, files in os.walk(dest, followlinks=False):
            if any((Path(dirpath) / name).is_symlink() for name in dirs):
                raise DicomStagingError("completed DICOM staging contains a symlinked directory")
            for filename in files:
                path = Path(dirpath) / filename
                if path.is_symlink():
                    raise DicomStagingError("completed DICOM staging contains a symlinked file")
                relative = path.relative_to(dest).as_posix()
                if relative not in {_MANIFEST, _MARKER}:
                    actual_files.add(relative)
        if actual_files != listed_files:
            raise DicomStagingError("completed DICOM staging contains unlisted or missing files")
        return manifest
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DicomStagingError(f"invalid completed DICOM staging directory {dest}: {exc}") from exc


class _StageBuilder:
    def __init__(self, root: Path, request: AcquisitionRequest, source: str) -> None:
        self.root = root
        self.request = request
        self.source = source
        self.study_uids: set[str] = set()
        self.patient_fingerprints: set[str] = set()
        self.sop_uids: set[str] = set()
        self.instances: dict[str, dict[str, dict]] = {
            uid: {} for uid in request.series_by_role.values()
        }
        self.acquisition_fingerprints: dict[str, set[str]] = {
            uid: set() for uid in request.series_by_role.values()
        }
        self.total_bytes = 0

    def add_dataset(self, ds: Any, *, original: Path | None = None) -> bool:
        ids = _identities(ds)
        series_uid = ids["series_uid"]
        if series_uid not in self.instances:
            return False
        if not _valid_uid(ids["study_uid"]) or not _valid_uid(series_uid) or not _valid_uid(ids["sop_uid"]):
            raise DicomStagingError("DICOM instance is missing a valid Study/Series/SOP UID")
        if self.request.study_uid and ids["study_uid"] != self.request.study_uid:
            raise DicomStagingError(
                f"instance StudyInstanceUID {ids['study_uid']} != requested {self.request.study_uid}")
        if ids["sop_uid"] in self.sop_uids:
            raise DicomStagingError(f"duplicate SOPInstanceUID {ids['sop_uid']}")
        if len(self.sop_uids) >= wsettings.dicom_max_instances_per_run:
            raise DicomStagingError("DICOM acquisition exceeds configured instance limit")
        self.study_uids.add(ids["study_uid"])
        self.patient_fingerprints.add(_patient_fingerprint(ids))
        self.sop_uids.add(ids["sop_uid"])
        acquisition = canonical_acquisition(_acquisition(ds))
        if not acquisition and (wsettings.deployment_mode in {"research", "production"}
                                or (self.request.expected_fingerprints or {}).get(series_uid)):
            raise DicomStagingError("DICOM instance lacks acquisition fingerprint metadata")
        if acquisition:
            expected_acquisition = (self.request.expected_acquisitions or {}).get(series_uid)
            if expected_acquisition:
                expected_acquisition = canonical_acquisition(expected_acquisition)
                observed_subset = {
                    key: acquisition.get(key) for key in expected_acquisition
                    if acquisition.get(key) is not None
                }
                if observed_subset != expected_acquisition:
                    raise DicomStagingError(
                        "DICOM instance acquisition tags differ from the confirmed series")
                acquisition = observed_subset
            self.acquisition_fingerprints[series_uid].add(
                acquisition_fingerprint(acquisition))
        series_dir = self.root / "series" / series_uid
        series_dir.mkdir(parents=True, exist_ok=True)
        path = series_dir / f"{ids['sop_uid']}.dcm"
        if original is None:
            ds.save_as(path)
        else:
            if original.is_symlink():
                raise DicomStagingError(f"symlinked DICOM imports are not allowed: {original}")
            if self.total_bytes + original.stat().st_size > wsettings.dicom_max_bytes_per_run:
                raise DicomStagingError("DICOM acquisition exceeds configured byte limit")
            shutil.copyfile(original, path)
        self.total_bytes += path.stat().st_size
        if self.total_bytes > wsettings.dicom_max_bytes_per_run:
            raise DicomStagingError("DICOM acquisition exceeds configured byte limit")
        self.instances[series_uid][ids["sop_uid"]] = {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": _hash_file(path),
            "size": path.stat().st_size,
        }
        return True

    def finish(self, expected_sops: dict[str, set[str]] | None = None) -> dict:
        if len(self.study_uids) != 1:
            raise DicomStagingError(
                f"requested series resolved to {len(self.study_uids)} studies; expected exactly one")
        if len(self.patient_fingerprints) != 1:
            raise DicomStagingError("requested series do not have one consistent patient identity")
        series_manifest = []
        for uid in sorted(set(self.request.series_by_role.values())):
            found = set(self.instances[uid])
            if not found:
                raise DicomStagingError(f"requested SeriesInstanceUID {uid} contained no instances")
            expected_count = (self.request.expected_counts or {}).get(uid)
            if expected_count is not None and len(found) != int(expected_count):
                raise DicomStagingError(
                    f"series {uid} has {len(found)} instances; expected {expected_count}")
            if expected_sops is not None and found != expected_sops.get(uid, set()):
                missing = sorted(expected_sops.get(uid, set()) - found)
                extra = sorted(found - expected_sops.get(uid, set()))
                raise DicomStagingError(
                    f"incomplete WADO series {uid}: missing={missing[:5]}, extra={extra[:5]}")
            fingerprints = self.acquisition_fingerprints[uid]
            if len(fingerprints) != 1:
                raise DicomStagingError(
                    f"series {uid} has inconsistent/missing acquisition fingerprints")
            fingerprint = next(iter(fingerprints))
            expected_fingerprint = (self.request.expected_fingerprints or {}).get(uid)
            if expected_fingerprint and fingerprint != expected_fingerprint:
                raise DicomStagingError(
                    f"series {uid} fingerprint differs from the confirmed acquisition")
            series_manifest.append({
                "series_uid": uid,
                "acquisition_fingerprint": fingerprint,
                "instance_count": len(found),
                "instances": [dict(sop_uid=sop, **self.instances[uid][sop])
                              for sop in sorted(found)],
            })
        return {
            "schema_version": 1,
            "source": self.source,
            "study_uid": next(iter(self.study_uids)),
            "series_by_role": self.request.series_by_role,
            "patient_fingerprint": next(iter(self.patient_fingerprints)),
            "series": series_manifest,
        }


def _publish(dest: Path, request: AcquisitionRequest, populate) -> tuple[str, dict]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_completed(dest, request)
    if existing is not None:
        return str(dest), existing
    if dest.exists():
        # Never consume or overwrite an unverified directory.  A partial acquisition is always in
        # a hidden temporary sibling and is removed by the writer's finally block.
        raise DicomStagingError(f"unverified staging path already exists: {dest}")
    temp = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        manifest = populate(temp)
        raw = _manifest_bytes(manifest)
        (temp / _MANIFEST).write_bytes(raw)
        marker = {"manifest_sha256": hashlib.sha256(raw).hexdigest()}
        (temp / _MARKER).write_text(json.dumps(marker, sort_keys=True) + "\n")
        # Sync metadata and contents before the directory becomes visible at its final name.
        for path in (temp / _MANIFEST, temp / _MARKER):
            with path.open("rb") as fh:
                os.fsync(fh.fileno())
        os.replace(temp, dest)
        dir_fd = os.open(dest.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return str(dest), manifest
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def _stage_local(source_root: Path, temp: Path, request: AcquisitionRequest) -> dict:
    import pydicom

    # Do not persist a PHI-bearing or host-specific import path in run provenance/API responses.
    builder = _StageBuilder(temp, request, "local-import")
    for dirpath, dirs, files in os.walk(source_root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(dirpath) / d).is_symlink()]
        for filename in files:
            path = Path(dirpath) / filename
            if path.is_symlink():
                continue
            try:
                ds = pydicom.dcmread(
                    path, stop_before_pixels=True,
                )
            except Exception:
                continue
            builder.add_dataset(ds, original=path)
    return builder.finish()


def _qido_sops(client: Any, request: AcquisitionRequest) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {}
    assert request.study_uid is not None
    for uid in sorted(set(request.series_by_role.values())):
        found = client.search_for_instances(
            study_instance_uid=request.study_uid,
            series_instance_uid=uid,
            fields=["00080018"],
        )
        sops = {_dataset_value(ds, "SOPInstanceUID", "00080018") for ds in found}
        if not sops or any(not _valid_uid(sop) for sop in sops):
            raise DicomStagingError(f"QIDO returned no complete SOP manifest for series {uid}")
        expected[uid] = sops
    return expected


def _stage_wado(client: Any, temp: Path, request: AcquisitionRequest) -> dict:
    if not request.study_uid:
        raise DicomStagingError("WADO acquisition requires an exact StudyInstanceUID")
    expected = _qido_sops(client, request)
    builder = _StageBuilder(temp, request, "orthanc-wado-rs")
    for uid in sorted(set(request.series_by_role.values())):
        for ds in client.retrieve_series(request.study_uid, uid):
            builder.add_dataset(ds)
    return builder.finish(expected)


def dicom_root_for(case: Any, request: AcquisitionRequest) -> tuple[str, dict]:
    """Return a verified local snapshot and its exact input manifest for one run."""
    dest = Path(wsettings.dicom_staging) / request.run_id
    existing = _load_completed(dest, request)
    if existing is not None:
        return str(dest), existing

    staging_id = getattr(case, "staging_id", None)
    if staging_id:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(staging_id)):
            raise DicomStagingError("case staging_id is invalid")
        local_path = str(Path(wsettings.dicom_import_root) / str(staging_id))
    else:
        # Legacy rows may retain an absolute path.  It is never mounted directly and is accepted
        # only when canonicalized below the configured import root.
        local_path = getattr(case, "dicom_path", None)
    if local_path:
        source = _allowed_import(local_path)
        return _publish(dest, request, lambda temp: _stage_local(source, temp, request))

    study_uid = getattr(case, "orthanc_study_uid", None)
    if not study_uid:
        raise DicomStagingError("case has neither a permitted local import nor an Orthanc study")
    if request.study_uid and request.study_uid != study_uid:
        raise DicomStagingError("run StudyInstanceUID does not match its case")

    from app.orthanc import dicomweb_client

    client = dicomweb_client()
    return _publish(dest, request, lambda temp: _stage_wado(client, temp, request))


def wado_pull(study_uid: str, dest: str, series_uids: Iterable[str]) -> str:
    """Compatibility helper for explicit-series callers; never retrieves an entire study."""
    by_role = {f"series_{i}": uid for i, uid in enumerate(series_uids)}
    request = AcquisitionRequest(Path(dest).name, by_role, study_uid)
    from app.orthanc import dicomweb_client

    client = dicomweb_client()
    path, _ = _publish(Path(dest), request, lambda temp: _stage_wado(client, temp, request))
    return path
