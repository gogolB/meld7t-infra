"""Detector runner abstraction (spec §18, §25.1). Each detector is a versioned command template
with its own compute → ingest → package. The worker dispatches by detector_id; the prepare step
(DICOM → BIDS) is shared. MELD is one runner among many."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
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
    uids: dict[str, Any]
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
                      uid_seed: str, study_uid_seed: str,
                      expected_clusters: int | None = None,
                      validated_ingest: DetectorCompletion | None = None,
                      harmonization: ResolvedHarmonization | None = None) -> dict:
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
        completed = DetectorCompletion(
            outputs.result, outputs.clusters, dict(uids), outputs.artifacts)
        return self._validate_dicom_publication(completed)

    def _validate_dicom_publication(self, completed: DetectorCompletion) -> DetectorCompletion:
        """Validate the generalized per-SOP and derived-series publication contract."""
        if not self.required_uid_keys:
            return completed
        for key in self.required_uid_keys:
            value = completed.uids[key]
            if len(value) > 64 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value) is None:
                raise CompletionValidationError(f"packaging returned invalid DICOM UID for {key}")
        try:
            sop_count = int(completed.uids.get("dicom_sop_count", "0"))
        except (TypeError, ValueError) as exc:
            raise CompletionValidationError("packaging returned an invalid SOP count") from exc
        if sop_count < 2:
            raise CompletionValidationError("packaging returned an invalid SOP count")
        manifest_hash = completed.uids.get("dicom_manifest_sha256", "")
        series_hash = completed.uids.get("derived_series_manifest_sha256", "")
        if re.fullmatch(r"[0-9a-f]{64}", str(manifest_hash)) is None:
            raise CompletionValidationError("packaging returned an invalid DICOM manifest hash")
        if re.fullmatch(r"[0-9a-f]{64}", str(series_hash)) is None:
            raise CompletionValidationError(
                "packaging returned an invalid derived-series manifest hash")
        manifest_relative = completed.uids.get("dicom_manifest_path", "")
        if (not isinstance(manifest_relative, str) or not manifest_relative
                or os.path.isabs(manifest_relative) or ".." in Path(manifest_relative).parts):
            raise CompletionValidationError("packaging returned an invalid DICOM manifest path")
        manifest_path = Path(wsettings.meld_data, manifest_relative)
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CompletionValidationError("DICOM per-SOP manifest is missing/invalid") from exc
        files = manifest.get("files")
        series = completed.uids.get("derived_series_manifest")
        if (manifest.get("schema_version") != 1
                or manifest.get("manifest_sha256") != manifest_hash
                or manifest.get("study_instance_uid") != completed.uids["study_uid"]
                or manifest.get("sop_count") != sop_count
                or not isinstance(files, list) or len(files) != sop_count
                or manifest.get("derived_series") != series
                or manifest.get("derived_series_manifest_sha256") != series_hash):
            raise CompletionValidationError("DICOM per-SOP manifest contract is inconsistent")
        if hashlib.sha256(json.dumps(
                files, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != manifest_hash:
            raise CompletionValidationError("DICOM per-SOP manifest hash is inconsistent")
        if hashlib.sha256(json.dumps(
                series, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != series_hash:
            raise CompletionValidationError("derived-series manifest hash is inconsistent")
        if (not isinstance(series, dict) or series.get("schema_version") != 1
                or series.get("study_uid") != completed.uids["study_uid"]
                or not isinstance(series.get("series"), list) or not series["series"]):
            raise CompletionValidationError("derived-series manifest schema is invalid")
        harmonization = series.get("harmonization")
        if not isinstance(harmonization, dict) or harmonization.get("status") not in {
                "applied", "unharmonized", "not_applicable"}:
            raise CompletionValidationError(
                "derived-series manifest lacks harmonization provenance")
        if harmonization["status"] == "applied":
            if (not isinstance(harmonization.get("code"), str)
                    or not harmonization["code"]
                    or isinstance(harmonization.get("version"), bool)
                    or not isinstance(harmonization.get("version"), int)
                    or harmonization["version"] < 1
                    or not isinstance(harmonization.get("method"), str)
                    or not harmonization["method"]):
                raise CompletionValidationError(
                    "derived-series harmonization profile is invalid")
            if completed.result.get("harmo_code") not in {None, harmonization["code"]}:
                raise CompletionValidationError(
                    "DICOM harmonization code differs from detector result")
        elif (harmonization.get("code") != "none" or harmonization.get("version") != 0
              or harmonization.get("method") != harmonization["status"]):
            raise CompletionValidationError(
                "derived-series non-applied harmonization marker is invalid")
        declared_series: dict[str, int] = {}
        for item in series["series"]:
            if not isinstance(item, dict):
                raise CompletionValidationError("derived-series manifest item is invalid")
            uid = item.get("series_uid")
            count = item.get("sop_count")
            if (not isinstance(uid, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", uid) is None
                    or uid in declared_series or isinstance(count, bool)
                    or not isinstance(count, int) or count < 1
                    or not isinstance(item.get("role"), str) or not item["role"]):
                raise CompletionValidationError("derived-series manifest item is invalid")
            declared_series[uid] = count
        observed_series: dict[str, int] = {}
        observed_sops: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                raise CompletionValidationError("DICOM per-SOP manifest item is invalid")
            sop_uid, series_uid = item.get("sop_instance_uid"), item.get("series_instance_uid")
            if (not isinstance(sop_uid, str)
                    or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", sop_uid) is None
                    or sop_uid in observed_sops or series_uid not in declared_series
                    or re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))) is None
                    or isinstance(item.get("size"), bool)
                    or not isinstance(item.get("size"), int) or item["size"] < 1):
                raise CompletionValidationError("DICOM per-SOP manifest item is invalid")
            observed_sops.add(sop_uid)
            observed_series[series_uid] = observed_series.get(series_uid, 0) + 1
        if observed_series != declared_series:
            raise CompletionValidationError("DICOM series/SOP counts are inconsistent")
        for key in self.required_uid_keys:
            if key.endswith("_series_uid") and completed.uids[key] not in declared_series:
                raise CompletionValidationError(f"required {key} is absent from series manifest")
        probability_series = completed.uids.get("probmap_series_uids")
        if probability_series is not None and (
                not isinstance(probability_series, list) or not probability_series
                or any(not isinstance(uid, str) or uid not in declared_series
                       for uid in probability_series)
                or len(probability_series) != len(set(probability_series))
                or probability_series[0] != completed.uids.get("probmap_series_uid")):
            raise CompletionValidationError("parametric-map series aliases are inconsistent")
        return DetectorCompletion(
            completed.result, completed.clusters, completed.uids,
            (*completed.artifacts, manifest_relative),
        )
