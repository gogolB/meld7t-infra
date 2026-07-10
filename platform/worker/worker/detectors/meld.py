"""MELD-FCD runner (cortical-surface GNN). Wraps the validated MELD invocation + cluster ingest
+ DICOM-SEG packaging that were the worker's original hardcoded path."""
from __future__ import annotations

import os
from typing import Optional

from app.models import RunStatus

from .. import ingest, pipeline
from ..config import wsettings
from .base import DetectorRunner


class MeldRunner(DetectorRunner):
    detector_id = "meld_fcd"

    async def compute(self, subject: str, workdir: str) -> tuple[int, Optional[RunStatus]]:
        rc = await pipeline.run_meld(subject, workdir)
        if rc != 0:
            oom = pipeline.is_oom(os.path.join(workdir, "meld.log"))
            return rc, (RunStatus.failed_oom if oom else RunStatus.failed)
        return 0, None

    async def ingest(self, subject: str) -> dict:
        return {"result": ingest.result_fields(wsettings.meld_data, subject),
                "clusters": ingest.parse_clusters(wsettings.meld_data, subject)}

    async def package(self, subject: str, pseudonym: str, workdir: str) -> dict:
        _rc, uids = await pipeline.run_package(subject, pseudonym, workdir)
        return uids
