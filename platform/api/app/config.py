"""Environment-driven API settings.

The checked-in defaults are suitable only for a developer workstation.  A deployment must set
``MELD7T_DEPLOYMENT_MODE`` to ``research`` or ``production`` and configure at least one trusted
identity source.  Authentication still fails closed when no identity source is configured.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


AuthRoleName = Literal["submitter", "reviewer", "admin", "auditor", "service"]
DeploymentMode = Literal["development", "test", "research", "production"]

_PLACEHOLDER_SECRETS = {
    "changeme",
    "change-me",
    "default",
    "immudb",
    "meld",
    "password",
    "replace-me",
    "secret",
    "test",
}


def _is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.strip().lower()
    return (not normalized or normalized in _PLACEHOLDER_SECRETS
            or "placeholder" in normalized
            or normalized.startswith(("replace_", "replace-", "change_", "change-")))


class LocalAuthToken(BaseModel):
    """One locally verified bearer/service credential.

    Configure this as JSON in ``MELD7T_AUTH_LOCAL_TOKENS``.  Example::

        [{"subject":"pipeline","token":"<random 32+ bytes>",
          "roles":["service"],"service":true}]

    Tokens are deliberately not accepted as hashes: a server must be able to compare the supplied
    secret in constant time.  Deployments should inject the JSON from a root-readable credential
    file/environment generator rather than commit it to source control.
    """

    subject: str = Field(min_length=1, max_length=128)
    token: SecretStr
    roles: set[AuthRoleName] = Field(min_length=1)
    service: bool = False

    @field_validator("subject")
    @classmethod
    def clean_subject(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char in value for char in "\r\n\0"):
            raise ValueError("token subject must be a single non-empty line")
        return value

    @model_validator(mode="after")
    def service_role_matches_type(self) -> "LocalAuthToken":
        if self.service and "service" not in self.roles:
            raise ValueError("service credentials must include the service role")
        if not self.service and "service" in self.roles:
            raise ValueError("the service role is only valid for service credentials")
        return self


class ExpectedHarmonizationProfile(BaseModel):
    """One active scanner/protocol profile authorized by the signed site release."""

    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    version: int = Field(ge=1)
    detector_id: Literal["meld_fcd", "map"]
    document_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("document_sha256")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        return value.lower()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MELD7T_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ``development`` does not itself enable anonymous access.  The separate bypass flag must also
    # be deliberately enabled, which prevents a forgotten environment label from opening the API.
    deployment_mode: DeploymentMode = "development"
    auth_dev_bypass: bool = False

    # Trusted reverse-proxy identity.  The ASGI peer address (never X-Forwarded-For) must fall in
    # one of these networks before any identity header is honored.
    auth_trusted_proxy_networks: list[str] = Field(default_factory=list)
    auth_proxy_user_header: str = "X-Remote-User"
    auth_proxy_roles_header: str = "X-Remote-Roles"
    auth_proxy_request_id_header: str = "X-Request-ID"
    auth_proxy_secret_header: str = "X-MELD-Proxy-Secret"
    auth_proxy_shared_secret: SecretStr | None = None
    auth_proxy_default_roles: set[AuthRoleName] = Field(
        default_factory=lambda: {"submitter"}
    )

    # Offline fallback credentials.  Regular entries are accepted as ``Authorization: Bearer``;
    # service entries are additionally accepted via ``X-Service-Token``.
    auth_local_tokens: list[LocalAuthToken] = Field(default_factory=list)
    auth_service_token_header: str = "X-Service-Token"

    # Postgres (the ``meld`` DB created by the Quadlet init).
    db_url: str = "postgresql+psycopg://meld:meld@postgres:5432/meld"

    # Redis (job broker + hot cache).
    redis_url: str = "redis://redis:6379/0"
    outbox_dispatch_interval_s: float = Field(default=5.0, ge=1.0, le=300.0)
    outbox_max_lag_s: int = Field(default=300, ge=30, le=86400)
    queue_reconcile_grace_s: int = Field(default=30, ge=10, le=3600)
    # A server is not ready merely because Redis accepts jobs.  The host ARQ consumer publishes
    # an HMAC-authenticated, release-bound heartbeat under this key.
    worker_heartbeat_required: bool = False
    worker_heartbeat_key: str = "meld7t:worker:heartbeat"
    worker_heartbeat_max_age_s: int = Field(default=60, ge=20, le=600)
    storage_min_free_bytes: int = Field(
        default=50 * 1024 * 1024 * 1024, ge=1024 * 1024 * 1024,
    )
    storage_min_free_percent: float = Field(default=10.0, ge=1.0, le=50.0)

    # Orthanc DICOMweb (same isolated network; QIDO/WADO/STOW).
    orthanc_dicomweb: str = "http://orthanc:8042/dicom-web"
    dicomweb_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)

    # immudb audit ledger.
    immudb_host: str = "immudb"
    immudb_port: int = 3322
    immudb_user: str = "immudb"
    immudb_password: SecretStr = SecretStr("immudb")
    immudb_db: str = "defaultdb"
    immudb_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # A production verified client must retain its last trusted tree root across restarts and pin
    # the public key used to authenticate immudb's signed state responses.
    immudb_root_state_path: str | None = None
    immudb_public_key_path: str | None = None

    # Defaults true in server modes and false in development/test.  Research deployments may
    # explicitly accept degraded operation; production may not.
    audit_require_immudb: bool = False
    audit_hmac_key: SecretStr = SecretStr("change-me")

    # meld-data root (mounted read-only) — report PDFs + key-frame PNGs.
    meld_data: str = "/data"

    # Site/scanner/protocol harmonization manifests and reference assets.  Development/test can run
    # without a selected profile by default; server deployments fail closed unless explicitly
    # configured otherwise for a documented migration window.
    harmonization_root: str = "/data/harmonization"
    harmonization_required: bool = False
    # The exact set expected to be active. An empty inventory deliberately keeps server readiness
    # red even if an administrator happens to activate an unapproved profile in the database.
    harmonization_expected_profiles: list[ExpectedHarmonizationProfile] = Field(
        default_factory=list
    )
    harmonization_integrity_scan_interval_s: int = Field(default=900, ge=60, le=86400)
    harmonization_integrity_max_age_s: int = Field(default=1800, ge=120, le=172800)
    release_manifest_digest: str | None = None

    @field_validator("meld_data", "harmonization_root")
    @classmethod
    def absolute_data_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("data roots must be absolute paths without '..'")
        return str(path)

    @field_validator("immudb_root_state_path", "immudb_public_key_path")
    @classmethod
    def absolute_optional_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("immudb trust-state/key paths must be absolute without '..'")
        return str(path)

    @field_validator("release_manifest_digest")
    @classmethod
    def valid_release_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.lower().removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("release_manifest_digest must be a SHA-256 digest")
        return value

    @field_validator("auth_trusted_proxy_networks")
    @classmethod
    def valid_proxy_networks(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            try:
                normalized.append(str(ipaddress.ip_network(value.strip(), strict=False)))
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy network: {value!r}") from exc
        return normalized

    @field_validator(
        "auth_proxy_user_header",
        "auth_proxy_roles_header",
        "auth_proxy_request_id_header",
        "auth_proxy_secret_header",
        "auth_service_token_header",
    )
    @classmethod
    def valid_header_name(cls, value: str) -> str:
        value = value.strip()
        # RFC 9110 token characters.  Keeping this strict also prevents accidental header splitting.
        allowed = set("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
        if not value or any(char not in allowed for char in value):
            raise ValueError(f"invalid HTTP header name: {value!r}")
        return value

    @model_validator(mode="after")
    def secure_deployment_settings(self) -> "Settings":
        if "harmonization_required" not in self.model_fields_set:
            self.harmonization_required = self.deployment_mode in {"research", "production"}
        if "audit_require_immudb" not in self.model_fields_set:
            self.audit_require_immudb = self.deployment_mode in {"research", "production"}
        if "worker_heartbeat_required" not in self.model_fields_set:
            self.worker_heartbeat_required = self.deployment_mode in {"research", "production"}
        if self.harmonization_integrity_max_age_s <= self.harmonization_integrity_scan_interval_s:
            raise ValueError(
                "harmonization integrity maximum age must exceed the scan interval"
            )
        expected_targets = [
            (item.code, item.version) for item in self.harmonization_expected_profiles
        ]
        if len(expected_targets) != len(set(expected_targets)):
            raise ValueError("harmonization expected-profile inventory contains duplicates")
        expected_codes = [item.code for item in self.harmonization_expected_profiles]
        if len(expected_codes) != len(set(expected_codes)):
            raise ValueError(
                "harmonization expected-profile inventory cannot activate two versions "
                "of one code"
            )
        if self.deployment_mode in {"research", "production"} and self.auth_dev_bypass:
            raise ValueError("auth_dev_bypass is forbidden in research/production mode")

        subjects: set[str] = set()
        token_values: set[str] = set()
        for credential in self.auth_local_tokens:
            if credential.subject in subjects:
                raise ValueError(f"duplicate local token subject: {credential.subject}")
            subjects.add(credential.subject)
            secret = credential.token.get_secret_value()
            if secret in token_values:
                raise ValueError("local authentication tokens must be unique")
            token_values.add(secret)

        if self.deployment_mode in {"research", "production"}:
            try:
                database_url = make_url(self.db_url)
            except Exception as exc:
                raise ValueError("server db_url must be a valid SQLAlchemy database URL") from exc
            if not database_url.drivername.startswith("postgresql"):
                raise ValueError("research/production db_url must use PostgreSQL")
            if _is_placeholder(database_url.password):
                raise ValueError(
                    "research/production db_url must not use a default/placeholder password"
                )
            if len(database_url.password or "") < 32:
                raise ValueError("research/production db_url password must be at least 32 characters")
            try:
                redis_url = make_url(self.redis_url)
            except Exception as exc:
                raise ValueError("server redis_url must be a valid URL") from exc
            if redis_url.drivername not in {"redis", "rediss"}:
                raise ValueError("research/production redis_url must use redis or rediss")
            if _is_placeholder(redis_url.password):
                raise ValueError(
                    "research/production redis_url must include a non-placeholder password"
                )
            if len(redis_url.password or "") < 32:
                raise ValueError("research/production redis_url password must be at least 32 characters")
            if _is_placeholder(self.immudb_password.get_secret_value()):
                raise ValueError(
                    "research/production immudb_password must not be a placeholder"
                )
            if len(self.immudb_password.get_secret_value()) < 32:
                raise ValueError("research/production immudb_password must be at least 32 characters")
            audit_key = self.audit_hmac_key.get_secret_value()
            if _is_placeholder(audit_key) or len(audit_key) < 32:
                raise ValueError("research/production audit_hmac_key must be at least 32 characters")
            has_proxy_network = bool(self.auth_trusted_proxy_networks)
            has_proxy_secret = self.auth_proxy_shared_secret is not None
            if has_proxy_network != has_proxy_secret:
                raise ValueError(
                    "server trusted-proxy authentication requires both an allowlisted network "
                    "and a shared secret"
                )
            if self.auth_proxy_shared_secret is not None:
                proxy_secret = self.auth_proxy_shared_secret.get_secret_value()
                if _is_placeholder(proxy_secret) or len(proxy_secret) < 32:
                    raise ValueError(
                        "research/production proxy shared secret must be at least 32 characters"
                    )
            for credential in self.auth_local_tokens:
                secret = credential.token.get_secret_value()
                if _is_placeholder(secret) or len(secret) < 32:
                    raise ValueError(
                        f"research/production token for {credential.subject!r} must be random and "
                        "at least 32 characters"
                    )
            if not self.auth_trusted_proxy_networks and not self.auth_local_tokens:
                raise ValueError("research/production needs a trusted proxy or at least one local token")
            if self.release_manifest_digest is None:
                raise ValueError(
                    "research/production requires a signed release_manifest_digest"
                )

        if self.deployment_mode == "production":
            if not self.audit_require_immudb:
                raise ValueError("production requires audit_require_immudb=true")
            if not self.immudb_root_state_path or not self.immudb_public_key_path:
                raise ValueError(
                    "production requires persistent immudb_root_state_path and a pinned "
                    "immudb_public_key_path"
                )

        return self

    @property
    def is_development(self) -> bool:
        return self.deployment_mode in {"development", "test"}

    @property
    def is_server_mode(self) -> bool:
        return self.deployment_mode in {"research", "production"}


settings = Settings()
