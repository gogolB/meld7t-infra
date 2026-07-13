from __future__ import annotations

import hashlib
import importlib.util
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "validate-production-config.py"
SPEC = importlib.util.spec_from_file_location("validate_production_branding", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)
RENDER_SCRIPT = Path(__file__).resolve().parents[1] / "verify-report-logo.py"
RENDER_SPEC = importlib.util.spec_from_file_location("verify_report_logo", RENDER_SCRIPT)
assert RENDER_SPEC is not None and RENDER_SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(RENDER_SPEC)
RENDER_SPEC.loader.exec_module(RENDERER)
BUNDLED_LOGO = Path(__file__).resolve().parents[3] / "containers/config/branding/report-logo.png"
BUNDLED_LOGO_SHA256 = "f49320e2faddc9e4c8a650d9e90a03abb7325d1990fa2666849e911301c67f96"


def png(width: int, height: int) -> bytes:
    payload = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(kind: bytes, value: bytes) -> bytes:
        return (struct.pack(">I", len(value)) + kind + value
                + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF))

    rows = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", payload)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def jpeg(width: int, height: int) -> bytes:
    frame = (b"\x08" + struct.pack(">HH", height, width) + b"\x03"
             + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00")
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame + b"\xff\xd9"


class ProductionBrandingValidationTests(unittest.TestCase):
    def test_bundled_houston_methodist_logo_and_defaults_are_pinned(self) -> None:
        payload = BUNDLED_LOGO.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), BUNDLED_LOGO_SHA256)
        self.assertEqual(RENDERER.verify_logo(BUNDLED_LOGO), (1010, 298, "PNG", "P"))
        api_example = (BUNDLED_LOGO.parents[1] / "production" / "api.env.example").read_text(
            encoding="utf-8",
        )
        self.assertIn("MELD7T_BRANDING_LOGO_URL=/branding/report-logo.png\n", api_example)
        self.assertIn(
            "MELD7T_BRANDING_LOGO_PATH=/run/branding/report-logo.png\n", api_example,
        )

    def test_text_only_branding_allows_omitted_or_blank_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(VALIDATOR.validate_report_branding({}, {}, root))
            self.assertIsNone(VALIDATOR.validate_report_branding(
                {"MELD7T_BRANDING_LOGO_PATH": ""},
                {"MELD7T_BRANDING_LOGO_PATH": "  "}, root))
            with self.assertRaisesRegex(ValueError, "worker report logo path to be absent"):
                VALIDATOR.validate_report_branding(
                    {}, {"MELD7T_BRANDING_LOGO_PATH": "/unexpected/logo.png"}, root)

    def test_configured_png_or_jpeg_must_map_to_installed_asset(self) -> None:
        for payload, dimensions in ((png(1200, 800), (1200, 800)),
                                    (jpeg(640, 480), (640, 480))):
            with self.subTest(dimensions=dimensions), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                logo = root / "branding" / "report-logo.png"
                logo.parent.mkdir()
                logo.write_bytes(payload)
                observed = VALIDATOR.validate_report_branding(
                    {"MELD7T_BRANDING_LOGO_PATH": "/run/branding/report-logo.png"},
                    {"MELD7T_BRANDING_LOGO_PATH": str(logo)}, root)
                self.assertEqual(observed, dimensions)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logo = root / "branding" / "report-logo.png"
            logo.parent.mkdir()
            logo.write_bytes(png(100, 100))
            with self.assertRaisesRegex(ValueError, "must use /run/branding"):
                VALIDATOR.validate_report_branding(
                    {"MELD7T_BRANDING_LOGO_PATH": "/run/other.png"}, {}, root)
            with self.assertRaisesRegex(ValueError, "map to the installed"):
                VALIDATOR.validate_report_branding(
                    {"MELD7T_BRANDING_LOGO_PATH": "/run/branding/report-logo.png"},
                    {"MELD7T_BRANDING_LOGO_PATH": str(root / "other.png")}, root)

    def test_missing_symlink_invalid_oversize_and_excessive_dimensions_fail(self) -> None:
        api = {"MELD7T_BRANDING_LOGO_PATH": "/run/branding/report-logo.png"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logo = root / "branding" / "report-logo.png"
            logo.parent.mkdir()
            worker = {"MELD7T_BRANDING_LOGO_PATH": str(logo)}
            with self.assertRaisesRegex(ValueError, "missing"):
                VALIDATOR.validate_report_branding(api, worker, root)

            target = root / "target.png"
            target.write_bytes(png(100, 100))
            logo.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "non-symlink regular file"):
                VALIDATOR.validate_report_branding(api, worker, root)
            logo.unlink()

            logo.write_bytes(b"not an image")
            with self.assertRaisesRegex(ValueError, "PNG or JPEG"):
                VALIDATOR.validate_report_branding(api, worker, root)

            with logo.open("wb") as handle:
                handle.truncate(VALIDATOR.MAX_REPORT_LOGO_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "no larger than 5 MiB"):
                VALIDATOR.validate_report_branding(api, worker, root)

            for width, height in ((8193, 1), (2001, 2000)):
                with self.subTest(width=width, height=height):
                    logo.write_bytes(png(width, height))
                    with self.assertRaisesRegex(ValueError, "dimensions exceed"):
                        VALIDATOR.validate_report_branding(api, worker, root)

    def test_env_parser_allows_only_the_optional_logo_path_to_be_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "api.env"
            os.chmod(path.parent, 0o700)
            path.write_text("MELD7T_BRANDING_LOGO_PATH=\n", encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(VALIDATOR.env_file(path), {
                "MELD7T_BRANDING_LOGO_PATH": "",
            })
            path.write_text("MELD7T_DB_URL=\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty or placeholder"):
                VALIDATOR.env_file(path)

    def test_branding_tree_rejects_links_special_files_and_writable_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "branding"
            root.mkdir(mode=0o750)
            asset = root / "browser.png"
            asset.write_bytes(png(10, 10))
            asset.chmod(0o640)
            self.assertEqual(VALIDATOR.validate_branding_tree(root, os.getuid()), 1)

            link = root / "escape.png"
            link.symlink_to(asset)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                VALIDATOR.validate_branding_tree(root, os.getuid())
            link.unlink()

            pipe = root / "asset.pipe"
            os.mkfifo(pipe)
            with self.assertRaisesRegex(ValueError, "regular files and directories"):
                VALIDATOR.validate_branding_tree(root, os.getuid())
            pipe.unlink()

            asset.chmod(0o666)
            with self.assertRaisesRegex(ValueError, "not group/world writable"):
                VALIDATOR.validate_branding_tree(root, os.getuid())

    def test_pinned_pillow_verifier_requires_a_fully_decodable_bounded_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logo = Path(temporary) / "report-logo.png"
            logo.write_bytes(png(1200, 800))
            self.assertEqual(RENDERER.verify_logo(logo), (1200, 800, "PNG", "RGB"))

            # A plausible signature/IHDR is insufficient; Pillow must decode the complete image.
            logo.write_bytes(png(100, 100)[:33])
            with self.assertRaisesRegex(ValueError, "fully decodable"):
                RENDERER.verify_logo(logo)

            logo.write_bytes(png(2001, 2000))
            with self.assertRaises(ValueError):
                RENDERER.verify_logo(logo)


if __name__ == "__main__":
    unittest.main()
