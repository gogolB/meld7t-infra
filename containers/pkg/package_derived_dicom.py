#!/usr/bin/env python3
"""Publish MAP and HippUnfold outputs as viewer-ready derived DICOM.

All detector runs belonging to one immutable recipe share a deterministic derived Study UID.
Each run retains its own deterministic reference/SEG/Parametric Map series and SOP UIDs.  The
source clinical study is therefore never mutated, while one logical review study can contain the
outputs of MELD, MAP, and HS.

MAP's MNI maps are *pulled* onto the exact native T1 grid with SPM's ``iy_T1`` inverse deformation
field.  Threshold maps use nearest-neighbour sampling and quantitative z maps use linear sampling.
HippUnfold subfield labels are nearest-neighbour resampled onto the consumed native T2 grid.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import highdicom as hd
import nibabel as nib
import numpy as np
from nibabel.processing import resample_from_to
from scipy import ndimage

import package_dicom as dicom


SUBFIELD_LABELS = {
    1: "Subiculum", 2: "CA1", 3: "CA2", 4: "CA3", 5: "CA4", 6: "Dentate gyrus",
    7: "SRLM", 8: "Hippocampal cyst",
}
SUBFIELD_GM = {1, 2, 3, 4, 5, 6}


def _raw_image(path: str) -> tuple[nib.spatialimages.SpatialImage, np.ndarray]:
    image = nib.load(path)
    data = np.asanyarray(image.dataobj)
    if data.ndim != 3 or 0 in data.shape:
        raise dicom.GeometryError(f"expected a non-empty 3D NIfTI: {path}")
    affine = np.asarray(image.affine, dtype=np.float64)
    if not np.all(np.isfinite(affine)) or abs(np.linalg.det(affine[:3, :3])) < 1e-8:
        raise dicom.GeometryError(f"invalid/singular NIfTI affine: {path}")
    if not np.all(np.isfinite(data)):
        raise dicom.GeometryError(f"NIfTI contains NaN or infinite values: {path}")
    return image, data


def _canonical_on_target(data: np.ndarray, target_image) -> tuple[np.ndarray, np.ndarray]:
    transformed = nib.as_closest_canonical(nib.Nifti1Image(data, target_image.affine))
    return np.asanyarray(transformed.dataobj), np.asarray(transformed.affine, dtype=np.float64)


def _deformation_vectors(path: str, target_image) -> np.ndarray:
    deformation = nib.load(path)
    values = np.asanyarray(deformation.dataobj, dtype=np.float64)
    # SPM writes vector fields as X×Y×Z×1×3.  Accept X×Y×Z×3 as well for
    # fixtures and SPM variants, but never squeeze arbitrary singleton spatial axes.
    if values.ndim == 5 and values.shape[3:] == (1, 3):
        values = values[:, :, :, 0, :]
    if values.ndim != 4 or values.shape[-1] != 3:
        raise dicom.GeometryError(
            f"SPM inverse deformation must have shape X×Y×Z×1×3: {path}")
    if values.shape[:3] != target_image.shape[:3]:
        raise dicom.GeometryError(
            f"SPM inverse deformation grid {values.shape[:3]} does not match native target "
            f"{target_image.shape[:3]}")
    if not np.allclose(deformation.affine, target_image.affine, atol=1e-3, rtol=1e-6):
        raise dicom.GeometryError(
            "SPM inverse deformation affine does not match the consumed native target grid")
    finite = np.all(np.isfinite(values), axis=-1)
    if float(np.count_nonzero(finite)) / finite.size < 0.5:
        raise dicom.GeometryError("SPM inverse deformation is predominantly non-finite")
    return values


def pull_mni_to_native(mni_path: str, inverse_deformation_path: str, native_path: str,
                       *, order: int) -> tuple[np.ndarray, np.ndarray]:
    """Sample an MNI volume at SPM ``iy`` coordinates for every native target voxel.

    SPM deformation values are physical millimetre coordinates.  Converting those coordinates
    through the source MNI affine before ``map_coordinates`` avoids assuming a particular MNI
    voxel size, origin, or axis order.
    """
    if order not in {0, 1}:
        raise ValueError("only nearest-neighbour or linear inverse-deformation pulls are allowed")
    native_image, _ = _raw_image(native_path)
    mni_image, mni_data = _raw_image(mni_path)
    vectors = _deformation_vectors(inverse_deformation_path, native_image)
    finite = np.all(np.isfinite(vectors), axis=-1)
    safe_vectors = np.where(finite[..., None], vectors, 0.0)
    homogeneous = np.concatenate(
        (safe_vectors.reshape(-1, 3), np.ones((safe_vectors[..., 0].size, 1))), axis=1)
    source_voxels = (np.linalg.inv(mni_image.affine) @ homogeneous.T)[:3]
    pulled = ndimage.map_coordinates(
        np.asarray(mni_data, dtype=np.float32), source_voxels,
        order=order, mode="constant", cval=0.0, prefilter=False,
    ).reshape(native_image.shape[:3])
    pulled[~finite] = 0.0
    data, affine = _canonical_on_target(pulled, native_image)
    native_canonical, native_affine = dicom._canonical(native_path)
    if data.shape != native_canonical.shape or not np.allclose(
            affine, native_affine, atol=1e-4, rtol=1e-6):
        raise dicom.GeometryError("inverse-deformed MAP output missed the packaged native grid")
    if not np.all(np.isfinite(data)):
        raise dicom.GeometryError("inverse-deformed MAP output contains non-finite values")
    return data, affine


def resample_dseg_to_native(dseg_path: str, native_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Nearest-neighbour resample a discrete dseg onto the exact consumed native grid."""
    native_image, _ = _raw_image(native_path)
    dseg_image, dseg_data = _raw_image(dseg_path)
    rounded = np.rint(dseg_data)
    if not np.allclose(dseg_data, rounded, atol=1e-5, rtol=0):
        raise dicom.GeometryError(f"HippUnfold dseg contains non-integer labels: {dseg_path}")
    unknown = set(np.unique(rounded).astype(int)) - {0, *SUBFIELD_LABELS}
    if unknown:
        raise dicom.GeometryError(f"HippUnfold dseg has unsupported labels: {sorted(unknown)}")
    resampled = resample_from_to(
        nib.Nifti1Image(rounded.astype(np.uint8), dseg_image.affine),
        (native_image.shape[:3], native_image.affine), order=0, mode="constant", cval=0,
    )
    data, affine = _canonical_on_target(np.asanyarray(resampled.dataobj), native_image)
    native_canonical, native_affine = dicom._canonical(native_path)
    if data.shape != native_canonical.shape or not np.allclose(
            affine, native_affine, atol=1e-4, rtol=1e-6):
        raise dicom.GeometryError("resampled HippUnfold dseg missed the packaged native T2 grid")
    rounded = np.rint(data).astype(np.uint8)
    if not np.any(rounded):
        raise dicom.GeometryError(f"HippUnfold dseg has no overlap with native T2: {dseg_path}")
    return rounded, affine


def _frames(channels: list[np.ndarray]) -> np.ndarray:
    shape = channels[0].shape
    if any(channel.shape != shape for channel in channels):
        raise dicom.GeometryError("derived DICOM channels do not share a grid")
    return np.stack([
        np.stack([np.asarray(channel[:, :, k]).T for k in range(shape[2])])
        for channel in channels
    ], axis=-1)


def build_parametric_map(source_datasets, data: np.ndarray, *, uid_seed: str,
                         uid_role: str, series_number: int, label: str,
                         description: str, software_version: str):
    values = np.asarray(data, dtype=np.float32)
    if values.ndim != 3 or not np.all(np.isfinite(values)):
        raise dicom.GeometryError(f"{label} parametric values must be finite 3D data")
    low, high = float(values.min()), float(values.max())
    width = max(high - low, 1.0)
    mapping = hd.pm.RealWorldValueMapping(
        lut_label=label[:16], lut_explanation=description[:64],
        unit=hd.sr.CodedConcept("1", "UCUM", "no units"),
        value_range=(low, high), slope=1.0, intercept=0.0,
        quantity_definition=hd.sr.CodedConcept(
            "MAP-ZSCORE", "99MELD", "MAP voxelwise z-score"),
    )
    pm = hd.pm.ParametricMap(
        source_images=source_datasets,
        pixel_array=_frames([values])[..., 0],
        series_instance_uid=dicom._uid(uid_seed, f"{uid_role}-series"),
        series_number=series_number,
        sop_instance_uid=dicom._uid(uid_seed, f"{uid_role}-instance-1"),
        instance_number=1,
        manufacturer="meld7t", manufacturer_model_name="pkg",
        software_versions=software_version, device_serial_number="meld7t-pkg",
        contains_recognizable_visual_features=True,
        real_world_value_mappings=[mapping],
        window_center=(low + high) / 2.0, window_width=width,
        content_description=description[:64], content_label=label[:16].upper(),
    )
    pm.SeriesDescription = description
    return pm


def _finish(datasets, roles, args, *, t1_series_uid, seg_series_uid,
            probmap_series_uids=None):
    harmonization = dicom.harmonization_provenance(
        args.harmonization_status, args.harmonization_code,
        args.harmonization_version, args.harmonization_method)
    dicom.mark_harmonization(datasets, harmonization)
    files, manifest_sha256 = dicom.dicom_manifest(datasets)
    dicom.write_dicom_manifest(
        args.manifest_output, datasets, files, manifest_sha256, roles_by_series=roles,
        harmonization=harmonization)
    series_manifest = dicom.derived_series_manifest(datasets, roles, harmonization)
    series_canonical = json.dumps(
        series_manifest, sort_keys=True, separators=(",", ":")).encode()
    series_digest = hashlib.sha256(series_canonical).hexdigest()
    if args.stow:
        dicom.stow(args.stow, datasets, args.http_timeout)
    print(f"study_uid={datasets[0].StudyInstanceUID}")
    # Legacy Result fields retain these aliases.  For HS, t1_series_uid aliases the native T2
    # reference; its true role remains unambiguous in derived_series_manifest_json.
    print(f"t1_series_uid={t1_series_uid}")
    print(f"seg_series_uid={seg_series_uid}")
    if probmap_series_uids:
        print(f"probmap_series_uid={probmap_series_uids[0]}")
        print("probmap_series_uids_json=" + json.dumps(probmap_series_uids))
    print(f"n_t1_slices={sum(1 for d in datasets if str(d.SeriesInstanceUID) == t1_series_uid)}")
    print(f"dicom_sop_count={len(datasets)}")
    print(f"dicom_manifest_sha256={manifest_sha256}")
    print("derived_series_manifest_json=" + series_canonical.decode())
    print(f"derived_series_manifest_sha256={series_digest}")


def package_map(args) -> None:
    study_uid = dicom._uid(args.study_uid_seed, "study")
    loaded_t1 = dicom._canonical(args.t1)
    t1 = dicom.build_t1_series(
        args.t1, study_uid, args.pseudonym, loaded_t1, args.uid_seed,
        args.software_version, uid_role="map-t1", series_number=20,
        series_description="MAP native T1 reference (derived)",
        derivation_description=(
            "Research-only native T1 consumed by MAP; MNI results inverse-deformed with "
            f"SPM iy_T1; release {args.software_version}"),
    )
    thresholds, z_maps = [], []
    for threshold_path, z_path in (
            (args.junction_threshold, args.junction_z),
            (args.extension_threshold, args.extension_z)):
        threshold, _ = pull_mni_to_native(
            threshold_path, args.inverse_deformation, args.t1, order=0)
        rounded = np.rint(threshold)
        if not np.allclose(threshold, rounded, atol=1e-5, rtol=0) or not set(
                np.unique(rounded).astype(int)) <= {0, 1}:
            raise dicom.GeometryError("inverse-deformed MAP threshold is not binary")
        z_map, _ = pull_mni_to_native(z_path, args.inverse_deformation, args.t1, order=1)
        thresholds.append(rounded.astype(bool))
        z_maps.append(z_map.astype(np.float32))
    definitions = [
        {"label": "MAP junction candidate", "property_code": "MAP-JUNCTION",
         "property_meaning": "MAP junction z-score threshold candidate"},
        {"label": "MAP extension candidate", "property_code": "MAP-EXTENSION",
         "property_meaning": "MAP extension z-score threshold candidate"},
    ]
    seg = dicom.build_binary_seg(
        t1, _frames(thresholds), definitions, uid_seed=args.uid_seed, uid_role="map-seg",
        series_number=21, software_version=args.software_version, algorithm_name="MAP",
        algorithm_family="Voxel morphometry", series_description="MAP candidate segmentations",
    )
    junction_pm = build_parametric_map(
        t1, z_maps[0], uid_seed=args.uid_seed, uid_role="map-junction-z", series_number=22,
        label="MAPJUNCTIONZ", description="MAP junction quantitative z map",
        software_version=args.software_version,
    )
    extension_pm = build_parametric_map(
        t1, z_maps[1], uid_seed=args.uid_seed, uid_role="map-extension-z", series_number=23,
        label="MAPEXTENSIONZ", description="MAP extension quantitative z map",
        software_version=args.software_version,
    )
    datasets = [*t1, seg, junction_pm, extension_pm]
    roles = {
        str(t1[0].SeriesInstanceUID): "map_native_t1_reference",
        str(seg.SeriesInstanceUID): "map_candidate_segmentation",
        str(junction_pm.SeriesInstanceUID): "map_junction_z_parametric_map",
        str(extension_pm.SeriesInstanceUID): "map_extension_z_parametric_map",
    }
    _finish(
        datasets, roles, args, t1_series_uid=str(t1[0].SeriesInstanceUID),
        seg_series_uid=str(seg.SeriesInstanceUID),
        probmap_series_uids=[
            str(junction_pm.SeriesInstanceUID), str(extension_pm.SeriesInstanceUID)],
    )


def package_hippunfold(args) -> None:
    if "subfields" not in os.path.basename(args.left_dseg).lower() or "subfields" not in os.path.basename(
            args.right_dseg).lower():
        raise dicom.GeometryError(
            "viewer publication requires bilateral HippUnfold desc-subfields dsegs")
    if (args.flagged_side == "none") != (args.expected_clusters == 0):
        raise ValueError("flagged side disagrees with the validated HS finding count")
    if args.expected_clusters not in {0, 1}:
        raise ValueError("HS packaging expects zero or one validated asymmetry finding")
    study_uid = dicom._uid(args.study_uid_seed, "study")
    loaded_t2 = dicom._canonical(args.t2)
    t2 = dicom.build_t1_series(
        args.t2, study_uid, args.pseudonym, loaded_t2, args.uid_seed,
        args.software_version, uid_role="hs-t2", series_number=30,
        series_description="HS native T2 reference (derived)",
        derivation_description=(
            "Research-only native T2 consumed by HippUnfold; subfields resampled with nearest "
            f"neighbour interpolation; release {args.software_version}"),
    )
    left, _ = resample_dseg_to_native(args.left_dseg, args.t2)
    right, _ = resample_dseg_to_native(args.right_dseg, args.t2)
    if np.any((left > 0) & (right > 0)):
        raise dicom.GeometryError("resampled left/right HippUnfold dsegs overlap")
    channels, definitions = [], []
    for hemi, data in (("Left", left), ("Right", right)):
        for value, name in SUBFIELD_LABELS.items():
            mask = data == value
            if not np.any(mask):
                continue
            channels.append(mask)
            definitions.append({
                "label": f"{hemi} {name}"[:64],
                "category_code": "91723000", "category_scheme": "SCT",
                "category_meaning": "Anatomical structure",
                "property_code": f"HIPPO-{value}",
                "property_meaning": f"Hippocampal subfield {name}",
            })
    if args.flagged_side != "none":
        selected = left if args.flagged_side == "left" else right
        flag = np.isin(selected, sorted(SUBFIELD_GM))
        if not np.any(flag):
            raise dicom.GeometryError("flagged atrophy-side mask is empty on native T2")
        channels.append(flag)
        definitions.append({
            "label": f"Flagged {args.flagged_side} hippocampal atrophy",
            "property_code": "HS-ATROPHY-FLAG",
            "property_meaning": "Research hippocampal atrophy-side flag",
        })
    seg = dicom.build_binary_seg(
        t2, _frames(channels), definitions, uid_seed=args.uid_seed, uid_role="hs-seg",
        series_number=31, software_version=args.software_version,
        algorithm_name="HippUnfold", algorithm_family="Hippocampal unfolding and segmentation",
        series_description="HS bilateral subfields and asymmetry flag",
    )
    datasets = [*t2, seg]
    roles = {
        str(t2[0].SeriesInstanceUID): "hs_native_t2_reference",
        str(seg.SeriesInstanceUID): "hs_subfields_and_atrophy_segmentation",
    }
    _finish(
        datasets, roles, args, t1_series_uid=str(t2[0].SeriesInstanceUID),
        seg_series_uid=str(seg.SeriesInstanceUID),
    )


def _common(parser) -> None:
    parser.add_argument("--pseudonym", required=True)
    parser.add_argument("--uid-seed", required=True,
                        help="stable run identifier for series/SOP UIDs")
    parser.add_argument("--study-uid-seed", required=True,
                        help="stable recipe identifier for the shared review Study UID")
    parser.add_argument("--software-version", default="unknown")
    parser.add_argument("--manifest-output", required=True)
    parser.add_argument("--stow", default=os.environ.get("MELD7T_ORTHANC_INNET"))
    parser.add_argument("--http-timeout", type=float, default=60.0)
    parser.add_argument("--expected-clusters", type=int, required=True)
    parser.add_argument("--harmonization-status",
                        choices=("applied", "unharmonized", "not_applicable"),
                        default="unharmonized")
    parser.add_argument("--harmonization-code", default=None)
    parser.add_argument("--harmonization-version", type=int, default=None)
    parser.add_argument("--harmonization-method", default=None)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="detector", required=True)
    map_parser = sub.add_parser("map")
    _common(map_parser)
    map_parser.add_argument("--t1", required=True)
    map_parser.add_argument("--inverse-deformation", required=True)
    map_parser.add_argument("--junction-threshold", required=True)
    map_parser.add_argument("--junction-z", required=True)
    map_parser.add_argument("--extension-threshold", required=True)
    map_parser.add_argument("--extension-z", required=True)
    hs_parser = sub.add_parser("hippunfold")
    _common(hs_parser)
    hs_parser.add_argument("--t2", required=True)
    hs_parser.add_argument("--left-dseg", required=True)
    hs_parser.add_argument("--right-dseg", required=True)
    hs_parser.add_argument("--flagged-side", choices=("left", "right", "none"), required=True)
    args = parser.parse_args()
    if not 1.0 <= args.http_timeout <= 600.0:
        raise ValueError("--http-timeout must be between 1 and 600 seconds")
    if args.expected_clusters < 0:
        raise ValueError("--expected-clusters must be non-negative")
    if not 1 <= len(args.software_version) <= 16 or any(
            char in args.software_version for char in "\r\n\0"):
        raise ValueError("--software-version must contain 1-16 safe characters")
    if args.detector == "map":
        package_map(args)
    else:
        package_hippunfold(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
