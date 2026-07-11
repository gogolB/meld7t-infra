"""Shared local-scratch admission and readiness policy."""
from __future__ import annotations

import shutil


def storage_health(path: str, *, minimum_free_bytes: int,
                   minimum_free_percent: float) -> dict[str, object]:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return {"ready": False, "status": f"unavailable:{type(exc).__name__}"}
    percent = (100.0 * usage.free / usage.total) if usage.total else 0.0
    ready = usage.free >= minimum_free_bytes and percent >= minimum_free_percent
    return {
        "ready": ready,
        "status": "healthy" if ready else "below_admission_watermark",
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "free_percent": round(percent, 2),
        "minimum_free_bytes": minimum_free_bytes,
        "minimum_free_percent": minimum_free_percent,
    }
