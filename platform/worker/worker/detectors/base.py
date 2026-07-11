"""Detector runner abstraction (spec §18, §25.1). Each detector is a versioned command template
with its own compute → ingest → package. The worker dispatches by detector_id; the prepare step
(DICOM → BIDS) is shared. MELD is one runner among many."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Optional

from app.models import RunStatus

from ..config import wsettings
from ..harmonization import ResolvedHarmonization
from ..process import run_process


class CompletionValidationError(RuntimeError):
    """A detector exited but did not produce its declared completion contract."""


@dataclass(frozen=True)
class DetectorCompletion:
    result: dict[str, Any]
    clusters: list[dict[str, Any]]
    uids: dict[str, str]
    artifacts: tuple[str, ...] = ()


async def run_cmd(cmd: list[str], log_path: str) -> int:
    """Run a sibling job with hard timeout and cancellation/container cleanup."""
    return (await run_process(cmd, log_path)).returncode


class DetectorRunner:
    detector_id: str = ""
    needs_t2: bool = False          # prepare also emits sub-<id>_T2w (HS detectors)
    uses_gpu: bool = True           # False → skip the GPU mutex, run alongside a GPU job (§18)
    supports_harmonization: bool = False
    allowed_harmonization_methods: frozenset[str] = frozenset()
    required_uid_keys: tuple[str, ...] = ()

    async def compute(self, subject: str, workdir: str,
                      harmonization: ResolvedHarmonization | None = None
                      ) -> tuple[int, Optional[RunStatus]]:
        """Run the detector container. Return (exit_code, special_fail_status or None)."""
        raise NotImplementedError

    async def ingest(self, subject: str, workdir: str,
                     harmonization: ResolvedHarmonization | None = None) -> dict:
        """Parse outputs → {'result': {report_path, n_clusters, ...}, 'clusters': [...]}. """
        raise NotImplementedError

    async def package(self, subject: str, pseudonym: str, workdir: str,
                      uid_seed: str, expected_clusters: int | None = None) -> dict:
        """Package overlays → Orthanc; return {orthanc_study_uid, orthanc_t1_uid, orthanc_seg_uid}."""
        return {}

    def validate_harmonization(self, profile: ResolvedHarmonization | None) -> None:
        if profile is None:
            if wsettings.deployment_mode in {"research", "production"}:
                raise CompletionValidationError(
                    f"{self.detector_id or 'detector'} lacks an explicit harmonization contract")
            return
        if not profile.applied:
            if profile.method not in {"unharmonized", "not_applicable"}:
                raise CompletionValidationError(
                    f"invalid non-applied harmonization mode {profile.method!r}")
            if profile.method == "not_applicable" and self.supports_harmonization:
                raise CompletionValidationError(
                    f"{self.detector_id} cannot mark supported harmonization as not applicable")
            return
        if (not self.supports_harmonization
                or profile.method not in self.allowed_harmonization_methods):
            raise CompletionValidationError(
                f"{self.detector_id} does not support harmonization method {profile.method!r}")

    def validate_ingest(self, ingested: dict) -> DetectorCompletion:
        """Validate detector files/JSON before any external DICOM publication."""
        if not isinstance(ingested, dict):
            raise CompletionValidationError("detector ingest did not return an object")
        result, clusters = ingested.get("result"), ingested.get("clusters")
        if not isinstance(result, dict) or not isinstance(clusters, list):
            raise CompletionValidationError("detector output requires result object and clusters list")
        n_clusters = result.get("n_clusters")
        if isinstance(n_clusters, bool) or not isinstance(n_clusters, int):
            raise CompletionValidationError("result.n_clusters must be an integer")
        if n_clusters != len(clusters):
            raise CompletionValidationError(
                f"result count {n_clusters} does not match {len(clusters)} clusters")
        allowed = {"index", "hemi", "location", "size", "confidence", "saliency"}
        indices: set[int] = set()
        clean: list[dict[str, Any]] = []
        for pos, cluster in enumerate(clusters):
            if not isinstance(cluster, dict) or set(cluster) - allowed:
                raise CompletionValidationError(f"cluster {pos} has an invalid schema")
            index = cluster.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 1 or index in indices:
                raise CompletionValidationError(f"cluster {pos} has invalid/duplicate index")
            indices.add(index)
            hemi = cluster.get("hemi")
            if hemi not in (None, "left", "right"):
                raise CompletionValidationError(f"cluster {index} has invalid hemisphere {hemi!r}")
            for field in ("size", "confidence"):
                value = cluster.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(float(value))):
                    raise CompletionValidationError(f"cluster {index} has invalid {field}")
            if cluster.get("saliency") is not None and not isinstance(cluster["saliency"], dict):
                raise CompletionValidationError(f"cluster {index} saliency must be an object")
            if cluster.get("saliency") is not None:
                try:
                    encoded = json.dumps(cluster["saliency"], allow_nan=False, sort_keys=True)
                except (TypeError, ValueError) as exc:
                    raise CompletionValidationError(
                        f"cluster {index} saliency is not finite JSON") from exc
                if len(encoded.encode("utf-8")) > 100_000:
                    raise CompletionValidationError(
                        f"cluster {index} saliency exceeds the size limit")
            clean.append(cluster)
        artifacts = ingested.get("artifacts", [])
        if (not isinstance(artifacts, list) or
                any(not isinstance(path, str) or not path for path in artifacts)):
            raise CompletionValidationError("detector artifacts must be a list of paths")
        return DetectorCompletion(dict(result), clean, {}, tuple(artifacts))

    def validate_completion(self, ingested: dict, uids: dict) -> DetectorCompletion:
        """Validate the typed detector and DICOM completion schema before any DB write."""
        outputs = self.validate_ingest(ingested)
        if not isinstance(uids, dict):
            raise CompletionValidationError("detector package UIDs must be an object")
        for key in self.required_uid_keys:
            value = uids.get(key)
            if not isinstance(value, str) or not value:
                raise CompletionValidationError(f"packaging did not return required {key}")
        return DetectorCompletion(outputs.result, outputs.clusters, dict(uids), outputs.artifacts)
