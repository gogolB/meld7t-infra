#!/usr/bin/env python3
"""Prepare a MELD BIDS T1w input from raw DICOM (spec §2.2, §6, §16).

Runs inside the pkg container (--network=none). Discovers the source series by folder-name
pattern, converts with dcm2niix, background-cleans the UNI (O'Brien) when source=uni, and writes
    <out>/sub-<id>/anat/sub-<id>_T1w.nii.gz
plus a provenance sidecar recording exactly which series were used.

Series selection is propose-then-FAIL-loud, never silent-auto (§16): if a role matches zero or
(ambiguously) multiple series, it errors and lists the candidates — mis-selection corrupts every
downstream result invisibly. Override any role with --<role>-series "<exact folder name>".
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

DEFAULT_PATTERNS = {
    "uni": r"uni",
    "inv1": r"inv1",
    "inv2": r"inv2",
    # this site emits "t1 mprage" and "t1+orig"; both are conventional MPRAGE (§16).
    "mprage": r"mprage|t1\+orig|orig",
}


def find_series_dirs(root):
    """Return {folder_name: abspath} for every dir that directly contains .dcm files."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        if any(f.lower().endswith(".dcm") for f in files):
            out[os.path.basename(dirpath)] = dirpath
    return out


def pick(role, pattern, series, override):
    if override:
        if override not in series:
            sys.exit(f"ERROR: --{role}-series '{override}' not found under DICOM root. "
                     f"Available: {sorted(series)}")
        return series[override]
    rx = re.compile(pattern, re.IGNORECASE)
    matches = sorted(n for n in series if rx.search(n))
    if not matches:
        sys.exit(f"ERROR: no series matched role '{role}' (pattern /{pattern}/). "
                 f"Available: {sorted(series)}. Pass --{role}-series to select explicitly.")
    if len(matches) > 1:
        # one deterministic tiebreak: for MPRAGE prefer the plain 'orig' magnitude.
        if role == "mprage":
            orig = [m for m in matches if "orig" in m.lower()]
            if len(orig) == 1:
                return series[orig[0]]
        sys.exit(f"ERROR: role '{role}' is AMBIGUOUS — matched {matches}. "
                 f"Pass --{role}-series to disambiguate (§16: no silent guessing).")
    return series[matches[0]]


def dcm2niix_largest(series_dir, workdir, tag):
    """Convert a series; return the largest produced .nii.gz (handles multi-file series)."""
    sub = os.path.join(workdir, tag)
    os.makedirs(sub, exist_ok=True)
    subprocess.run(["dcm2niix", "-o", sub, "-f", "%d_%s", "-z", "y", series_dir],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    niis = [os.path.join(sub, f) for f in os.listdir(sub) if f.endswith(".nii.gz")]
    if not niis:
        sys.exit(f"ERROR: dcm2niix produced no NIfTI for {series_dir}")
    return max(niis, key=os.path.getsize)


def main():
    ap = argparse.ArgumentParser(description="Prepare MELD BIDS T1w input from DICOM")
    ap.add_argument("--dicom-root", required=True)
    ap.add_argument("--subject", required=True, help="e.g. sub-s01uni (BIDS label)")
    ap.add_argument("--source", choices=["uni", "mprage"], required=True)
    ap.add_argument("--out", required=True, help="BIDS input root (MELD's /data/input)")
    ap.add_argument("--beta-factor", type=float, default=6.0)
    for role in ("uni", "inv1", "inv2", "mprage"):
        ap.add_argument(f"--{role}-series", default=None, help=f"exact folder name for {role}")
    a = ap.parse_args()

    series = find_series_dirs(a.dicom_root)
    if not series:
        sys.exit(f"ERROR: no DICOM series (dirs with .dcm files) under {a.dicom_root}")

    anat = os.path.join(a.out, a.subject, "anat")
    os.makedirs(anat, exist_ok=True)
    t1w = os.path.join(anat, f"{a.subject}_T1w.nii.gz")

    # dcm2niix exits non-zero on --version (by design) but still prints it → don't check.
    _v = subprocess.run(["dcm2niix", "--version"], capture_output=True, text=True)
    prov = {"source": a.source, "subject": a.subject,
            "dcm2niix": (_v.stdout + _v.stderr).strip().splitlines()[-1]}

    with tempfile.TemporaryDirectory() as work:
        if a.source == "mprage":
            sd = pick("mprage", DEFAULT_PATTERNS["mprage"], series, a.mprage_series)
            nii = dcm2niix_largest(sd, work, "mprage")
            shutil.copyfile(nii, t1w)
            prov["series"] = {"mprage": os.path.basename(sd)}
        else:  # uni
            sds = {r: pick(r, DEFAULT_PATTERNS[r], series, getattr(a, f"{r}_series"))
                   for r in ("uni", "inv1", "inv2")}
            niis = {r: dcm2niix_largest(sd, work, r) for r, sd in sds.items()}
            from clean_uni import obrien_clean
            info = obrien_clean(niis["uni"], niis["inv1"], niis["inv2"], t1w, a.beta_factor)
            prov["series"] = {r: os.path.basename(sd) for r, sd in sds.items()}
            prov["obrien"] = {"beta_factor": a.beta_factor, "beta": info["beta"]}

    with open(os.path.join(anat, f"{a.subject}_recon-provenance.json"), "w") as fh:
        json.dump(prov, fh, indent=2)

    print(f"OK: {a.source} -> {t1w}")
    print(f"    series: {prov['series']}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
