from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "check-storage-reserve.py"
SPEC = importlib.util.spec_from_file_location("check_storage_reserve", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class StorageReserveTests(unittest.TestCase):
    def test_defaults_cover_routine_staging_expansion_compute_and_orthanc(self) -> None:
        reserves = CHECKER.calculate_reserves({}, {}, {}, {}, {})
        self.assertEqual(reserves, {
            "routine_state": 750 * CHECKER.GIB,
            "research_orthanc": 2048 * CHECKER.GIB,
            "harmonization_state": 1624 * CHECKER.GIB,
            "harmonization_orthanc": 2048 * CHECKER.GIB,
            "headroom": 100 * CHECKER.GIB,
        })

    def test_example_limits_are_aggregated_on_each_physical_device(self) -> None:
        reserves = CHECKER.calculate_reserves(
            {"MELD7T_CASE_UPLOAD_QUOTA_BYTES": str(500 * CHECKER.GIB)},
            {
                "MELD7T_STORAGE_MIN_FREE_BYTES": str(50 * CHECKER.GIB),
                "MELD7T_WORKER_MAX_JOBS": "2",
                "MELD7T_DICOM_MAX_BYTES_PER_RUN": str(100 * CHECKER.GIB),
                "MELD7T_STORAGE_OUTPUT_HEADROOM_BYTES": str(25 * CHECKER.GIB),
                "MELD7T_CASE_UPLOAD_MAX_EXPANDED_BYTES": str(100 * CHECKER.GIB),
            },
            {
                "MELD7T_STORAGE_MIN_FREE_BYTES": str(100 * CHECKER.GIB),
                "MELD7T_HARMONIZATION_MAX_UPLOAD_BYTES": str(500 * CHECKER.GIB),
                "MELD7T_HARMONIZATION_UPLOAD_MAX_EXPANDED_BYTES": str(500 * CHECKER.GIB),
                "MELD7T_HARMONIZATION_BUILD_MAX_BYTES": str(1024 * CHECKER.GIB),
            },
            {"ORTHANC__MAXIMUM_STORAGE_SIZE": str(2 * 1024 * 1024)},
            {"ORTHANC__MAXIMUM_STORAGE_SIZE": str(2 * 1024 * 1024)},
        )
        separate = CHECKER.required_checks(
            Path("/state"), Path("/podman"), reserves,
            device_id=lambda path: 1 if path == Path("/state") else 2,
        )
        self.assertEqual(separate, [
            (Path("/state"), 2874 * CHECKER.GIB),
            (Path("/podman"), 4196 * CHECKER.GIB),
        ])
        same = CHECKER.required_checks(
            Path("/state"), Path("/podman"), reserves, device_id=lambda _path: 1)
        self.assertEqual(same, [(Path("/state"), 6970 * CHECKER.GIB)])

    def test_capacity_fails_closed_and_env_parser_rejects_duplicates(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "120 bytes required"):
            CHECKER.verify_capacity(
                [(Path("/state"), 120)],
                disk_usage=lambda _path: SimpleNamespace(free=119),
            )
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / "worker.env"
            env.write_text("A=1\nA=2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                CHECKER.read_env(env)


if __name__ == "__main__":
    unittest.main()
