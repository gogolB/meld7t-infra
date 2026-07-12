"""Shared exact-byte DICOM confidentiality admission checks for harmonization controls."""
from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, Any

import pydicom


PROHIBITED_DIRECT_KEYWORDS = {
    "PatientName", "PatientBirthDate", "PatientBirthTime", "PatientAddress",
    "PatientTelephoneNumbers", "OtherPatientIDs", "OtherPatientNames",
    "PatientMotherBirthName", "AccessionNumber", "ReferringPhysicianName",
    "PerformingPhysicianName", "OperatorsName", "MedicalRecordLocator", "EthnicGroup",
    "Occupation", "AdditionalPatientHistory", "PatientComments", "InstitutionAddress",
    "InstitutionalDepartmentName", "PhysiciansOfRecord", "NameOfPhysiciansReadingStudy",
    "RequestingPhysician", "PatientSex", "PatientAge", "InstitutionName",
}


def _value(dataset, keyword: str) -> Any:
    value = getattr(dataset, keyword, None)
    return str(value) if value is not None else None


def _values(dataset, keyword: str) -> list[str]:
    value = getattr(dataset, keyword, None)
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(item) for item in value]
    return [str(value)]


def _float(dataset, keyword: str) -> float | None:
    try:
        value = getattr(dataset, keyword, None)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(dataset, keyword: str) -> int | None:
    try:
        value = getattr(dataset, keyword, None)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_values(dataset, keyword: str) -> list[float]:
    values: list[float] = []
    for value in _values(dataset, keyword):
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return []
    return values


def _exact_acquisition(dataset) -> dict[str, Any]:
    """Extract the fingerprint inputs from the parsed, exact Part-10 bytes."""
    return {
        "manufacturer": _value(dataset, "Manufacturer"),
        "model": _value(dataset, "ManufacturerModelName"),
        "station_name": _value(dataset, "StationName"),
        "field_strength_t": _float(dataset, "MagneticFieldStrength"),
        "software_versions": _values(dataset, "SoftwareVersions"),
        "protocol_name": _value(dataset, "ProtocolName"),
        "sequence_name": _value(dataset, "SequenceName"),
        "scanning_sequence": _values(dataset, "ScanningSequence"),
        "repetition_time_ms": _float(dataset, "RepetitionTime"),
        "echo_time_ms": _float(dataset, "EchoTime"),
        "inversion_time_ms": _float(dataset, "InversionTime"),
        "flip_angle_deg": _float(dataset, "FlipAngle"),
        "slice_thickness_mm": _float(dataset, "SliceThickness"),
        "spacing_between_slices_mm": _float(dataset, "SpacingBetweenSlices"),
        "receive_coil_name": _value(dataset, "ReceiveCoilName"),
        "transmit_coil_name": _value(dataset, "TransmitCoilName"),
        "imaged_nucleus": _value(dataset, "ImagedNucleus"),
        "pixel_bandwidth_hz": _float(dataset, "PixelBandwidth"),
        "percent_phase_fov": _float(dataset, "PercentPhaseFieldOfView"),
        "acquisition_matrix": [
            int(value) for value in _values(dataset, "AcquisitionMatrix")
            if value.strip().isdigit()
        ],
        "phase_encoding_direction": _value(dataset, "InPlanePhaseEncodingDirection"),
        "phase_encoding_steps": _integer(dataset, "NumberOfPhaseEncodingSteps"),
        "parallel_acquisition": _value(dataset, "ParallelAcquisition"),
        "parallel_technique": _value(dataset, "ParallelAcquisitionTechnique"),
        "acceleration_factor_in_plane": _float(dataset, "ParallelReductionFactorInPlane"),
        "acceleration_factor_out_of_plane": _float(
            dataset, "ParallelReductionFactorOutOfPlane"),
        "reconstruction_diameter_mm": _float(dataset, "ReconstructionDiameter"),
        "echo_train_length": _integer(dataset, "EchoTrainLength"),
        "number_of_averages": _float(dataset, "NumberOfAverages"),
        "mr_acquisition_type": _value(dataset, "MRAcquisitionType"),
        "complex_image_component": _value(dataset, "ComplexImageComponent"),
        "acquisition_contrast": _value(dataset, "AcquisitionContrast"),
        "image_type": _values(dataset, "ImageType"),
        "rescale_slope": _float(dataset, "RescaleSlope"),
        "rescale_intercept": _float(dataset, "RescaleIntercept"),
        "bits_stored": _integer(dataset, "BitsStored"),
        "pixel_representation": _integer(dataset, "PixelRepresentation"),
        "rows": _integer(dataset, "Rows"),
        "columns": _integer(dataset, "Columns"),
        "voxel_spacing_mm": _float_values(dataset, "PixelSpacing"),
    }


def validate_deidentified_part10(
        source: str | Path | BinaryIO, *, allowed_transfer_syntaxes: list[str],
        allowed_private_tags: list[str]) -> dict[str, Any]:
    """Validate the non-pixel confidentiality contract on the exact Part-10 object bytes.

    The burned-in annotation flag is an attestation, not pixel-content analysis. Sites remain
    responsible for visual/pixel-PHI review when their approved policy requires it.
    """
    try:
        dataset = pydicom.dcmread(source, stop_before_pixels=True, force=False)
    except Exception as exc:
        raise ValueError("upload_contains_invalid_dicom") from exc
    transfer_syntax = str(getattr(dataset.file_meta, "TransferSyntaxUID", ""))
    if transfer_syntax not in set(allowed_transfer_syntaxes):
        raise ValueError("dicom_transfer_syntax_not_allowed")
    allowed_private = set(allowed_private_tags)
    for element in dataset.iterall():
        keyword = element.keyword or ""
        tag_number = f"{element.tag.group:04X},{element.tag.element:04X}"
        if element.tag.is_private and tag_number not in allowed_private:
            raise ValueError("dicom_private_tag_policy_failed")
        if ((keyword in PROHIBITED_DIRECT_KEYWORDS or element.VR == "PN")
                and str(element.value or "").strip()):
            raise ValueError("dicom_direct_identifier_policy_failed")
    if str(getattr(dataset, "PatientIdentityRemoved", "")).upper() != "YES":
        raise ValueError("dicom_deidentification_attestation_missing")
    if not (str(getattr(dataset, "DeidentificationMethod", "")).strip()
            or getattr(dataset, "DeidentificationMethodCodeSequence", None)):
        raise ValueError("dicom_deidentification_method_missing")
    if str(getattr(dataset, "BurnedInAnnotation", "")).upper() != "NO":
        raise ValueError("dicom_pixel_phi_attestation_missing")
    if str(getattr(dataset, "RecognizableVisualFeatures", "NO")).upper() == "YES":
        raise ValueError("dicom_recognizable_visual_features_present")
    identifiers = {
        "sop_instance_uid": str(getattr(dataset, "SOPInstanceUID", "")),
        "series_instance_uid": str(getattr(dataset, "SeriesInstanceUID", "")),
        "study_instance_uid": str(getattr(dataset, "StudyInstanceUID", "")),
    }
    if any(re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value) is None
           for value in identifiers.values()):
        raise ValueError("dicom_uid_contract_invalid")
    patient_id = str(getattr(dataset, "PatientID", "")).strip()
    if (not patient_id or len(patient_id) > 128
            or any(char in patient_id for char in "\r\n\0,")):
        raise ValueError("dicom_pseudonymous_patient_id_invalid")
    return {
        **identifiers,
        "patient_id": patient_id,
        "transfer_syntax": transfer_syntax,
        "modality": _value(dataset, "Modality"),
        "series_description": _value(dataset, "SeriesDescription"),
        "acquisition": _exact_acquisition(dataset),
    }
