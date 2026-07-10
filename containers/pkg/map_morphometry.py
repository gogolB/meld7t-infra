#!/usr/bin/env python3
"""MAP-style voxel morphometry from SPM tissue segments (Huppertz MAP07 method, spec §25.4).

Runs in the pkg container (nibabel/scipy). Consumes the MNI-space tissue probabilities SPM's
unified segmentation wrote (wc1 = GM, wc2 = WM) and computes two FCD feature maps, then converts
them to candidate clusters emitted as the platform's cluster dicts.

Feature maps (open reimplementations of the MAP07 concept — named for what they measure, not the
proprietary MAP toolbox):
  * junction  — grey/white *junction blurring*: 4·GM·WM, maximal at the tissue interface. FCD
                blurs the GM/WM boundary, broadening/intensifying this band.
  * extension — grey matter *extending into deep white matter* (transmantle-like): GM probability
                restricted to voxels well inside the WM compartment.

DETECTION regime — how a feature map becomes clusters:
  * NORMATIVE (preferred, §25.2): if /data/normative/map/<feature>_{mean,std}.nii.gz is staged
    (built from a 7T control cohort on this exact MNI grid), z is voxelwise (feat-mean)/std. This
    is real MAP07. harmo_code="normative-v1".
  * ASYMMETRY (control-free fallback): with no cohort, a raw single-subject z of these features is
    ill-posed — the junction band is high along every *normal* boundary and the extension map is
    zero-inflated. Instead we use inter-hemispheric ASYMMETRY: mirror the feature across the MNI
    mid-sagittal plane and z-score (feat − mirror). FCD is usually unilateral, so a feature
    elevated on one side vs its mirror is the candidate. Robust and control-free, but blind to
    symmetric/bilateral disease. harmo_code="none". Hypothesis-generating, for adjudication only.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
import nibabel as nib
from scipy import ndimage

# Per-feature: z threshold, floor below which a voxel is "not in play" for the asymmetry test.
FEATURES = {
    "junction":  {"z_thresh": 4.5, "floor": 0.10, "label": "GM/WM junction blurring"},
    "extension": {"z_thresh": 4.5, "floor": 0.05, "label": "GM extension into deep WM"},
}
SMOOTH_SIGMA_VOX = 2.0        # feature-map smoothing before analysis (MAP smooths too)
MIN_CLUSTER_ML = 0.50         # drop specks below 0.5 mL
WM_ERODE_ITERS = 3            # "deep WM" = WM core, eroded from the GM/WM interface
MAX_CLUSTERS = 12             # cap the candidate list (single-subject is high-FP)


def _load(path):
    img = nib.load(path)
    return img, np.asanyarray(img.dataobj, dtype=np.float32)


def _find(root, subject, stem):
    for p in (f"{root}/{subject}/{stem}T1.nii", f"{root}/{subject}/{stem}T1.nii.gz",
              f"{root}/{subject}/{stem}*.nii*"):
        hits = sorted(glob.glob(p))
        if hits:
            return hits[0]
    return None


def compute_features(gm, wm):
    junction = 4.0 * gm * wm
    deep_wm = ndimage.binary_erosion(wm >= 0.5, iterations=WM_ERODE_ITERS)
    extension = gm * deep_wm.astype(np.float32)
    return {"junction": ndimage.gaussian_filter(junction, SMOOTH_SIGMA_VOX),
            "extension": ndimage.gaussian_filter(extension, SMOOTH_SIGMA_VOX)}


def _x_axis(affine):
    """Voxel axis that maps to world (MNI) left-right, so np.flip mirrors hemispheres."""
    return int(np.argmax(np.abs(affine[0, :3])))


def z_normative(feat, name, data_root):
    mean_p = f"{data_root}/normative/map/{name}_mean.nii.gz"
    std_p = f"{data_root}/normative/map/{name}_std.nii.gz"
    if not (os.path.exists(mean_p) and os.path.exists(std_p)):
        return None
    mean = np.asanyarray(nib.load(mean_p).dataobj, dtype=np.float32)
    std = np.asanyarray(nib.load(std_p).dataobj, dtype=np.float32)
    std = np.where(std > 1e-6, std, np.nan)
    return np.nan_to_num((feat - mean) / std, nan=0.0)


def z_asymmetry(feat, name, affine, tissue):
    """Robust z of (feature − mirror) over voxels where either side is in play. Positive z = this
    side elevated vs its contralateral mirror (the candidate). Antisymmetric, so mirror clusters
    on the other side are the negative tail and are naturally excluded by thresholding z>0."""
    mirror = np.flip(feat, axis=_x_axis(affine))
    asym = feat - mirror
    m = ((feat + mirror) > FEATURES[name]["floor"]) & (tissue > 0)
    vals = asym[m]
    if vals.size < 100:
        return np.zeros_like(feat)
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med))) or 1e-6
    z = (asym - med) / (1.4826 * mad)
    return np.where(m, z, 0.0)


def _hemi(x_mm):
    return "left" if x_mm < 0 else "right"


def _lobe(x, y, z):
    """Coarse MNI-coordinate → lobe label (display only; not an atlas lookup)."""
    if z < -25 and y < -10:
        return "cerebellum"
    if y > 30:
        return "frontal"
    if y < -55:
        return "occipital"
    if x < -35 or x > 35:
        return "temporal" if z < 5 else "parietal/lateral"
    if z > 45:
        return "parietal/superior-frontal"
    return "insular/central"


def clusters_from_z(z, feat_name, affine, voxvol_mm3):
    thr = FEATURES[feat_name]["z_thresh"]
    lab, n = ndimage.label(z >= thr)
    out = []
    for i in range(1, n + 1):
        comp = lab == i
        vol_ml = int(comp.sum()) * voxvol_mm3 / 1000.0
        if vol_ml < MIN_CLUSTER_ML:
            continue
        mni = nib.affines.apply_affine(affine, np.argwhere(comp).mean(axis=0))
        out.append({
            "hemi": _hemi(mni[0]), "location": _lobe(*mni), "size": round(vol_ml, 3),
            "confidence": round(float(z[comp].max()), 2), "_feature": feat_name,
            "_mni": [round(float(c), 1) for c in mni],
            "_zmean": round(float(z[comp].mean()), 2)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)      # /data/output/map
    ap.add_argument("--subject", required=True)
    ap.add_argument("--data-root", default=None)  # normative template lookup (default: root/..)
    a = ap.parse_args()

    gm_p, wm_p = _find(a.root, a.subject, "wc1"), _find(a.root, a.subject, "wc2")
    if not gm_p or not wm_p:
        print(json.dumps({"subject": a.subject, "error": "missing SPM wc1/wc2 segments",
                          "clusters": []}))
        return 0

    gm_img, gm = _load(gm_p)
    _, wm = _load(wm_p)
    affine = gm_img.affine
    voxvol = float(abs(np.linalg.det(affine[:3, :3])))
    tissue = ((gm + wm) > 0.5).astype(np.float32)
    data_root = a.data_root or os.path.dirname(a.root.rstrip("/"))

    feats = compute_features(gm, wm)
    clusters, harmo = [], None
    for name, feat in feats.items():
        zn = z_normative(feat, name, data_root)
        if zn is not None:
            z, mode = zn, "normative-v1"
        else:
            z, mode = z_asymmetry(feat, name, affine, tissue), "none"
        harmo = mode if harmo in (None, mode) else "mixed"
        clusters.extend(clusters_from_z(z, name, affine, voxvol))

    clusters.sort(key=lambda c: c["confidence"], reverse=True)
    clusters = clusters[:MAX_CLUSTERS]
    for i, c in enumerate(clusters, 1):
        c["index"] = i
        c["saliency"] = {"feature": c.pop("_feature"), "z_max": c["confidence"],
                         "z_mean": c.pop("_zmean"), "mni": c.pop("_mni"),
                         "method": "asymmetry" if harmo == "none" else "normative",
                         "harmonisation": harmo, "single_subject": harmo == "none"}

    print(json.dumps({"subject": a.subject, "harmo_code": harmo, "space": "MNI152",
                      "n_clusters": len(clusters), "clusters": clusters}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
