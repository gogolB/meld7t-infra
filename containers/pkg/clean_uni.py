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
    if not np.isfinite(beta_factor) or beta_factor <= 0:
        raise ValueError("beta_factor must be finite and positive")

    images = [nib.load(path) for path in (uni_path, inv1_path, inv2_path)]
    arrays = [image.get_fdata(dtype=np.float32) for image in images]
    uni_img, (uni, inv1, inv2) = images[0], arrays
    if any(data.ndim != 3 or 0 in data.shape or not np.all(np.isfinite(data))
           for data in arrays):
        raise ValueError("UNI/INV inputs must be finite, non-empty 3D images")
    if any(data.shape != uni.shape for data in arrays[1:]):
        raise ValueError("UNI/INV input shapes do not match")
    if any(not np.allclose(image.affine, uni_img.affine, atol=1e-4, rtol=1e-6)
           for image in images[1:]):
        raise ValueError("UNI/INV input affines do not match")
    if min(uni.shape) < 24:
        raise ValueError("UNI/INV field of view is too small for corner-noise estimation")
    # The algebra below is valid only for the Siemens 12-bit UNI convention. Refuse silently
    # rescaled/signed exports instead of producing plausible but scientifically wrong intensities.
    if float(np.min(uni)) < -1e-3 or float(np.max(uni)) > 4095.0 + 1e-3:
        raise ValueError("UNI intensity is not in the required scanner 0..4095 convention")
    if float(np.min(inv1)) < -1e-3 or float(np.min(inv2)) < -1e-3:
        raise ValueError("INV1/INV2 must be non-negative magnitude images")

    denom = inv1 ** 2 + inv2 ** 2
    uni_norm = uni / 4095.0 - 0.5
    num = uni_norm * denom

    # Estimate beta from low-energy corner voxels, while explicitly detecting a cropped/wrapped
    # head contaminating the corners. Fixed-corner means silently bias cleaning on some exports.
    c = 12
    corners = np.concatenate([
        denom[:c, :c, :c].ravel(), denom[-c:, :c, :c].ravel(),
        denom[:c, -c:, :c].ravel(), denom[-c:, -c:, :c].ravel(),
        denom[:c, :c, -c:].ravel(), denom[-c:, :c, -c:].ravel(),
        denom[:c, -c:, -c:].ravel(), denom[-c:, -c:, -c:].ravel(),
    ])
    lower_quartile = float(np.quantile(denom, 0.25))
    background = corners[corners <= lower_quartile]
    background_fraction = float(background.size / corners.size)
    if background_fraction < 0.5:
        raise ValueError(
            "FOV corners are contaminated; MP2RAGE background energy is not reliable")
    bg = float(np.mean(background)) if background.size else 0.0
    max_energy = float(np.max(denom))
    if not np.isfinite(bg) or not np.isfinite(max_energy) or max_energy <= 0:
        raise ValueError("could not estimate finite non-zero MP2RAGE signal energy")
    # Some reconstructed magnitude images have exactly-zero padded corners. A scale-relative
    # epsilon preserves already-clean tissue while keeping the regularized denominator non-zero.
    beta = max(beta_factor * bg, np.finfo(np.float32).eps * max_energy)

    robust = np.clip((num - beta) / (denom + 2.0 * beta) + 0.5, 0.0, 1.0)
    uni_clean = (robust * 4095.0).astype(np.float32)

    out = nib.Nifti1Image(uni_clean, uni_img.affine, uni_img.header)
    nib.save(out, out_path)
    return {"bg_energy": bg, "beta": beta,
            "background_corner_fraction": background_fraction,
            "background_energy_quartile": lower_quartile,
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
