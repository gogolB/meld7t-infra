#!/usr/bin/env python3
"""Validate and describe the GitHub developer component release."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STABLE_SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SENSITIVE_SUFFIXES = {
    ".bval",
    ".bvec",
    ".db",
    ".dcm",
    ".dicom",
    ".h5",
    ".hdf5",
    ".hdr",
    ".ima",
    ".img",
    ".key",
    ".log",
    ".mgh",
    ".mgz",
    ".mnc",
    ".nhdr",
    ".nii",
    ".npy",
    ".npz",
    ".nrrd",
    ".p12",
    ".par",
    ".pem",
    ".pfx",
    ".rec",
    ".sqlite",
}
SENSITIVE_MULTI_SUFFIXES = {".dcm.gz", ".hdr.gz", ".img.gz", ".mnc.gz", ".nii.gz"}
SENSITIVE_DIRECTORIES = {"data", "meld-data", "provenance", "secrets"}
SENSITIVE_BASENAMES = {".env", "id_ed25519", "id_rsa"}


def validate_stable_version(value: str) -> str:
    if STABLE_SEMVER.fullmatch(value) is None:
        raise ValueError(
            f"version must be stable SemVer without a leading v or prerelease suffix: {value!r}"
        )
    return value


def _project_version(path: Path) -> str:
    with path.open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def _locked_project_version(path: Path, project_name: str) -> str:
    with path.open("rb") as stream:
        packages = tomllib.load(stream)["package"]
    matches = [
        package for package in packages
        if package.get("name") == project_name and package.get("source", {}).get("editable") == "."
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one editable {project_name!r} entry in {path}")
    return str(matches[0]["version"])


def component_versions(root: Path = REPOSITORY_ROOT) -> dict[str, str]:
    web_package = json.loads((root / "platform/web/package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((root / "platform/web/package-lock.json").read_text(encoding="utf-8"))
    return {
        "api": _project_version(root / "platform/api/pyproject.toml"),
        "api-lock": _locked_project_version(root / "platform/api/uv.lock", "meld7t-api"),
        "worker": _project_version(root / "platform/worker/pyproject.toml"),
        "worker-lock": _locked_project_version(
            root / "platform/worker/uv.lock", "meld7t-worker"
        ),
        "web": str(web_package["version"]),
        "web-lock": str(web_lock["packages"][""]["version"]),
        "web-lock-root": str(web_lock["version"]),
    }


def validate_component_versions(root: Path, expected: str) -> dict[str, str]:
    expected = validate_stable_version(expected)
    versions = component_versions(root)
    mismatches = {name: version for name, version in versions.items() if version != expected}
    if mismatches:
        detail = ", ".join(f"{name}={version}" for name, version in sorted(mismatches.items()))
        raise ValueError(f"component versions do not match {expected}: {detail}")
    return versions


def repository_version(root: Path = REPOSITORY_ROOT) -> str:
    """Return the single stable version shared by every releasable component."""
    versions = component_versions(root)
    distinct = set(versions.values())
    if len(distinct) != 1:
        detail = ", ".join(f"{name}={version}" for name, version in sorted(versions.items()))
        raise ValueError(f"component versions do not agree: {detail}")
    return validate_stable_version(distinct.pop())


def forbidden_tracked_paths(paths: list[str]) -> list[str]:
    forbidden: list[str] = []
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        lowered_parts = tuple(part.lower() for part in path.parts)
        basename = path.name.lower()
        suffix = path.suffix.lower()
        lowered_path = raw_path.lower()
        if (
            any(part in SENSITIVE_DIRECTORIES for part in lowered_parts)
            or basename in SENSITIVE_BASENAMES
            or suffix in SENSITIVE_SUFFIXES
            or any(lowered_path.endswith(item) for item in SENSITIVE_MULTI_SUFFIXES)
        ):
            forbidden.append(raw_path)
    return sorted(set(forbidden))


def validate_tracked_paths(root: Path) -> None:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, stdout=subprocess.PIPE
    )
    paths = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    forbidden = forbidden_tracked_paths(paths)
    if forbidden:
        rendered = "\n  - ".join(forbidden[:20])
        extra = f"\n  ... and {len(forbidden) - 20} more" if len(forbidden) > 20 else ""
        raise ValueError(f"tracked release source contains sensitive paths:\n  - {rendered}{extra}")


def _command_version(*command: str) -> str:
    return subprocess.check_output(command, text=True).strip().splitlines()[0]


def release_metadata(
    root: Path,
    version: str,
    git_sha: str,
    source_date_epoch: int,
) -> dict[str, object]:
    versions = validate_component_versions(root, version)
    if re.fullmatch(r"[0-9a-f]{40}", git_sha) is None:
        raise ValueError("git SHA must be a full lowercase 40-character SHA-1")
    timestamp = dt.datetime.fromtimestamp(source_date_epoch, tz=dt.timezone.utc)
    return {
        "schema": "meld7t.github-developer-release.v1",
        "release_class": "research-developer-component-packages",
        "version": version,
        "tag": f"v{version}",
        "git_sha": git_sha,
        "source_date_epoch": source_date_epoch,
        "source_timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
        "components": {
            "api": versions["api"],
            "worker": versions["worker"],
            "web": versions["web"],
        },
        "contents": [
            "exact committed source archive",
            "API wheel and source distribution",
            "worker wheel and source distribution",
            "static web distribution archive",
            "checksums and build metadata",
        ],
        "excluded": [
            "OCI images and model/cache assets",
            "air-gap deployment bundle and private release signature",
            "DICOM, application data, secrets, and harmonization profiles",
        ],
        "deployment": {
            "airgap_bundle": False,
            "production_installer": False,
            "scientific_or_clinical_approval": False,
        },
        "repository_license": None,
        "repository_license_note": (
            "No repository-wide LICENSE file was present at this source revision; publication "
            "does not add an implied license grant."
        ),
        "toolchain": {
            "python": f"CPython {platform.python_version()}",
            "uv": _command_version("uv", "--version"),
            "node": _command_version("node", "--version"),
            "npm": _command_version("npm", "--version"),
            "archive": _command_version("tar", "--version"),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="validate versions and tracked release paths")
    check.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    check.add_argument("--version", required=True)
    check.add_argument("--tracked", action="store_true")

    current_version = subparsers.add_parser(
        "current-version", help="print the stable version shared by all components"
    )
    current_version.add_argument("--root", type=Path, default=REPOSITORY_ROOT)

    metadata = subparsers.add_parser("metadata", help="write release metadata JSON")
    metadata.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    metadata.add_argument("--version", required=True)
    metadata.add_argument("--git-sha", required=True)
    metadata.add_argument("--source-date-epoch", required=True, type=int)
    metadata.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            versions = validate_component_versions(args.root.resolve(), args.version)
            if args.tracked:
                validate_tracked_paths(args.root.resolve())
            print(json.dumps(versions, sort_keys=True))
        elif args.command == "current-version":
            print(repository_version(args.root.resolve()))
        else:
            metadata = release_metadata(
                args.root.resolve(), args.version, args.git_sha, args.source_date_epoch
            )
            args.output.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    except (KeyError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
