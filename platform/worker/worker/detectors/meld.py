"""MELD-FCD runner (cortical-surface GNN). Wraps the validated MELD invocation + cluster ingest
+ DICOM-SEG packaging that were the worker's original hardcoded path."""
from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Optional

from app.models import RunStatus

from .. import ingest, pipeline
from ..config import wsettings
from ..harmonization import ResolvedHarmonization
from .base import CompletionValidationError, DetectorCompletion, DetectorRunner


class MeldRunner(DetectorRunner):
    detector_id = "meld_fcd"
    supports_harmonization = True
    allowed_harmonization_methods = frozenset({"meld_distributed_combat"})
    required_uid_keys = ("study_uid", "t1_series_uid", "seg_series_uid")

    @staticmethod
    def _verify_harmonization_invocation(
            log_path: str, harmonization: ResolvedHarmonization) -> None:
        try:
            first_line = Path(log_path).read_text(errors="replace").splitlines()[0]
            tokens = shlex.split(first_line.removeprefix("$ "))
            code_index = tokens.index("-harmo_code")
            code = tokens[code_index + 1]
        except (OSError, IndexError, ValueError) as exc:
            raise CompletionValidationError(
                "MELD log does not prove a harmonized invocation") from exc
        expected_mount = (
            f"{harmonization.host_data_root}:/data/meld_params/distributed_combat:ro,z")
        if code != harmonization.code or expected_mount not in tokens:
            raise CompletionValidationError(
                "MELD invocation does not match the requested harmonization profile")

    async def compute(self, subject: str, workdir: str,
                      harmonization: ResolvedHarmonization | None = None
                      ) -> tuple[int, Optional[RunStatus]]:
        # Same-contract retries are allowed, but must never accept a mixture of previous outputs.
        output = os.path.join(wsettings.meld_data, "output", "predictions_reports", subject)
        if os.path.isdir(output):
            shutil.rmtree(output)
        rc = await pipeline.run_meld(subject, workdir, harmonization)
        if rc != 0:
            oom = pipeline.is_oom(os.path.join(workdir, "meld.log"))
            return rc, (RunStatus.failed_oom if oom else RunStatus.failed)
        return 0, None

    async def ingest(self, subject: str, workdir: str,
                     harmonization: ResolvedHarmonization | None = None) -> dict:
        clusters = ingest.parse_clusters(wsettings.meld_data, subject)
        result = ingest.result_fields(wsettings.meld_data, subject, clusters=clusters)
        result["metric_schema"] = {
            "size": {"label": "MELD cluster extent", "unit": "detector-native; validate"},
            "confidence": {"label": "MELD confidence", "unit": "detector-specific score"},
            "comparable_across_detectors": False,
        }
        log_path = os.path.join(workdir, "meld.log")
        if harmonization and harmonization.applied:
            self._verify_harmonization_invocation(log_path, harmonization)
            result["harmo_code"] = harmonization.code
        root = os.path.join("output", "predictions_reports", subject)
        log_relative = Path(log_path).resolve().relative_to(
            Path(wsettings.meld_data).resolve()).as_posix()
        report_dir = Path(wsettings.meld_data, result["report_path"]).parent
        frames = [
            path.relative_to(wsettings.meld_data).as_posix()
            for path in sorted(report_dir.glob("*.png")) if path.is_file()
        ]
        return {"result": result,
                "clusters": clusters,
                "artifacts": [result["report_path"],
                              os.path.join(root, "reports", f"info_clusters_{subject}.csv"),
                              os.path.join(root, "predictions", "prediction.nii.gz"),
                              log_relative,
                              *frames]}

    async def package(self, subject: str, pseudonym: str, workdir: str,
                      uid_seed: str, study_uid_seed: str,
                      expected_clusters: int | None = None,
                      validated_ingest: DetectorCompletion | None = None,
                      harmonization: ResolvedHarmonization | None = None) -> dict:
        prediction = os.path.join(wsettings.meld_data, "output", "predictions_reports", subject,
                                  "predictions", "prediction.nii.gz")
        if not os.path.isfile(prediction) or os.path.getsize(prediction) == 0:
            raise CompletionValidationError(f"MELD prediction is missing or empty: {prediction}")
        if expected_clusters is None:
            raise CompletionValidationError("MELD packaging requires the validated cluster count")
        rc, uids = await pipeline.run_package(
            subject, pseudonym, workdir, uid_seed, study_uid_seed, expected_clusters,
            harmonization)
        if rc != 0:
            raise CompletionValidationError(f"DICOM packaging/STOW failed with rc={rc}")
        return uids

    def validate_completion(self, ingested: dict, uids: dict) -> DetectorCompletion:
        completed = super().validate_completion(ingested, uids)
        roles = {item["role"] for item in completed.uids[
            "derived_series_manifest"]["series"]}
        if roles != {"meld_native_t1_reference", "meld_fcd_segmentation"}:
            raise CompletionValidationError("MELD derived-series roles are incomplete")
        try:
            slices = int(completed.uids.get("n_t1_slices", "0"))
            sop_count = int(completed.uids.get("dicom_sop_count", "0"))
            if slices < 1 or sop_count != slices + 1:
                raise ValueError
        except ValueError as exc:
            raise CompletionValidationError("packaging returned an invalid SOP count") from exc
        report = completed.result.get("report_path")
        if not isinstance(report, str) or os.path.isabs(report) or ".." in report.split(os.sep):
            raise CompletionValidationError("MELD report_path must be a safe relative path")
        path = os.path.realpath(os.path.join(wsettings.meld_data, report))
        root = os.path.realpath(wsettings.meld_data)
        if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
            raise CompletionValidationError("MELD report_path escapes data root or is missing")
        return DetectorCompletion(
            completed.result, completed.clusters, completed.uids,
            completed.artifacts,
        )
