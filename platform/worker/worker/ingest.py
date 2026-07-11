"""Parse MELD outputs into Result + Cluster fields (spec §8). Pure, testable against the real
predictions_reports/<subject>/ tree that new_pt_pipeline produces."""
from __future__ import annotations

import csv
import math
import os

# columns in info_clusters_<subject>.csv that identify a cluster (rest are feature stats)
_CORE = {"cluster", "size", "hemi", "location", "confidence"}


def meld_output_dir(meld_data: str, subject: str) -> str:
    return os.path.join(meld_data, "output", "predictions_reports", subject)


def parse_clusters(meld_data: str, subject: str) -> list[dict]:
    """Return one dict per predicted cluster (index/hemi/location/size/confidence + saliency)."""
    csv_path = os.path.join(meld_output_dir(meld_data, subject), "reports",
                            f"info_clusters_{subject}.csv")
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"MELD cluster CSV is missing: {csv_path}")
    clusters = []
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "cluster" not in reader.fieldnames:
            raise ValueError(f"MELD cluster CSV has no cluster column: {csv_path}")
        for line_no, row in enumerate(reader, start=2):
            if not row.get("cluster"):
                continue
            try:
                raw_index = float(row["cluster"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid MELD cluster index on CSV line {line_no}") from exc
            if not math.isfinite(raw_index) or not raw_index.is_integer() or raw_index < 1:
                raise ValueError(f"invalid MELD cluster index on CSV line {line_no}")
            index = int(raw_index)
            saliency = {k.rsplit(" saliency", 1)[0]: _optional_float(v)
                        for k, v in row.items() if k and k.endswith("saliency")}
            clusters.append({
                "index": index,
                "hemi": row.get("hemi"),
                "location": row.get("location"),
                "size": _optional_float(row.get("size")),
                "confidence": _optional_float(row.get("confidence")),
                "saliency": saliency,
            })
    return clusters


def result_fields(meld_data: str, subject: str, clusters: list[dict] | None = None) -> dict:
    """Non-DICOM result fields. report_path is stored RELATIVE to the meld-data root so the api
    (which mounts meld-data at its own path) can resolve it regardless of absolute layout."""
    rel = os.path.join("output", "predictions_reports", subject, "reports",
                       f"MELD_report_{subject}.pdf")
    report = os.path.join(meld_data, rel)
    if not os.path.isfile(report) or os.path.getsize(report) == 0:
        raise FileNotFoundError(f"MELD report is missing or empty: {report}")
    return {"report_path": rel,
            "n_clusters": len(parse_clusters(meld_data, subject) if clusters is None else clusters)}


def _optional_float(v):
    if v in (None, ""):
        return None
    try:
        value = float(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric MELD output: {v!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"non-finite numeric MELD output: {v!r}")
    return value
