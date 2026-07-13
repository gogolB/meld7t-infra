from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harmonization_release_policy import validate_policy  # noqa: E402


class HarmonizationReleasePolicyTests(unittest.TestCase):
    def test_signed_bootstrap_accepts_only_zero_profiles_and_empty_inventory(self) -> None:
        validate_policy([], 0, True)

        with self.assertRaisesRegex(ValueError, "cannot contain"):
            validate_policy([], 1, True)
        with self.assertRaisesRegex(ValueError, "exactly empty"):
            validate_policy([{"code": "H_SITE"}], 0, True)

    def test_normal_release_requires_profiles_and_nonempty_inventory(self) -> None:
        validate_policy([{"code": "H_SITE"}], 1, False)

        with self.assertRaisesRegex(ValueError, "at least one"):
            validate_policy([], 0, False)
        with self.assertRaisesRegex(ValueError, "non-empty"):
            validate_policy([], 1, False)

    def test_inventory_must_be_array(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON array"):
            validate_policy({"code": "H_SITE"}, 1, False)


if __name__ == "__main__":
    unittest.main()
