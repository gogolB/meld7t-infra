"""Deployment-wide white-label configuration and authenticated API contract."""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app
from app import reporting
from app.reporting import ReportNotReadyError, branding_snapshot


def test_branding_endpoint_returns_validated_deployment_defaults() -> None:
    with TestClient(app) as client:
        response = client.get("/api/branding")
    assert response.status_code == 200
    assert response.json() == {
        "product_name": "MELD 7T",
        "institution_name": "Houston Methodist",
        "department_name": "Houston Methodist Research Institute",
        "logo_url": "/branding/report-logo.png",
        "primary_color": "#124A7E",
        "secondary_color": "#749ABB",
        "footer_text": "Houston Methodist Research Institute",
    }


@pytest.mark.parametrize("logo", [
    "https://remote.example/logo.svg", "//remote.example/logo.svg",
    "/branding/../secret.svg", "/assets/logo.svg",
])
def test_branding_logo_is_restricted_to_same_origin_asset_namespace(logo: str) -> None:
    with pytest.raises(ValidationError, match="same-origin"):
        Settings(_env_file=None, deployment_mode="test", branding_logo_url=logo)


def test_branding_colors_require_six_digit_hex() -> None:
    with pytest.raises(ValidationError, match="six-digit hexadecimal"):
        Settings(_env_file=None, deployment_mode="test", branding_primary_color="navy")


def test_report_logo_path_is_absolute_but_not_exposed_by_endpoint() -> None:
    configured = Settings(
        _env_file=None, deployment_mode="test", branding_logo_path="/run/branding/report-logo.png",
    )
    assert configured.branding_logo_path == "/run/branding/report-logo.png"
    with pytest.raises(ValidationError, match="absolute path"):
        Settings(_env_file=None, deployment_mode="test", branding_logo_path="logos/site.png")


def test_report_branding_snapshot_binds_logo_bytes_not_mount_path(tmp_path, monkeypatch) -> None:
    logo = tmp_path / "report-logo.png"
    logo.write_bytes(b"approved-logo")
    monkeypatch.setattr(reporting.settings, "branding_logo_path", str(logo))
    snapshot = branding_snapshot()
    assert "logo_path" not in snapshot
    assert snapshot["logo_sha256"] == hashlib.sha256(logo.read_bytes()).hexdigest()
    assert snapshot["logo_size"] == logo.stat().st_size


def test_report_branding_snapshot_rejects_symlinked_logo(tmp_path, monkeypatch) -> None:
    target = tmp_path / "logo.png"
    target.write_bytes(b"logo")
    link = tmp_path / "linked.png"
    link.symlink_to(target)
    monkeypatch.setattr(reporting.settings, "branding_logo_path", str(link))
    with pytest.raises(ReportNotReadyError, match="unavailable or unsafe"):
        branding_snapshot()
