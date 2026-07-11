"""Pure harmonization/recipe contract tests (no service dependencies)."""
from types import SimpleNamespace

import pytest

from app.harmonization import (
    acquisition_fingerprint,
    canonical_json_sha256,
    canonical_acquisition,
    match_selector,
    rank_profiles,
    selectors_may_overlap,
    sha256_file,
    validate_profile_semantics,
    validate_selector,
    verify_artifact_manifest,
)
from app.models import Workup
from app.recipe import build_recipe, spec_hash


def _validation(code, version, detector, images):
    return {
        "schema_version": 1,
        "profile": {"code": code, "version": version, "detector_id": detector},
        "approval_id": "SITE-VALIDATION-001",
        "independent_reviewer": "reviewer@example.test",
        "approved_at": "2026-07-10T12:00:00Z",
        "acquisition_fingerprints": ["e" * 64],
        "qc": {"included": 20, "excluded": 1},
        "holdout": {
            "case_count": 3, "positive_cases": 1,
            "negative_cases": 1, "control_cases": 1,
        },
        "metrics_sha256": "f" * 64,
        "golden_case_evidence_sha256": "1" * 64,
        "methodology_sha256": "2" * 64,
        "image_digests": images,
    }


def test_fingerprint_is_stable_normalized_and_excludes_direct_patient_tags():
    first = {
        "manufacturer": " Siemens   Healthineers ",
        "model": "MAGNETOM Terra",
        "field_strength_t": 7.00000001,
        "scanning_sequence": ["IR", "GR"],
        "patient_name": "must-not-enter-fingerprint",
    }
    second = {
        "manufacturer": "siemens healthineers",
        "model": "magnetom terra",
        "field_strength_t": 7.0,
        "scanning_sequence": ["GR", "IR"],
        "patient_name": "different person",
    }
    assert acquisition_fingerprint(first) == acquisition_fingerprint(second)
    assert "patient_name" not in canonical_acquisition(first)


def test_selector_ranking_supports_scanner_protocol_ranges():
    acquisition = {
        "manufacturer": "Siemens Healthineers",
        "model": "MAGNETOM Terra",
        "field_strength_t": 7.0,
        "protocol_name": "research MP2RAGE 0.7mm",
    }
    exact = SimpleNamespace(
        id="exact", detector_id="meld_fcd",
        selector={"roles": ["t1_uni"], "acquisition": {
            "manufacturer": {"eq": "siemens healthineers"},
            "model": {"in": ["magnetom terra"]},
            "field_strength_t": {"target": 7, "tolerance": 0.2},
            "protocol_name": {"regex": "mp2rage"},
        }},
    )
    generic = SimpleNamespace(
        id="generic", detector_id=None,
        selector={"acquisition": {"field_strength_t": {"min": 6.5, "max": 7.5}}},
    )
    matches = rank_profiles([generic, exact], acquisition, role="t1_uni",
                            detector_id="meld_fcd")
    assert [m.profile_id for m in matches] == ["exact", "generic"]
    assert all(m.matched for m in matches)
    assert not match_selector("bad", exact.selector, acquisition, role="flair").matched


def test_selector_validation_rejects_unknown_or_ambiguous_rules():
    with pytest.raises(ValueError, match="unsupported selector key"):
        validate_selector({"acquisition": {"patient_name": "never"}})
    with pytest.raises(ValueError, match="exactly one comparison mode"):
        validate_selector({"acquisition": {"field_strength_t": {"eq": 7, "min": 6.5}}})
    with pytest.raises(ValueError, match="invalid regex"):
        validate_selector({"acquisition": {"protocol_name": {"regex": "["}}})
    with pytest.raises(ValueError, match="unsupported role"):
        validate_selector({"roles": ["t1_typo"], "acquisition": {"field_strength_t": 7}})


def test_selector_overlap_is_conservative_but_proves_disjoint_protocols():
    terra = {"roles": ["t1_uni"], "acquisition": {
        "model": {"eq": "terra"},
        "field_strength_t": {"target": 7.0, "tolerance": 0.2},
    }}
    prisma = {"roles": ["t1_uni"], "acquisition": {
        "model": {"eq": "prisma"},
        "field_strength_t": {"target": 3.0, "tolerance": 0.2},
    }}
    broad_regex = {"roles": ["t1_uni"], "acquisition": {
        "protocol_name": {"regex": "mp2rage"},
    }}
    assert selectors_may_overlap(terra, prisma) is False
    assert selectors_may_overlap(terra, broad_regex) is True


def test_artifact_manifest_requires_containment_and_hash(tmp_path):
    artifact = tmp_path / "combat.json"
    artifact.write_text('{"batch":"scanner-protocol"}')
    manifest = {"files": [{"path": artifact.name, "sha256": sha256_file(artifact),
                            "size": artifact.stat().st_size}]}
    verified = verify_artifact_manifest(manifest, tmp_path)
    assert verified["files"][0]["path"] == "combat.json"
    assert len(verified["manifest_sha256"]) == 64
    with pytest.raises(ValueError, match="relative"):
        verify_artifact_manifest({"files": [{"path": "../outside", "sha256": "0" * 64}]},
                                 tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(artifact)
    with pytest.raises(ValueError, match="symlink"):
        verify_artifact_manifest(
            {"files": [{"path": link.name, "sha256": sha256_file(artifact)}]}, tmp_path)


def test_recipe_blocks_missing_companions_and_harmonization():
    roles = {"uni": "t1_uni", "inv1": "t1_inv1", "inv2": "t1_inv2"}
    entries = build_recipe(Workup.fcd, roles)
    built = [e for e in entries if e["detector_id"] in {"meld_fcd", "map"}]
    assert built and all(e["status"] == "blocked" for e in built)
    assert all("harmonization_profile" in e["note"] for e in built)

    contracts = {
        (detector, "uni"): {"profile_id": f"p-{detector}", "code": "H7T", "version": 1,
                             "method": "combat", "artifact_manifest": {"files": []}}
        for detector in ("meld_fcd", "map")
    }
    entries = build_recipe(Workup.fcd, roles, harmonization=contracts)
    built = [e for e in entries if e["status"] == "created"]
    assert len(built) == 2
    assert all(e["params"]["series_uids"] == {
        "t1_uni": "uni", "t1_inv1": "inv1", "t1_inv2": "inv2"} for e in built)
    assert len({e["entry_id"] for e in entries}) == len(entries)
    assert len(spec_hash(entries)) == 64


def test_unharmonized_run_requires_recorded_reason_at_api_boundary():
    entries = build_recipe(
        Workup.fcd,
        {"m": "t1_mprage"},
        require_harmonization=False,
        unharmonized_reason="pilot profile development",
    )
    built = [e for e in entries if e["status"] == "created"]
    assert built
    assert all(e["params"]["harmonization"] == {
        "mode": "unharmonized", "reason": "pilot profile development"} for e in built)


def test_profile_activation_semantics_are_detector_specific():
    selector = {"acquisition": {"field_strength_t": 7}}
    meld_images = {"meld": "example/meld@sha256:" + "9" * 64}
    meld = SimpleNamespace(
        detector_id="meld_fcd", method="meld_distributed_combat", code="H7T", version=1,
        selector=selector,
        parameters={"activation_eligible": True, "harmo_code": "H7T",
                    "control_count": 20, "minimum_subjects": 20,
                    "cohort_manifest_sha256": "a" * 64,
                    "selector_canonical_sha256": canonical_json_sha256(selector),
                    "build_images": meld_images,
                    "scientific_validation": _validation(
                        "H7T", 1, "meld_fcd", meld_images)},
        artifact_manifest={"cohort_manifest_sha256": "a" * 64},
    )
    validate_profile_semantics(
        meld, {"files": [{"path": "MELD_H7Tcombat_parameters.hdf5"}]})
    meld.parameters["activation_eligible"] = False
    with pytest.raises(ValueError, match="not marked activation-eligible"):
        validate_profile_semantics(meld, {"files": [{"path": "MELD_H7Tcombat_parameters.hdf5"}]})

    map_images = {
        "spm": "example/spm@sha256:" + "c" * 64,
        "pkg": "example/pkg@sha256:" + "d" * 64,
    }
    map_profile = SimpleNamespace(
        detector_id="map", method="map_normative", code="MAP7T", version=1,
        selector=selector,
        parameters={
            "data_root": "profiles/MAP7T", "activation_eligible": True,
            "control_count": 20, "minimum_subjects": 20,
            "cohort_manifest_sha256": "b" * 64,
            "selector_canonical_sha256": canonical_json_sha256(selector),
            "build_images": map_images,
            "scientific_validation": _validation("MAP7T", 1, "map", map_images),
        },
        artifact_manifest={"cohort_manifest_sha256": "b" * 64},
    )
    files = [{"path": f"profiles/MAP7T/normative/map/{name}_{stat}.nii.gz"}
             for name in ("junction", "extension") for stat in ("mean", "std")]
    validate_profile_semantics(map_profile, {"files": files})
    with pytest.raises(ValueError, match="incomplete"):
        validate_profile_semantics(map_profile, {"files": files[:-1]})
    map_profile.parameters["control_count"] = 19
    with pytest.raises(ValueError, match="not activation-eligible"):
        validate_profile_semantics(map_profile, {"files": files})


def test_profile_activation_rejects_unlisted_files_in_detector_data_root(tmp_path):
    data_root = tmp_path / "profiles" / "H7T-v1"
    data_root.mkdir(parents=True)
    artifact = data_root / "MELD_H7Tcombat_parameters.hdf5"
    artifact.write_bytes(b"combat")
    manifest = {"files": [{
        "path": "profiles/H7T-v1/MELD_H7Tcombat_parameters.hdf5",
        "sha256": sha256_file(artifact), "size": artifact.stat().st_size,
    }], "cohort_manifest_sha256": "a" * 64}
    images = {"meld": "example/meld@sha256:" + "9" * 64}
    profile = SimpleNamespace(
        detector_id="meld_fcd", method="meld_distributed_combat", code="H7T", version=1,
        selector={"acquisition": {"field_strength_t": 7}},
        parameters={"activation_eligible": True, "harmo_code": "H7T",
                    "control_count": 20, "minimum_subjects": 20,
                    "cohort_manifest_sha256": "a" * 64,
                    "data_root": "profiles/H7T-v1",
                    "build_images": images,
                    "scientific_validation": _validation(
                        "H7T", 1, "meld_fcd", images),
                    "selector_canonical_sha256": canonical_json_sha256(
                        {"acquisition": {"field_strength_t": 7}})},
        artifact_manifest=manifest,
    )
    verified = verify_artifact_manifest(manifest, tmp_path)
    validate_profile_semantics(profile, verified)
    (data_root / "unlisted.hdf5").write_bytes(b"different parameters")
    with pytest.raises(ValueError, match="unlisted or missing"):
        validate_profile_semantics(profile, verified)
