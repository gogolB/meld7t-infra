from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "validate-network-subnets.py"


class NetworkSubnetValidationTests(unittest.TestCase):
    def run_validator(self, planned: str, existing: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            planned_path = root / "planned.tsv"
            existing_path = root / "existing.tsv"
            planned_path.write_text(planned, encoding="utf-8")
            existing_path.write_text(existing, encoding="utf-8")
            return subprocess.run(
                ["python3", str(SCRIPT), str(planned_path), str(existing_path)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )

    def test_accepts_exact_existing_replacement_and_disjoint_network(self) -> None:
        result = self.run_validator(
            "meld-data-net\t10.89.20.0/24\tdata.network\n"
            "meld-harmonization-net\t10.89.50.0/24\tharmonization.network\n",
            "meld-data-net\t-\n"
            "meld-data-net\t10.89.20.0/24\n"
            "podman\t-\n"
            "podman\t10.88.0.0/16\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_new_compute_identity_beside_disjoint_legacy_network(self) -> None:
        result = self.run_validator(
            "meld-compute-net\t10.89.30.0/24\tmeld-net.network\n",
            "meld-net\t-\n"
            "meld-net\t10.89.0.0/24\n"
            "podman\t10.88.0.0/16\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_partial_overlap_between_planned_networks(self) -> None:
        result = self.run_validator(
            "first\t10.89.0.0/16\tfirst.network\n"
            "second\t10.89.50.0/24\tsecond.network\n",
            "",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("overlaps", result.stderr)

    def test_rejects_overlap_with_differently_named_existing_network(self) -> None:
        result = self.run_validator(
            "harmonization\t10.89.50.0/24\tharmonization.network\n",
            "unrelated\t10.89.0.0/16\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existing Podman network", result.stderr)

    def test_rejects_same_name_with_different_existing_topology(self) -> None:
        result = self.run_validator(
            "harmonization\t10.89.50.0/24\tharmonization.network\n",
            "harmonization\t10.89.51.0/24\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected", result.stderr)

    def test_rejects_same_name_network_without_subnets(self) -> None:
        result = self.run_validator(
            "harmonization\t10.89.50.0/24\tharmonization.network\n",
            "harmonization\t-\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected", result.stderr)

    def test_rejects_noncanonical_subnet(self) -> None:
        result = self.run_validator(
            "harmonization\t10.89.50.1/24\tharmonization.network\n", "",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid canonical subnet", result.stderr)

    def test_builder_unit_requires_every_environment_file(self) -> None:
        unit = (SCRIPT.parent / "systemd/meld7t-harmonization-builder.service").read_text(
            encoding="utf-8"
        )
        environment_lines = [
            line for line in unit.splitlines() if line.startswith("EnvironmentFile=")
        ]
        self.assertEqual(len(environment_lines), 3)
        self.assertTrue(all(not line.startswith("EnvironmentFile=-")
                            for line in environment_lines))


if __name__ == "__main__":
    unittest.main()
