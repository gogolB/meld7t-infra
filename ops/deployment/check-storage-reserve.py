#!/usr/bin/env python3
"""Fail closed unless production storage covers configured concurrent workloads."""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Mapping


GIB = 1024**3
MIB = 1024**2


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: malformed environment row")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in values:
            raise ValueError(f"{path}:{line_number}: duplicate or empty environment key")
        values[key] = value.strip()
    return values


def positive(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def calculate_reserves(api: Mapping[str, str], worker: Mapping[str, str],
                       builder: Mapping[str, str], orthanc: Mapping[str, str],
                       harmonization_orthanc: Mapping[str, str]) -> dict[str, int]:
    """Return conservative byte reserves for state and rootless Podman filesystems."""
    worker_floor = positive(worker, "MELD7T_STORAGE_MIN_FREE_BYTES", 50 * GIB)
    worker_jobs = positive(worker, "MELD7T_WORKER_MAX_JOBS", 2)
    compute_per_job = (
        positive(worker, "MELD7T_DICOM_MAX_BYTES_PER_RUN", 100 * GIB)
        + positive(worker, "MELD7T_STORAGE_OUTPUT_HEADROOM_BYTES", 25 * GIB)
    )
    case_quota = positive(api, "MELD7T_CASE_UPLOAD_QUOTA_BYTES", 500 * GIB)
    case_expanded = positive(
        worker, "MELD7T_CASE_UPLOAD_MAX_EXPANDED_BYTES", 100 * GIB)

    # Compressed routine uploads can fill the API quota while each occupied worker slot either
    # expands one archive or runs one detector. Research Orthanc then retains each imported source.
    routine_state = case_quota + worker_jobs * max(compute_per_job, case_expanded)

    builder_floor = positive(builder, "MELD7T_STORAGE_MIN_FREE_BYTES", 100 * GIB)
    harmonization_upload = positive(
        builder, "MELD7T_HARMONIZATION_MAX_UPLOAD_BYTES", 100 * GIB)
    harmonization_expanded = positive(
        builder, "MELD7T_HARMONIZATION_UPLOAD_MAX_EXPANDED_BYTES", 500 * GIB)
    harmonization_build = positive(
        builder, "MELD7T_HARMONIZATION_BUILD_MAX_BYTES", 1024 * GIB)
    harmonization_state = harmonization_upload + harmonization_expanded + harmonization_build
    research_orthanc = positive(
        orthanc, "ORTHANC__MAXIMUM_STORAGE_SIZE", 2 * 1024 * GIB // MIB) * MIB
    harmonization_orthanc_cap = positive(
        harmonization_orthanc, "ORTHANC__MAXIMUM_STORAGE_SIZE",
        2 * 1024 * GIB // MIB) * MIB

    return {
        "routine_state": routine_state,
        "research_orthanc": research_orthanc,
        "harmonization_state": harmonization_state,
        "harmonization_orthanc": harmonization_orthanc_cap,
        # One physical filesystem needs one watermark, not a copy per service.
        "headroom": max(worker_floor, builder_floor),
    }


def required_checks(state_path: Path, podman_path: Path, reserves: Mapping[str, int],
                    *, device_id=None) -> list[tuple[Path, int]]:
    device_id = device_id or (lambda path: os.stat(path).st_dev)
    state_workload = reserves["routine_state"] + reserves["harmonization_state"]
    podman_workload = reserves["research_orthanc"] + reserves["harmonization_orthanc"]
    headroom = reserves["headroom"]
    if device_id(state_path) == device_id(podman_path):
        return [(state_path, state_workload + podman_workload + headroom)]
    return [(state_path, state_workload + headroom),
            (podman_path, podman_workload + headroom)]


def verify_capacity(checks: list[tuple[Path, int]], *, disk_usage=None) -> None:
    disk_usage = disk_usage or shutil.disk_usage
    for path, required in checks:
        free = disk_usage(path).free
        if free < required:
            raise RuntimeError(
                f"{path} has {free} bytes free; {required} bytes required")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-env", type=Path, required=True)
    parser.add_argument("--worker-env", type=Path, required=True)
    parser.add_argument("--builder-env", type=Path, required=True)
    parser.add_argument("--orthanc-env", type=Path, required=True)
    parser.add_argument("--harmonization-orthanc-env", type=Path, required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--podman-path", type=Path, required=True)
    args = parser.parse_args()

    reserves = calculate_reserves(
        read_env(args.api_env), read_env(args.worker_env), read_env(args.builder_env),
        read_env(args.orthanc_env), read_env(args.harmonization_orthanc_env))
    checks = required_checks(args.state_path, args.podman_path, reserves)
    verify_capacity(checks)
    for path, required in checks:
        print(f"storage reserve accepted: {path}: {required} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
