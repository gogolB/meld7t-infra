#!/usr/bin/env python3
"""Verify an installed report logo using the exact Pillow shipped to the worker."""
from __future__ import annotations

import argparse
import stat
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError


MAX_BYTES = 5 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_PIXELS = 4_000_000
ALLOWED_FORMATS = {"JPEG", "PNG"}
ALLOWED_MODES = {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK"}


def verify_logo(path: Path) -> tuple[int, int, str, str]:
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or path.is_symlink()
            or metadata.st_size < 1 or metadata.st_size > MAX_BYTES):
        raise ValueError("logo must be a bounded non-symlink regular file")
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                image_format = str(source.format or "")
                width, height = source.size
                mode = source.mode
                frames = int(getattr(source, "n_frames", 1))
                if (image_format not in ALLOWED_FORMATS or mode not in ALLOWED_MODES
                        or frames != 1 or width < 1 or height < 1
                        or width > MAX_DIMENSION or height > MAX_DIMENSION
                        or width * height > MAX_PIXELS):
                    raise ValueError("logo format, mode, frames, or dimensions are unsupported")
                source.verify()
            # verify() intentionally invalidates the decoder; reopen and force complete decoding.
            with Image.open(path) as decoded:
                decoded.load()
                if (decoded.size != (width, height) or decoded.mode != mode
                        or str(decoded.format or "") != image_format):
                    raise ValueError("logo identity changed between verification and decoding")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning,
            UnidentifiedImageError, OSError) as exc:
        raise ValueError("logo is not a safe, fully decodable PNG/JPEG") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    return width, height, image_format, mode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    width, height, image_format, mode = verify_logo(args.path)
    print(f"report logo accepted: {image_format} {mode} {width}x{height}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        raise SystemExit(f"verify-report-logo: {exc}") from None
