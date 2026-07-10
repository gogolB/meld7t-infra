"""MAP runner (spec §25.4) — FCD: SPM voxel morphometry (Huppertz MAP07 method).

CPU-only, T1-only. compute runs SPM12 Standalone's unified segmentation (stock spmcentral/spm image
+ our segment.m); ingest computes the junction/extension feature maps + single-subject z-scores in
the pkg container (map_morphometry.py) and emits candidate clusters. No viewer overlay yet — the
feature maps live in MNI space; warping thresholded clusters back to the T1 frame for a DICOM-SEG
is a follow-up (findings already render in the MDT/concordance view). Clinical z-scoring needs the
§25.2 normative control cohort, which is not staged; until then harmo_code="none" (single-subject,
hypothesis-generating — same footing as MELD without harmonisation).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
from typing import Optional

from app.models import RunStatus

from ..config import wsettings
from .base import DetectorRunner, run_cmd


class MapRunner(DetectorRunner):
    detector_id = "map"
    needs_t2 = False
    uses_gpu = False                # SPM segmentation is CPU — runs alongside a GPU job (§18)

    async def compute(self, subject: str, workdir: str) -> tuple[int, Optional[RunStatus]]:
        outdir = os.path.join(wsettings.meld_data, "output", "map", subject)
        os.makedirs(outdir, exist_ok=True)
        # SPM can't read .nii.gz — gunzip the prepared T1 into the SPM work dir as /work/T1.nii.
        src = os.path.join(wsettings.meld_data, "input", subject, "anat", f"{subject}_T1w.nii.gz")
        if not os.path.exists(src):
            return 1, RunStatus.failed
        with gzip.open(src, "rb") as fi, open(os.path.join(outdir, "T1.nii"), "wb") as fo:
            shutil.copyfileobj(fi, fo)

        segment = os.path.join(wsettings.repo_dir, "containers", "map", "segment.m")
        cmd = [
            "podman", "run", "--rm", "--network=none",
            "-v", f"{outdir}:/work:z",
            "-v", f"{segment}:/opt/map/segment.m:ro,z",
            wsettings.map_image,
            "script", "/opt/map/segment.m",
        ]
        rc = await run_cmd(cmd, os.path.join(workdir, "map.log"))
        # SPM's MCR exit code is unreliable; require the key MNI output to exist.
        if rc == 0 and not os.path.exists(os.path.join(outdir, "wc1T1.nii")):
            rc = 1
        return rc, (None if rc == 0 else RunStatus.failed)

    async def ingest(self, subject: str) -> dict:
        """Junction/extension morphometry + single-subject z-scoring in the pkg container."""
        cmd = [
            "podman", "run", "--rm", "--network=none",
            "-v", f"{wsettings.meld_data}:/data:ro,z",
            wsettings.pkg_image,
            "python3", "/opt/pkg/map_morphometry.py",
            "--root", "/data/output/map", "--subject", subject,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        try:
            summary = json.loads(out.decode() or "{}")
        except json.JSONDecodeError:
            summary = {}
        clusters = summary.get("clusters", [])
        return {"result": {"report_path": None, "n_clusters": len(clusters),
                           "harmo_code": summary.get("harmo_code")},
                "clusters": clusters}
