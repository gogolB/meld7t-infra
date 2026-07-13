from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ACTIVATE = REPOSITORY_ROOT / "ops/deployment/activate-release.sh"


class ActivateReleaseRollbackTests(unittest.TestCase):
    def _write(self, path: Path, content: str = "\n", *, executable: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if executable:
            path.chmod(0o755)

    def test_failed_first_builder_activation_restores_previous_links_and_unit_set(self) -> None:
        with tempfile.TemporaryDirectory(prefix="meld7t-activate-test-") as temp:
            root = Path(temp)
            release_root = root / "releases"
            config_root = root / "config"
            quadlet_root = root / "quadlet-root"
            user_unit_root = root / "user-units"
            old_release = release_root / "releases/old"
            new_release = release_root / "releases/new"
            old_config = config_root / "releases/old"
            new_config = config_root / "releases/new"
            old_quadlets = old_config / "quadlets"
            new_quadlets = new_config / "quadlets"
            for directory in (
                old_release, new_release / "release-receipt", old_quadlets,
                new_quadlets, user_unit_root, quadlet_root,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            self._write(new_release / "release-receipt/images.lock")

            old_units = (
                "meld7t-worker.service", "meld7t-health.service", "meld7t-health.timer",
            )
            new_units = old_units + ("meld7t-harmonization-builder.service",)
            for unit in old_units:
                self._write(old_config / "systemd" / unit, f"old {unit}\n")
            for unit in new_units:
                self._write(new_config / "systemd" / unit, f"new {unit}\n")
            self._write(new_quadlets / "harmonization-postgres.container")
            self._write(new_quadlets / "harmonization-orthanc.container")

            (release_root / "current").symlink_to(old_release, target_is_directory=True)
            (config_root / "current").symlink_to(old_config, target_is_directory=True)
            (quadlet_root / "meld7t-current").symlink_to(
                old_quadlets, target_is_directory=True
            )
            for unit in old_units:
                (user_unit_root / unit).symlink_to(old_config / "systemd" / unit)

            fake_bin = root / "bin"
            systemctl_log = root / "systemctl.log"
            wants_dir = root / "default.target.wants"
            wants_dir.mkdir()
            self._write(
                fake_bin / "systemctl",
                """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$SYSTEMCTL_LOG"
action=${2:-}
unit=${3:-}
if [[ $action == enable ]]; then
  [[ -e $TEST_USER_UNIT_ROOT/$unit ]] || exit 1
  ln -sfn "$TEST_USER_UNIT_ROOT/$unit" "$SYSTEMD_WANTS_DIR/$unit"
elif [[ $action == disable ]]; then
  # Model systemd's inability to disable an unknown fragment after its link was removed.
  [[ -e $TEST_USER_UNIT_ROOT/$unit ]] || exit 1
  rm -f -- "$SYSTEMD_WANTS_DIR/$unit"
fi
if [[ $* == *" start "* && $* == *"harmonization-postgres.service"* ]]; then
  exit 1
fi
exit 0
""",
                executable=True,
            )
            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                "SYSTEMCTL_LOG": str(systemctl_log),
                "SYSTEMD_WANTS_DIR": str(wants_dir),
                "TEST_USER_UNIT_ROOT": str(user_unit_root),
                "MELD7T_RELEASE_ROOT": str(release_root),
                "MELD7T_CONFIG_ROOT_BASE": str(config_root),
                "MELD7T_QUADLET_ROOT": str(quadlet_root),
                "MELD7T_USER_UNIT_ROOT": str(user_unit_root),
            })
            result = subprocess.run(
                [str(ACTIVATE), "new", "--confirm-migrated"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual((release_root / "current").resolve(), old_release)
            self.assertEqual((config_root / "current").resolve(), old_config)
            self.assertEqual((quadlet_root / "meld7t-current").resolve(), old_quadlets)
            for unit in old_units:
                self.assertEqual(
                    (user_unit_root / unit).resolve(), old_config / "systemd" / unit
                )
            self.assertFalse(
                (user_unit_root / "meld7t-harmonization-builder.service").is_symlink()
            )
            self.assertFalse(
                (wants_dir / "meld7t-harmonization-builder.service").is_symlink()
            )
            self.assertTrue((wants_dir / "meld7t-worker.service").is_symlink())
            self.assertTrue((wants_dir / "meld7t-health.timer").is_symlink())

            calls = systemctl_log.read_text(encoding="utf-8").splitlines()
            starts = [line for line in calls if " start " in line]
            self.assertIn("harmonization-postgres.service", starts[0])
            self.assertNotIn("harmonization-postgres.service", starts[1])
            self.assertIn("meld7t-worker.service", starts[1])
            self.assertEqual(starts[2], "--user start meld7t-health.timer")
            disable_builder = calls.index(
                "--user disable meld7t-harmonization-builder.service"
            )
            daemon_reloads = [
                index for index, call in enumerate(calls) if call == "--user daemon-reload"
            ]
            self.assertGreater(disable_builder, daemon_reloads[0])
            self.assertLess(disable_builder, daemon_reloads[-1])


if __name__ == "__main__":
    unittest.main()
