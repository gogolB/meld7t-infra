"""Parse MELD outputs into Result + Cluster fields (spec §8). Pure, testable against the real
predictions_reports/<subject>/ tree that new_pt_pipeline produces."""
from __future__ import annotations

import csv
import os

# columns in info_clusters_<subject>.csv that identify a cluster (rest are feature stats)
_CORE = {"cluster", "size", "hemi", "location", "confidence"}


def meld_output_dir(meld_data: str, subject: str) -> str:
    return os.path.join(meld_data, "output", "predictions_reports", subject)


def parse_clusters(meld_data: str, subject: str) -> list[dict]:
    """Return one dict per predicted cluster (index/hemi/location/size/confidence + saliency)."""
    csv_path = os.path.join(meld_output_dir(meld_data, subject), "reports",
                            f"info_clusters_{subject}.csv")
    if not os.path.exists(csv_path):
        return []
    clusters = []
    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            if not row.get("cluster"):
                continue
            saliency = {k.rsplit(" saliency", 1)[0]: _f(v)
                        for k, v in row.items() if k and k.endswith("saliency")}
            clusters.append({
                "index": int(float(row["cluster"])),
                "hemi": row.get("hemi"),
                "location": row.get("location"),
                "size": _f(row.get("size")),
                "confidence": _f(row.get("confidence")),
                "saliency": saliency,
            })
    return clusters


def result_fields(meld_data: str, subject: str) -> dict:
    """Non-DICOM result fields. report_path is stored RELATIVE to the meld-data root so the api
    (which mounts meld-data at its own path) can resolve it regardless of absolute layout."""
    rel = os.path.join("output", "predictions_reports", subject, "reports",
                       f"MELD_report_{subject}.pdf")
    exists = os.path.exists(os.path.join(meld_data, rel))
    return {"report_path": rel if exists else None,
            "n_clusters": len(parse_clusters(meld_data, subject))}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
