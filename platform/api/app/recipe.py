"""Recipe builder (spec §25.1 routing, §16 tandem). Pure logic — unit-testable without a DB.

From the confirmed series roles + chosen workup, propose the job list: one entry per
(detector × source series). For built detectors, tandem = every viable source present (e.g. MELD
on BOTH the UNI and the MPRAGE). Pending detectors get one entry marked `pending` against their
preferred available source, so the recipe SHOWS the full plan without running unbuilt code.
"""
from __future__ import annotations

from .detectors import detectors_for
from .models import DetectorId, RunStatus, SeriesRole, Workup


def build_recipe(workup: Workup, confirmed_roles: dict[str, str]) -> list[dict]:
    """`confirmed_roles` maps series_uid -> role string. Returns a list of recipe entries."""
    # invert: role -> [series_uid]
    by_role: dict[str, list[str]] = {}
    for uid, role in confirmed_roles.items():
        by_role.setdefault(role, []).append(uid)

    entries: list[dict] = []
    for det in detectors_for(workup):
        if det.status == "built":
            # tandem: run on EVERY viable source that is present
            ran_any = False
            for role in det.source_roles:
                for uid in by_role.get(role.value, []):
                    entries.append({
                        "detector_id": det.id.value,
                        "detector_label": det.label,
                        "source_role": role.value,
                        "source_series_uid": uid,
                        "status": RunStatus.created.value,
                        "params": {},
                    })
                    ran_any = True
            if not ran_any:
                entries.append({
                    "detector_id": det.id.value, "detector_label": det.label,
                    "source_role": None, "source_series_uid": None,
                    "status": RunStatus.blocked.value,
                    "note": f"no source series present for {det.label} "
                            f"(needs one of {[r.value for r in det.source_roles]})",
                })
        else:
            # pending detector: one slot against its best available source (may be None)
            src_uid, src_role = None, None
            for role in det.source_roles:
                uids = by_role.get(role.value, [])
                if uids:
                    src_uid, src_role = uids[0], role.value
                    break
            entries.append({
                "detector_id": det.id.value, "detector_label": det.label,
                "source_role": src_role, "source_series_uid": src_uid,
                "status": RunStatus.pending.value,
                "note": f"{det.label} not yet integrated ({det.method}) — "
                        f"declared slot (§25.7)",
            })
    return entries


def recipe_summary(entries: list[dict]) -> dict:
    built = [e for e in entries if e["status"] == RunStatus.created.value]
    pending = [e for e in entries if e["status"] == RunStatus.pending.value]
    blocked = [e for e in entries if e["status"] == RunStatus.blocked.value]
    return {
        "total": len(entries),
        "will_run": len(built),
        "pending": len(pending),
        "blocked": len(blocked),
        "tandem": len({e["detector_id"] for e in built}) < len(built),  # >1 source for a detector
    }
