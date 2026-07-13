from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


RELEASE_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
RUNTIME_ROLES = (
    "meld_graph", "pkg", "spm", "hippunfold", "api", "postgres", "redis",
    "orthanc", "immudb", "ohif", "caddy", "registry",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_tar(path: Path, members: dict[str, bytes], *, gzipped: bool = True) -> None:
    mode = "w:gz" if gzipped else "w"
    with tarfile.open(path, mode) as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))


class SignedBootstrapIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required")
        self.temp = tempfile.TemporaryDirectory(prefix="meld7t-release-test-")
        self.root = Path(self.temp.name)
        self.private_key = self.root / "release-private.pem"
        self.public_key = self.root / "release-public.pem"
        subprocess.run([
            "openssl", "genpkey", "-quiet", "-algorithm", "RSA",
            "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(self.private_key),
        ], check=True)
        subprocess.run([
            "openssl", "pkey", "-in", str(self.private_key), "-pubout",
            "-out", str(self.public_key),
        ], check=True, stdout=subprocess.DEVNULL)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build_bundle(self, *, bootstrap: str = "true", profile_count: int = 0) -> Path:
        bundle = self.root / f"bundle-{bootstrap}-{profile_count}"
        assets = bundle / "assets"
        images = bundle / "images"
        assets.mkdir(parents=True)
        images.mkdir()

        digest = "a" * 64
        (bundle / "images.lock").write_text("".join(
            f"{role} test.invalid/{role}@sha256:{digest}\n" for role in RUNTIME_ROLES
        ), encoding="utf-8")
        for role in RUNTIME_ROLES:
            (images / f"{role}.oci.tar").write_bytes(b"")

        git_sha = "b" * 40
        map_script = b"function test_map\nend\n"
        _write_tar(bundle / "source.tar.gz", {
            "source/containers/map/segment.m": map_script,
        })
        _write_tar(assets / "web-dist.tar.gz", {
            "dist/.meld7t-git-sha": git_sha.encode(),
            "dist/index.html": b"ok",
        })
        _write_tar(assets / "worker-artifacts.tar.gz", {"requirements.lock": b"ok\n"})
        _write_tar(assets / "api-build-artifacts.tar.gz", {"requirements.lock": b"ok\n"})

        cache_content = b"model-cache\n"
        _write_tar(
            assets / "hippunfold-cache.tar", {"models/test.bin": cache_content}, gzipped=False,
        )
        cache_manifest = (
            f"{hashlib.sha256(cache_content).hexdigest()}  models/test.bin\n".encode()
        )
        (assets / "hippunfold-cache-files.sha256").write_bytes(cache_manifest)

        inventory = b"[]\n"
        _write_tar(assets / "harmonization.tar.gz", {
            "expected-active-profiles.json": inventory,
        })
        _write_tar(bundle / "attestations.tar.gz", {
            "approval.txt": b"approved\n",
            "sbom.spdx.json": b"{}\n",
            "vulnerability-report.json": b"{}\n",
            "vulnerability-exceptions.txt": b"EXPIRES=2999-12-31\n",
            "license-report.json": b"{}\n",
            "golden-case-evidence.txt": b"accepted\n",
        })

        public_der = subprocess.run([
            "openssl", "pkey", "-in", str(self.private_key), "-pubout", "-outform", "DER",
        ], check=True, stdout=subprocess.PIPE).stdout
        release_env = "\n".join((
            "MELD7T_RELEASE_FORMAT=1",
            "MELD7T_RELEASE_ID=bootstrap-test",
            f"MELD7T_GIT_SHA={git_sha}",
            f"MELD7T_MAP_SCRIPT_SHA256={hashlib.sha256(map_script).hexdigest()}",
            f"MELD7T_HIPPUNFOLD_CACHE_SHA256={hashlib.sha256(cache_manifest).hexdigest()}",
            f"MELD7T_HARMONIZATION_INVENTORY_SHA256={hashlib.sha256(inventory).hexdigest()}",
            "MELD7T_SOURCE_DATE_EPOCH=1700000000",
            f"MELD7T_SIGNER_SHA256={hashlib.sha256(public_der).hexdigest()}",
            "MELD7T_IMAGE_SCOPE=runtime",
            "MELD7T_HOST_ARTIFACTS=external-prerequisite",
            f"MELD7T_HARMONIZATION_PROFILES={profile_count}",
            f"MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED={bootstrap}",
            "",
        ))
        (bundle / "release.env").write_text(release_env, encoding="utf-8")
        self._sign_bundle(bundle)
        return bundle

    def _sign_bundle(self, bundle: Path) -> None:
        rows = []
        for path in sorted(bundle.rglob("*")):
            if (not path.is_file()
                    or path.name in {"SHA256SUMS", "SHA256SUMS.sig"}):
                continue
            relative = path.relative_to(bundle).as_posix()
            rows.append(f"{_sha256(path)}  ./{relative}\n")
        sums = bundle / "SHA256SUMS"
        sums.write_text("".join(rows), encoding="utf-8")
        subprocess.run([
            "openssl", "dgst", "-sha256", "-sign", str(self.private_key),
            "-out", str(bundle / "SHA256SUMS.sig"), str(sums),
        ], check=True)

    def _verify(self, bundle: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            str(RELEASE_DIR / "verify-airgap.sh"), str(bundle), str(self.public_key),
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_signed_empty_bootstrap_verifies_and_import_propagates_flag(self) -> None:
        bundle = self._build_bundle()
        verified = self._verify(bundle)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("verified release bootstrap-test", verified.stdout)

        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_podman = fake_bin / "podman"
        shutil.copyfile(FIXTURE_DIR / "fake_podman.py", fake_podman)
        fake_podman.chmod(0o700)
        release_root = self.root / "installed"
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env["FAKE_PODMAN_CACHE_TAR"] = str(bundle / "assets/hippunfold-cache.tar")
        imported = subprocess.run([
            str(RELEASE_DIR / "import-airgap.sh"), "--bundle", str(bundle),
            "--trusted-key", str(self.public_key), "--release-root", str(release_root),
        ], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(imported.returncode, 0, imported.stderr)

        receipt = release_root / "releases/bootstrap-test/release-receipt"
        runtime = (receipt / "runtime-images.env").read_text(encoding="utf-8")
        self.assertEqual(
            runtime.count("MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED=true\n"), 1,
        )
        self.assertIn(
            "MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED=true\n",
            (receipt / "release.env").read_text(encoding="utf-8"),
        )
        inventory = release_root / "releases/bootstrap-test/harmonization/expected-active-profiles.json"
        self.assertEqual(json.loads(inventory.read_text(encoding="utf-8")), [])

    def test_verifier_rejects_signed_empty_inventory_without_bootstrap_flag(self) -> None:
        rejected = self._verify(self._build_bundle(bootstrap="false"))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("production release requires at least one harmonization profile", rejected.stderr)

    def test_verifier_rejects_signed_profile_count_mismatch(self) -> None:
        rejected = self._verify(self._build_bundle(profile_count=1))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("profile count differs from signed manifest", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
