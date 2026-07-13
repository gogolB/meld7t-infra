"""HippUnfold runner (spec §25.5) — HS: unfold + nnU-Net hippocampal subfield segmentation.

BIDS App on the T2 SPACE (0.58mm — ideal hippocampal contrast) with T1 present. Ingest computes
per-subfield volumes + L/R asymmetry via the pkg container (which has nibabel); the asymmetry is
surfaced as a first-class finding. No normative DB needed — asymmetry is intrinsic.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Optional

from app.models import RunStatus

from .. import pipeline
from ..config import wsettings
from ..harmonization import ResolvedHarmonization
from ..process import run_process
from .base import CompletionValidationError, DetectorCompletion, DetectorRunner, run_cmd


class HippUnfoldRunner(DetectorRunner):
    detector_id = "hippunfold"
    needs_t2 = True                 # segment on the high-res T2 SPACE
    uses_gpu = False                # CPU-only nnU-Net (bundled torch lacks sm_86) — no GPU mutex
    required_uid_keys = ("study_uid", "t1_series_uid", "seg_series_uid")

    async def compute(self, subject: str, workdir: str,
                      harmonization: ResolvedHarmonization | None = None
                      ) -> tuple[int, Optional[RunStatus]]:
        label = subject.replace("sub-", "")
        if wsettings.hippunfold_cache_sha256:
            verify_cmd = [
                "podman", "run", "--rm", "--name", f"meld7t-hipp-cache-{subject}",
                "--network=none", "--security-opt=no-new-privileges", "--cap-drop=all",
                "-v", f"{wsettings.hippunfold_cache}:/cache:ro",
                wsettings.pkg_image,
                "python3", "/opt/pkg/verify_cache.py",
                "--root", "/cache",
                "--expected-manifest-sha256", wsettings.hippunfold_cache_sha256,
            ]
            if await run_cmd(verify_cmd, os.path.join(workdir, "hippunfold-cache.log")) != 0:
                raise CompletionValidationError(
                    "HippUnfold cache does not match the signed per-file release closure")
        # Clean single-subject BIDS dir (avoid pybids indexing the shared input folder).
        bids = os.path.join(workdir, "bids")
        anat = os.path.join(bids, subject, "anat")
        os.makedirs(anat, exist_ok=True)
        src = os.path.join(wsettings.meld_data, "input", subject, "anat")
        for mod in ("T1w", "T2w"):
            s = os.path.join(src, f"{subject}_{mod}.nii.gz")
            if not os.path.isfile(s) or os.path.getsize(s) == 0:
                return 1, RunStatus.failed
            shutil.copyfile(s, os.path.join(anat, f"{subject}_{mod}.nii.gz"))
        with open(os.path.join(bids, "dataset_description.json"), "w") as fh:
            fh.write('{"Name": "meld7t", "BIDSVersion": "1.8.0"}')

        # A per-run output root prevents concurrent CPU-only HippUnfold jobs from sharing
        # SnakeMake state and prevents a retry from accepting stale subject artifacts.
        outdir = os.path.join(wsettings.meld_data, "output", "hippunfold-runs", subject)
        if os.path.isdir(outdir):
            shutil.rmtree(outdir)
        os.makedirs(outdir, exist_ok=True)
        # No --device: HippUnfold's bundled torch (py3.9) lacks sm_86 kernels for Ampere GPUs
        # ("no kernel image available"), so nnU-Net runs on CPU (the hippocampal crop is small).
        # The cache must hold the model tar rewritten to owner 0 (rootless chown fix) — see README.
        cmd = [
            "podman", "run", "--rm", "--name", f"meld7t-hippunfold-{subject}",
            "--network=none", "--security-opt=no-new-privileges", "--cap-drop=all",
            "-v", f"{bids}:/bids:ro",
            "-v", f"{outdir}:/out",
            # This cache is imported from the signed release.  Runtime mutation would invalidate
            # its accepted scientific identity, so a missing write-time asset fails closed.
            "-v", f"{wsettings.hippunfold_cache}:/root/.cache/hippunfold:ro",
            wsettings.hippunfold_image,
            "/bids", "/out", "participant",
            "--participant-label", label,
            "--modality", "T2w",
            "--cores", "8",
        ]
        rc = await run_cmd(cmd, os.path.join(workdir, "hippunfold.log"))
        return rc, (None if rc == 0 else RunStatus.failed)

    async def ingest(self, subject: str, workdir: str,
                     harmonization: ResolvedHarmonization | None = None) -> dict:
        """Summarize subfield volumes + asymmetry in the pkg container (has nibabel)."""
        cmd = [
            "podman", "run", "--rm", "--name", f"meld7t-hippunfold-ingest-{subject}",
            "--network=none",
            "--security-opt=no-new-privileges", "--cap-drop=all",
            "-v", f"{wsettings.meld_data}:/data:ro,z",
            wsettings.pkg_image,
            "python3", "/opt/pkg/hippunfold_summarize.py",
            "--root", f"/data/output/hippunfold-runs/{subject}", "--subject", subject,
            "--ai-threshold", str(wsettings.hippunfold_ai_threshold_pct),
        ]
        result = await run_process(cmd, os.path.join(workdir, "hippunfold-ingest.log"),
                                   capture_stdout=True)
        if result.returncode != 0:
            raise CompletionValidationError(
                f"HippUnfold ingest failed with rc={result.returncode}")
        try:
            summary = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompletionValidationError("HippUnfold ingest did not emit valid JSON") from exc
        if (not isinstance(summary, dict) or summary.get("error") or
                summary.get("subject") != subject or
                not isinstance(summary.get("clusters"), list) or
                set(summary.get("volumes_mm3", {})) != {"L", "R"}):
            raise CompletionValidationError("HippUnfold ingest output is incomplete or invalid")
        clusters = summary["clusters"]
        sources = [os.path.join("output", "hippunfold-runs", subject, path)
                   for path in summary.get("dseg_sources", {}).values()]
        detector_summary = {
            key: summary[key] for key in (
                "volumes_mm3", "subfields_mm3", "dseg_space", "asymmetry_index_pct", "flagged",
                "ai_threshold_pct", "dseg_sources",
            ) if key in summary
        }
        return {"result": {"report_path": None, "n_clusters": len(clusters),
                           "detector_summary": detector_summary,
                           "metric_schema": {
                               "size": {"label": "smaller-side hippocampal volume", "unit": "mL"},
                               "confidence": {"label": "absolute L/R asymmetry", "unit": "%"},
                               "comparable_across_detectors": False,
                           }},
                "clusters": clusters, "artifacts": sources}

    async def package(self, subject: str, pseudonym: str, workdir: str,
                      uid_seed: str, study_uid_seed: str,
                      expected_clusters: int | None = None,
                      validated_ingest: DetectorCompletion | None = None,
                      harmonization: ResolvedHarmonization | None = None) -> dict:
        if expected_clusters is None or validated_ingest is None:
            raise CompletionValidationError(
                "HippUnfold packaging requires validated subfields and finding count")
        summary = validated_ingest.result.get("detector_summary")
        sources = summary.get("dseg_sources") if isinstance(summary, dict) else None
        if not isinstance(sources, dict) or set(sources) != {"L", "R"}:
            raise CompletionValidationError("HippUnfold packaging lacks bilateral dseg sources")
        relative = {}
        root = os.path.join("output", "hippunfold-runs", subject)
        for hemi, path in sources.items():
            if (not isinstance(path, str) or not path or os.path.isabs(path)
                    or ".." in path.split(os.sep)):
                raise CompletionValidationError("HippUnfold dseg source path is unsafe")
            relative[hemi] = os.path.join(root, path)
        flagged = bool(summary.get("flagged"))
        if flagged:
            if len(validated_ingest.clusters) != 1 or validated_ingest.clusters[0].get(
                    "hemi") not in {"left", "right"}:
                raise CompletionValidationError("HippUnfold flagged side is inconsistent")
            flagged_side = validated_ingest.clusters[0]["hemi"]
        else:
            flagged_side = "none"
        rc, uids = await pipeline.run_hippunfold_package(
            subject, pseudonym, workdir, uid_seed, study_uid_seed, expected_clusters,
            relative["L"], relative["R"], flagged_side, harmonization,
        )
        if rc != 0:
            raise CompletionValidationError(
                f"HippUnfold DICOM packaging/STOW failed with rc={rc}")
        return uids

    def validate_completion(self, ingested: dict, uids: dict) -> DetectorCompletion:
        completed = super().validate_completion(ingested, uids)
        roles = {item["role"] for item in completed.uids[
            "derived_series_manifest"]["series"]}
        try:
            slices = int(completed.uids.get("n_t1_slices", "0"))
            sop_count = int(completed.uids.get("dicom_sop_count", "0"))
            if (roles != {"hs_native_t2_reference", "hs_subfields_and_atrophy_segmentation"}
                    or slices < 1 or sop_count != slices + 1):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise CompletionValidationError(
                "HippUnfold derived DICOM series contract is incomplete") from exc
        return completed
