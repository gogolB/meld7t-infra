"""Per-run compute steps — launched as host podman sibling jobs (spec §2.2, §2.3, §6).

recon_prepare (pkg, --network=none) → MELD (meld_graph, --device gpu). The commands are the exact
validated invocations from the justfile; the worker builds them from the run spec (§18: declarative
job, no ad-hoc fragments). Logs land in a per-run workdir, retained on failure (§18, §28).
"""
from __future__ import annotations

import asyncio
import os

from .config import wsettings

_ROLE_TO_SOURCE = {"t1_uni": "uni", "t1_mprage": "mprage"}


def subject_id(run_id: str) -> str:
    return f"sub-r{run_id.replace('-', '')[:10]}"


async def _run(cmd: list[str], log_path: str) -> int:
    """Run a command, streaming combined output to log_path. Returns exit code."""
    with open(log_path, "ab") as log:
        log.write(("$ " + " ".join(cmd) + "\n").encode())
        log.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=log, stderr=asyncio.subprocess.STDOUT)
        return await proc.wait()


async def run_prepare(run_id: str, source_role: str | None, dicom_root: str,
                      workdir: str, also_t2: bool = False) -> tuple[int, str]:
    """recon_prepare in the pkg container → BIDS T1w (+ T2w if also_t2) under meld_data/input."""
    subject = subject_id(run_id)
    source = _ROLE_TO_SOURCE.get(source_role or "", "mprage")
    os.makedirs(os.path.join(wsettings.meld_data, "input"), exist_ok=True)
    cmd = [
        "podman", "run", "--rm", "--network=none",
        "-v", f"{dicom_root}:/dicom:ro,z",
        "-v", f"{wsettings.meld_data}/input:/out:z",
        wsettings.pkg_image,
        "python3", "/opt/pkg/recon_prepare.py",
        "--dicom-root", "/dicom", "--subject", subject, "--source", source, "--out", "/out",
    ]
    if also_t2:
        cmd.append("--also-t2")
    rc = await _run(cmd, os.path.join(workdir, "prepare.log"))
    return rc, subject


async def run_meld(subject: str, workdir: str) -> int:
    """The validated MELD FCD invocation (GPU via CDI, --fastsurfer)."""
    cmd = [
        "podman", "run", "--rm", "--device", "nvidia.com/gpu=all",
        "-v", f"{wsettings.meld_data}:/data:z",
        "-v", f"{wsettings.fs_license}:/run/secrets/license.txt:ro,z",
        "-v", f"{wsettings.meld_license}:/run/secrets/meld_license.txt:ro,z",
        "-e", "FS_LICENSE=/run/secrets/license.txt",
        "-e", "MELD_LICENSE=/run/secrets/meld_license.txt",
        wsettings.meld_image,
        "python", "scripts/new_patient_pipeline/new_pt_pipeline.py",
        "-id", subject, "--fastsurfer",
    ]
    return await _run(cmd, os.path.join(workdir, "meld.log"))


async def run_package(subject: str, pseudonym: str, workdir: str) -> tuple[int, dict]:
    """Package MELD outputs → T1 DICOM series + DICOM-SEG, STOW to Orthanc (§17). Runs on
    meld-net (needs Orthanc); parses the printed UIDs from stdout."""
    cmd = [
        "podman", "run", "--rm", "--network", "meld-net",
        "-v", f"{wsettings.meld_data}:/data:ro,z",
        wsettings.pkg_image,
        "python3", "/opt/pkg/package_dicom.py",
        "--t1", f"/data/input/{subject}/anat/{subject}_T1w.nii.gz",
        "--pred", f"/data/output/predictions_reports/{subject}/predictions/prediction.nii.gz",
        "--pseudonym", pseudonym or subject,
        "--stow", wsettings.orthanc_innet,
    ]
    log_path = os.path.join(workdir, "package.log")
    with open(log_path, "ab") as log:
        log.write(("$ " + " ".join(cmd) + "\n").encode())
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=log)
        out, _ = await proc.communicate()
    uids = {}
    for line in out.decode(errors="ignore").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            uids[k.strip()] = v.strip()
    return proc.returncode, uids


def is_oom(log_path: str) -> bool:
    """Classify a MELD failure as OOM vs terminal (§6, §18)."""
    try:
        with open(log_path, "rb") as fh:
            tail = fh.read()[-8000:].decode(errors="ignore").lower()
        return "cuda out of memory" in tail or "out of memory" in tail
    except OSError:
        return False
