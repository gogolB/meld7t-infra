#!/usr/bin/env python3
"""Prepare a MELD BIDS input from raw DICOM (spec §2.2, §6, §16).

Runs inside the pkg container (--network=none). Discovers series by reading their DICOM HEADERS
and classifying by acquisition parameters (series_classify) — robust to blank/anonymized
descriptions — with folder-name patterns only as a fallback. Converts with dcm2niix,
background-cleans the UNI (O'Brien) when source=uni, optionally also emits the T2 (for HippUnfold),
and writes  <out>/sub-<id>/anat/sub-<id>_{T1w,T2w}.nii.gz  plus a provenance sidecar recording
exactly which series were used AND why (the header rule that matched).

Selection is propose-then-FAIL-loud, never silent-auto (§16). Override any role with
--<role>-series "<exact folder name>".
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from series_classify import classify, is_qt2_capable

# folder-name fallbacks, used only when header classification is inconclusive
NAME_PATTERNS = {
    "t1_uni": r"uni",
    "t1_inv1": r"inv1",
    "t1_inv2": r"inv2",
    "t1_mprage": r"mprage|t1\+orig|orig",
    "t2": r"t2.*(space|spc)",
    "flair": r"flair|dark[\s_-]*fluid",
}


def read_series_tags(series_dir: str) -> dict:
    """Representative DICOM tags for a series + echo count (for multi-echo/qT2 detection)."""
    import pydicom

    files = sorted(glob.glob(os.path.join(series_dir, "*.dcm")))
    ds = pydicom.dcmread(files[0], stop_before_pixels=True)
    echoes = set()
    for f in files[:40]:
        try:
            d = pydicom.dcmread(f, stop_before_pixels=True, specific_tags=["EchoTime"])
            if "EchoTime" in d and d.EchoTime not in (None, ""):
                echoes.add(round(float(d.EchoTime), 2))
        except Exception:
            pass
    seq = ds.get("ScanningSequence", [])
    seq = [seq] if isinstance(seq, str) else list(seq or [])
    ti = ds.get("InversionTime", None)
    return {
        "scanning_sequence": seq,
        "image_type": [str(x) for x in (ds.get("ImageType", []) or [])],
        "tr": float(ds.get("RepetitionTime", 0) or 0),
        "te": float(ds.get("EchoTime", 0) or 0),
        "ti": float(ti) if ti not in (None, "") else None,
        "n_echoes": len(echoes) if echoes else 1,
        "description": str(ds.get("SeriesDescription", "")),
    }


def discover(root: str) -> dict:
    """Classify every series under root by header. Returns {name: {path, role, reason, tags}}."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        if not any(f.lower().endswith(".dcm") for f in files):
            continue
        tags = read_series_tags(dirpath)
        role, reason = classify(tags)
        out[os.path.basename(dirpath)] = {"path": dirpath, "role": role, "reason": reason,
                                          "tags": tags}
    return out


def select(role: str, series: dict, override: str | None) -> tuple[str, str]:
    """Return (series_dir, reason) for a role. Header first, then name fallback, else fail loud."""
    if override:
        if override not in series:
            sys.exit(f"ERROR: --{role}-series '{override}' not found. Available: {sorted(series)}")
        return series[override]["path"], f"operator override → {override}"

    # 1) header classification
    hits = [(n, s) for n, s in series.items() if s["role"] == role]
    if len(hits) == 1:
        return hits[0][1]["path"], hits[0][1]["reason"]
    if len(hits) > 1 and role == "t1_mprage":
        # prefer the plain normalized magnitude over an extra-filtered variant (ImageType FIL)
        plain = [(n, s) for n, s in hits if not any("FIL" in t.upper() for t in s["tags"]["image_type"])]
        if len(plain) == 1:
            return plain[0][1]["path"], plain[0][1]["reason"] + " (plain, not FIL-filtered)"
    if len(hits) > 1:
        sys.exit(f"ERROR: role '{role}' AMBIGUOUS by header — {[n for n, _ in hits]}. "
                 f"Pass --{role}-series (§16: no silent guessing).")

    # 2) name-pattern fallback
    rx = re.compile(NAME_PATTERNS.get(role, role), re.IGNORECASE)
    named = sorted(n for n in series if rx.search(n))
    if len(named) == 1:
        return series[named[0]]["path"], f"name fallback /{rx.pattern}/ (header inconclusive)"

    sys.exit(f"ERROR: could not identify role '{role}' by header or name. "
             f"Series: { {n: s['role'] for n, s in series.items()} }. Pass --{role}-series.")


def dcm2niix_largest(series_dir, workdir, tag):
    sub = os.path.join(workdir, tag)
    os.makedirs(sub, exist_ok=True)
    subprocess.run(["dcm2niix", "-o", sub, "-f", "%d_%s", "-z", "y", series_dir],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    niis = [os.path.join(sub, f) for f in os.listdir(sub) if f.endswith(".nii.gz")]
    if not niis:
        sys.exit(f"ERROR: dcm2niix produced no NIfTI for {series_dir}")
    return max(niis, key=os.path.getsize)


def main():
    ap = argparse.ArgumentParser(description="Prepare MELD BIDS input from DICOM (header-based)")
    ap.add_argument("--dicom-root", required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--source", choices=["uni", "mprage"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--beta-factor", type=float, default=6.0)
    ap.add_argument("--also-t2", action="store_true", help="also emit sub-<id>_T2w (HippUnfold)")
    for role in ("uni", "inv1", "inv2", "mprage", "t2"):
        ap.add_argument(f"--{role}-series", default=None)
    a = ap.parse_args()

    series = discover(a.dicom_root)
    if not series:
        sys.exit(f"ERROR: no DICOM series under {a.dicom_root}")

    anat = os.path.join(a.out, a.subject, "anat")
    os.makedirs(anat, exist_ok=True)
    _v = subprocess.run(["dcm2niix", "--version"], capture_output=True, text=True)
    prov = {"source": a.source, "subject": a.subject,
            "dcm2niix": (_v.stdout + _v.stderr).strip().splitlines()[-1],
            "classified": {n: {"role": s["role"], "reason": s["reason"]} for n, s in series.items()},
            "series": {}}

    with tempfile.TemporaryDirectory() as work:
        if a.source == "mprage":
            sd, why = select("t1_mprage", series, a.mprage_series)
            shutil.copyfile(dcm2niix_largest(sd, work, "mprage"),
                            os.path.join(anat, f"{a.subject}_T1w.nii.gz"))
            prov["series"]["T1w"] = {"folder": os.path.basename(sd), "why": why}
        else:  # uni → O'Brien clean from UNI + INV1 + INV2
            picks = {r: select(f"t1_{r}", series, getattr(a, f"{r}_series"))
                     for r in ("uni", "inv1", "inv2")}
            niis = {r: dcm2niix_largest(p[0], work, r) for r, p in picks.items()}
            from clean_uni import obrien_clean
            info = obrien_clean(niis["uni"], niis["inv1"], niis["inv2"],
                                os.path.join(anat, f"{a.subject}_T1w.nii.gz"), a.beta_factor)
            prov["series"]["T1w"] = {r: {"folder": os.path.basename(p[0]), "why": p[1]}
                                     for r, p in picks.items()}
            prov["obrien"] = {"beta_factor": a.beta_factor, "beta": info["beta"]}

        if a.also_t2:
            sd, why = select("t2", series, a.t2_series)
            shutil.copyfile(dcm2niix_largest(sd, work, "t2"),
                            os.path.join(anat, f"{a.subject}_T2w.nii.gz"))
            prov["series"]["T2w"] = {"folder": os.path.basename(sd), "why": why,
                                     "qt2_capable": is_qt2_capable(series[os.path.basename(sd)]["tags"])}

    with open(os.path.join(anat, f"{a.subject}_recon-provenance.json"), "w") as fh:
        json.dump(prov, fh, indent=2)
    print(f"OK: {a.source} -> {anat}")
    for k, v in prov["series"].items():
        print(f"    {k}: {v}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
