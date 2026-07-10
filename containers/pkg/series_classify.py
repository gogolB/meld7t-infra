"""Header-based MRI series classifier (spec §16).

Classifies a series by ACQUISITION PARAMETERS (ScanningSequence / ImageType / TR / TE / TI /
echo count) rather than SeriesDescription strings — robust to anonymized or blank descriptions
(this site's are scrubbed to "DummySeriesDesc!"). Returns a role + a human-readable reason that
goes into provenance, so the confirmed source is header-justified (§16).

Roles: t1_uni, t1_inv1, t1_inv2, t1_mprage, flair, t2, unknown.
"""
from __future__ import annotations


def classify(tags: dict) -> tuple[str, str]:
    seq = [str(s).upper() for s in (tags.get("scanning_sequence") or [])]
    itype = [str(s).upper() for s in (tags.get("image_type") or [])]
    tr = float(tags.get("tr") or 0)
    te = float(tags.get("te") or 0)
    ti = tags.get("ti")
    ti = float(ti) if ti not in (None, "") else None
    n_echoes = int(tags.get("n_echoes") or 1)
    gr, ir, se = "GR" in seq, "IR" in seq, "SE" in seq

    # MP2RAGE / MPRAGE — gradient-echo inversion recovery (TFL).
    if gr and ir:
        if "UNI" in itype:
            return "t1_uni", "GR/IR gradient-echo, ImageType=UNI → MP2RAGE UNI"
        if tr >= 5000:                       # MP2RAGE inversions share a long TR (~6000)
            if ti is not None and ti < 1500:
                return "t1_inv1", f"MP2RAGE inversion, short TI={ti:.0f}ms → INV1"
            if ti is not None:
                return "t1_inv2", f"MP2RAGE inversion, long TI={ti:.0f}ms → INV2"
            return "unknown", "GR/IR long-TR but no TI/UNI tag"
        return "t1_mprage", f"GR/IR TFL, TR={tr:.0f}ms → conventional MPRAGE"

    # T2-FLAIR — spin-echo inversion recovery, long TE.
    if se and ir and te > 150:
        return "flair", f"SE/IR, long TE={te:.0f}ms, TI={ti} → T2-FLAIR"

    # T2 — spin-echo, long TE, no inversion.
    if se and te > 80:
        if n_echoes > 1:
            return "t2", f"multi-echo T2 ({n_echoes} echoes, TE~{te:.0f}) → qT2-capable"
        return "t2", f"SE, TE={te:.0f}ms, single-echo → T2-weighted"

    return "unknown", f"no rule matched (seq={seq}, TE={te:.0f}, TR={tr:.0f})"


def is_qt2_capable(tags: dict) -> bool:
    """A quantitative T2 map needs multiple echoes to fit the decay."""
    return int(tags.get("n_echoes") or 1) > 1
