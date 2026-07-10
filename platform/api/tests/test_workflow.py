"""Phase-1 verification: schema builds, recipe logic (tandem), full API workflow, audit chain."""
import os

os.environ["MELD7T_DB_URL"] = "sqlite:///./test_meld.db"
os.environ["MELD7T_AUDIT_REQUIRE_IMMUDB"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from app import models  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SeriesRole, Workup  # noqa: E402
from app.orthanc import propose_role  # noqa: E402
from app.recipe import build_recipe, recipe_summary  # noqa: E402


def setup_module(_m):
    if os.path.exists("test_meld.db"):
        os.remove("test_meld.db")
    SQLModel.metadata.create_all(engine)


def test_propose_role():
    assert propose_role("SAG T1 MP2RAGE UNI") == SeriesRole.t1_uni
    assert propose_role("SAG T1 MP2RAGE INV1") == SeriesRole.t1_inv1
    assert propose_role("AX_T1_MPRAGE") == SeriesRole.t1_mprage
    assert propose_role("SAG_DARKFLUID") == SeriesRole.flair
    assert propose_role("SAG_T2SPACE") == SeriesRole.t2


def test_recipe_tandem():
    entries = build_recipe(Workup.fcd, {"u1": "t1_uni", "m1": "t1_mprage"})
    meld = [e for e in entries if e["detector_id"] == "meld_fcd" and e["status"] == "created"]
    assert len(meld) == 2                      # tandem: MELD on both UNI and MPRAGE
    mapd = [e for e in entries if e["detector_id"] == "map" and e["status"] == "created"]
    assert len(mapd) == 2                      # MAP is built too → also tandem on both T1 sources
    s = recipe_summary(entries)
    assert s["will_run"] == 4 and s["tandem"] is True
    # the un-built HS detectors still surface as declared-pending slots in a full 'both' workup
    both = build_recipe(Workup.both, {"u1": "t1_uni"})
    assert any(e["detector_id"] in ("qt2", "aid_hs") and e["status"] == "pending" for e in both)


def test_full_workflow():
    c = TestClient(app)
    cid = c.post("/api/cases", json={"pseudonym": "P01"}).json()["id"]
    with Session(engine) as s:
        s.add(models.Series(case_id=cid, orthanc_series_uid="u1",
                            series_description="SAG T1 MP2RAGE UNI", proposed_role=SeriesRole.t1_uni))
        s.add(models.Series(case_id=cid, orthanc_series_uid="m1",
                            series_description="AX_T1_MPRAGE", proposed_role=SeriesRole.t1_mprage))
        s.commit()

    r = c.post(f"/api/cases/{cid}/series/confirm",
               json={"roles": {"u1": "t1_uni", "m1": "t1_mprage"}})
    assert r.status_code == 200, r.text

    r = c.post(f"/api/cases/{cid}/recipe", json={"workup": "fcd"})
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["will_run"] == 4     # MELD ×2 + MAP ×2 (both built, tandem)

    runs = c.post(f"/api/cases/{cid}/recipe/confirm").json()
    built = [x for x in runs if x["status"] == "queued"]   # confirm enqueues built runs as 'queued'
    assert len(built) == 4

    r = c.post(f"/api/runs/{built[0]['id']}/adjudication",
               json={"reviewer": "dr_x", "agree": True, "confidence": 4})
    assert r.status_code == 200

    v = c.get("/api/audit/verify").json()
    assert v["ok"] is True and v["count"] >= 4     # case+series+recipe+recipe.confirm+adjudication
