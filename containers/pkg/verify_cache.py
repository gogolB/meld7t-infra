#!/usr/bin/env python3
"""Verify an extracted HippUnfold cache against its signed per-file closure."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath


RESERVED = {".meld7t-cache-files.sha256", ".meld7t-signed-archive-sha256"}
HASH_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized(value: str) -> str:
    if not value or value.startswith("/") or any(char in value for char in "\0\r\n"):
        raise ValueError("cache manifest contains an unsafe path")
    path = PurePosixPath(value)
    normalized = str(path).removeprefix("./")
    if normalized in {"", "."} or ".." in path.parts or normalized in RESERVED:
        raise ValueError("cache manifest contains an unsafe/reserved path")
    return normalized


def _expected(manifest: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    previous = ""
    with manifest.open(encoding="utf-8", newline="") as source:
        for line_no, raw in enumerate(source, 1):
            match = HASH_LINE.fullmatch(raw.rstrip("\n"))
            if match is None:
                raise ValueError(f"cache manifest row {line_no} is malformed")
            digest, member = match.groups()
            member = _normalized(member)
            if member in result or (previous and member <= previous):
                raise ValueError("cache manifest paths must be unique and sorted")
            result[member] = digest
            previous = member
    if not result:
        raise ValueError("cache manifest is empty")
    return result


def verify_cache(root_value: str | Path, expected_manifest_sha256: str) -> dict[str, int | str]:
    if re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256) is None:
        raise ValueError("expected cache manifest digest is invalid")
    root = Path(root_value)
    if root.is_symlink():
        raise ValueError("cache root must not be a symlink")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("cache root is not a directory")
    manifest = root / ".meld7t-cache-files.sha256"
    marker = root / ".meld7t-signed-archive-sha256"
    for required in (manifest, marker):
        mode = os.lstat(required).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"cache control file is not regular: {required.name}")
    if _sha256(manifest) != expected_manifest_sha256:
        raise ValueError("installed cache manifest differs from the signed release")
    if marker.read_text(encoding="ascii").strip() != expected_manifest_sha256:
        raise ValueError("installed cache marker differs from the signed release")
    expected = _expected(manifest)
    actual: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        for name in dirs:
            mode = os.lstat(directory / name).st_mode
            if not stat.S_ISDIR(mode):
                raise ValueError(f"cache contains a symlink or special directory: {name}")
        for name in files:
            path = directory / name
            relative = path.relative_to(root).as_posix()
            mode = os.lstat(path).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"cache contains a symlink or special file: {relative}")
            if relative in RESERVED:
                continue
            actual[_normalized(relative)] = _sha256(path)
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(
            path for path in actual.keys() & expected.keys()
            if actual[path] != expected[path]
        )
        raise ValueError(
            f"cache closure mismatch: missing={missing[:5]} extra={extra[:5]} "
            f"changed={changed[:5]}"
        )
    return {
        "files": len(actual),
        "manifest_sha256": expected_manifest_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(verify_cache(args.root, args.expected_manifest_sha256), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"verify-cache: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
