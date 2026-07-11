#!/usr/bin/env python3
"""Package MELD outputs as viewer-ready DICOM and STOW to Orthanc (spec §10, §17).

Produces, in one study, all in the input-T1 frame of reference:
  * a base T1 MR series (from the T1w NIfTI MELD consumed), and
  * a DICOM-SEG (highdicom) of the discrete clusters (prediction.nii.gz labels), referencing it.

The continuous parametric probability series (§17) needs surface→volume reprojection of the
hdf5 per-vertex probability MELD does not emit as a volume by default — tracked as a follow-up.

Usage: package_dicom.py --t1 T1.nii.gz --pred prediction.nii.gz --pseudonym P --stow URL
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from io import BytesIO

import numpy as np
import nibabel as nib
import highdicom as hd
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid, MRImageStorage

RAS_TO_LPS = np.diag([-1.0, -1.0, 1.0])

# discrete prediction labels → SEG segments
SEGMENTS = [(100.0, "FCD cluster (MELD)"), (1.0, "MELD prediction border")]


def deterministic_uid(seed: str, purpose: str) -> str:
    """Stable UUID-derived DICOM UID: retries STOW the same instances instead of duplicating."""
    return f"2.25.{uuid.uuid5(uuid.NAMESPACE_URL, f'meld7t:{seed}:{purpose}').int}"


def _uid(seed: str | None, purpose: str) -> str:
    return deterministic_uid(seed, purpose) if seed else generate_uid()


class GeometryError(ValueError):
    pass


def _canonical(path):
    img = nib.as_closest_canonical(nib.load(path))
    data, affine = np.asanyarray(img.dataobj), np.asarray(img.affine, dtype=np.float64)
    if data.ndim != 3 or 0 in data.shape:
        raise GeometryError(f"expected a non-empty 3D NIfTI: {path}")
    if not np.all(np.isfinite(affine)) or abs(np.linalg.det(affine[:3, :3])) < 1e-8:
        raise GeometryError(f"invalid/singular NIfTI affine: {path}")
    axes = affine[:3, :3]
    unit = axes / np.linalg.norm(axes, axis=0)
    if not np.allclose(unit.T @ unit, np.eye(3), atol=1e-4, rtol=0):
        raise GeometryError(f"sheared/non-orthogonal NIfTI grid cannot be encoded safely: {path}")
    return data, affine


def validate_nifti_geometry(t1_path, pred_path):
    """Load canonical inputs and prove the prediction is on the exact consumed T1 grid."""
    t1, t1_affine = _canonical(t1_path)
    pred, pred_affine = _canonical(pred_path)
    if t1.shape != pred.shape:
        raise GeometryError(f"T1/prediction shape mismatch: {t1.shape} != {pred.shape}")
    if not np.allclose(t1_affine, pred_affine, atol=1e-4, rtol=1e-6):
        delta = float(np.max(np.abs(t1_affine - pred_affine)))
        raise GeometryError(f"T1/prediction affine mismatch (max delta {delta:g})")
    if not np.all(np.isfinite(t1)) or not np.all(np.isfinite(pred)):
        raise GeometryError("T1/prediction contains NaN or infinite values")
    rounded = np.rint(pred)
    if not np.allclose(pred, rounded, atol=1e-5, rtol=0):
        raise GeometryError("prediction labelmap contains non-integer values")
    allowed = {0, *(int(label) for label, _ in SEGMENTS)}
    unknown = set(np.unique(rounded).astype(int)) - allowed
    if unknown:
        raise GeometryError(f"prediction labelmap contains unsupported labels: {sorted(unknown)}")
    return (t1, t1_affine), (rounded.astype(np.int16), pred_affine)


def build_t1_series(t1_path, study_uid, patient_id, loaded=None, uid_seed=None,
                    software_version="unknown"):
    data, affine = loaded if loaded is not None else _canonical(t1_path)
    data = np.clip(data, 0, None)
    data = (data / (data.max() or 1) * 4095).astype(np.uint16)
    nx, ny, nz = data.shape
    R = affine[:3, :3]
    ui = R[:, 0] / np.linalg.norm(R[:, 0])
    uj = R[:, 1] / np.linalg.norm(R[:, 1])
    sp_i = float(np.linalg.norm(R[:, 0]))
    sp_j = float(np.linalg.norm(R[:, 1]))
    sp_k = float(np.linalg.norm(R[:, 2]))
    iop = list(RAS_TO_LPS @ ui) + list(RAS_TO_LPS @ uj)

    series_uid = _uid(uid_seed, "t1-series")
    frame_uid = _uid(uid_seed, "frame-of-reference")
    datasets = []
    for k in range(nz):
        ipp = list(RAS_TO_LPS @ (affine @ np.array([0, 0, k, 1]))[:3])
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = MRImageStorage
        ds.file_meta.MediaStorageSOPInstanceUID = _uid(uid_seed, f"t1-instance-{k + 1}")
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.SOPClassUID = MRImageStorage
        ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "MR"
        ds.ImageType = ["DERIVED", "SECONDARY"]
        ds.DerivationDescription = (
            f"Research-only MELD input T1 derived from the contracted NIfTI; "
            f"release {software_version}")
        ds.PatientID = patient_id
        ds.PatientName = patient_id
        ds.PatientIdentityRemoved = "YES"
        ds.DeidentificationMethod = "Research pseudonymization; direct identifiers removed"
        ds.BurnedInAnnotation = "NO"
        # standard patient/study attrs highdicom copies from source images
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        ds.StudyDate = ""
        ds.StudyTime = ""
        ds.AccessionNumber = ""
        ds.StudyID = ""
        ds.ReferringPhysicianName = ""
        ds.SeriesDescription = "MELD input T1 (derived)"
        ds.SeriesNumber = 1
        ds.InstanceNumber = k + 1
        ds.ImageOrientationPatient = [f"{v:.6f}" for v in iop]
        ds.ImagePositionPatient = [f"{v:.4f}" for v in ipp]
        ds.PixelSpacing = [f"{sp_j:.6f}", f"{sp_i:.6f}"]
        ds.SliceThickness = f"{sp_k:.6f}"
        frame = data[:, :, k].T                       # (rows=j, cols=i)
        ds.Rows, ds.Columns = frame.shape
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleSlope = 1
        ds.RescaleIntercept = 0
        ds.PixelData = np.ascontiguousarray(frame).tobytes()
        datasets.append(ds)
    return datasets


def build_seg(t1_datasets, pred_path, loaded=None, uid_seed=None,
              software_version="unknown"):
    pred, _ = loaded if loaded is not None else _canonical(pred_path)
    expected_shape = (int(t1_datasets[0].Columns), int(t1_datasets[0].Rows), len(t1_datasets))
    if pred.shape != expected_shape:
        raise GeometryError(
            f"prediction grid {pred.shape} does not match packaged T1 {expected_shape}")
    labelmap = np.zeros(pred.shape, dtype=np.uint8)
    descriptions = []
    for seg_num, (label, name) in enumerate(SEGMENTS, start=1):
        labelmap[pred == label] = seg_num
        descriptions.append(hd.seg.SegmentDescription(
            segment_number=seg_num, segment_label=name,
            segmented_property_category=hd.sr.CodedConcept(
                "49755003", "SCT", "Morphologically Abnormal Structure"),
            # Do not miscode a research candidate as a clinical mass/diagnosis.
            segmented_property_type=hd.sr.CodedConcept(
                "FCD-CANDIDATE", "99MELD", "Research FCD candidate"),
            algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
            algorithm_identification=hd.AlgorithmIdentificationSequence(
                name="MELD-FCD", version=software_version, family=hd.sr.CodedConcept(
                    "123456", "99MELD", "Cortical-surface GNN"))))
    # frames ordered to match the source datasets (slice order along k, transposed)
    frames = np.stack([labelmap[:, :, k].T for k in range(labelmap.shape[2])])
    seg = hd.seg.Segmentation(
        source_images=t1_datasets,
        pixel_array=frames,
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
        segment_descriptions=descriptions,
        series_instance_uid=_uid(uid_seed, "seg-series"),
        series_number=2,
        sop_instance_uid=_uid(uid_seed, "seg-instance-1"),
        instance_number=1,
        manufacturer="meld7t",
        manufacturer_model_name="pkg",
        software_versions=software_version,
        device_serial_number="meld7t-pkg",
        omit_empty_frames=False,
    )
    return seg


def stow(url, datasets, timeout_seconds=60.0):
    from dicomweb_client.api import DICOMwebClient
    import requests

    class InternalSession(requests.Session):
        def request(self, method, target, **kwargs):
            kwargs.setdefault("timeout", timeout_seconds)
            kwargs.setdefault("allow_redirects", False)
            return super().request(method, target, **kwargs)

    session = InternalSession()
    session.trust_env = False
    client = DICOMwebClient(url=url, session=session)
    response = client.store_instances(datasets)
    expected = {str(dataset.SOPInstanceUID) for dataset in datasets}
    failed = {
        str(item.ReferencedSOPInstanceUID)
        for item in getattr(response, "FailedSOPSequence", [])
        if getattr(item, "ReferencedSOPInstanceUID", None)
    }
    stored = {
        str(item.ReferencedSOPInstanceUID)
        for item in getattr(response, "ReferencedSOPSequence", [])
        if getattr(item, "ReferencedSOPInstanceUID", None)
    }
    if failed or stored != expected:
        raise RuntimeError(
            f"STOW response did not confirm every instance (expected={len(expected)}, "
            f"stored={len(stored)}, failed={len(failed)})"
        )

    by_series = {}
    source_by_sop = {str(dataset.SOPInstanceUID): dataset for dataset in datasets}
    for dataset in datasets:
        by_series.setdefault(str(dataset.SeriesInstanceUID), set()).add(str(dataset.SOPInstanceUID))
    study_uid = str(datasets[0].StudyInstanceUID)
    for series_uid, expected_series in by_series.items():
        rows = client.search_for_instances(
            study_instance_uid=study_uid,
            series_instance_uid=series_uid,
            fields=["00080018"],
        )
        actual = {
            str((row.get("00080018", {}).get("Value") or [""])[0])
            for row in rows
        }
        actual.discard("")
        if actual != expected_series:
            raise RuntimeError(
                f"post-STOW QIDO verification failed for derived series "
                f"(expected={len(expected_series)}, actual={len(actual)})"
            )
        for sop_uid in sorted(expected_series):
            retrieved = client.retrieve_instance(study_uid, series_uid, sop_uid)
            if _critical_dicom_semantics(retrieved) != _critical_dicom_semantics(
                    source_by_sop[sop_uid]):
                raise RuntimeError(
                    f"post-STOW WADO verification failed for derived SOP {sop_uid}"
                )
    return response


def _critical_dicom_semantics(dataset):
    """Hash pixel bytes and the identity/geometry/segment fields that drive research review."""
    def value(name, default=""):
        return str(getattr(dataset, name, default) or default)

    def values(name):
        raw = getattr(dataset, name, []) or []
        if isinstance(raw, (str, bytes)):
            return [str(raw)]
        return [str(item) for item in raw]

    def sequence(parent, name):
        return list(getattr(parent, name, []) or []) if parent is not None else []

    def codes(parent, name):
        return [{
            "code_value": value_from(item, "CodeValue"),
            "coding_scheme": value_from(item, "CodingSchemeDesignator"),
            "coding_scheme_version": value_from(item, "CodingSchemeVersion"),
            "meaning": value_from(item, "CodeMeaning"),
        } for item in sequence(parent, name)]

    def functional_group(group):
        pixel_measures = [{
            "pixel_spacing": values_from(item, "PixelSpacing"),
            "slice_thickness": value_from(item, "SliceThickness"),
            "spacing_between_slices": value_from(item, "SpacingBetweenSlices"),
        } for item in sequence(group, "PixelMeasuresSequence")]
        orientations = [{
            "image_orientation_patient": values_from(item, "ImageOrientationPatient"),
        } for item in sequence(group, "PlaneOrientationSequence")]
        positions = [{
            "image_position_patient": values_from(item, "ImagePositionPatient"),
        } for item in sequence(group, "PlanePositionSequence")]
        segment_identification = [{
            "referenced_segment_number": value_from(item, "ReferencedSegmentNumber"),
        } for item in sequence(group, "SegmentIdentificationSequence")]
        frame_content = [{
            "dimension_index_values": values_from(item, "DimensionIndexValues"),
            "stack_id": value_from(item, "StackID"),
            "in_stack_position_number": value_from(item, "InStackPositionNumber"),
        } for item in sequence(group, "FrameContentSequence")]
        derivations = []
        for derivation in sequence(group, "DerivationImageSequence"):
            sources = []
            for source in sequence(derivation, "SourceImageSequence"):
                sources.append({
                    "referenced_sop_class_uid": value_from(
                        source, "ReferencedSOPClassUID"),
                    "referenced_sop_instance_uid": value_from(
                        source, "ReferencedSOPInstanceUID"),
                    "referenced_frame_number": values_from(source, "ReferencedFrameNumber"),
                    "referenced_segment_number": values_from(
                        source, "ReferencedSegmentNumber"),
                    "spatial_locations_preserved": value_from(
                        source, "SpatialLocationsPreserved"),
                    "purpose_of_reference": codes(
                        source, "PurposeOfReferenceCodeSequence"),
                })
            derivations.append({
                "derivation_code": codes(derivation, "DerivationCodeSequence"),
                "sources": sources,
            })
        return {
            "pixel_measures": pixel_measures,
            "plane_orientations": orientations,
            "plane_positions": positions,
            "segment_identification": segment_identification,
            "frame_content": frame_content,
            "derivations": derivations,
        }

    segments = []
    for segment in getattr(dataset, "SegmentSequence", []) or []:
        category = (getattr(segment, "SegmentedPropertyCategoryCodeSequence", []) or [None])[0]
        prop = (getattr(segment, "SegmentedPropertyTypeCodeSequence", []) or [None])[0]
        segments.append({
            "number": int(getattr(segment, "SegmentNumber", 0) or 0),
            "label": value_from(segment, "SegmentLabel"),
            "category": value_from(category, "CodeValue"),
            "property": value_from(prop, "CodeValue"),
        })
    pixel_data = bytes(getattr(dataset, "PixelData", b"") or b"")
    referenced_sops = sorted({
        str(element.value)
        for element in (dataset.iterall() if hasattr(dataset, "iterall") else [])
        if getattr(element, "keyword", "") == "ReferencedSOPInstanceUID" and element.value
    })
    return {
        "sop_class_uid": value("SOPClassUID"),
        "sop_instance_uid": value("SOPInstanceUID"),
        "study_instance_uid": value("StudyInstanceUID"),
        "series_instance_uid": value("SeriesInstanceUID"),
        "frame_of_reference_uid": value("FrameOfReferenceUID"),
        "modality": value("Modality"),
        "patient_id": value("PatientID"),
        "patient_name": value("PatientName"),
        "patient_identity_removed": value("PatientIdentityRemoved"),
        "deidentification_method": value("DeidentificationMethod"),
        "burned_in_annotation": value("BurnedInAnnotation"),
        "image_type": values("ImageType"),
        "image_orientation_patient": values("ImageOrientationPatient"),
        "image_position_patient": values("ImagePositionPatient"),
        "pixel_spacing": values("PixelSpacing"),
        "slice_thickness": value("SliceThickness"),
        "rows": int(getattr(dataset, "Rows", 0) or 0),
        "columns": int(getattr(dataset, "Columns", 0) or 0),
        "number_of_frames": int(getattr(dataset, "NumberOfFrames", 1) or 1),
        "samples_per_pixel": int(getattr(dataset, "SamplesPerPixel", 0) or 0),
        "photometric_interpretation": value("PhotometricInterpretation"),
        "bits_allocated": int(getattr(dataset, "BitsAllocated", 0) or 0),
        "bits_stored": int(getattr(dataset, "BitsStored", 0) or 0),
        "high_bit": int(getattr(dataset, "HighBit", 0) or 0),
        "pixel_representation": int(getattr(dataset, "PixelRepresentation", 0) or 0),
        "segmentation_type": value("SegmentationType"),
        "maximum_fractional_value": int(
            getattr(dataset, "MaximumFractionalValue", 0) or 0),
        "pixel_sha256": hashlib.sha256(pixel_data).hexdigest(),
        "segments": segments,
        "referenced_sop_instance_uids": referenced_sops,
        "shared_functional_groups": [
            functional_group(group)
            for group in sequence(dataset, "SharedFunctionalGroupsSequence")
        ],
        "per_frame_functional_groups": [
            functional_group(group)
            for group in sequence(dataset, "PerFrameFunctionalGroupsSequence")
        ],
        "dimension_indices": [{
            "dimension_organization_uid": value_from(item, "DimensionOrganizationUID"),
            "dimension_index_pointer": value_from(item, "DimensionIndexPointer"),
            "functional_group_pointer": value_from(item, "FunctionalGroupPointer"),
        } for item in sequence(dataset, "DimensionIndexSequence")],
    }


def value_from(dataset, name, default=""):
    return str(getattr(dataset, name, default) or default) if dataset is not None else default


def values_from(dataset, name):
    raw = getattr(dataset, name, []) or [] if dataset is not None else []
    if isinstance(raw, (str, bytes)):
        return [str(raw)]
    try:
        return [str(item) for item in raw]
    except TypeError:
        return [str(raw)]


def dicom_manifest(datasets):
    files = []
    for dataset in datasets:
        encoded = BytesIO()
        pydicom.dcmwrite(encoded, dataset)
        payload = encoded.getvalue()
        files.append({
            "sop_instance_uid": str(dataset.SOPInstanceUID),
            "series_instance_uid": str(dataset.SeriesInstanceUID),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        })
    files.sort(key=lambda item: item["sop_instance_uid"])
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return files, hashlib.sha256(canonical).hexdigest()


def write_dicom_manifest(path, datasets, files, manifest_sha256):
    destination = os.path.abspath(path)
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    if os.path.islink(destination) or os.path.exists(destination):
        raise ValueError("DICOM manifest output already exists or is a symlink")
    document = {
        "schema_version": 1,
        "study_instance_uid": str(datasets[0].StudyInstanceUID),
        "sop_count": len(files),
        "files": files,
        "manifest_sha256": manifest_sha256,
    }
    temporary = destination + ".tmp"
    with open(temporary, "x", encoding="utf-8") as fh:
        json.dump(document, fh, sort_keys=True, separators=(",", ":"))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, destination)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--t1", required=True)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--pseudonym", required=True)
    ap.add_argument("--stow", default=None, help="Orthanc DICOMweb URL; if omitted, print UIDs only")
    ap.add_argument("--http-timeout", type=float, default=60.0)
    ap.add_argument("--study-uid", default=None)
    ap.add_argument("--uid-seed", default=None,
                    help="stable non-PHI run identifier for idempotent derived SOP UIDs")
    ap.add_argument("--software-version", default="unknown",
                    help="1-16 character signed release identifier for DICOM provenance")
    ap.add_argument("--manifest-output", required=True,
                    help="new JSON path retaining per-SOP encoded hashes")
    ap.add_argument("--expected-clusters", required=True, type=int)
    a = ap.parse_args()
    if not 1.0 <= a.http_timeout <= 600.0:
        raise ValueError("--http-timeout must be between 1 and 600 seconds")
    if not 1 <= len(a.software_version) <= 16 or any(
            char in a.software_version for char in "\r\n\0"):
        raise ValueError("--software-version must contain 1-16 safe characters")

    study_uid = a.study_uid or _uid(a.uid_seed, "study")
    loaded_t1, loaded_pred = validate_nifti_geometry(a.t1, a.pred)
    if a.expected_clusters < 0:
        raise ValueError("--expected-clusters must be non-negative")
    candidate_voxels = int(np.count_nonzero(loaded_pred[0] == int(SEGMENTS[0][0])))
    if (a.expected_clusters == 0) != (candidate_voxels == 0):
        raise ValueError(
            "prediction candidate-label presence disagrees with reported cluster count")
    t1 = build_t1_series(
        a.t1, study_uid, a.pseudonym, loaded_t1, a.uid_seed, a.software_version)
    seg = build_seg(t1, a.pred, loaded_pred, a.uid_seed, a.software_version)
    datasets = [*t1, seg]
    sops, manifest_sha256 = dicom_manifest(datasets)
    write_dicom_manifest(a.manifest_output, datasets, sops, manifest_sha256)
    if a.stow:
        # One STOW transaction plus explicit response/QIDO checks minimizes and detects partial
        # publication. Deterministic SOP UIDs make an identical-contract retry idempotent.
        stow(a.stow, datasets, a.http_timeout)
    print(f"study_uid={study_uid}")
    print(f"t1_series_uid={t1[0].SeriesInstanceUID}")
    print(f"seg_series_uid={seg.SeriesInstanceUID}")
    print(f"n_t1_slices={len(t1)}")
    print(f"dicom_sop_count={len(datasets)}")
    print(f"dicom_manifest_sha256={manifest_sha256}")


if __name__ == "__main__":
    sys.exit(main())
