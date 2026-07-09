#!/usr/bin/env python3
"""O'Brien (2014) robust MP2RAGE background removal (spec §6).

The scanner UNI is  UNI_norm = num/denom,  num = Re(INV1*·INV2),  denom = |INV1|²+|INV2|²,
stored as UNI = 4095·(UNI_norm + 0.5). We recover `num` from the scanner UNI and the magnitude
denom, then apply O'Brien's regularization:

    UNI_robust = (num − β)/(denom + 2β) + 0.5

Background (denom ≈ noise) collapses to ~0 (black); tissue (denom ≫ β) is preserved. β is set
from the FOV-corner background signal-energy, so the salt-and-pepper background that FreeSurfer
chokes on vanishes WITHOUT a spatial mask (a mask clips the brain — the reason this method exists).
No skull-strip (spec §7): MELD does its own brain extraction.
"""
import argparse
import numpy as np
import nibabel as nib


def obrien_clean(uni_path, inv1_path, inv2_path, out_path, beta_factor=6.0):
    uni_img = nib.load(uni_path)
    uni = uni_img.get_fdata()
    inv1 = nib.load(inv1_path).get_fdata()
    inv2 = nib.load(inv2_path).get_fdata()

    denom = inv1 ** 2 + inv2 ** 2
    uni_norm = uni / 4095.0 - 0.5
    num = uni_norm * denom

    # β from background signal-energy: the 8 FOV corners are unambiguous background.
    c = 12
    corners = np.concatenate([
        denom[:c, :c, :c].ravel(), denom[-c:, :c, :c].ravel(),
        denom[:c, -c:, :c].ravel(), denom[-c:, -c:, :c].ravel(),
        denom[:c, :c, -c:].ravel(), denom[-c:, :c, -c:].ravel(),
        denom[:c, -c:, -c:].ravel(), denom[-c:, -c:, -c:].ravel(),
    ])
    bg = float(np.mean(corners))
    beta = beta_factor * bg

    robust = np.clip((num - beta) / (denom + 2.0 * beta) + 0.5, 0.0, 1.0)
    uni_clean = (robust * 4095.0).astype(np.float32)

    out = nib.Nifti1Image(uni_clean, uni_img.affine, uni_img.header)
    nib.save(out, out_path)
    return {"bg_energy": bg, "beta": beta,
            "clean_range": [float(uni_clean.min()), float(uni_clean.max())]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="O'Brien robust MP2RAGE background clean")
    ap.add_argument("--uni", required=True)
    ap.add_argument("--inv1", required=True)
    ap.add_argument("--inv2", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--beta-factor", type=float, default=6.0)
    a = ap.parse_args()
    info = obrien_clean(a.uni, a.inv1, a.inv2, a.out, a.beta_factor)
    print(f"O'Brien clean: bg_energy={info['bg_energy']:.1f} beta={info['beta']:.1f} "
          f"range={info['clean_range']}  -> {a.out}")
