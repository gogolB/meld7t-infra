#!/usr/bin/env python3
"""Summarize HippUnfold output → per-subfield volumes + L/R hippocampal asymmetry (spec §25.5).

Runs in the pkg container (has nibabel/numpy). Reads the subfield dseg labelmaps HippUnfold writes
per hemisphere, sums volumes, computes the asymmetry index AI = 100*(L-R)/(0.5*(L+R)), and emits a
first-class finding (a "cluster" on the atrophic side) so it slots into the platform's result model
and the concordance view. Prints JSON to stdout.

HippUnfold layout varies by version; we glob robustly for the subfield dseg per hemisphere.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
import nibabel as nib

# HippUnfold uses TWO different label schemes (confirmed from the image's snakebids.yml
# tissue_atlas_mapping). Which one a dseg uses depends on the file, so we pick by scheme:
#
#  * multihist7 SUBFIELD atlas (the `desc-subfields` dseg): 1=Sub 2=CA1 3=CA2 4=CA3 5=CA4
#    6=DG 7=SRLM 8=Cyst. GM = subiculum+CA1-4+DG (exclude SRLM 7, cyst 8). Per-subfield valid.
#  * TISSUE labelmap (the `desc-postproc`/`desc-tissue` corobl dseg): dg=8 srlm=2 cyst=7 —
#    a DIFFERENT scheme. Only a GM total is meaningful (exclude SRLM 2, cyst 7); the remaining
#    hippocampal tissue labels are NOT subfields, so no per-subfield breakdown is reported.
SUBFIELD_LABELS = {1: "Sub", 2: "CA1", 3: "CA2", 4: "CA3", 5: "CA4", 6: "DG", 7: "SRLM", 8: "Cyst"}
SUBFIELD_GM = {1, 2, 3, 4, 5, 6}         # multihist7 grey matter
TISSUE_NONGM = {2, 7}                     # tissue scheme: SRLM=2, cyst=7 excluded from GM


def _scheme(dseg_path: str) -> str:
    return "subfields" if "subfields" in os.path.basename(dseg_path) else "tissue"


def hemi_volume_mm3(dseg_path: str) -> tuple[float, dict]:
    img = nib.load(dseg_path)
    data = np.asanyarray(img.dataobj)
    if data.ndim != 3 or 0 in data.shape or not np.all(np.isfinite(data)):
        raise ValueError(f"invalid/non-finite hippocampal dseg: {dseg_path}")
    voxvol = float(np.prod(img.header.get_zooms()[:3]))
    if not np.isfinite(voxvol) or voxvol <= 0:
        raise ValueError(f"invalid voxel geometry in hippocampal dseg: {dseg_path}")
    labels, counts = np.unique(data[data > 0], return_counts=True)
    if _scheme(dseg_path) == "subfields":
        per_label = {SUBFIELD_LABELS.get(int(l), str(int(l))): float(c) * voxvol
                     for l, c in zip(labels, counts)}
        gm = sum(int(c) for l, c in zip(labels, counts) if int(l) in SUBFIELD_GM) * voxvol
    else:                                 # tissue scheme: GM total only, no subfield names
        per_label = {}
        gm = sum(int(c) for l, c in zip(labels, counts) if int(l) not in TISSUE_NONGM) * voxvol
    return float(gm), per_label


def find_dseg(root: str, subject: str, hemi: str) -> str | None:
    """Locate a per-hemisphere volumetric subfield labelmap, most-preferred first.

    1. native T2w-space subfield dseg (emitted by some versions/flags) — preferred analysis space;
    2. corobl-space postproc dseg (ALWAYS produced, isotropic, identical res both hemis) — the
       robust fallback: because both hemispheres share the same voxel grid, L/R volumes are
       directly comparable for the asymmetry index even though it isn't the subject's native space.
    """
    pats = [
        f"{root}/hippunfold/{subject}/anat/*hemi-{hemi}*space-T2w*subfields*dseg.nii.gz",
        f"{root}/hippunfold/{subject}/anat/*hemi-{hemi}*subfields*dseg.nii.gz",
        f"{root}/{subject}/anat/*hemi-{hemi}*subfields*dseg.nii.gz",
        f"{root}/**/*hemi-{hemi}*subfields*dseg.nii.gz",
        # fallback: corobl postproc subfield labelmap (work/ dir), guaranteed to exist
        f"{root}/work/{subject}/anat/*hemi-{hemi}_space-corobl_desc-postproc_dseg.nii.gz",
        f"{root}/**/*hemi-{hemi}_space-corobl_desc-postproc_dseg.nii.gz",
    ]
    for p in pats:
        hits = sorted({hit for hit in glob.glob(p, recursive=True) if os.path.isfile(hit)})
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ValueError(
                f"ambiguous HippUnfold {hemi} dseg output for {subject}: {hits}"
            )
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)     # meld-data/output/hippunfold
    ap.add_argument("--subject", required=True)
    ap.add_argument("--ai-threshold", type=float, default=10.0)  # % asymmetry to flag
    a = ap.parse_args()
    if not np.isfinite(a.ai_threshold) or a.ai_threshold < 0:
        raise ValueError("--ai-threshold must be a finite non-negative percentage")

    vols, subfields, sources = {}, {}, {}
    for hemi in ("L", "R"):
        p = find_dseg(a.root, a.subject, hemi)
        if p:
            vols[hemi], subfields[hemi] = hemi_volume_mm3(p)
            sources[hemi] = os.path.relpath(p, a.root)
    if set(vols) != {"L", "R"}:
        raise FileNotFoundError("both left and right HippUnfold dseg outputs are required")
    if any(not np.isfinite(v) or v <= 0 for v in vols.values()):
        raise ValueError("both hippocampal grey-matter volumes must be finite and positive")
    spaces = {"corobl" if "corobl" in source else "T2w" for source in sources.values()}
    if len(spaces) != 1:
        raise ValueError("left/right HippUnfold outputs use different spaces")
    schemes = {_scheme(source) for source in sources.values()}
    if len(schemes) != 1:
        raise ValueError("left/right HippUnfold outputs use different label schemes")
    space = "corobl" if any("corobl" in s for s in sources.values()) else "T2w"

    out = {"subject": a.subject, "volumes_mm3": vols, "subfields_mm3": subfields,
           "dseg_space": space, "dseg_sources": sources, "clusters": []}
    L, R = vols["L"], vols["R"]
    ai = 100.0 * (L - R) / (0.5 * (L + R))
    out["asymmetry_index_pct"] = round(ai, 2)
    out["ai_threshold_pct"] = a.ai_threshold
    out["flagged"] = abs(ai) >= a.ai_threshold
    if out["flagged"]:
        atrophic = "left" if L < R else "right"
        out["clusters"] = [{
            "index": 1, "hemi": atrophic, "location": "hippocampus",
            "size": round(min(L, R) / 1000.0, 3),                 # cm^3 of the smaller side
            "confidence": round(abs(ai), 2),                       # |asymmetry| as the signal
            "saliency": {"volume_L_mm3": round(L, 1), "volume_R_mm3": round(R, 1),
                         "asymmetry_index_pct": round(ai, 2), "dseg_space": space,
                         "subfields_L_mm3": subfields.get("L", {}),
                         "subfields_R_mm3": subfields.get("R", {}),
                         "flagged": True},
        }]
    out["n_clusters"] = len(out["clusters"])
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
