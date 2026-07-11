#!/usr/bin/env python3
"""Create or verify the sorted per-file closure manifest for a HippUnfold cache tar stream."""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tarfile
from pathlib import PurePosixPath


RESERVED = {
    ".meld7t-cache-files.sha256",
    ".meld7t-signed-archive-sha256",
}
HASH_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")


def normalized(name: str) -> str:
    if not name or name.startswith("/") or "\x00" in name or "\n" in name or "\r" in name:
        raise ValueError(f"unsafe cache member name: {name!r}")
    path = PurePosixPath(name)
    if ".." in path.parts:
        raise ValueError(f"unsafe cache member name: {name!r}")
    result = str(path)
    if result in {"", "."}:
        return "."
    return result.removeprefix("./")


def archive_hashes(archive: str) -> dict[str, str]:
    if archive == "-":
        handle = tarfile.open(fileobj=sys.stdin.buffer, mode="r|*")
    else:
        handle = tarfile.open(archive, mode="r:*")
    result: dict[str, str] = {}
    seen: set[str] = set()
    with handle:
        for member in handle:
            path = normalized(member.name)
            if path == "." or member.isdir():
                continue
            if path in seen:
                raise ValueError(f"duplicate cache member: {path}")
            seen.add(path)
            if not member.isfile():
                raise ValueError(f"cache contains non-regular member: {path}")
            if path in RESERVED:
                continue
            source = handle.extractfile(member)
            if source is None:
                raise ValueError(f"unable to read cache member: {path}")
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
            result[path] = digest.hexdigest()
    if not result:
        raise ValueError("HippUnfold cache contains no model files")
    return result


def read_manifest(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    previous = ""
    with open(path, encoding="utf-8", newline="") as source:
        for line_no, raw in enumerate(source, 1):
            line = raw.rstrip("\n")
            match = HASH_LINE.fullmatch(line)
            if match is None:
                raise ValueError(f"{path}:{line_no}: malformed manifest row")
            digest, member = match.groups()
            member = normalized(member)
            if member in RESERVED or member in result or (previous and member <= previous):
                raise ValueError(f"{path}:{line_no}: duplicate/reserved/unsorted member")
            result[member] = digest
            previous = member
    if not result:
        raise ValueError("cache file manifest is empty")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("archive")
    create.add_argument("output")
    verify = sub.add_parser("verify")
    verify.add_argument("archive")
    verify.add_argument("manifest")
    args = parser.parse_args()
    actual = archive_hashes(args.archive)
    if args.command == "create":
        with open(args.output, "w", encoding="utf-8", newline="\n") as output:
            for member in sorted(actual):
                output.write(f"{actual[member]}  {member}\n")
        return 0
    expected = read_manifest(args.manifest)
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(k for k in actual.keys() & expected.keys() if actual[k] != expected[k])
        raise ValueError(
            f"cache closure mismatch: missing={missing[:5]} extra={extra[:5]} changed={changed[:5]}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, tarfile.TarError, ValueError) as exc:
        print(f"cache-manifest: {exc}", file=sys.stderr)
        raise SystemExit(1)
