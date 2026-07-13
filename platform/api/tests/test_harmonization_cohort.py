"""Offline harmonization cohort-manifest validation tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.harmonization import profile_document_sha256
from app.models import HarmonizationProfile


SCRIPT = Path(__file__).resolve().parents[3] / "ops" / "harmonization" / "manage.py"
SPEC = importlib.util.spec_from_file_location("harmonization_manage", SCRIPT)
manage = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(manage)


def _cohort(tmp_path: Path, count: int = 20, *, varied: bool = True):
    subjects = tmp_path / "subjects.txt"
    demographics = tmp_path / "demographics.csv"
    ids = [f"sub-{index:03d}" for index in range(count)]
    subjects.write_text("\n".join(ids) + "\n")
    rows = ["ID,Age,Sex"]
    for index, subject in enumerate(ids):
        age = 20 + index if varied else 30
        sex = "female" if varied and index % 2 else "male"
        rows.append(f"{subject},{age},{sex}")
    demographics.write_text("\n".join(rows) + "\n")
    return subjects, demographics


def test_activation_eligible_cohort_is_hash_only(tmp_path):
    subjects, demographics = _cohort(tmp_path)
    result = manage.load_cohort(subjects, demographics, b"k" * 32)
    assert result["subject_count"] == 20
    assert result["age_has_variance"] and result["sex_has_variance"]
    assert "sub-000" not in repr(result)
    assert len(result["subjects_hmac_sha256"]) == 64


def test_cohort_rejects_duplicates_missing_rows_and_zero_variance(tmp_path):
    subjects, demographics = _cohort(tmp_path, varied=False)
    result = manage.load_cohort(subjects, demographics, b"k" * 32)
    assert result["age_has_variance"] is False
    assert result["sex_has_variance"] is False

    subjects.write_text(subjects.read_text() + "sub-000\n")
    with pytest.raises(ValueError, match="duplicates"):
        manage.load_cohort(subjects, demographics, b"k" * 32)


def test_meld_profile_builder_never_allows_fewer_than_twenty_controls(tmp_path):
    subjects, demographics = _cohort(tmp_path)
    selector = tmp_path / "selector.json"
    selector.write_text('{"acquisition":{"model":"terra"}}')
    evidence_key = tmp_path / "evidence.key"
    evidence_key.write_bytes(b"k" * 32)
    with pytest.raises(ValueError, match="at least 20"):
        manage.prepare(SimpleNamespace(
            code="HSITE", version=1, name="site", subjects=subjects,
            demographics=demographics, selector=selector,
            image="example/meld@sha256:" + "a" * 64,
            output=tmp_path / "unsafe-build", minimum_subjects=19,
            allow_ineligible_draft=False, evidence_hmac_key_file=evidence_key,
        ))


def test_meld_finalize_rejects_selector_changed_after_cohort_preparation(tmp_path):
    subjects, demographics = _cohort(tmp_path)
    selector = tmp_path / "selector.json"
    selector.write_text('{"acquisition":{"model":"terra"}}')
    build = tmp_path / "build"
    evidence_key = tmp_path / "evidence.key"
    evidence_key.write_bytes(b"k" * 32)
    manage.prepare(SimpleNamespace(
        code="HSITE", version=1, name="site", subjects=subjects,
        demographics=demographics, selector=selector,
        image="example/meld@sha256:" + "a" * 64, output=build,
        minimum_subjects=20, allow_ineligible_draft=False,
        evidence_hmac_key_file=evidence_key,
    ))
    draft_path = build / "profile-draft.json"
    draft = json.loads(draft_path.read_text())
    draft["selector"] = {"acquisition": {"model": "different-scanner"}}
    draft_path.write_text(json.dumps(draft))
    artifacts = tmp_path / "meld-output"
    artifacts.mkdir()
    (artifacts / "MELD_HSITEcombat_parameters.hdf5").write_bytes(b"combat")
    with pytest.raises(ValueError, match="selector changed"):
        manage.finalize(SimpleNamespace(
            profile_draft=draft_path, artifact_source=artifacts,
            output=tmp_path / "final-artifacts", manifest_prefix="artifacts/HSITE/v1",
            final_profile=tmp_path / "profiles" / "HSITE-v1.json",
        ))


def test_expected_inventory_binds_exact_profile_document(tmp_path):
    profile = {
        "code": "HSITE", "version": 1, "name": "site",
        "method": "meld_distributed_combat", "detector_id": "meld_fcd",
        "selector": {"roles": ["t1_uni"], "acquisition": {"model": "terra"}},
        "artifact_manifest": {"schema_version": 1, "files": [{
            "path": "artifacts/HSITE/v1/params.hdf5", "sha256": "a" * 64,
        }]},
        "parameters": {"data_root": "artifacts/HSITE/v1"},
    }
    source = tmp_path / "HSITE-v1.json"
    source.write_text(json.dumps(profile))
    destination = tmp_path / "expected-profiles.json"
    assert manage.build_expected_inventory(SimpleNamespace(
        profile=[source], output=destination)) == 0
    inventory = json.loads(destination.read_text())
    model = HarmonizationProfile(**profile, created_by="test")
    assert inventory == [{
        "code": "HSITE", "version": 1, "detector_id": "meld_fcd",
        "document_sha256": profile_document_sha256(model),
    }]


def test_empty_expected_inventory_requires_explicit_bootstrap_flag(tmp_path):
    blocked = tmp_path / "blocked.json"
    with pytest.raises(ValueError, match="at least one"):
        manage.build_expected_inventory(SimpleNamespace(
            profile=None, output=blocked, allow_empty_bootstrap=False))
    allowed = tmp_path / "bootstrap.json"
    assert manage.build_expected_inventory(SimpleNamespace(
        profile=None, output=allowed, allow_empty_bootstrap=True)) == 0
    assert json.loads(allowed.read_text()) == []
    assert manage._read_inventory(allowed) == []


def test_profile_build_images_must_equal_release_image_lock(tmp_path):
    meld = "example/meld@sha256:" + "a" * 64
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "method": "meld_distributed_combat",
        "parameters": {"build_images": {"meld": meld}},
    }))
    image_lock = tmp_path / "images.lock"
    image_lock.write_text(f"meld_graph {meld}\n")
    assert manage.verify_runtime_images(SimpleNamespace(
        profile=profile, image_lock=image_lock)) == 0
    changed = json.loads(profile.read_text())
    changed["parameters"]["build_images"]["meld"] = (
        "example/meld@sha256:" + "b" * 64
    )
    profile.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="differ from the release"):
        manage.verify_runtime_images(SimpleNamespace(
            profile=profile, image_lock=image_lock))
