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
import math
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

    # glob.escape the dir: anonymized exports often bracket IDs/descriptions (e.g.
    # "Series 008 [MR - AX T1 mprage Pre]"), and unescaped [ ] are glob character classes → no match.
    files = sorted(glob.glob(os.path.join(glob.escape(series_dir), "*.dcm")))
    if not files:      # defensive: discover() already gates on .dcm presence
        return {"scanning_sequence": [], "image_type": [], "tr": 0.0, "te": 0.0,
                "ti": None, "n_echoes": 1, "description": ""}
    ds = pydicom.dcmread(files[0], stop_before_pixels=True)
    series_uid = str(ds.get("SeriesInstanceUID", "") or "")
    study_uid = str(ds.get("StudyInstanceUID", "") or "")
    patient = (str(ds.get("PatientID", "") or ""),
               str(ds.get("IssuerOfPatientID", "") or ""),
               str(ds.get("PatientName", "") or ""))
    if not series_uid or not study_uid:
        raise ValueError(f"DICOM series is missing Study/SeriesInstanceUID: {series_dir}")
    echoes = set()
    pixel_scalings = set()
    for f in files:
        try:
            d = pydicom.dcmread(
                f, stop_before_pixels=True,
                specific_tags=["EchoTime", "StudyInstanceUID", "SeriesInstanceUID", "PatientID",
                               "IssuerOfPatientID", "PatientName", "RescaleSlope",
                               "RescaleIntercept", "BitsStored", "PixelRepresentation"],
            )
            identity = (str(d.get("PatientID", "") or ""),
                        str(d.get("IssuerOfPatientID", "") or ""),
                        str(d.get("PatientName", "") or ""))
            if str(d.get("SeriesInstanceUID", "")) != series_uid:
                raise ValueError(f"directory mixes SeriesInstanceUIDs: {series_dir}")
            if str(d.get("StudyInstanceUID", "")) != study_uid or identity != patient:
                raise ValueError(f"directory mixes study/patient identities: {series_dir}")
            if "EchoTime" in d and d.EchoTime not in (None, ""):
                echoes.add(round(float(d.EchoTime), 2))
            pixel_scalings.add((
                float(d.get("RescaleSlope", 1) or 1),
                float(d.get("RescaleIntercept", 0) or 0),
                int(d.get("BitsStored", 0) or 0),
                int(d.get("PixelRepresentation", 0) or 0),
            ))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"could not validate DICOM header {f}: {exc}") from exc
    if len(pixel_scalings) != 1:
        raise ValueError(f"series contains inconsistent DICOM pixel scaling: {series_dir}")
    slope, intercept, bits_stored, pixel_representation = next(iter(pixel_scalings))
    if (not math.isfinite(slope) or not math.isfinite(intercept) or slope == 0
            or bits_stored < 1 or bits_stored > 32 or pixel_representation not in {0, 1}):
        raise ValueError(f"series contains invalid DICOM pixel scaling: {series_dir}")
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
        "study_uid": study_uid,
        "series_uid": series_uid,
        "instance_count": len(files),
        "pixel_scaling": {
            "rescale_slope": slope,
            "rescale_intercept": intercept,
            "bits_stored": bits_stored,
            "pixel_representation": pixel_representation,
        },
    }


def discover(root: str) -> dict:
    """Classify every series under root by header. Returns {name: {path, role, reason, tags}}."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        if not any(f.lower().endswith(".dcm") for f in files):
            continue
        tags = read_series_tags(dirpath)
        role, reason = classify(tags)
        uid = tags["series_uid"]
        if uid in out:
            raise ValueError(f"SeriesInstanceUID {uid} appears in more than one directory")
        out[uid] = {"path": dirpath, "folder": os.path.basename(dirpath), "role": role,
                    "reason": reason, "tags": tags}
    return out


def select(role: str, series: dict, override: str | None,
           override_uid: str | None = None) -> tuple[str, str]:
    """Return (series_dir, reason) for a role. Header first, then name fallback, else fail loud."""
    if override_uid:
        if override_uid not in series:
            sys.exit(f"ERROR: confirmed SeriesInstanceUID '{override_uid}' not found. "
                     f"Available UIDs: {sorted(series)}")
        return series[override_uid]["path"], f"confirmed SeriesInstanceUID → {override_uid}"
    if override:
        hits = [s for s in series.values() if s["folder"] == override]
        if len(hits) != 1:
            sys.exit(f"ERROR: --{role}-series '{override}' did not resolve exactly once")
        return hits[0]["path"], f"operator folder override → {override}"

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
    named = sorted(uid for uid, s in series.items() if rx.search(s["folder"]))
    if len(named) == 1:
        return series[named[0]]["path"], f"name fallback /{rx.pattern}/ (header inconclusive)"

    sys.exit(f"ERROR: could not identify role '{role}' by header or name. "
             f"Series: { {n: s['role'] for n, s in series.items()} }. Pass --{role}-series.")


def dcm2niix_single(series_dir, workdir, tag):
    sub = os.path.join(workdir, tag)
    os.makedirs(sub, exist_ok=True)
    subprocess.run(["dcm2niix", "-o", sub, "-f", "%d_%s", "-z", "y", series_dir],
                   check=True)
    niis = [os.path.join(sub, f) for f in os.listdir(sub) if f.endswith(".nii.gz")]
    if not niis:
        sys.exit(f"ERROR: dcm2niix produced no NIfTI for {series_dir}")
    if len(niis) != 1:
        sys.exit(
            f"ERROR: one confirmed DICOM series produced {len(niis)} NIfTI files; "
            "refusing to choose silently"
        )
    return niis[0]


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
        ap.add_argument(f"--{role}-series-uid", default=None,
                        help=f"exact confirmed SeriesInstanceUID for {role}")
    a = ap.parse_args()
    if re.fullmatch(r"sub-[A-Za-z0-9][A-Za-z0-9_-]{0,79}", a.subject) is None:
        raise ValueError("--subject must be one safe BIDS subject identifier")
    if not math.isfinite(a.beta_factor) or a.beta_factor <= 0:
        raise ValueError("--beta-factor must be finite and positive")

    series = discover(a.dicom_root)
    if not series:
        sys.exit(f"ERROR: no DICOM series under {a.dicom_root}")

    subject_root = os.path.join(a.out, a.subject)
    if os.path.exists(subject_root):
        shutil.rmtree(subject_root)
    anat = os.path.join(subject_root, "anat")
    os.makedirs(anat, exist_ok=True)
    _v = subprocess.run(["dcm2niix", "--version"], capture_output=True, text=True, check=True)
    version_lines = (_v.stdout + _v.stderr).strip().splitlines()
    if not version_lines:
        raise RuntimeError("dcm2niix returned no version string")
    prov = {"source": a.source, "subject": a.subject,
            "dcm2niix": version_lines[-1],
            "classified": {n: {"role": s["role"], "reason": s["reason"]} for n, s in series.items()},
            "series": {}}

    with tempfile.TemporaryDirectory() as work:
        if a.source == "mprage":
            sd, why = select("t1_mprage", series, a.mprage_series, a.mprage_series_uid)
            shutil.copyfile(dcm2niix_single(sd, work, "mprage"),
                            os.path.join(anat, f"{a.subject}_T1w.nii.gz"))
            chosen = read_series_tags(sd)
            prov["series"]["T1w"] = {"folder": os.path.basename(sd),
                                         "series_uid": chosen["series_uid"],
                                         "pixel_scaling": chosen["pixel_scaling"],
                                         "why": why}
        else:  # uni → O'Brien clean from UNI + INV1 + INV2
            picks = {r: select(f"t1_{r}", series, getattr(a, f"{r}_series"),
                               getattr(a, f"{r}_series_uid"))
                     for r in ("uni", "inv1", "inv2")}
            niis = {r: dcm2niix_single(p[0], work, r) for r, p in picks.items()}
            from clean_uni import obrien_clean
            info = obrien_clean(niis["uni"], niis["inv1"], niis["inv2"],
                                os.path.join(anat, f"{a.subject}_T1w.nii.gz"), a.beta_factor)
            prov["series"]["T1w"] = {r: {"folder": os.path.basename(p[0]),
                                                "series_uid": read_series_tags(p[0])["series_uid"],
                                                "pixel_scaling": read_series_tags(p[0])[
                                                    "pixel_scaling"],
                                                "why": p[1]}
                                     for r, p in picks.items()}
            prov["obrien"] = {"beta_factor": a.beta_factor, "beta": info["beta"]}

        if a.also_t2:
            sd, why = select("t2", series, a.t2_series, a.t2_series_uid)
            shutil.copyfile(dcm2niix_single(sd, work, "t2"),
                            os.path.join(anat, f"{a.subject}_T2w.nii.gz"))
            prov["series"]["T2w"] = {"folder": os.path.basename(sd), "why": why,
                                     "series_uid": read_series_tags(sd)["series_uid"],
                                     "qt2_capable": is_qt2_capable(
                                         series[read_series_tags(sd)["series_uid"]]["tags"])}

    with open(os.path.join(anat, f"{a.subject}_recon-provenance.json"), "w") as fh:
        json.dump(prov, fh, indent=2)
    print(f"OK: {a.source} -> {anat}")
    for k, v in prov["series"].items():
        print(f"    {k}: {v}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
