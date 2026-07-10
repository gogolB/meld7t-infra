"""Detector runner abstraction (spec §18, §25.1). Each detector is a versioned command template
with its own compute → ingest → package. The worker dispatches by detector_id; the prepare step
(DICOM → BIDS) is shared. MELD is one runner among many."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from app.models import RunStatus


async def run_cmd(cmd: list[str], log_path: str) -> int:
    """Run a podman sibling job, streaming combined output to log_path. Returns exit code."""
    with open(log_path, "ab") as log:
        log.write(("$ " + " ".join(cmd) + "\n").encode())
        log.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=log, stderr=asyncio.subprocess.STDOUT)
        return await proc.wait()


class DetectorRunner:
    detector_id: str = ""
    needs_t2: bool = False          # prepare also emits sub-<id>_T2w (HS detectors)

    async def compute(self, subject: str, workdir: str) -> tuple[int, Optional[RunStatus]]:
        """Run the detector container. Return (exit_code, special_fail_status or None)."""
        raise NotImplementedError

    async def ingest(self, subject: str) -> dict:
        """Parse outputs → {'result': {report_path, n_clusters, ...}, 'clusters': [...]}. """
        raise NotImplementedError

    async def package(self, subject: str, pseudonym: str, workdir: str) -> dict:
        """Package overlays → Orthanc; return {orthanc_study_uid, orthanc_t1_uid, orthanc_seg_uid}."""
        return {}
