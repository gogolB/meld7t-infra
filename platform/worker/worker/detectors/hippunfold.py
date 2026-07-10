"""HippUnfold runner (spec §25.5) — HS: unfold + nnU-Net hippocampal subfield segmentation.

BIDS App on the T2 SPACE (0.58mm — ideal hippocampal contrast) with T1 present. Ingest computes
per-subfield volumes + L/R asymmetry via the pkg container (which has nibabel); the asymmetry is
surfaced as a first-class finding. No normative DB needed — asymmetry is intrinsic.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Optional

from app.models import RunStatus

from ..config import wsettings
from .base import DetectorRunner, run_cmd


class HippUnfoldRunner(DetectorRunner):
    detector_id = "hippunfold"
    needs_t2 = True                 # segment on the high-res T2 SPACE
    uses_gpu = False                # CPU-only nnU-Net (bundled torch lacks sm_86) — no GPU mutex

    async def compute(self, subject: str, workdir: str) -> tuple[int, Optional[RunStatus]]:
        label = subject.replace("sub-", "")
        # Clean single-subject BIDS dir (avoid pybids indexing the shared input folder).
        bids = os.path.join(workdir, "bids")
        anat = os.path.join(bids, subject, "anat")
        os.makedirs(anat, exist_ok=True)
        src = os.path.join(wsettings.meld_data, "input", subject, "anat")
        for mod in ("T1w", "T2w"):
            s = os.path.join(src, f"{subject}_{mod}.nii.gz")
            if os.path.exists(s):
                shutil.copyfile(s, os.path.join(anat, f"{subject}_{mod}.nii.gz"))
        with open(os.path.join(bids, "dataset_description.json"), "w") as fh:
            fh.write('{"Name": "meld7t", "BIDSVersion": "1.8.0"}')

        outdir = os.path.join(wsettings.meld_data, "output", "hippunfold")
        os.makedirs(outdir, exist_ok=True)
        # No --device: HippUnfold's bundled torch (py3.9) lacks sm_86 kernels for Ampere GPUs
        # ("no kernel image available"), so nnU-Net runs on CPU (the hippocampal crop is small).
        # The cache must hold the model tar rewritten to owner 0 (rootless chown fix) — see README.
        cmd = [
            "podman", "run", "--rm",
            "-v", f"{bids}:/bids:ro",
            "-v", f"{outdir}:/out",
            "-v", f"{wsettings.hippunfold_cache}:/root/.cache/hippunfold",   # models/templates (§11)
            wsettings.hippunfold_image,
            "/bids", "/out", "participant",
            "--participant-label", label,
            "--modality", "T2w",
            "--cores", "8",
        ]
        rc = await run_cmd(cmd, os.path.join(workdir, "hippunfold.log"))
        return rc, (None if rc == 0 else RunStatus.failed)

    async def ingest(self, subject: str) -> dict:
        """Summarize subfield volumes + asymmetry in the pkg container (has nibabel)."""
        cmd = [
            "podman", "run", "--rm", "--network=none",
            "-v", f"{wsettings.meld_data}:/data:ro,z",
            wsettings.pkg_image,
            "python3", "/opt/pkg/hippunfold_summarize.py",
            "--root", f"/data/output/hippunfold", "--subject", subject,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        try:
            summary = json.loads(out.decode() or "{}")
        except json.JSONDecodeError:
            summary = {}
        clusters = summary.get("clusters", [])
        return {"result": {"report_path": None, "n_clusters": len(clusters)},
                "clusters": clusters}
