from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

import nibabel as nib
import numpy as np

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
HAS_HIGHDICOM = importlib.util.find_spec("highdicom") is not None

import map_morphometry  # noqa: E402
import recon_prepare  # noqa: E402
import clean_uni  # noqa: E402
import hippunfold_summarize  # noqa: E402
import verify_cache  # noqa: E402

MANAGE_SPEC = importlib.util.spec_from_file_location(
    "harmonization_manage_for_pkg_tests", PKG.parents[1] / "ops" / "harmonization" / "manage.py")
harmonization_manage = importlib.util.module_from_spec(MANAGE_SPEC)
assert MANAGE_SPEC and MANAGE_SPEC.loader
MANAGE_SPEC.loader.exec_module(harmonization_manage)


def _load_package_dicom():
    """Geometry helpers do not need highdicom/pydicom; stub them on lean dev hosts."""
    for name in ("highdicom", "pydicom", "pydicom.dataset", "pydicom.uid"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["pydicom"].dataset = sys.modules["pydicom.dataset"]
    sys.modules["pydicom"].uid = sys.modules["pydicom.uid"]
    sys.modules["pydicom.dataset"].Dataset = type("Dataset", (), {})
    sys.modules["pydicom.dataset"].FileMetaDataset = type("FileMetaDataset", (), {})
    sys.modules["pydicom.uid"].ExplicitVRLittleEndian = "1.2.840.10008.1.2.1"
    sys.modules["pydicom.uid"].MRImageStorage = "1.2.840.10008.5.1.4.1.1.4"
    sys.modules["pydicom.uid"].generate_uid = lambda: "1.2.3"
    spec = importlib.util.spec_from_file_location("package_dicom_under_test", PKG / "package_dicom.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_derived_dicom():
    package_dicom = _load_package_dicom()
    sys.modules["package_dicom"] = package_dicom
    spec = importlib.util.spec_from_file_location(
        "package_derived_dicom_under_test", PKG / "package_derived_dicom.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackageContractsTest(unittest.TestCase):
    @unittest.skipUnless(HAS_HIGHDICOM, "real highdicom is exercised in the pkg image")
    def test_real_highdicom_builds_map_seg_and_parametric_maps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shape = (4, 5, 6)
            affine = np.diag([1.5, 2.0, 2.5, 1.0])
            t1 = root / "T1.nii.gz"
            deformation = root / "iy_T1.nii"
            nib.save(nib.Nifti1Image(np.ones(shape, dtype=np.float32), affine), t1)
            indices = np.indices(shape).reshape(3, -1).T
            world = nib.affines.apply_affine(affine, indices).reshape((*shape, 3))
            nib.save(nib.Nifti1Image(world[:, :, :, None, :], affine), deformation)
            paths = {}
            for feature, axis in (("junction", 0), ("extension", 1)):
                threshold = (np.indices(shape)[axis] > 1).astype(np.uint8)
                z_map = np.linspace(-4, 7, np.prod(shape), dtype=np.float32).reshape(shape)
                paths[f"{feature}_threshold"] = root / f"{feature}_threshold.nii.gz"
                paths[f"{feature}_z"] = root / f"{feature}_z.nii.gz"
                nib.save(nib.Nifti1Image(threshold, affine), paths[f"{feature}_threshold"])
                nib.save(nib.Nifti1Image(z_map, affine), paths[f"{feature}_z"])
            manifest = root / "manifest.json"
            subprocess.run([
                sys.executable, str(PKG / "package_derived_dicom.py"), "map",
                "--t1", str(t1), "--inverse-deformation", str(deformation),
                "--junction-threshold", str(paths["junction_threshold"]),
                "--junction-z", str(paths["junction_z"]),
                "--extension-threshold", str(paths["extension_threshold"]),
                "--extension-z", str(paths["extension_z"]),
                "--pseudonym", "P001", "--uid-seed", "run-1",
                "--study-uid-seed", "recipe-1", "--expected-clusters", "1",
                "--harmonization-status", "unharmonized",
                "--manifest-output", str(manifest),
            ], check=True, capture_output=True, text=True)
            document = json.loads(manifest.read_text())
            roles = {item["role"] for item in document["derived_series"]["series"]}
            self.assertEqual(roles, {
                "map_native_t1_reference", "map_candidate_segmentation",
                "map_junction_z_parametric_map", "map_extension_z_parametric_map",
            })
            self.assertEqual(document["derived_series"]["harmonization"]["status"],
                             "unharmonized")

    def test_hippunfold_cache_runtime_verifies_exact_file_closure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "models" / "weights.bin"
            model.parent.mkdir()
            model.write_bytes(b"immutable-model")
            file_digest = hashlib.sha256(model.read_bytes()).hexdigest()
            manifest = root / ".meld7t-cache-files.sha256"
            manifest.write_text(f"{file_digest}  models/weights.bin\n")
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            (root / ".meld7t-signed-archive-sha256").write_text(manifest_digest + "\n")
            self.assertEqual(
                verify_cache.verify_cache(root, manifest_digest)["files"], 1
            )
            model.write_bytes(b"mutated-model")
            with self.assertRaisesRegex(ValueError, "closure mismatch"):
                verify_cache.verify_cache(root, manifest_digest)

    def test_mp2rage_clean_requires_exact_inv_geometry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            affine = np.eye(4)
            data = np.ones((24, 24, 24), dtype=np.float32)
            paths = [root / name for name in ("uni.nii.gz", "inv1.nii.gz", "inv2.nii.gz")]
            nib.save(nib.Nifti1Image(data, affine), paths[0])
            nib.save(nib.Nifti1Image(data, affine), paths[1])
            shifted = affine.copy()
            shifted[0, 3] = 1
            nib.save(nib.Nifti1Image(data, shifted), paths[2])
            with self.assertRaisesRegex(ValueError, "affines do not match"):
                clean_uni.obrien_clean(*paths, root / "out.nii.gz")

            nib.save(nib.Nifti1Image(np.full((24, 24, 24), 5000, dtype=np.float32), affine),
                     paths[0])
            nib.save(nib.Nifti1Image(data, affine), paths[2])
            with self.assertRaisesRegex(ValueError, "0..4095"):
                clean_uni.obrien_clean(*paths, root / "out.nii.gz")

    def test_seg_geometry_must_match_exact_t1_grid(self):
        package_dicom = _load_package_dicom()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            t1, pred = td / "t1.nii.gz", td / "pred.nii.gz"
            nib.save(nib.Nifti1Image(np.ones((4, 5, 6), dtype=np.float32), np.eye(4)), t1)
            nib.save(nib.Nifti1Image(np.zeros((4, 5, 5), dtype=np.float32), np.eye(4)), pred)
            with self.assertRaisesRegex(package_dicom.GeometryError, "shape mismatch"):
                package_dicom.validate_nifti_geometry(t1, pred)

            shifted = np.eye(4)
            shifted[0, 3] = 2.0
            nib.save(nib.Nifti1Image(np.zeros((4, 5, 6), dtype=np.float32), shifted), pred)
            with self.assertRaisesRegex(package_dicom.GeometryError, "affine mismatch"):
                package_dicom.validate_nifti_geometry(t1, pred)

    def test_stow_requires_exact_response_and_qido_confirmation(self):
        package_dicom = _load_package_dicom()
        datasets = [
            types.SimpleNamespace(
                SOPInstanceUID="1.2.3.1", SeriesInstanceUID="1.2.4", StudyInstanceUID="1.2.5"),
            types.SimpleNamespace(
                SOPInstanceUID="1.2.3.2", SeriesInstanceUID="1.2.4", StudyInstanceUID="1.2.5"),
        ]

        class Session:
            def __init__(self):
                self.trust_env = True

            def request(self, *_args, **_kwargs):
                raise AssertionError("the fake DICOMweb client should not make HTTP requests")

        class DICOMwebClient:
            response_sops = ["1.2.3.1"]
            qido_sops = ["1.2.3.1", "1.2.3.2"]
            last_url = None
            last_session = None

            def __init__(self, *, url, session):
                self.url = url
                self.session = session
                type(self).last_url = url
                type(self).last_session = session

            def store_instances(self, _datasets):
                return types.SimpleNamespace(
                    ReferencedSOPSequence=[
                        types.SimpleNamespace(ReferencedSOPInstanceUID=uid)
                        for uid in self.response_sops
                    ],
                    FailedSOPSequence=[],
                )

            def search_for_instances(self, **_kwargs):
                return [
                    {"00080018": {"Value": [uid]}}
                    for uid in self.qido_sops
                ]

            def retrieve_instance(self, _study_uid, _series_uid, sop_uid):
                return next(ds for ds in datasets if ds.SOPInstanceUID == sop_uid)

        requests_module = types.ModuleType("requests")
        requests_module.Session = Session
        dicomweb_module = types.ModuleType("dicomweb_client")
        dicomweb_api_module = types.ModuleType("dicomweb_client.api")
        dicomweb_api_module.DICOMwebClient = DICOMwebClient
        dicomweb_module.api = dicomweb_api_module
        modules = {
            "requests": requests_module,
            "dicomweb_client": dicomweb_module,
            "dicomweb_client.api": dicomweb_api_module,
        }
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, "STOW response"):
                package_dicom.stow("http://orthanc/dicom-web", datasets)

            DICOMwebClient.response_sops = ["1.2.3.1", "1.2.3.2"]
            DICOMwebClient.qido_sops = ["1.2.3.1"]
            with self.assertRaisesRegex(RuntimeError, "post-STOW QIDO"):
                package_dicom.stow("http://orthanc/dicom-web", datasets)

            DICOMwebClient.qido_sops = ["1.2.3.1", "1.2.3.2"]
            package_dicom.stow(
                "http://meld-internal:p%40ssword@orthanc:8042/dicom-web", datasets)
            self.assertEqual(
                DICOMwebClient.last_url, "http://orthanc:8042/dicom-web")
            self.assertEqual(
                DICOMwebClient.last_session.auth, ("meld-internal", "p@ssword"))

    def test_seg_wado_semantics_bind_per_frame_geometry(self):
        package_dicom = _load_package_dicom()

        def dataset(position):
            source = types.SimpleNamespace(
                ReferencedSOPClassUID="1.2.3",
                ReferencedSOPInstanceUID="1.2.3.4",
                ReferencedFrameNumber=[1],
                PurposeOfReferenceCodeSequence=[],
            )
            group = types.SimpleNamespace(
                PlanePositionSequence=[types.SimpleNamespace(
                    ImagePositionPatient=position)],
                PlaneOrientationSequence=[types.SimpleNamespace(
                    ImageOrientationPatient=[1, 0, 0, 0, 1, 0])],
                PixelMeasuresSequence=[types.SimpleNamespace(
                    PixelSpacing=[0.7, 0.7], SliceThickness=0.7)],
                SegmentIdentificationSequence=[types.SimpleNamespace(
                    ReferencedSegmentNumber=1)],
                FrameContentSequence=[types.SimpleNamespace(
                    DimensionIndexValues=[1, 1])],
                DerivationImageSequence=[types.SimpleNamespace(
                    DerivationCodeSequence=[], SourceImageSequence=[source])],
            )
            return types.SimpleNamespace(
                SOPClassUID="1.2.840", SOPInstanceUID="1.2.840.1",
                StudyInstanceUID="1.2.5", SeriesInstanceUID="1.2.6",
                FrameOfReferenceUID="1.2.7", Modality="SEG", NumberOfFrames=1,
                PixelData=b"pixels", PerFrameFunctionalGroupsSequence=[group],
            )

        first = package_dicom._critical_dicom_semantics(dataset([0, 0, 0]))
        changed = package_dicom._critical_dicom_semantics(dataset([0, 0, 0.7]))
        self.assertNotEqual(first, changed)

    def test_parametric_map_wado_semantics_bind_float_pixels(self):
        package_dicom = _load_package_dicom()

        def dataset(values, slope=1.0):
            mapping = types.SimpleNamespace(
                LUTLabel="MAPZ", LUTExplanation="MAP z", RealWorldValueSlope=slope,
                RealWorldValueIntercept=0.0, MeasurementUnitsCodeSequence=[
                    types.SimpleNamespace(CodeValue="1", CodingSchemeDesignator="UCUM",
                                          CodeMeaning="no units")],
                QuantityDefinitionSequence=[],
            )
            return types.SimpleNamespace(
                SOPClassUID="1.2.840", SOPInstanceUID="1.2.840.1",
                StudyInstanceUID="1.2.5", SeriesInstanceUID="1.2.6",
                FrameOfReferenceUID="1.2.7", Modality="OT", NumberOfFrames=1,
                FloatPixelData=np.asarray(values, dtype=np.float32).tobytes(),
                SharedFunctionalGroupsSequence=[types.SimpleNamespace(
                    RealWorldValueMappingSequence=[mapping])],
            )

        first = package_dicom._critical_dicom_semantics(dataset([0.0, 1.0]))
        changed = package_dicom._critical_dicom_semantics(dataset([0.0, 2.0]))
        self.assertEqual(first["pixel_data_keyword"], "FloatPixelData")
        self.assertNotEqual(first["pixel_sha256"], changed["pixel_sha256"])
        changed_mapping = package_dicom._critical_dicom_semantics(
            dataset([0.0, 1.0], slope=2.0))
        self.assertNotEqual(first, changed_mapping)

    def test_recipe_study_uid_is_shared_but_run_series_uids_are_unique(self):
        package_dicom = _load_package_dicom()
        self.assertEqual(
            package_dicom.deterministic_uid("recipe-1", "study"),
            package_dicom.deterministic_uid("recipe-1", "study"),
        )
        self.assertNotEqual(
            package_dicom.deterministic_uid("run-1", "map-seg-series"),
            package_dicom.deterministic_uid("run-2", "map-seg-series"),
        )

    def test_unharmonized_warning_is_visible_and_hashed_in_series_manifest(self):
        package_dicom = _load_package_dicom()
        datasets = [types.SimpleNamespace(
            StudyInstanceUID="1.2.3", SeriesInstanceUID="1.2.4",
            SOPInstanceUID="1.2.5", Modality="SEG", SeriesDescription="MAP candidates",
        )]
        provenance = package_dicom.harmonization_provenance("unharmonized")
        package_dicom.mark_harmonization(datasets, provenance)
        self.assertIn("UNHARMONIZED RESEARCH RESULT", datasets[0].SeriesDescription)
        self.assertIn("UNHARMONIZED RESEARCH RESULT", datasets[0].DerivationDescription)
        manifest = package_dicom.derived_series_manifest(
            datasets, {"1.2.4": "map_candidate_segmentation"}, provenance)
        self.assertEqual(manifest["harmonization"]["status"], "unharmonized")
        before = package_dicom._critical_dicom_semantics(datasets[0])
        datasets[0].DerivationDescription = "warning removed"
        after = package_dicom._critical_dicom_semantics(datasets[0])
        self.assertNotEqual(before, after)

    def test_spm_inverse_deformation_pulls_mni_map_to_native_grid(self):
        derived = _load_derived_dicom()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shape = (4, 5, 6)
            affine = np.diag([1.5, 2.0, 2.5, 1.0])
            native = root / "T1.nii.gz"
            mni = root / "z.nii.gz"
            deformation = root / "iy_T1.nii"
            values = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
            nib.save(nib.Nifti1Image(np.ones(shape, dtype=np.float32), affine), native)
            nib.save(nib.Nifti1Image(values, affine), mni)
            indices = np.indices(shape).reshape(3, -1).T
            world = nib.affines.apply_affine(affine, indices).reshape((*shape, 3))
            nib.save(nib.Nifti1Image(world[:, :, :, None, :], affine), deformation)

            pulled, pulled_affine = derived.pull_mni_to_native(
                str(mni), str(deformation), str(native), order=1)
            np.testing.assert_allclose(pulled, values, atol=1e-4)
            np.testing.assert_allclose(pulled_affine, affine)

    def test_spm_inverse_deformation_rejects_wrong_native_grid(self):
        derived = _load_derived_dicom()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            native = root / "T1.nii.gz"
            mni = root / "z.nii.gz"
            deformation = root / "iy_T1.nii"
            nib.save(nib.Nifti1Image(np.ones((4, 4, 4)), np.eye(4)), native)
            nib.save(nib.Nifti1Image(np.ones((4, 4, 4)), np.eye(4)), mni)
            nib.save(nib.Nifti1Image(np.zeros((3, 4, 4, 1, 3)), np.eye(4)), deformation)
            with self.assertRaisesRegex(derived.dicom.GeometryError, "does not match"):
                derived.pull_mni_to_native(
                    str(mni), str(deformation), str(native), order=0)

    def test_hippunfold_dseg_resampling_is_discrete_and_requires_overlap(self):
        derived = _load_derived_dicom()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            native = root / "T2.nii.gz"
            dseg = root / "hemi-L_desc-subfields_dseg.nii.gz"
            nib.save(nib.Nifti1Image(np.ones((6, 6, 6)), np.eye(4)), native)
            data = np.zeros((3, 3, 3), dtype=np.uint8)
            data[1, 1, 1] = 3
            nib.save(nib.Nifti1Image(data, np.diag([2.0, 2.0, 2.0, 1.0])), dseg)
            resampled, _ = derived.resample_dseg_to_native(str(dseg), str(native))
            self.assertLessEqual(set(np.unique(resampled)), {0, 3})
            self.assertTrue(np.any(resampled == 3))

            shifted = np.eye(4)
            shifted[:3, 3] = 1000
            nib.save(nib.Nifti1Image(data, shifted), dseg)
            with self.assertRaisesRegex(derived.dicom.GeometryError, "no overlap"):
                derived.resample_dseg_to_native(str(dseg), str(native))

    def test_recon_exact_uid_override_never_rediscovers(self):
        series = {
            "1.2.3.1": {"path": "/one", "folder": "ambiguous-one", "role": "unknown",
                        "reason": "", "tags": {"image_type": []}},
            "1.2.3.2": {"path": "/two", "folder": "ambiguous-two", "role": "t1_mprage",
                        "reason": "classified", "tags": {"image_type": []}},
        }
        path, why = recon_prepare.select("t1_mprage", series, None, "1.2.3.1")
        self.assertEqual(path, "/one")
        self.assertIn("1.2.3.1", why)
        with self.assertRaises(SystemExit):
            recon_prepare.select("t1_mprage", series, None, "9.9.9")

    def test_map_normative_grid_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            normative = root / "normative" / "map"
            normative.mkdir(parents=True)
            affine = np.eye(4)
            reference = nib.Nifti1Image(np.ones((5, 5, 5), dtype=np.float32), affine)
            shifted = affine.copy()
            shifted[1, 3] = 3
            nib.save(nib.Nifti1Image(np.zeros((5, 5, 5), dtype=np.float32), shifted),
                     normative / "junction_mean.nii.gz")
            nib.save(nib.Nifti1Image(np.ones((5, 5, 5), dtype=np.float32), affine),
                     normative / "junction_std.nii.gz")
            with self.assertRaisesRegex(ValueError, "affine"):
                map_morphometry.z_normative(
                    np.ones((5, 5, 5), dtype=np.float32), "junction", str(root), reference)

    def test_map_output_discovery_rejects_duplicate_exact_derivatives(self):
        with tempfile.TemporaryDirectory() as td:
            root, subject = Path(td), "sub-test"
            output = root / subject
            output.mkdir(parents=True)
            (output / "wc1T1.nii").write_bytes(b"first")
            (output / "wc1T1.nii.gz").write_bytes(b"second")

            with self.assertRaisesRegex(ValueError, "ambiguous MAP wc1 output"):
                map_morphometry._find(str(root), subject, "wc1")

    def test_hippunfold_output_discovery_rejects_duplicate_preferred_dsegs(self):
        with tempfile.TemporaryDirectory() as td:
            root, subject = Path(td), "sub-test"
            output = root / "hippunfold" / subject / "anat"
            output.mkdir(parents=True)
            for suffix in ("first", "second"):
                (output / f"{suffix}_hemi-L_space-T2w_desc-subfields_dseg.nii.gz").write_bytes(
                    b"duplicate"
                )

            with self.assertRaisesRegex(ValueError, "ambiguous HippUnfold L dseg output"):
                hippunfold_summarize.find_dseg(str(root), subject, "L")

    def test_map_profile_finalizer_emits_hashed_versioned_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subjects = root / "subjects.txt"
            demographics = root / "demographics.csv"
            ids = [f"sub-{index:03d}" for index in range(20)]
            subjects.write_text("\n".join(ids) + "\n")
            demographics.write_text("ID,Age,Sex\n" + "\n".join(
                f"{subject},{20 + index},{'female' if index % 2 else 'male'}"
                for index, subject in enumerate(ids)
            ) + "\n")
            selector = root / "selector.json"
            selector.write_text('{"roles":["t1_mprage"],"acquisition":{"model":"Terra"}}')
            source = root / "source" / "normative" / "map"
            source.mkdir(parents=True)
            for feature in ("junction", "extension"):
                nib.save(nib.Nifti1Image(np.ones((4, 5, 6), dtype=np.float32), np.eye(4)),
                         source / f"{feature}_mean.nii.gz")
                nib.save(nib.Nifti1Image(np.full((4, 5, 6), 0.5, dtype=np.float32), np.eye(4)),
                         source / f"{feature}_std.nii.gz")
            output = root / "bundle" / "profiles" / "MAPSITE-v1"
            profile = root / "bundle" / "profiles" / "MAPSITE-v1.json"
            cohort = root / "attestations" / "MAPSITE-v1-cohort.json"
            validation = root / "attestations" / "MAPSITE-v1-validation.json"
            validation.parent.mkdir(parents=True)
            validation.write_text(json.dumps({
                "schema_version": 1,
                "profile": {"code": "MAPSITE", "version": 1, "detector_id": "map"},
                "approval_id": "SITE-VALIDATION-001",
                "independent_reviewer": "reviewer@example.test",
                "approved_at": "2026-07-10T12:00:00Z",
                "acquisition_fingerprints": ["c" * 64],
                "qc": {"included": 20, "excluded": 1},
                "holdout": {"case_count": 3, "positive_cases": 1,
                            "negative_cases": 1, "control_cases": 1},
                "metrics_sha256": "d" * 64,
                "golden_case_evidence_sha256": "e" * 64,
                "methodology_sha256": "f" * 64,
                "image_digests": {
                    "spm": "example/spm@sha256:" + "a" * 64,
                    "pkg": "example/pkg@sha256:" + "b" * 64,
                },
            }))
            evidence_key = root / "evidence.key"
            evidence_key.write_bytes(b"k" * 32)
            args = types.SimpleNamespace(
                code="MAPSITE", version=1, name="MAP site profile", subjects=subjects,
                demographics=demographics, selector=selector,
                artifact_source=root / "source", output=output,
                manifest_prefix="profiles/MAPSITE-v1", final_profile=profile,
                cohort_manifest=cohort,
                spm_image="example/spm@sha256:" + "a" * 64,
                pkg_image="example/pkg@sha256:" + "b" * 64,
                minimum_subjects=20,
                validation_report=validation,
                evidence_hmac_key_file=evidence_key,
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(harmonization_manage.map_finalize(args), 0)
            contract = json.loads(profile.read_text())
            self.assertEqual(contract["method"], "map_normative")
            self.assertEqual(len(contract["artifact_manifest"]["files"]), 4)
            self.assertEqual(contract["parameters"]["control_count"], 20)

    def test_hippunfold_below_threshold_emits_no_cluster(self):
        with tempfile.TemporaryDirectory() as td:
            root, subject = Path(td), "sub-test"
            anat = root / "hippunfold" / subject / "anat"
            anat.mkdir(parents=True)

            def write(hemi, count):
                data = np.zeros(1000, dtype=np.uint8)
                data[:count] = 1
                data = data.reshape((10, 10, 10))
                nib.save(nib.Nifti1Image(data, np.eye(4)),
                         anat / f"x_hemi-{hemi}_space-T2w_desc-subfields_dseg.nii.gz")

            write("L", 950)
            write("R", 1000)
            proc = subprocess.run(
                [sys.executable, str(PKG / "hippunfold_summarize.py"),
                 "--root", str(root), "--subject", subject],
                check=True, capture_output=True, text=True,
            )
            out = json.loads(proc.stdout)
            self.assertFalse(out["flagged"])
            self.assertEqual(out["ai_threshold_pct"], 10.0)
            self.assertEqual(out["clusters"], [])
            self.assertEqual(out["n_clusters"], 0)


if __name__ == "__main__":
    unittest.main()
