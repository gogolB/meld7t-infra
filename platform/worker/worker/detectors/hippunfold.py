"""HippUnfold runner (spec §25.5) — HS: unfold + nnU-Net hippocampal subfield segmentation.

BIDS App on the T2 SPACE (0.58mm — ideal hippocampal contrast) with T1 present. Ingest computes
per-subfield volumes + L/R asymmetry via the pkg container (which has nibabel); the asymmetry is
surfaced as a first-class finding. No normative DB needed — asymmetry is intrinsic.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from app.models import RunStatus

from ..config import wsettings
from .base import DetectorRunner, run_cmd


class HippUnfoldRunner(DetectorRunner):
    detector_id = "hippunfold"
    needs_t2 = True                 # segment on the high-res T2 SPACE

    async def compute(self, subject: str, workdir: str) -> tuple[int, Optional[RunStatus]]:
        label = subject.replace("sub-", "")
        outdir = os.path.join(wsettings.meld_data, "output", "hippunfold")
        os.makedirs(outdir, exist_ok=True)
        cmd = [
            "podman", "run", "--rm", "--device", "nvidia.com/gpu=all",
            "-v", f"{wsettings.meld_data}/input:/bids:ro",
            "-v", f"{outdir}:/out",
            wsettings.hippunfold_image,
            "/bids", "/out", "participant",
            "--participant_label", label,
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
