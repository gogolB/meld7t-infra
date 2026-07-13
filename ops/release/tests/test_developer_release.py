from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "ops/release"))

from developer_release import (  # noqa: E402
    forbidden_tracked_paths,
    repository_version,
    validate_component_versions,
    validate_stable_version,
)

sys.path.insert(0, str(REPOSITORY_ROOT / "platform/api"))
from app.version import API_VERSION  # noqa: E402


class DeveloperReleaseTests(unittest.TestCase):
    def test_stable_semver_is_strict(self) -> None:
        self.assertEqual(validate_stable_version("1.2.3"), "1.2.3")
        for invalid in ("v1.2.3", "01.2.3", "1.2", "1.2.3-rc.1", "1.2.3+local"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_stable_version(invalid)

    def test_repository_component_versions_match(self) -> None:
        expected = repository_version(REPOSITORY_ROOT)
        versions = validate_component_versions(REPOSITORY_ROOT, expected)
        self.assertEqual(set(versions.values()), {expected})
        self.assertEqual(API_VERSION, expected)

    def test_sensitive_tracked_paths_are_rejected_but_examples_are_allowed(self) -> None:
        paths = [
            "containers/config/production/api.env.example",
            "docs/key.pem.example",
            "fixtures/synthetic.dcm.example",
            "secrets/license.txt",
            "runtime/.env",
            "state/cases.sqlite",
            "keys/release-private.pem",
            "fixtures/control.DCM",
            "fixtures/anatomical.nii.gz",
            "fixtures/surface.mgz",
            "fixtures/measurements.npz",
        ]
        self.assertEqual(
            forbidden_tracked_paths(paths),
            [
                "fixtures/anatomical.nii.gz",
                "fixtures/control.DCM",
                "fixtures/measurements.npz",
                "fixtures/surface.mgz",
                "keys/release-private.pem",
                "runtime/.env",
                "secrets/license.txt",
                "state/cases.sqlite",
            ],
        )

    def test_workflow_actions_are_immutable_and_privileges_are_separated(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github/workflows/developer-release.yml"
        ).read_text(encoding="utf-8")
        action_refs = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)
        self.assertTrue(action_refs)
        for action_ref in action_refs:
            with self.subTest(action_ref=action_ref):
                self.assertRegex(action_ref, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        self.assertNotIn("pull_request_target", workflow)
        self.assertIn("pull_request:", workflow)
        self.assertIn("persist-credentials: false", workflow)
        attest = workflow.split("\n  attest:", maxsplit=1)[1].split(
            "\n  publish:", maxsplit=1
        )[0]
        publish = workflow.split("\n  publish:", maxsplit=1)[1]
        self.assertNotIn("actions/checkout", attest)
        self.assertIn("contents: read", attest)
        self.assertNotIn("contents: write", attest)
        self.assertIn("id-token: write", attest)
        self.assertIn("attestations: write", attest)
        self.assertIn("artifact-metadata: write", attest)
        self.assertNotIn("actions/checkout", publish)
        self.assertIn("contents: write", publish)
        self.assertNotIn("id-token: write", publish)
        self.assertNotIn("attestations: write", publish)
        self.assertNotIn("artifact-metadata: write", publish)
        self.assertIn("--draft", publish)
        self.assertIn(".digest", publish)
        self.assertIn("--draft=false", publish)

    def test_packager_requires_the_exact_local_release_tag(self) -> None:
        packager = (
            REPOSITORY_ROOT / "ops/release/build-github-release.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('RELEASE_TAG="v$VERSION"', packager)
        self.assertIn('refs/tags/$RELEASE_TAG^{commit}', packager)
        self.assertIn('[[ $TAG_SHA == "$GIT_SHA" ]]', packager)


if __name__ == "__main__":
    unittest.main()
