import hashlib
from pathlib import Path

from PIL import Image

from worker import report_pdf
from worker.report_pdf import render_case_report

BUNDLED_LOGO = Path(__file__).resolve().parents[3] / "containers/config/branding/report-logo.png"


def test_combined_report_renders_unharmonized_versioned_pdf(tmp_path: Path, monkeypatch):
    warning = "UNHARMONIZED RESEARCH RESULT: no scanner/protocol profile was applied."
    snapshot = {
        "report_kind": "preliminary",
        "version": 1,
        "created_at": "2026-07-12T12:00:00+00:00",
        "snapshot_sha256": "a" * 64,
        "release_manifest_digest": "b" * 64,
        "case": {"id": "case-1", "pseudonym": "HMRI-001", "workup": "both",
                 "orthanc_study_uid": "1.2.3"},
        "recipe": {"id": "recipe-1", "spec_hash": "c" * 64},
        "source_series": [{"series_description": "MP2RAGE UNI", "modality": "MR",
                           "orthanc_series_uid": "1.2.3.4", "instance_count": 240,
                           "confirmed_role": "t1_uni"}],
        "warnings": [warning],
        "runs": [{
            "run": {"id": "run-1", "detector_id": "map", "status": "review_ready",
                    "source_role": "t1_uni", "detector_version": "test",
                    "harmonization": {"mode": "unharmonized"}, "warnings": [warning]},
            "result": {"detector_summary": {
                "asymmetry_index_pct": 12.5,
                "volumes_mm3": {"L": 2800, "R": 3200},
                "dseg_sources": {"L": "output/sub-case-1/anat/internal-left_dseg.nii.gz"},
            }},
            "clusters": [{"index": 1, "hemi": "left", "location": "frontal",
                          "size": 0.8, "confidence": 5.2}],
        }, {
            "run": {"id": "run-pending", "detector_id": "hippunfold", "status": "pending",
                    "source_role": "t2", "detector_version": None,
                    "harmonization": {"mode": "unharmonized"}, "warnings": []},
            "result": None,
            "clusters": [],
        }],
        "adjudications": [],
    }
    visible_values = []
    original_key_values = report_pdf._Document.key_values

    def capture_visible_values(document, values):
        rows = list(values)
        visible_values.extend(value for _key, value in rows)
        return original_key_values(document, rows)

    monkeypatch.setattr(report_pdf._Document, "key_values", capture_visible_values)
    output = tmp_path / "combined.pdf"
    logo = BUNDLED_LOGO.read_bytes()
    result = render_case_report(snapshot, {
        "product_name": "MELD 7T",
        "institution_name": "Houston Methodist",
        "department_name": "Houston Methodist Research Institute",
        "logo_path": str(BUNDLED_LOGO),
        "logo_sha256": hashlib.sha256(logo).hexdigest(),
        "logo_size": len(logo),
        "primary_color": "#124A7E", "secondary_color": "#749ABB",
        "footer_text": "HMRI · Research use only",
    }, output)

    assert output.read_bytes().startswith(b"%PDF")
    assert result["sha256"] and result["size"] == output.stat().st_size
    assert result["page_count"] >= 1
    visible = " ".join(str(value) for value in visible_values)
    # Technical linkage remains in the frozen evidence, but the default human PDF is pseudonym
    # only and does not print internal case/recipe or source DICOM identifiers.
    for hidden in ("case-1", "recipe-1", "1.2.3", "1.2.3.4", "internal-left_dseg"):
        assert hidden not in visible
    assert "12.5" in visible


def test_report_refuses_overwrite(tmp_path: Path):
    output = tmp_path / "exists.pdf"
    output.write_bytes(b"existing")
    try:
        render_case_report({"case": {}}, {}, output)
    except ValueError as exc:
        assert "new absolute path" in str(exc)
    else:
        raise AssertionError("existing output should be rejected")


def test_report_renderer_rejects_excessive_raster_dimensions(tmp_path: Path):
    frame = tmp_path / "wide.png"
    Image.new("L", (report_pdf.MAX_RASTER_DIMENSION + 1, 1)).save(frame)
    payload = frame.read_bytes()
    document = report_pdf._Document(
        report_pdf._Brand.from_dict({}), report_kind="preliminary", warnings=[])
    before = document.y
    document.image({
        "path": str(frame), "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }, "oversized frame")
    assert document.y == before
