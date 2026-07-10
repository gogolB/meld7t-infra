"""Detector registry (spec §25.1, §25.3). Detector-plural: MELD is one row among many.

Each detector declares its target, which workup it belongs to, the input series role(s) it consumes,
and its integration status. `built` detectors actually run; `pending` ones appear in the recipe/UI
as declared-but-not-yet-integrated slots (§25.7 build-now) so the concordance vision is visible
without fabricating results.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import DetectorId, SeriesRole, Workup


@dataclass(frozen=True)
class Detector:
    id: DetectorId
    label: str
    target: str                      # FCD / HS
    workups: tuple[Workup, ...]
    # source roles this detector can consume, in preference order (empirical A/B, §16)
    source_roles: tuple[SeriesRole, ...]
    status: str                      # "built" | "pending"
    method: str


REGISTRY: dict[DetectorId, Detector] = {
    DetectorId.meld_fcd: Detector(
        id=DetectorId.meld_fcd, label="MELD-FCD", target="FCD (neocortex)",
        workups=(Workup.fcd, Workup.both),
        # both T1 sources are viable (tandem); UNI is the provisional lead (pilot A/B)
        source_roles=(SeriesRole.t1_uni, SeriesRole.t1_mprage),
        status="built", method="cortical-surface GNN"),
    DetectorId.map: Detector(
        id=DetectorId.map, label="MAP", target="FCD (neocortex)",
        workups=(Workup.fcd, Workup.both),
        source_roles=(SeriesRole.t1_mprage, SeriesRole.t1_uni),   # MAP prefers MPRAGE (§25.4)
        status="built", method="voxel morphometry (SPM MAP07)"),
    DetectorId.hippunfold: Detector(
        id=DetectorId.hippunfold, label="HippUnfold", target="HS (hippocampus)",
        workups=(Workup.hs, Workup.both),
        # segments on the T2 SPACE (worker sets needs_t2); the T1 companion source is picked here
        source_roles=(SeriesRole.t1_uni, SeriesRole.t1_mprage),
        status="built", method="unfold + nnU-Net subfield volumetry"),
    DetectorId.qt2: Detector(
        id=DetectorId.qt2, label="qT2", target="HS (hippocampus)",
        workups=(Workup.hs, Workup.both),
        source_roles=(SeriesRole.t2,),
        status="pending", method="T2 relaxometry"),
    DetectorId.aid_hs: Detector(
        id=DetectorId.aid_hs, label="AID-HS", target="HS (hippocampus)",
        workups=(Workup.hs, Workup.both),
        source_roles=(SeriesRole.t1_mprage, SeriesRole.flair),
        status="pending", method="hippocampal-surface classifier"),
}


def detectors_for(workup: Workup) -> list[Detector]:
    return [d for d in REGISTRY.values() if workup in d.workups]
