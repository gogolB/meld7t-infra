from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "validate-production-config.py"
SPEC = importlib.util.spec_from_file_location("validate_production_config", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class ProductionIdentityValidationTests(unittest.TestCase):
    def validate(self, roles: str):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            users_file = root / "users.caddy"
            roles_file = root / "roles.caddy"
            users_file.write_text(
                f"alice $2b$12${'A' * 53}\n", encoding="utf-8"
            )
            roles_file.write_text(roles, encoding="utf-8")
            return VALIDATOR.validate_identity_maps(users_file, roles_file)

    def test_accepts_one_named_admin(self) -> None:
        users, admins = self.validate('alice "admin"\n')
        self.assertEqual(users, {"alice"})
        self.assertEqual(admins, {"alice"})

    def test_requires_one_named_admin(self) -> None:
        with self.assertRaisesRegex(ValueError, "one named institutional admin"):
            self.validate('alice "submitter"\n')

    def test_rejects_empty_role_mapping_instead_of_falling_back_to_submitter(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty role mapping"):
            self.validate('alice ""\n')

    def test_orthanc_storage_caps_must_reject_instead_of_recycling(self) -> None:
        self.assertEqual(VALIDATOR.orthanc_storage_cap({
            "ORTHANC__MAXIMUM_STORAGE_SIZE": "2097152",
            "ORTHANC__MAXIMUM_STORAGE_MODE": "Reject",
        }, "Research Orthanc"), 2097152)
        with self.assertRaisesRegex(ValueError, "instead of recycling studies"):
            VALIDATOR.orthanc_storage_cap({
                "ORTHANC__MAXIMUM_STORAGE_SIZE": "2097152",
                "ORTHANC__MAXIMUM_STORAGE_MODE": "Recycle",
            }, "Research Orthanc")


if __name__ == "__main__":
    unittest.main()
