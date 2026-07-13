"""Authenticated worker-capacity heartbeat contracts."""
from __future__ import annotations

import json

from app import queue
from app.config import settings


def test_worker_heartbeat_is_release_bound_fresh_and_authenticated(monkeypatch):
    monkeypatch.setattr(settings, "release_manifest_digest", "a" * 64)
    monkeypatch.setattr(settings, "worker_heartbeat_max_age_s", 60)
    value = queue.make_worker_heartbeat(
        boot_id="boot-1", release_manifest_digest="a" * 64,
        git_sha="b" * 40, os_checksum="c" * 64,
        images={"pkg": "example@sha256:" + "d" * 64}, observed_at=1000.0,
    )
    verified = queue.verify_worker_heartbeat(value, now=1010.0)
    assert verified["ready"] is True
    assert verified["age_seconds"] == 10

    tampered = json.loads(value)
    tampered["capacity"]["max_jobs"] = 200
    assert queue.verify_worker_heartbeat(json.dumps(tampered), now=1010.0)["status"] == (
        "authentication_failed"
    )
    assert queue.verify_worker_heartbeat(value, now=1100.0)["status"] == "stale"


def test_worker_heartbeat_rejects_exhausted_storage(monkeypatch):
    monkeypatch.setattr(settings, "release_manifest_digest", "a" * 64)
    value = queue.make_worker_heartbeat(
        boot_id="boot-low-space", release_manifest_digest="a" * 64,
        git_sha="b" * 40, os_checksum="c" * 64,
        images={"pkg": "example@sha256:" + "d" * 64}, observed_at=1000.0,
        capacity={"max_jobs": 2, "max_gpu_jobs": 1, "storage_ready": False,
                  "free_bytes": 10, "required_free_bytes": 20},
    )
    verified = queue.verify_worker_heartbeat(value, now=1001.0)
    assert verified["ready"] is False
    assert verified["status"] == "storage_capacity_unavailable"


def test_worker_heartbeat_rejects_another_release(monkeypatch):
    monkeypatch.setattr(settings, "release_manifest_digest", "a" * 64)
    value = queue.make_worker_heartbeat(
        boot_id="boot-2", release_manifest_digest="f" * 64,
        git_sha="b" * 40, os_checksum="c" * 64,
        images={"pkg": "example@sha256:" + "d" * 64}, observed_at=1000.0,
    )
    assert queue.verify_worker_heartbeat(value, now=1001.0)["status"] == "release_mismatch"


def test_harmonization_builder_heartbeat_is_bound_to_reviewed_adapter(monkeypatch):
    monkeypatch.setattr(settings, "release_manifest_digest", "a" * 64)
    adapter_sha256 = "7" * 64
    value = queue.make_worker_heartbeat(
        boot_id="builder-1", release_manifest_digest="a" * 64,
        git_sha="b" * 40, os_checksum="c" * 64,
        images={"meld": "example/meld@sha256:" + "d" * 64}, observed_at=1000.0,
        capacity={
            "kind": "harmonization-builder", "max_jobs": 2, "max_gpu_jobs": 1,
            "storage_ready": True, "adapter_ready": True,
            "adapter_sha256": adapter_sha256,
        },
    )
    verified = queue.verify_worker_heartbeat(
        value, now=1001.0, expected_capacity_kind="harmonization-builder",
        expected_images={"meld": "example/meld@sha256:" + "d" * 64},
        expected_adapter_sha256=adapter_sha256,
    )
    assert verified["ready"] is True
    mismatch = queue.verify_worker_heartbeat(
        value, now=1001.0, expected_capacity_kind="harmonization-builder",
        expected_images={"meld": "example/meld@sha256:" + "d" * 64},
        expected_adapter_sha256="8" * 64,
    )
    assert mismatch["ready"] is False
    assert mismatch["status"] == "adapter_mismatch"
