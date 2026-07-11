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
