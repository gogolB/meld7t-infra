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


def hemi_volume_mm3(dseg_path: str) -> tuple[float, dict]:
    img = nib.load(dseg_path)
    data = np.asanyarray(img.dataobj)
    voxvol = float(np.prod(img.header.get_zooms()[:3]))
    labels, counts = np.unique(data[data > 0], return_counts=True)
    per_label = {int(l): float(c) * voxvol for l, c in zip(labels, counts)}
    return float((data > 0).sum()) * voxvol, per_label


def find_dseg(root: str, subject: str, hemi: str) -> str | None:
    # e.g. <root>/hippunfold/sub-X/anat/sub-X_hemi-L_space-*_desc-subfields_dseg.nii.gz
    pats = [
        f"{root}/hippunfold/{subject}/anat/*hemi-{hemi}*subfields*dseg.nii.gz",
        f"{root}/{subject}/anat/*hemi-{hemi}*subfields*dseg.nii.gz",
        f"{root}/**/*hemi-{hemi}*subfields*dseg.nii.gz",
    ]
    for p in pats:
        hits = sorted(glob.glob(p, recursive=True))
        if hits:
            return hits[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)     # meld-data/output/hippunfold
    ap.add_argument("--subject", required=True)
    ap.add_argument("--ai-threshold", type=float, default=10.0)  # % asymmetry to flag
    a = ap.parse_args()

    vols, subfields = {}, {}
    for hemi in ("L", "R"):
        p = find_dseg(a.root, a.subject, hemi)
        if p:
            vols[hemi], subfields[hemi] = hemi_volume_mm3(p)

    out = {"subject": a.subject, "volumes_mm3": vols, "subfields_mm3": subfields, "clusters": []}
    if "L" in vols and "R" in vols and (vols["L"] + vols["R"]) > 0:
        L, R = vols["L"], vols["R"]
        ai = 100.0 * (L - R) / (0.5 * (L + R))
        out["asymmetry_index_pct"] = round(ai, 2)
        atrophic = "left" if L < R else "right"
        out["clusters"] = [{
            "index": 1, "hemi": atrophic, "location": "hippocampus",
            "size": round(min(L, R) / 1000.0, 3),                 # cm^3 of the smaller side
            "confidence": round(abs(ai), 2),                       # |asymmetry| as the signal
            "saliency": {"volume_L_mm3": round(L, 1), "volume_R_mm3": round(R, 1),
                         "asymmetry_index_pct": round(ai, 2),
                         "flagged": abs(ai) >= a.ai_threshold},
        }]
    print(json.dumps(out))


if __name__ == "__main__":
    sys.exit(main())
