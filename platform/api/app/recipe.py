"""Recipe builder (spec §25.1 routing, §16 tandem). Pure logic — unit-testable without a DB.

From the confirmed series roles + chosen workup, propose the job list: one entry per
(detector × source series). For built detectors, tandem = every viable source present (e.g. MELD
on BOTH the UNI and the MPRAGE). Pending detectors get one entry marked `pending` against their
preferred available source, so the recipe SHOWS the full plan without running unbuilt code.
"""
from __future__ import annotations

import hashlib
import json

from .detectors import detectors_for
from .models import DetectorId, RunStatus, SeriesRole, Workup


_COMPANIONS = {
    # A cleaned MP2RAGE UNI cannot be reproduced without its two inversion images.
    ("meld_fcd", "t1_uni"): ("t1_inv1", "t1_inv2"),
    ("map", "t1_uni"): ("t1_inv1", "t1_inv2"),
    ("hippunfold", "t1_uni"): ("t1_inv1", "t1_inv2", "t2"),
    ("hippunfold", "t1_mprage"): ("t2",),
}

# These integrations have a defined, artifact-backed scanner/protocol correction contract.
# HippUnfold currently exposes no validated transform; its intrinsic L/R research comparison is
# retained with an explicit not-applicable provenance marker instead of inventing a profile.
_PROFILE_REQUIRED = frozenset({"meld_fcd", "map"})


def entry_id(detector_id: str, source_uid: str | None, params: dict) -> str:
    body = json.dumps({"detector_id": detector_id, "source_series_uid": source_uid,
                       "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:24]


def spec_hash(entries: list[dict]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":"),
                                         default=str).encode()).hexdigest()


def build_recipe(workup: Workup, confirmed_roles: dict[str, str], *,
                 harmonization: dict[tuple[str, str], dict] | None = None,
                 require_harmonization: bool = True,
                 unharmonized_reason: str | None = None) -> list[dict]:
    """Build a reproducible detector/source plan from explicitly confirmed series.

    ``harmonization`` maps ``(detector_id, source_series_uid)`` to a versioned profile contract.
    When harmonization is required, a missing assignment blocks that exact run. Researchers may
    explicitly confirm an unharmonized research run. A standard provenance reason is recorded
    when the operator does not add a note.
    """
    # invert: role -> [series_uid]
    by_role: dict[str, list[str]] = {}
    for uid, role in confirmed_roles.items():
        by_role.setdefault(role, []).append(uid)

    harmonization = harmonization or {}
    entries: list[dict] = []
    for det in detectors_for(workup):
        if det.status == "built":
            # tandem: run on EVERY viable source that is present
            ran_any = False
            for role in det.source_roles:
                for uid in by_role.get(role.value, []):
                    params = {"series_uids": {role.value: uid}}
                    missing, ambiguous = [], []
                    for companion in _COMPANIONS.get((det.id.value, role.value), ()):
                        candidates = by_role.get(companion, [])
                        if not candidates:
                            missing.append(companion)
                        elif len(candidates) > 1:
                            ambiguous.append({"role": companion, "series_uids": candidates})
                        else:
                            params["series_uids"][companion] = candidates[0]
                    profile = harmonization.get((det.id.value, uid))
                    if profile:
                        params["harmonization"] = profile
                    elif require_harmonization and det.id.value in _PROFILE_REQUIRED:
                        missing.append("harmonization_profile")
                    elif det.id.value not in _PROFILE_REQUIRED:
                        params["harmonization"] = {
                            "mode": "not_applicable",
                            "reason": "detector has no validated scanner harmonization interface",
                        }
                    else:
                        params["harmonization"] = {
                            "mode": "unharmonized",
                            "reason": (unharmonized_reason or
                                       "explicitly confirmed without a harmonization profile")}

                    status = RunStatus.created.value
                    notes = []
                    if missing or ambiguous:
                        status = RunStatus.blocked.value
                        if missing:
                            notes.append(f"missing required {missing}")
                        if ambiguous:
                            notes.append(f"ambiguous companions {ambiguous}")
                    entry = {
                        "detector_id": det.id.value,
                        "detector_label": det.label,
                        "source_role": role.value,
                        "source_series_uid": uid,
                        "status": status,
                        "params": params,
                    }
                    if notes:
                        entry["note"] = "; ".join(notes)
                    entry["entry_id"] = entry_id(det.id.value, uid, params)
                    entries.append(entry)
                    ran_any = True
            if not ran_any:
                entry = {
                    "detector_id": det.id.value, "detector_label": det.label,
                    "source_role": None, "source_series_uid": None,
                    "status": RunStatus.blocked.value,
                    "note": f"no source series present for {det.label} "
                            f"(needs one of {[r.value for r in det.source_roles]})",
                    "params": {},
                }
                entry["entry_id"] = entry_id(det.id.value, None, {})
                entries.append(entry)
        else:
            # pending detector: one slot against its best available source (may be None)
            src_uid, src_role = None, None
            for role in det.source_roles:
                uids = by_role.get(role.value, [])
                if uids:
                    src_uid, src_role = uids[0], role.value
                    break
            params = {}
            entry = {
                "detector_id": det.id.value, "detector_label": det.label,
                "source_role": src_role, "source_series_uid": src_uid,
                "status": RunStatus.pending.value,
                "params": params,
                "note": f"{det.label} not yet integrated ({det.method}) — "
                        f"declared slot (§25.7)",
            }
            entry["entry_id"] = entry_id(det.id.value, src_uid, params)
            entries.append(entry)
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
        "harmonized": sum(1 for e in built if e.get("params", {}).get("harmonization", {})
                           .get("profile_id")),
        "unharmonized": sum(1 for e in built if e.get("params", {}).get("harmonization", {})
                             .get("mode") == "unharmonized"),
        "tandem": len({e["detector_id"] for e in built}) < len(built),  # >1 source for a detector
    }
