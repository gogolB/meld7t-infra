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
import sys

import numpy as np
import nibabel as nib
import highdicom as hd
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid, MRImageStorage

RAS_TO_LPS = np.diag([-1.0, -1.0, 1.0])

# discrete prediction labels → SEG segments
SEGMENTS = [(100.0, "FCD cluster (MELD)"), (1.0, "MELD prediction border")]


def _canonical(path):
    img = nib.as_closest_canonical(nib.load(path))
    return np.asanyarray(img.dataobj), img.affine


def build_t1_series(t1_path, study_uid, patient_id):
    data, affine = _canonical(t1_path)
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

    series_uid = generate_uid()
    frame_uid = generate_uid()
    datasets = []
    for k in range(nz):
        ipp = list(RAS_TO_LPS @ (affine @ np.array([0, 0, k, 1]))[:3])
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = MRImageStorage
        ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.SOPClassUID = MRImageStorage
        ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "MR"
        ds.PatientID = patient_id
        ds.PatientName = patient_id
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
        ds.is_little_endian = True
        ds.is_implicit_VR = False
        datasets.append(ds)
    return datasets


def build_seg(t1_datasets, pred_path):
    pred, _ = _canonical(pred_path)
    labelmap = np.zeros(pred.shape, dtype=np.uint8)
    descriptions = []
    for seg_num, (label, name) in enumerate(SEGMENTS, start=1):
        if not np.any(pred == label):
            continue
        labelmap[pred == label] = seg_num
        descriptions.append(hd.seg.SegmentDescription(
            segment_number=seg_num, segment_label=name,
            segmented_property_category=hd.sr.CodedConcept("91723000", "SCT", "Anatomical Structure"),
            segmented_property_type=hd.sr.CodedConcept("4147007", "SCT", "Mass"),
            algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
            algorithm_identification=hd.AlgorithmIdentificationSequence(
                name="MELD-FCD", version="v2.2.5", family=hd.sr.CodedConcept(
                    "123456", "99MELD", "Cortical-surface GNN"))))
    # frames ordered to match the source datasets (slice order along k, transposed)
    frames = np.stack([labelmap[:, :, k].T for k in range(labelmap.shape[2])])
    seg = hd.seg.Segmentation(
        source_images=t1_datasets,
        pixel_array=frames,
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
        segment_descriptions=descriptions,
        series_instance_uid=generate_uid(),
        series_number=2,
        sop_instance_uid=generate_uid(),
        instance_number=1,
        manufacturer="meld7t",
        manufacturer_model_name="pkg",
        software_versions="0.1.0",
        device_serial_number="meld7t-pkg",
        omit_empty_frames=False,
    )
    return seg


def stow(url, datasets):
    from dicomweb_client.api import DICOMwebClient

    client = DICOMwebClient(url=url)
    client.store_instances(datasets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--t1", required=True)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--pseudonym", required=True)
    ap.add_argument("--stow", default=None, help="Orthanc DICOMweb URL; if omitted, print UIDs only")
    ap.add_argument("--study-uid", default=None)
    a = ap.parse_args()

    study_uid = a.study_uid or generate_uid()
    t1 = build_t1_series(a.t1, study_uid, a.pseudonym)
    seg = build_seg(t1, a.pred)
    if a.stow:
        stow(a.stow, t1)
        stow(a.stow, [seg])
    print(f"study_uid={study_uid}")
    print(f"t1_series_uid={t1[0].SeriesInstanceUID}")
    print(f"seg_series_uid={seg.SeriesInstanceUID}")
    print(f"n_t1_slices={len(t1)}")


if __name__ == "__main__":
    sys.exit(main())
