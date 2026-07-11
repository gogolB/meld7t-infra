"""Experimental MAP-inspired runner — FCD candidate research via SPM voxel morphometry.

CPU-only, T1-only. compute runs SPM12 Standalone's unified segmentation (stock spmcentral/spm image
+ our segment.m); ingest computes the junction/extension feature maps + single-subject z-scores in
the pkg container (map_morphometry.py) and emits candidate clusters. No viewer overlay yet — the
feature maps live in MNI space; warping thresholded clusters back to the T1 frame for a DICOM-SEG
is a follow-up (findings already render in the MDT/concordance view). A versioned ``map_normative``
profile supplies the scanner/protocol-specific control mean and standard-deviation maps. An
explicitly approved development override may run without one; production fails closed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tempfile
from typing import Optional

from app.models import RunStatus

from ..config import wsettings
from ..harmonization import ResolvedHarmonization
from ..process import run_process
from .base import CompletionValidationError, DetectorRunner, run_cmd


class MapRunner(DetectorRunner):
    detector_id = "map"
    needs_t2 = False
    uses_gpu = False                # SPM segmentation is CPU — runs alongside a GPU job (§18)
    supports_harmonization = True
    allowed_harmonization_methods = frozenset({"map_normative"})

    async def compute(self, subject: str, workdir: str,
                      harmonization: ResolvedHarmonization | None = None
                      ) -> tuple[int, Optional[RunStatus]]:
        segment = os.path.join(wsettings.repo_dir, "containers", "map", "segment.m")
        if wsettings.map_script_sha256:
            digest = hashlib.sha256()
            try:
                with open(segment, "rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        digest.update(chunk)
            except OSError as exc:
                raise CompletionValidationError("signed MAP segment script is unavailable") from exc
            if digest.hexdigest() != wsettings.map_script_sha256:
                raise CompletionValidationError("MAP segment script differs from signed release")

        outdir = os.path.join(wsettings.meld_data, "output", "map", subject)
        if os.path.isdir(outdir):
            shutil.rmtree(outdir)
        os.makedirs(outdir, exist_ok=True)
        # SPM can't read .nii.gz — gunzip the prepared T1 into the SPM work dir as /work/T1.nii.
        src = os.path.join(wsettings.meld_data, "input", subject, "anat", f"{subject}_T1w.nii.gz")
        if not os.path.exists(src):
            return 1, RunStatus.failed
        fd, temp_t1 = tempfile.mkstemp(prefix=".T1.", suffix=".nii", dir=outdir)
        try:
            with gzip.open(src, "rb") as fi, os.fdopen(fd, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            os.replace(temp_t1, os.path.join(outdir, "T1.nii"))
        finally:
            if os.path.exists(temp_t1):
                os.unlink(temp_t1)

        cmd = [
            "podman", "run", "--rm", "--name", f"meld7t-map-{subject}", "--network=none",
            "--security-opt=no-new-privileges", "--cap-drop=all",
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

    async def ingest(self, subject: str, workdir: str,
                     harmonization: ResolvedHarmonization | None = None) -> dict:
        """Junction/extension morphometry + single-subject z-scoring in the pkg container."""
        cmd = [
            "podman", "run", "--rm", "--name", f"meld7t-map-ingest-{subject}",
            "--network=none",
            "--security-opt=no-new-privileges", "--cap-drop=all",
            "-v", f"{os.path.join(wsettings.meld_data, 'output', 'map')}:/map:rw,z",
        ]
        require_normative = bool(harmonization and harmonization.applied)
        if harmonization and harmonization.applied:
            cmd.extend(("-v", f"{harmonization.host_data_root}:/harmonization:ro,z"))
        cmd.extend((
            wsettings.pkg_image,
            "python3", "/opt/pkg/map_morphometry.py",
            "--root", "/map", "--subject", subject,
            "--data-root", ("/harmonization" if harmonization and harmonization.applied
                            else "/no-normative-profile"),
        ))
        if require_normative:
            cmd.extend(("--require-normative", "--harmo-code", harmonization.code))
        result = await run_process(cmd, os.path.join(workdir, "map-ingest.log"),
                                   capture_stdout=True)
        if result.returncode != 0:
            raise CompletionValidationError(f"MAP ingest failed with rc={result.returncode}")
        try:
            summary = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompletionValidationError("MAP ingest did not emit valid JSON") from exc
        if not isinstance(summary, dict) or summary.get("error"):
            raise CompletionValidationError(f"MAP ingest error: {summary!r}")
        if summary.get("subject") != subject or not isinstance(summary.get("clusters"), list):
            raise CompletionValidationError("MAP ingest output has wrong subject/schema")
        clusters = summary["clusters"]
        if summary.get("n_clusters") != len(clusters) or not summary.get("harmo_code"):
            raise CompletionValidationError("MAP ingest output has inconsistent result metadata")
        if require_normative and summary["harmo_code"] != harmonization.code:
            raise CompletionValidationError("MAP did not apply the requested harmonization profile")
        expected_artifacts = {
            f"{feature}_{kind}.nii.gz"
            for feature in ("junction", "extension")
            for kind in ("feature", "z", "threshold")
        }
        if set(summary.get("artifacts", [])) != expected_artifacts:
            raise CompletionValidationError("MAP did not retain the complete reviewable map set")
        relative_root = os.path.join("output", "map", subject)
        return {"result": {"report_path": None, "n_clusters": len(clusters),
                           "harmo_code": summary["harmo_code"],
                           "metric_schema": {
                               "size": {"label": "candidate volume", "unit": "mL"},
                               "confidence": {"label": "peak normative z-score", "unit": "z"},
                               "comparable_across_detectors": False,
                           }},
                "clusters": clusters,
                "artifacts": [os.path.join(relative_root, "wc1T1.nii"),
                              os.path.join(relative_root, "wc2T1.nii"),
                              *(os.path.join(relative_root, name)
                                for name in sorted(expected_artifacts))]}
