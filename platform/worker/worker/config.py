"""Worker settings (host service, §2.3). Reuses app.config for db/redis/orthanc; adds the
host paths + image names the worker needs to launch sibling podman jobs."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MELD7T_", env_file=".env", extra="ignore")

    deployment_mode: Literal["development", "test", "research", "production"] = "development"

    repo_dir: str = "/var/home/bazzite/meld7t"
    meld_data: str = "/var/home/bazzite/meld7t/meld-data"
    # Both are local-disk paths.  The worker never bind-mounts a caller supplied path into a
    # compute container: imports are copied into ``dicom_staging`` after their DICOM identity has
    # been checked.  Keep these off the durable NFS tier (SELinux relabelling and partial-read
    # semantics make NFS unsuitable for active compute scratch).
    dicom_staging: str = "/var/home/bazzite/meld7t/meld-data/staging"
    dicom_import_root: str = "/var/home/bazzite/meld7t/data/raw"
    dicom_max_instances_per_run: int = Field(default=20_000, ge=1, le=1_000_000)
    dicom_max_bytes_per_run: int = Field(
        default=100 * 1024 * 1024 * 1024, ge=1024 * 1024, le=10 * 1024**4)
    case_upload_root: str = "/var/home/bazzite/meld7t-state/case-uploads"
    case_upload_max_bytes: int = Field(
        default=100 * 1024 * 1024 * 1024, ge=1024 * 1024, le=2 * 1024**4)
    case_upload_max_files: int = Field(default=100_000, ge=1, le=1_000_000)
    case_upload_max_expanded_bytes: int = Field(
        default=100 * 1024 * 1024 * 1024, ge=1024 * 1024, le=10 * 1024**4)
    case_upload_max_instance_bytes: int = Field(
        default=16 * 1024 * 1024 * 1024, ge=1024 * 1024, le=1024**4)
    branding_logo_path: str | None = None
    harmonization_root: str = "/var/home/bazzite/meld7t/meld-data/harmonization"
    harmonization_generated_root: str = "/var/home/bazzite/meld7t-state/harmonization-profiles"
    harmonization_upload_root: str = "/var/home/bazzite/meld7t-state/harmonization-uploads"
    harmonization_build_root: str = "/var/home/bazzite/meld7t-state/harmonization-builds"
    harmonization_builder_adapter: str | None = None
    harmonization_builder_adapter_sha256: str | None = None
    harmonization_builder_queue: str = "meld7t:harmonization-builder"
    # One globally admitted build plus one concurrent upload-ingestion task.
    harmonization_builder_max_jobs: int = Field(default=2, ge=2, le=2)
    harmonization_builder_timeout_s: int = Field(default=24 * 60 * 60, ge=1800, le=7 * 24 * 60 * 60)
    harmonization_builder_lease_s: int = Field(default=180, ge=60, le=1800)
    harmonization_builder_heartbeat_s: int = Field(default=30, ge=5, le=300)
    harmonization_max_upload_bytes: int = Field(
        default=100 * 1024 * 1024 * 1024, ge=1024 * 1024, le=2 * 1024**4)
    harmonization_upload_max_files: int = Field(default=100_000, ge=1, le=1_000_000)
    harmonization_upload_max_expanded_bytes: int = Field(
        default=500 * 1024 * 1024 * 1024, ge=1024 * 1024, le=10 * 1024**4)
    harmonization_max_instance_bytes: int = Field(
        default=16 * 1024 * 1024 * 1024, ge=1024 * 1024, le=1024**4)
    harmonization_build_max_bytes: int = Field(
        default=1024 * 1024 * 1024 * 1024, ge=1024**3, le=10 * 1024**4)
    harmonization_failed_workspace_retention_hours: int = Field(default=24, ge=1, le=24)
    harmonization_orthanc_rest: str = "http://harmonization-orthanc:8042"
    harmonization_orthanc_user: str = "meld-builder"
    harmonization_orthanc_password: SecretStr = SecretStr("change-me")
    harmonization_allowed_transfer_syntaxes: list[str] = Field(default_factory=lambda: [
        "1.2.840.10008.1.2", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.2",
        "1.2.840.10008.1.2.1.99", "1.2.840.10008.1.2.4.70",
        "1.2.840.10008.1.2.4.80", "1.2.840.10008.1.2.4.90",
        "1.2.840.10008.1.2.5",
    ])
    # Empty is safest. Sites that need vendor reconstruction metadata must approve exact private
    # tag numbers after deidentification validation; broad private-group allowlisting is forbidden.
    harmonization_allowed_private_tags: list[str] = Field(default_factory=list)
    release_manifest_digest: str | None = None
    map_script_sha256: str | None = None
    os_checksum: str | None = None
    git_sha: str | None = None
    fs_license: str = "/var/home/bazzite/meld7t/secrets/license.txt"
    meld_license: str = "/var/home/bazzite/meld7t/secrets/meld_license.txt"

    pkg_image: str = "localhost/meld7t/pkg:0.3.3"
    meld_image: str = "meldproject/meld_graph:v2.2.5_gpu"
    hippunfold_image: str = "docker.io/khanlab/hippunfold:latest"  # HS (§25.5)
    map_image: str = "docker.io/spmcentral/spm:latest"             # SPM12 Standalone, MAP (§25.4)
    # persistent cache for HippUnfold's nnU-Net models + templates (avoids re-download; air-gap §11)
    hippunfold_cache: str = "hippunfold-cache"
    hippunfold_cache_sha256: str | None = None
    hippunfold_ai_threshold_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    # Orthanc DICOMweb as seen from the isolated compute network used by pkg/STOW containers.
    orthanc_innet: str = "http://orthanc:8042/dicom-web"
    # Ephemeral packaging jobs only need Orthanc.  Keep them off the API/database/audit network;
    # Orthanc deliberately joins this dedicated compute network as the sole service endpoint.
    podman_data_network: str = "meld-compute-net"

    gpu_lock_key: str = "meld7t:gpu:inuse"
    queue_paused_key: str = "meld7t:queue:paused"
    subprocess_timeout_s: int = Field(default=4 * 60 * 60, ge=300, le=24 * 60 * 60)
    subprocess_stop_grace_s: int = Field(default=20, ge=1, le=300)
    # ARQ enforces this single deadline across acquisition, GPU waiting, preparation, inference,
    # packaging, and completion. Per-process limits remain narrower stage fences.
    run_wall_timeout_s: int = Field(default=12 * 60 * 60, ge=1800, le=48 * 60 * 60)
    run_claim_lease_s: int = Field(default=300, ge=60, le=3600)
    run_claim_heartbeat_s: int = Field(default=60, ge=10, le=300)
    worker_heartbeat_interval_s: int = Field(default=15, ge=5, le=120)
    worker_heartbeat_ttl_s: int = Field(default=45, ge=15, le=600)
    storage_min_free_bytes: int = Field(
        default=50 * 1024 * 1024 * 1024, ge=1024 * 1024 * 1024,
    )
    storage_min_free_percent: float = Field(default=10.0, ge=1.0, le=50.0)
    # Admission reserves the configured maximum DICOM payload plus detector-output headroom for
    # every ARQ slot.  This is deliberately conservative: two jobs must not both pass a one-shot
    # 50 GiB watermark and then independently stage up to 100 GiB.
    storage_output_headroom_bytes: int = Field(
        default=25 * 1024 * 1024 * 1024, ge=1024 * 1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    worker_max_jobs: int = Field(default=2, ge=1, le=8)

    @field_validator(
        "repo_dir", "meld_data", "dicom_staging", "dicom_import_root", "case_upload_root",
        "harmonization_root", "harmonization_generated_root", "harmonization_upload_root",
        "harmonization_build_root", "fs_license", "meld_license",
    )
    @classmethod
    def absolute_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("worker filesystem paths must be absolute and contain no '..'")
        return str(path)

    @field_validator("branding_logo_path")
    @classmethod
    def optional_absolute_path(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        path = Path(value.strip())
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("branding logo path must be absolute without '..'")
        return str(path)

    @field_validator("harmonization_builder_adapter")
    @classmethod
    def absolute_optional_adapter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("harmonization builder adapter must be an absolute path")
        return str(path)

    @field_validator("harmonization_allowed_private_tags")
    @classmethod
    def exact_private_tag_allowlist(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if (len(normalized) != len(set(normalized))
                or any(re.fullmatch(r"[0-9A-F]{4},[0-9A-F]{4}", value) is None
                       for value in normalized)):
            raise ValueError("private DICOM allowlist entries must be unique GGGG,EEEE tags")
        return normalized

    @field_validator("harmonization_allowed_transfer_syntaxes")
    @classmethod
    def transfer_syntax_allowlist(cls, values: list[str]) -> list[str]:
        if (not values or len(values) != len(set(values))
                or any(re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value) is None
                       for value in values)):
            raise ValueError("DICOM transfer syntax allowlist must contain unique numeric UIDs")
        return values

    @field_validator("release_manifest_digest")
    @classmethod
    def normalize_release_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.lower().removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("release_manifest_digest must be a SHA-256 digest")
        return value

    @field_validator(
        "map_script_sha256", "hippunfold_cache_sha256",
        "harmonization_builder_adapter_sha256",
    )
    @classmethod
    def valid_map_script_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.lower().removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("signed asset digests must be SHA-256 values")
        return value

    @model_validator(mode="after")
    def server_provenance_is_immutable(self) -> "WorkerSettings":
        if self.run_claim_heartbeat_s * 2 >= self.run_claim_lease_s:
            raise ValueError("run claim heartbeat must be less than half the claim lease")
        if self.harmonization_builder_heartbeat_s * 2 >= self.harmonization_builder_lease_s:
            raise ValueError("builder heartbeat must be less than half the builder lease")
        if self.worker_heartbeat_interval_s * 2 >= self.worker_heartbeat_ttl_s:
            raise ValueError("worker heartbeat TTL must exceed twice its publication interval")
        if self.run_wall_timeout_s <= self.subprocess_timeout_s + self.subprocess_stop_grace_s:
            raise ValueError("whole-run timeout must exceed a subprocess timeout plus cleanup")
        staging = Path(self.dicom_staging)
        imports = Path(self.dicom_import_root)
        if staging == imports or staging in imports.parents or imports in staging.parents:
            raise ValueError("DICOM staging and import roots must be separate trees")
        if self.deployment_mode in {"research", "production"}:
            for field_name in ("pkg_image", "meld_image", "hippunfold_image", "map_image"):
                reference = getattr(self, field_name)
                if re.search(r"@sha256:[0-9a-f]{64}$", reference) is None:
                    raise ValueError(f"server {field_name} must be pinned by manifest digest")
            if self.release_manifest_digest is None:
                raise ValueError("server worker requires release_manifest_digest")
            if self.map_script_sha256 is None:
                raise ValueError("server worker requires signed map_script_sha256")
            if self.hippunfold_cache_sha256 is None:
                raise ValueError("server worker requires signed hippunfold_cache_sha256")
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.git_sha or "") is None:
                raise ValueError("server worker requires a pinned git_sha")
            if re.fullmatch(r"[0-9a-f]{64}", self.os_checksum or "") is None:
                raise ValueError("server worker requires the booted Bazzite OS checksum")
            if ((self.harmonization_builder_adapter is None)
                    != (self.harmonization_builder_adapter_sha256 is None)):
                raise ValueError(
                    "server harmonization adapter path and SHA-256 must be configured together")
        return self

    @property
    def storage_admission_min_free_bytes(self) -> int:
        """Worst-case scratch reserve required before the worker advertises capacity."""
        per_run = self.dicom_max_bytes_per_run + self.storage_output_headroom_bytes
        return self.storage_min_free_bytes + self.worker_max_jobs * per_run


wsettings = WorkerSettings()
