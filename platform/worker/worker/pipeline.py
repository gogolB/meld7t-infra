"""Per-run compute steps — launched as host podman sibling jobs (spec §2.2, §2.3, §6).

recon_prepare (pkg, --network=none) → MELD (meld_graph, --device gpu). The commands are the exact
validated invocations from the justfile; the worker builds them from the run spec (§18: declarative
job, no ad-hoc fragments). Logs land in a per-run workdir, retained on failure (§18, §28).
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import wsettings
from .harmonization import ResolvedHarmonization
from .process import run_process

_ROLE_TO_SOURCE = {"t1_uni": "uni", "t1_mprage": "mprage"}


def subject_id(run_id: str, claim_token: str | None = None) -> str:
    compact = run_id.replace("-", "")
    if len(compact) != 32 or not all(c in "0123456789abcdefABCDEF" for c in compact):
        raise ValueError("run_id must be a full UUID")
    suffix = ""
    if claim_token is not None:
        claim = claim_token.replace("-", "")
        if len(claim) != 32 or not all(c in "0123456789abcdefABCDEF" for c in claim):
            raise ValueError("claim_token must be a full UUID")
        # An expired attempt and its retry must never share writable scientific output paths.
        suffix = f"a{claim[:12].lower()}"
    return f"sub-r{compact.lower()}{suffix}"


async def _run(cmd: list[str], log_path: str) -> int:
    """Run a command with timeout and cancellation-safe process/container cleanup."""
    return (await run_process(cmd, log_path)).returncode


async def run_prepare(run_id: str, source_role: str | None, series_by_role: dict[str, str],
                      dicom_root: str, workdir: str, also_t2: bool = False,
                      claim_token: str | None = None) -> tuple[int, str]:
    """recon_prepare in the pkg container → BIDS T1w (+ T2w if also_t2) under meld_data/input."""
    subject = subject_id(run_id, claim_token)
    if source_role not in _ROLE_TO_SOURCE:
        raise ValueError(f"unsupported or missing source role: {source_role!r}")
    source = _ROLE_TO_SOURCE[source_role]
    required = [source_role]
    if source_role == "t1_uni":
        required.extend(("t1_inv1", "t1_inv2"))
    if also_t2:
        required.append("t2")
    missing = [role for role in required if not series_by_role.get(role)]
    if missing:
        raise ValueError(f"exact companion SeriesInstanceUIDs are missing for roles: {missing}")
    os.makedirs(os.path.join(wsettings.meld_data, "input"), exist_ok=True)
    cmd = [
        "podman", "run", "--rm", "--name", f"meld7t-prepare-{subject}", "--network=none",
        "--security-opt=no-new-privileges", "--cap-drop=all",
        "-v", f"{dicom_root}:/dicom:ro,z",
        "-v", f"{wsettings.meld_data}/input:/out:z",
        wsettings.pkg_image,
        "python3", "/opt/pkg/recon_prepare.py",
        "--dicom-root", "/dicom", "--subject", subject, "--source", source, "--out", "/out",
    ]
    role_to_cli = {
        "t1_uni": "uni", "t1_inv1": "inv1", "t1_inv2": "inv2",
        "t1_mprage": "mprage", "t2": "t2",
    }
    for role in required:
        cmd.extend((f"--{role_to_cli[role]}-series-uid", series_by_role[role]))
    if also_t2:
        cmd.append("--also-t2")
    rc = await _run(cmd, os.path.join(workdir, "prepare.log"))
    return rc, subject


async def run_meld(subject: str, workdir: str,
                   harmonization: ResolvedHarmonization | None = None) -> int:
    """The validated MELD FCD invocation (GPU via CDI, --fastsurfer)."""
    cmd = [
        "podman", "run", "--rm", "--name", f"meld7t-meld-{subject}",
        "--network=none", "--security-opt=no-new-privileges", "--cap-drop=all",
        "--device", "nvidia.com/gpu=all",
        "-v", f"{wsettings.meld_data}:/data:z",
        "-v", f"{wsettings.fs_license}:/run/secrets/license.txt:ro,z",
        "-v", f"{wsettings.meld_license}:/run/secrets/meld_license.txt:ro,z",
        "-e", "FS_LICENSE=/run/secrets/license.txt",
        "-e", "MELD_LICENSE=/run/secrets/meld_license.txt",
    ]
    if harmonization is not None and harmonization.applied:
        # The signed profile source stays read-only.  A nested bind overlays MELD's expected
        # distributed-ComBat parameter directory inside the writable /data volume.
        cmd.extend(("-v", f"{harmonization.host_data_root}:"
                          "/data/meld_params/distributed_combat:ro,z"))
    cmd.extend((
        wsettings.meld_image,
        "python", "scripts/new_patient_pipeline/new_pt_pipeline.py",
        "-id", subject, "--fastsurfer",
    ))
    if harmonization is not None and harmonization.applied:
        cmd.extend(("-harmo_code", harmonization.code))
    return await _run(cmd, os.path.join(workdir, "meld.log"))


_PACKAGE_SCALAR_KEYS = {
    "study_uid", "t1_series_uid", "seg_series_uid", "probmap_series_uid", "n_t1_slices",
    "dicom_sop_count", "dicom_manifest_sha256", "derived_series_manifest_sha256",
}
_PACKAGE_JSON_KEYS = {"derived_series_manifest_json", "probmap_series_uids_json"}


def _package_uids(stdout: bytes) -> dict:
    uids = {}
    for line in stdout.decode(errors="strict").splitlines():
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in _PACKAGE_SCALAR_KEYS:
            uids[key] = value
        elif key in _PACKAGE_JSON_KEYS:
            import json
            uids[key.removesuffix("_json")] = json.loads(value)
    return uids


def _package_manifest_relative(workdir: str) -> str:
    return Path(workdir, "dicom-manifest.json").resolve().relative_to(
        Path(wsettings.meld_data).resolve()).as_posix()


def _harmonization_cli(profile: ResolvedHarmonization | None) -> list[str]:
    if profile is not None and profile.applied:
        return [
            "--harmonization-status", "applied",
            "--harmonization-code", profile.code,
            "--harmonization-version", str(profile.version),
            "--harmonization-method", profile.method,
        ]
    status = (profile.method if profile is not None else "unharmonized")
    if status not in {"unharmonized", "not_applicable"}:
        raise ValueError(f"invalid non-applied harmonization status {status!r}")
    return ["--harmonization-status", status]


async def run_package(subject: str, pseudonym: str, workdir: str,
                      uid_seed: str, study_uid_seed: str,
                      expected_clusters: int,
                      harmonization: ResolvedHarmonization | None = None) -> tuple[int, dict]:
    """Package MELD outputs → T1 DICOM series + DICOM-SEG, STOW to Orthanc (§17). Runs on
    meld-compute-net (needs Orthanc); parses the printed UIDs from stdout."""
    cmd = [
        "podman", "run", "--rm", "--name", f"meld7t-package-{subject}",
        "--network", wsettings.podman_data_network,
        "--security-opt=no-new-privileges", "--cap-drop=all",
        "-e", "MELD7T_ORTHANC_INNET",
        "-v", f"{wsettings.meld_data}:/data:ro,z",
        "-v", f"{workdir}:/work:rw,z",
        wsettings.pkg_image,
        "python3", "/opt/pkg/package_dicom.py",
        "--t1", f"/data/input/{subject}/anat/{subject}_T1w.nii.gz",
        "--pred", f"/data/output/predictions_reports/{subject}/predictions/prediction.nii.gz",
        "--pseudonym", pseudonym or subject,
        # Retry attempts use isolated output subjects but retain identical DICOM UIDs under the
        # same immutable run/release contract.
        "--uid-seed", uid_seed,
        "--study-uid-seed", study_uid_seed,
        "--software-version", (wsettings.release_manifest_digest or "development")[:16],
        "--manifest-output", "/work/dicom-manifest.json",
        "--expected-clusters", str(expected_clusters),
        *_harmonization_cli(harmonization),
    ]
    log_path = os.path.join(workdir, "package.log")
    display_cmd = list(cmd)
    display_cmd[display_cmd.index("--pseudonym") + 1] = "<redacted>"
    result = await run_process(
        cmd, log_path, capture_stdout=True, display_cmd=display_cmd,
        env={"MELD7T_ORTHANC_INNET": wsettings.orthanc_innet},
    )
    uids = _package_uids(result.stdout)
    if result.returncode == 0:
        uids["dicom_manifest_path"] = _package_manifest_relative(workdir)
    return result.returncode, uids


async def _run_derived_package(subject: str, workdir: str, detector: str,
                               arguments: list[str]) -> tuple[int, dict]:
    cmd = [
        "podman", "run", "--rm", "--name", f"meld7t-package-{detector}-{subject}",
        "--network", wsettings.podman_data_network,
        "--security-opt=no-new-privileges", "--cap-drop=all",
        "-e", "MELD7T_ORTHANC_INNET",
        "-v", f"{wsettings.meld_data}:/data:ro,z",
        "-v", f"{workdir}:/work:rw,z",
        wsettings.pkg_image,
        "python3", "/opt/pkg/package_derived_dicom.py", detector,
        *arguments,
        "--software-version", (wsettings.release_manifest_digest or "development")[:16],
        "--manifest-output", "/work/dicom-manifest.json",
    ]
    display_cmd = list(cmd)
    display_cmd[display_cmd.index("--pseudonym") + 1] = "<redacted>"
    result = await run_process(
        cmd, os.path.join(workdir, f"package-{detector}.log"), capture_stdout=True,
        display_cmd=display_cmd,
        env={"MELD7T_ORTHANC_INNET": wsettings.orthanc_innet},
    )
    uids = _package_uids(result.stdout)
    if result.returncode == 0:
        uids["dicom_manifest_path"] = _package_manifest_relative(workdir)
    return result.returncode, uids


async def run_map_package(subject: str, pseudonym: str, workdir: str, uid_seed: str,
                          study_uid_seed: str, expected_clusters: int,
                          harmonization: ResolvedHarmonization | None = None) -> tuple[int, dict]:
    root = f"/data/output/map/{subject}"
    return await _run_derived_package(subject, workdir, "map", [
        "--t1", f"/data/input/{subject}/anat/{subject}_T1w.nii.gz",
        "--inverse-deformation", f"{root}/iy_T1.nii",
        "--junction-threshold", f"{root}/junction_threshold.nii.gz",
        "--junction-z", f"{root}/junction_z.nii.gz",
        "--extension-threshold", f"{root}/extension_threshold.nii.gz",
        "--extension-z", f"{root}/extension_z.nii.gz",
        "--pseudonym", pseudonym or subject,
        "--uid-seed", uid_seed,
        "--study-uid-seed", study_uid_seed,
        "--expected-clusters", str(expected_clusters),
        *_harmonization_cli(harmonization),
    ])


async def run_hippunfold_package(subject: str, pseudonym: str, workdir: str, uid_seed: str,
                                 study_uid_seed: str, expected_clusters: int,
                                 left_dseg: str, right_dseg: str,
                                 flagged_side: str,
                                 harmonization: ResolvedHarmonization | None = None
                                 ) -> tuple[int, dict]:
    return await _run_derived_package(subject, workdir, "hippunfold", [
        "--t2", f"/data/input/{subject}/anat/{subject}_T2w.nii.gz",
        "--left-dseg", f"/data/{left_dseg}",
        "--right-dseg", f"/data/{right_dseg}",
        "--flagged-side", flagged_side,
        "--pseudonym", pseudonym or subject,
        "--uid-seed", uid_seed,
        "--study-uid-seed", study_uid_seed,
        "--expected-clusters", str(expected_clusters),
        *_harmonization_cli(harmonization),
    ])


def is_oom(log_path: str) -> bool:
    """Classify a MELD failure as OOM vs terminal (§6, §18)."""
    try:
        with open(log_path, "rb") as fh:
            tail = fh.read()[-8000:].decode(errors="ignore").lower()
        return "cuda out of memory" in tail or "out of memory" in tail
    except OSError:
        return False
