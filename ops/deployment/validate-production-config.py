#!/usr/bin/env python3
"""Validate production secret/config relationships without printing secret material."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


PLACEHOLDER = re.compile(r"CHANGE_ME|REPLACE_|OPERATOR|example\.hospital|placeholder", re.I)
DIGEST_REF = re.compile(r"^[^\s@]+/[^\s@]+@sha256:[0-9a-f]{64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
BCRYPT = re.compile(r"^\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}$")


def fail(message: str) -> None:
    raise ValueError(message)


def env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        fail(f"missing environment file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        fail(f"secret file must not be group/world accessible: {path} ({mode:o})")
    values: dict[str, str] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if "=" not in raw:
            fail(f"{path}:{line_no}: expected KEY=VALUE")
        key, value = raw.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            fail(f"{path}:{line_no}: invalid variable name")
        if key in values:
            fail(f"{path}:{line_no}: duplicate variable {key}")
        if re.search(r"\s[#;]", value):
            fail(f"{path}:{line_no}: trailing comments are forbidden in EnvironmentFile values")
        if not value or PLACEHOLDER.search(value):
            fail(f"{path}:{line_no}: empty or placeholder value for {key}")
        values[key] = value
    return values


def required(values: dict[str, str], path: Path, *keys: str) -> None:
    missing = [key for key in keys if key not in values]
    if missing:
        fail(f"{path}: missing required variables: {', '.join(missing)}")


def service_url(
    raw: str, expected_scheme: str, expected_user: str, expected_host: str,
    expected_port: int, expected_path: str,
) -> str:
    parsed = urlsplit(raw)
    if (parsed.scheme != expected_scheme or parsed.username != expected_user
            or parsed.hostname != expected_host or parsed.port != expected_port
            or parsed.path != expected_path or parsed.query or parsed.fragment
            or parsed.password is None):
        fail(f"{expected_scheme} URL does not match the approved production topology")
    return unquote(parsed.password)


def strong_secret(value: str, label: str, minimum: int = 32) -> None:
    if len(value) < minimum or len(set(value)) < 10:
        fail(f"{label} must be at least {minimum} characters with adequate variation")


def load_lock(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 2 or parts[0] in result:
            fail(f"{path}:{line_no}: malformed or duplicate image lock row")
        if not DIGEST_REF.fullmatch(parts[1]):
            fail(f"{path}:{line_no}: image is not pinned by sha256 digest")
        result[parts[0]] = parts[1]
    return result


def openssl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["openssl", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config_root", type=Path, help="installed ~/.config/meld7t directory")
    parser.add_argument("image_lock", type=Path)
    parser.add_argument("release_env", type=Path, help="signature-verified release.env receipt")
    args = parser.parse_args()
    root = args.config_root
    env_dir = root / "env"

    files = {name: env_file(env_dir / f"{name}.env") for name in
             ("postgres", "redis", "orthanc", "harmonization-postgres",
              "harmonization-orthanc", "immudb", "api", "caddy", "worker",
              "harmonization-builder")}
    postgres, redis, orthanc, immudb = (files["postgres"], files["redis"], files["orthanc"],
                                        files["immudb"])
    api, caddy, worker = files["api"], files["caddy"], files["worker"]
    harmonization_postgres = files["harmonization-postgres"]
    harmonization_orthanc = files["harmonization-orthanc"]
    harmonization_builder = files["harmonization-builder"]
    runtime = env_file(env_dir / "runtime-images.env")
    release = env_file(args.release_env)

    required(postgres, env_dir / "postgres.env", "POSTGRES_PASSWORD",
             "ORTHANC__POSTGRESQL__PASSWORD", "MELD_DB_PASSWORD", "POSTGRES_INITDB_ARGS")
    required(redis, env_dir / "redis.env", "REDIS_PASSWORD")
    required(orthanc, env_dir / "orthanc.env", "ORTHANC__POSTGRESQL__PASSWORD")
    required(harmonization_postgres, env_dir / "harmonization-postgres.env",
             "POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_INITDB_ARGS",
             "ORTHANC__POSTGRESQL__PASSWORD")
    required(harmonization_orthanc, env_dir / "harmonization-orthanc.env",
             "ORTHANC__POSTGRESQL__PASSWORD", "ORTHANC__AUTHENTICATION_ENABLED",
             "ORTHANC__REGISTERED_USERS", "ORTHANC__MAXIMUM_STORAGE_SIZE",
             "ORTHANC__MAXIMUM_STORAGE_MODE")
    required(immudb, env_dir / "immudb.env", "IMMUDB_ADMIN_PASSWORD", "IMMUDB_AUTH",
             "IMMUDB_DEVMODE", "IMMUDB_MAINTENANCE", "IMMUDB_SIGNINGKEY")
    required(api, env_dir / "api.env", "MELD7T_DEPLOYMENT_MODE", "MELD7T_DB_URL",
             "MELD7T_REDIS_URL", "MELD7T_IMMUDB_PASSWORD", "MELD7T_AUTH_PROXY_SHARED_SECRET",
             "MELD7T_AUTH_TRUSTED_PROXY_NETWORKS", "MELD7T_RELEASE_MANIFEST_DIGEST",
             "MELD7T_AUDIT_HMAC_KEY", "MELD7T_IMMUDB_ROOT_STATE_PATH",
             "MELD7T_IMMUDB_PUBLIC_KEY_PATH", "MELD7T_ORTHANC_DICOMWEB",
             "MELD7T_HARMONIZATION_EXPECTED_PROFILES",
             "MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED",
             "MELD7T_HARMONIZATION_ORTHANC_DICOMWEB", "MELD7T_HARMONIZATION_UPLOAD_ROOT",
             "MELD7T_HARMONIZATION_ORTHANC_USER", "MELD7T_HARMONIZATION_ORTHANC_PASSWORD",
             "MELD7T_HARMONIZATION_GENERATED_ROOT",
             "MELD7T_HARMONIZATION_MAX_UPLOAD_BYTES", "MELD7T_HARMONIZATION_UPLOAD_CHUNK_BYTES",
             "MELD7T_HARMONIZATION_COHORT_QUOTA_BYTES",
             "MELD7T_HARMONIZATION_BUILDER_QUEUE", "MELD7T_HARMONIZATION_BUILDER_LEASE_S",
             "MELD7T_HARMONIZATION_BUILDER_HEARTBEAT_S",
             "MELD7T_HARMONIZATION_BUILDER_HEARTBEAT_KEY",
             "MELD7T_HARMONIZATION_BUILDER_HEARTBEAT_MAX_AGE_S",
             "MELD7T_HARMONIZATION_MAX_INSTANCE_BYTES",
             "MELD7T_HARMONIZATION_ALLOWED_PRIVATE_TAGS",
             "MELD7T_HARMONIZATION_ALLOWED_TRANSFER_SYNTAXES")
    required(caddy, env_dir / "caddy.env", "SITE_ADDRESS", "TLS_CERT_FILE", "TLS_KEY_FILE",
             "CADDY_IDENTITY_MODE", "MELD7T_AUTH_PROXY_SHARED_SECRET",
             "MELD7T_ORTHANC_BASIC_AUTH_B64")
    required(worker, env_dir / "worker.env", "MELD7T_DB_URL", "MELD7T_REDIS_URL",
             "MELD7T_IMMUDB_PASSWORD", "MELD7T_MELD_DATA", "MELD7T_DICOM_STAGING",
             "MELD7T_RELEASE_MANIFEST_DIGEST", "MELD7T_WORKER_VENV", "MELD7T_AUDIT_HMAC_KEY",
             "MELD7T_GIT_SHA", "MELD7T_OS_CHECKSUM", "MELD7T_IMMUDB_ROOT_STATE_PATH",
             "MELD7T_IMMUDB_PUBLIC_KEY_PATH", "MELD7T_ORTHANC_DICOMWEB",
             "MELD7T_ORTHANC_INNET", "MELD7T_DICOM_MAX_BYTES_PER_RUN",
             "MELD7T_HARMONIZATION_GENERATED_ROOT",
             "MELD7T_STORAGE_MIN_FREE_BYTES", "MELD7T_STORAGE_MIN_FREE_PERCENT",
             "MELD7T_STORAGE_OUTPUT_HEADROOM_BYTES", "MELD7T_WORKER_MAX_JOBS")
    required(harmonization_builder, env_dir / "harmonization-builder.env",
             "MELD7T_DEPLOYMENT_MODE", "MELD7T_HARMONIZATION_ORTHANC_DICOMWEB",
             "MELD7T_ORTHANC_DICOMWEB", "MELD7T_ORTHANC_INNET",
             "MELD7T_HARMONIZATION_ORTHANC_REST", "MELD7T_HARMONIZATION_ORTHANC_USER",
             "MELD7T_HARMONIZATION_ORTHANC_PASSWORD",
             "MELD7T_HARMONIZATION_UPLOAD_ROOT", "MELD7T_HARMONIZATION_BUILD_ROOT",
             "MELD7T_HARMONIZATION_GENERATED_ROOT", "MELD7T_HARMONIZATION_MAX_UPLOAD_BYTES",
             "MELD7T_HARMONIZATION_BUILDER_QUEUE", "MELD7T_HARMONIZATION_BUILDER_MAX_JOBS",
             "MELD7T_HARMONIZATION_BUILDER_TIMEOUT_S",
             "MELD7T_HARMONIZATION_BUILDER_LEASE_S",
             "MELD7T_HARMONIZATION_BUILDER_HEARTBEAT_S",
             "MELD7T_HARMONIZATION_UPLOAD_MAX_FILES",
             "MELD7T_HARMONIZATION_UPLOAD_MAX_EXPANDED_BYTES",
             "MELD7T_HARMONIZATION_MAX_INSTANCE_BYTES",
             "MELD7T_HARMONIZATION_BUILD_MAX_BYTES",
             "MELD7T_HARMONIZATION_FAILED_WORKSPACE_RETENTION_HOURS",
             "MELD7T_HARMONIZATION_ALLOWED_PRIVATE_TAGS",
             "MELD7T_HARMONIZATION_ALLOWED_TRANSFER_SYNTAXES",
             "MELD7T_STORAGE_MIN_FREE_BYTES", "MELD7T_STORAGE_MIN_FREE_PERCENT",
             "MELD7T_WORKER_MAX_JOBS", "MELD7T_IMMUDB_ROOT_STATE_PATH")
    required(runtime, env_dir / "runtime-images.env", "MELD7T_MAP_SCRIPT_SHA256",
             "MELD7T_HIPPUNFOLD_CACHE_SHA256", "MELD7T_HARMONIZATION_INVENTORY_SHA256",
             "MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED",
             "MELD7T_RELEASE_MANIFEST_DIGEST", "MELD7T_GIT_SHA")
    required(release, args.release_env, "MELD7T_HARMONIZATION_INVENTORY_SHA256",
             "MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED")

    if api["MELD7T_DEPLOYMENT_MODE"] != "production":
        fail("api.env must set MELD7T_DEPLOYMENT_MODE=production")
    if harmonization_builder["MELD7T_DEPLOYMENT_MODE"] != "production":
        fail("harmonization-builder.env must set MELD7T_DEPLOYMENT_MODE=production")
    if (harmonization_builder["MELD7T_ORTHANC_DICOMWEB"] != "http://127.0.0.1:9/disabled"
            or harmonization_builder["MELD7T_ORTHANC_INNET"]
            != "http://127.0.0.1:9/disabled"):
        fail("harmonization builder must not receive a usable research Orthanc endpoint")
    if api.get("MELD7T_AUTH_DEV_BYPASS") != "false":
        fail("production API authentication bypass must be false")
    try:
        local_tokens = json.loads(api.get("MELD7T_AUTH_LOCAL_TOKENS", "invalid"))
    except json.JSONDecodeError:
        local_tokens = None
    if local_tokens != []:
        fail("production API local authentication tokens must be an empty JSON list")
    if api.get("MELD7T_AUTO_MIGRATE") != "false":
        fail("production API must never migrate its schema during startup")
    if api.get("MELD7T_HARMONIZATION_REQUIRED") != "true":
        fail("production must fail closed when harmonization is unavailable")
    if api["MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED"] not in {"true", "false"}:
        fail("cohort bootstrap authorization must be an explicit boolean")
    signed_bootstrap = release["MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED"]
    if signed_bootstrap not in {"true", "false"}:
        fail("signed cohort bootstrap authorization must be an explicit boolean")
    if (api["MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED"] != signed_bootstrap
            or runtime["MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED"] != signed_bootstrap):
        fail("deployed cohort bootstrap authorization differs from the signed release value")
    if api.get("MELD7T_HARMONIZATION_ROOT") != "/data/harmonization":
        fail("API harmonization root must be the signed read-only release mount")
    expected_api_paths = {
        "MELD7T_HARMONIZATION_UPLOAD_ROOT": "/data/harmonization-uploads",
        "MELD7T_HARMONIZATION_GENERATED_ROOT": "/data/generated-harmonization",
    }
    for key, expected in expected_api_paths.items():
        if api[key] != expected:
            fail(f"API {key} must use its dedicated container mount")
    if api["MELD7T_HARMONIZATION_BUILDER_QUEUE"] != "harmonization-builder":
        fail("API must enqueue builds only on the dedicated harmonization-builder queue")
    try:
        expected_profiles = json.loads(api["MELD7T_HARMONIZATION_EXPECTED_PROFILES"])
    except json.JSONDecodeError as exc:
        fail(f"expected harmonization profiles must be JSON: {exc}")
    expected_keys: set[tuple[str, int]] = set()
    required_profile_keys = {"code", "version", "detector_id", "document_sha256"}
    if not isinstance(expected_profiles, list):
        fail("production expected harmonization inventory must be a list")
    bootstrap_allowed = api["MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED"] == "true"
    if (not expected_profiles) != bootstrap_allowed:
        fail("only the signed cohort bootstrap release may use an empty profile inventory")
    for item in expected_profiles:
        if (not isinstance(item, dict) or set(item) != required_profile_keys
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", str(item.get("code", ""))) is None
                or isinstance(item.get("version"), bool) or not isinstance(item.get("version"), int)
                or item["version"] < 1 or item.get("detector_id") not in {"meld_fcd", "map"}
                or HEX64.fullmatch(str(item.get("document_sha256", ""))) is None):
            fail("expected harmonization profile entry has an invalid schema or value")
        key = (item["code"], item["version"])
        if key in expected_keys or any(existing[0] == item["code"] for existing in expected_keys):
            fail("expected harmonization inventory contains duplicate profile codes")
        expected_keys.add(key)
    if api.get("MELD7T_AUDIT_REQUIRE_IMMUDB") != "true":
        fail("production API must fail closed when immudb is unavailable")
    if api["MELD7T_AUTH_PROXY_SHARED_SECRET"] != caddy["MELD7T_AUTH_PROXY_SHARED_SECRET"]:
        fail("Caddy/API proxy shared secrets do not match")
    if len(api["MELD7T_AUTH_PROXY_SHARED_SECRET"]) < 32:
        fail("proxy shared secret must contain at least 32 characters")
    if api["MELD7T_AUDIT_HMAC_KEY"] != worker["MELD7T_AUDIT_HMAC_KEY"]:
        fail("API and worker audit HMAC keys do not match")
    if len(api["MELD7T_AUDIT_HMAC_KEY"]) < 32:
        fail("audit HMAC key must contain at least 32 characters")
    try:
        networks = json.loads(api["MELD7T_AUTH_TRUSTED_PROXY_NETWORKS"])
    except json.JSONDecodeError as exc:
        fail(f"trusted proxy networks must be JSON: {exc}")
    if networks != ["10.89.10.0/24"]:
        fail("API must trust exactly the installed meld-edge subnet")
    try:
        worker_networks = json.loads(worker["MELD7T_AUTH_TRUSTED_PROXY_NETWORKS"])
    except json.JSONDecodeError as exc:
        fail(f"worker trusted proxy networks must be JSON: {exc}")
    if worker_networks != ["127.0.0.1/32"]:
        fail("worker environment must trust exactly localhost")
    try:
        dicom_limit = int(worker["MELD7T_DICOM_MAX_BYTES_PER_RUN"])
        storage_floor = int(worker["MELD7T_STORAGE_MIN_FREE_BYTES"])
        output_headroom = int(worker["MELD7T_STORAGE_OUTPUT_HEADROOM_BYTES"])
        max_jobs = int(worker["MELD7T_WORKER_MAX_JOBS"])
        free_percent = float(worker["MELD7T_STORAGE_MIN_FREE_PERCENT"])
    except ValueError as exc:
        fail(f"worker storage admission values must be numeric: {exc}")
    if (not 1_048_576 <= dicom_limit <= 10 * 1024**4
            or not 1024**3 <= storage_floor
            or not 1024**3 <= output_headroom <= 1024**4
            or not 1 <= max_jobs <= 8
            or not 1.0 <= free_percent <= 50.0):
        fail("worker storage admission values are outside supported bounds")

    try:
        api_upload_limit = int(api["MELD7T_HARMONIZATION_MAX_UPLOAD_BYTES"])
        api_upload_chunk = int(api["MELD7T_HARMONIZATION_UPLOAD_CHUNK_BYTES"])
        api_cohort_quota = int(api["MELD7T_HARMONIZATION_COHORT_QUOTA_BYTES"])
        builder_upload_limit = int(harmonization_builder["MELD7T_HARMONIZATION_MAX_UPLOAD_BYTES"])
        builder_jobs = int(harmonization_builder["MELD7T_HARMONIZATION_BUILDER_MAX_JOBS"])
        builder_timeout = int(harmonization_builder["MELD7T_HARMONIZATION_BUILDER_TIMEOUT_S"])
        builder_lease = int(harmonization_builder["MELD7T_HARMONIZATION_BUILDER_LEASE_S"])
        builder_heartbeat = int(
            harmonization_builder["MELD7T_HARMONIZATION_BUILDER_HEARTBEAT_S"])
        api_builder_lease = int(api["MELD7T_HARMONIZATION_BUILDER_LEASE_S"])
        api_builder_heartbeat = int(api["MELD7T_HARMONIZATION_BUILDER_HEARTBEAT_S"])
        api_builder_heartbeat_max_age = int(
            api["MELD7T_HARMONIZATION_BUILDER_HEARTBEAT_MAX_AGE_S"])
        api_max_instance = int(api["MELD7T_HARMONIZATION_MAX_INSTANCE_BYTES"])
        builder_max_files = int(
            harmonization_builder["MELD7T_HARMONIZATION_UPLOAD_MAX_FILES"]
        )
        builder_expanded_limit = int(
            harmonization_builder["MELD7T_HARMONIZATION_UPLOAD_MAX_EXPANDED_BYTES"]
        )
        builder_max_instance = int(
            harmonization_builder["MELD7T_HARMONIZATION_MAX_INSTANCE_BYTES"]
        )
        builder_build_limit = int(
            harmonization_builder["MELD7T_HARMONIZATION_BUILD_MAX_BYTES"]
        )
        builder_failed_retention = int(
            harmonization_builder["MELD7T_HARMONIZATION_FAILED_WORKSPACE_RETENTION_HOURS"]
        )
        builder_storage_floor = int(
            harmonization_builder["MELD7T_STORAGE_MIN_FREE_BYTES"]
        )
        builder_free_percent = float(harmonization_builder["MELD7T_STORAGE_MIN_FREE_PERCENT"])
        builder_worker_jobs = int(harmonization_builder["MELD7T_WORKER_MAX_JOBS"])
    except ValueError as exc:
        fail(f"harmonization builder limits must be numeric: {exc}")
    if (api_upload_limit != builder_upload_limit
            or builder_expanded_limit != builder_upload_limit
            or not 1024**3 <= api_upload_limit <= 2 * 1024**4
            or not 1024**2 <= api_upload_chunk <= 256 * 1024**2
            or api_upload_chunk > api_upload_limit
            or not api_upload_limit <= api_cohort_quota <= 10 * 1024**4
            or not 1 <= builder_max_files <= 1_000_000
            or builder_jobs != 2
            or builder_worker_jobs != 1
            or not 3600 <= builder_timeout <= 7 * 86400
            or builder_lease != api_builder_lease
            or builder_heartbeat != api_builder_heartbeat
            or api_builder_heartbeat_max_age < builder_heartbeat * 2
            or not 20 <= api_builder_heartbeat_max_age <= 600
            or api_builder_heartbeat_max_age >= builder_lease
            or not 60 <= builder_lease <= 1800
            or not 5 <= builder_heartbeat <= 300
            or builder_heartbeat * 2 >= builder_lease
            or api_max_instance != builder_max_instance
            or not 1024**2 <= builder_max_instance <= 1024**4
            or builder_build_limit != api_cohort_quota
            or not builder_max_instance <= builder_build_limit <= 10 * 1024**4
            or not 1 <= builder_failed_retention <= 24
            or builder_storage_floor < 10 * 1024**3
            or not 1.0 <= builder_free_percent <= 50.0):
        fail("harmonization builder limits are inconsistent or outside supported bounds")
    if harmonization_builder["MELD7T_HARMONIZATION_BUILDER_QUEUE"] != "harmonization-builder":
        fail("builder must consume only the dedicated harmonization-builder queue")
    if (api["MELD7T_HARMONIZATION_BUILDER_HEARTBEAT_KEY"]
            != "meld7t:harmonization-builder:heartbeat"):
        fail("API must monitor the dedicated harmonization-builder heartbeat key")
    try:
        api_private_tags = json.loads(api["MELD7T_HARMONIZATION_ALLOWED_PRIVATE_TAGS"])
        builder_private_tags = json.loads(
            harmonization_builder["MELD7T_HARMONIZATION_ALLOWED_PRIVATE_TAGS"])
    except json.JSONDecodeError as exc:
        fail(f"harmonization private-tag allowlists must be JSON: {exc}")
    private_tag_pattern = re.compile(r"[0-9A-F]{4},[0-9A-F]{4}")
    if (not isinstance(api_private_tags, list) or api_private_tags != builder_private_tags
            or any(not isinstance(tag, str) or private_tag_pattern.fullmatch(tag) is None
                   for tag in api_private_tags)
            or len(api_private_tags) != len(set(api_private_tags))):
        fail("API/builder private-tag allowlists must be the same unique uppercase DICOM tags")
    try:
        api_transfer_syntaxes = json.loads(
            api["MELD7T_HARMONIZATION_ALLOWED_TRANSFER_SYNTAXES"])
        builder_transfer_syntaxes = json.loads(
            harmonization_builder["MELD7T_HARMONIZATION_ALLOWED_TRANSFER_SYNTAXES"])
    except json.JSONDecodeError as exc:
        fail(f"harmonization transfer-syntax allowlists must be JSON: {exc}")
    uid_pattern = re.compile(r"[0-9]+(?:\.[0-9]+)+")
    if (not isinstance(api_transfer_syntaxes, list) or not api_transfer_syntaxes
            or api_transfer_syntaxes != builder_transfer_syntaxes
            or any(not isinstance(uid, str) or uid_pattern.fullmatch(uid) is None
                   for uid in api_transfer_syntaxes)
            or len(api_transfer_syntaxes) != len(set(api_transfer_syntaxes))):
        fail("API/builder transfer-syntax allowlists must be identical unique numeric UIDs")
    adapter_path = harmonization_builder.get("MELD7T_HARMONIZATION_BUILDER_ADAPTER")
    adapter_sha = harmonization_builder.get("MELD7T_HARMONIZATION_BUILDER_ADAPTER_SHA256")
    api_adapter_sha = api.get("MELD7T_HARMONIZATION_BUILDER_ADAPTER_SHA256")
    if (adapter_path is None) != (adapter_sha is None):
        fail("harmonization builder adapter path and SHA-256 must be configured together")
    if (adapter_sha is None) != (api_adapter_sha is None):
        fail("API and harmonization builder adapter SHA-256 must be configured together")
    if adapter_path is not None:
        adapter = Path(adapter_path)
        if (not adapter.is_absolute() or ".." in adapter.parts
                or HEX64.fullmatch(adapter_sha or "") is None
                or HEX64.fullmatch(api_adapter_sha or "") is None):
            fail("harmonization builder adapter must have an absolute path and 64-hex SHA-256")
        if api_adapter_sha != adapter_sha:
            fail("API and harmonization builder adapter SHA-256 values must match")
        try:
            adapter_stat = adapter.lstat()
        except OSError as exc:
            fail(f"harmonization builder adapter cannot be inspected: {type(exc).__name__}")
        if (adapter.is_symlink() or not stat.S_ISREG(adapter_stat.st_mode)
                or not os.access(adapter, os.X_OK)):
            fail("harmonization builder adapter must be a regular non-symlink executable")
        digest = hashlib.sha256()
        try:
            with adapter.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            fail(f"harmonization builder adapter cannot be read: {type(exc).__name__}")
        if digest.hexdigest() != adapter_sha:
            fail("harmonization builder adapter bytes differ from the configured SHA-256")
    builder_paths = [Path(harmonization_builder[key]) for key in (
        "MELD7T_HARMONIZATION_UPLOAD_ROOT", "MELD7T_HARMONIZATION_BUILD_ROOT",
        "MELD7T_HARMONIZATION_GENERATED_ROOT",
    )]
    if (any(not path.is_absolute() or ".." in path.parts for path in builder_paths)
            or len(set(builder_paths)) != 3):
        fail("harmonization builder storage roots must be distinct absolute paths")
    if Path(worker["MELD7T_HARMONIZATION_GENERATED_ROOT"]) != builder_paths[2]:
        fail("normal worker and builder must use the same generated-profile root")
    builder_audit_state = Path(harmonization_builder["MELD7T_IMMUDB_ROOT_STATE_PATH"])
    if (not builder_audit_state.is_absolute()
            or str(builder_audit_state) in {
                api["MELD7T_IMMUDB_ROOT_STATE_PATH"], worker["MELD7T_IMMUDB_ROOT_STATE_PATH"]
            }):
        fail("harmonization builder must use a distinct absolute immudb trust-state path")

    if postgres["ORTHANC__POSTGRESQL__PASSWORD"] != orthanc["ORTHANC__POSTGRESQL__PASSWORD"]:
        fail("Postgres and Orthanc role passwords do not match")
    if (harmonization_postgres["ORTHANC__POSTGRESQL__PASSWORD"]
            != harmonization_orthanc["ORTHANC__POSTGRESQL__PASSWORD"]):
        fail("harmonization Postgres and Orthanc role passwords do not match")
    if (harmonization_postgres["POSTGRES_USER"] != "postgres"
            or harmonization_postgres["POSTGRES_INITDB_ARGS"]
            != "--auth-host=scram-sha-256 --data-checksums"):
        fail("harmonization Postgres bootstrap settings are not production values")
    database_secrets = {
        postgres["POSTGRES_PASSWORD"], postgres["ORTHANC__POSTGRESQL__PASSWORD"],
        postgres["MELD_DB_PASSWORD"], harmonization_postgres["POSTGRES_PASSWORD"],
        harmonization_postgres["ORTHANC__POSTGRESQL__PASSWORD"],
    }
    if len(database_secrets) != 5:
        fail("application and harmonization database principals must use distinct secrets")
    if service_url(api["MELD7T_DB_URL"], "postgresql+psycopg", "meld", "postgres", 5432,
                   "/meld") != postgres["MELD_DB_PASSWORD"]:
        fail("API database URL does not encode the configured meld role password")
    if service_url(worker["MELD7T_DB_URL"], "postgresql+psycopg", "meld", "127.0.0.1", 5432,
                   "/meld") != postgres["MELD_DB_PASSWORD"]:
        fail("worker database URL does not encode the configured meld role password")
    if service_url(api["MELD7T_REDIS_URL"], "redis", "", "redis", 6379,
                   "/0") != redis["REDIS_PASSWORD"]:
        fail("API Redis URL does not encode redis.env password")
    if service_url(worker["MELD7T_REDIS_URL"], "redis", "", "127.0.0.1", 6379,
                   "/0") != redis["REDIS_PASSWORD"]:
        fail("worker Redis URL does not encode redis.env password")
    if api["MELD7T_IMMUDB_PASSWORD"] != worker["MELD7T_IMMUDB_PASSWORD"]:
        fail("API and worker immudb runtime credentials do not match")
    if (api.get("MELD7T_IMMUDB_HOST") != "immudb" or worker.get("MELD7T_IMMUDB_HOST") != "127.0.0.1"
            or api.get("MELD7T_IMMUDB_PORT") != "3322" or worker.get("MELD7T_IMMUDB_PORT") != "3322"
            or api.get("MELD7T_IMMUDB_DB") != "defaultdb"
            or worker.get("MELD7T_IMMUDB_DB") != "defaultdb"):
        fail("immudb endpoints do not match the isolated production topology")
    if (api.get("MELD7T_IMMUDB_USER") != worker.get("MELD7T_IMMUDB_USER")
            or api.get("MELD7T_IMMUDB_USER") in {"", "immudb", "admin"}):
        fail("API/worker must share a dedicated non-admin immudb runtime principal")
    if (immudb["IMMUDB_AUTH"] != "true" or immudb["IMMUDB_DEVMODE"] != "false"
            or immudb["IMMUDB_MAINTENANCE"] != "false"
            or immudb["IMMUDB_SIGNINGKEY"] != "/run/secrets/immudb-signing-private.pem"):
        fail("immudb authentication/signing safety flags are not production values")
    for value, label in (
        (postgres["POSTGRES_PASSWORD"], "Postgres administrator secret"),
        (postgres["ORTHANC__POSTGRESQL__PASSWORD"], "Orthanc database secret"),
        (postgres["MELD_DB_PASSWORD"], "MELD database secret"),
        (harmonization_postgres["POSTGRES_PASSWORD"],
         "harmonization Postgres administrator secret"),
        (harmonization_postgres["ORTHANC__POSTGRESQL__PASSWORD"],
         "harmonization Orthanc database secret"),
        (redis["REDIS_PASSWORD"], "Redis secret"),
        (immudb["IMMUDB_ADMIN_PASSWORD"], "immudb administrator secret"),
        (api["MELD7T_IMMUDB_PASSWORD"], "immudb runtime secret"),
    ):
        strong_secret(value, label)

    if orthanc.get("ORTHANC__AUTHENTICATION_ENABLED") != "true":
        fail("Orthanc internal authentication must be enabled")
    try:
        orthanc_users = json.loads(orthanc.get("ORTHANC__REGISTERED_USERS", "invalid"))
    except json.JSONDecodeError as exc:
        fail(f"Orthanc registered users must be JSON: {exc}")
    if not isinstance(orthanc_users, dict) or set(orthanc_users) != {"meld-internal"}:
        fail("Orthanc must expose exactly the meld-internal service principal")
    orthanc_password = orthanc_users["meld-internal"]
    strong_secret(orthanc_password, "Orthanc internal HTTP secret")
    for name, expected_host in (("api", "orthanc"), ("worker", "127.0.0.1")):
        actual = service_url(files[name]["MELD7T_ORTHANC_DICOMWEB"], "http", "meld-internal",
                             expected_host, 8042, "/dicom-web")
        if actual != orthanc_password:
            fail(f"{name} Orthanc URL does not contain the internal service secret")
    if service_url(worker["MELD7T_ORTHANC_INNET"], "http", "meld-internal", "orthanc", 8042,
                   "/dicom-web") != orthanc_password:
        fail("worker in-network Orthanc URL does not contain the service secret")
    try:
        caddy_basic = base64.b64decode(caddy["MELD7T_ORTHANC_BASIC_AUTH_B64"], validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        fail("Caddy Orthanc credential is not valid base64")
    if caddy_basic != f"meld-internal:{orthanc_password}":
        fail("Caddy Orthanc credential does not match orthanc.env")

    if harmonization_orthanc.get("ORTHANC__AUTHENTICATION_ENABLED") != "true":
        fail("harmonization Orthanc internal authentication must be enabled")
    try:
        harmonization_storage_mib = int(
            harmonization_orthanc["ORTHANC__MAXIMUM_STORAGE_SIZE"])
    except ValueError as exc:
        fail(f"harmonization Orthanc storage cap must be numeric: {exc}")
    if not 102400 <= harmonization_storage_mib <= 10 * 1024 * 1024:
        fail("harmonization Orthanc storage cap must be between 100 GiB and 10 TiB")
    if harmonization_orthanc["ORTHANC__MAXIMUM_STORAGE_MODE"] != "Reject":
        fail("harmonization Orthanc must reject at its cap instead of recycling controls")
    try:
        harmonization_users = json.loads(
            harmonization_orthanc.get("ORTHANC__REGISTERED_USERS", "invalid")
        )
    except json.JSONDecodeError as exc:
        fail(f"harmonization Orthanc registered users must be JSON: {exc}")
    expected_harmonization_users = {"harmonization-api", "harmonization-builder"}
    if (not isinstance(harmonization_users, dict)
            or set(harmonization_users) != expected_harmonization_users):
        fail("harmonization Orthanc must expose exactly API and builder service principals")
    for principal, password in harmonization_users.items():
        strong_secret(password, f"{principal} Orthanc HTTP secret")
    if len(set(harmonization_users.values())) != 2 or orthanc_password in harmonization_users.values():
        fail("all Orthanc service principals must have distinct credentials")
    api_harmonization_url = urlsplit(api["MELD7T_HARMONIZATION_ORTHANC_DICOMWEB"])
    if (api_harmonization_url.scheme != "http"
            or api_harmonization_url.hostname != "harmonization-orthanc"
            or api_harmonization_url.port != 8042 or api_harmonization_url.path != "/dicom-web"
            or api_harmonization_url.username is not None
            or api_harmonization_url.password is not None
            or api_harmonization_url.query or api_harmonization_url.fragment):
        fail("API harmonization Orthanc URL does not match the isolated topology")
    builder_harmonization_url = urlsplit(
        harmonization_builder["MELD7T_HARMONIZATION_ORTHANC_DICOMWEB"]
    )
    if (builder_harmonization_url.scheme != "http"
            or builder_harmonization_url.hostname != "127.0.0.1"
            or builder_harmonization_url.port != 8043
            or builder_harmonization_url.path != "/dicom-web"
            or builder_harmonization_url.username is not None
            or builder_harmonization_url.password is not None
            or builder_harmonization_url.query or builder_harmonization_url.fragment):
        fail("builder harmonization DICOMweb URL must use the loopback-only published port")
    if (api["MELD7T_HARMONIZATION_ORTHANC_USER"] != "harmonization-api"
            or api["MELD7T_HARMONIZATION_ORTHANC_PASSWORD"]
            != harmonization_users["harmonization-api"]):
        fail("API harmonization Orthanc credentials do not match its dedicated service principal")
    builder_rest = urlsplit(harmonization_builder["MELD7T_HARMONIZATION_ORTHANC_REST"])
    if (builder_rest.scheme != "http" or builder_rest.hostname != "127.0.0.1"
            or builder_rest.port != 8043 or builder_rest.path not in {"", "/"}
            or builder_rest.username is not None or builder_rest.password is not None
            or builder_rest.query or builder_rest.fragment):
        fail("builder Orthanc REST endpoint must use the loopback-only published port")
    if (harmonization_builder["MELD7T_HARMONIZATION_ORTHANC_USER"]
            != "harmonization-builder"
            or harmonization_builder["MELD7T_HARMONIZATION_ORTHANC_PASSWORD"]
            != harmonization_users["harmonization-builder"]):
        fail("builder Orthanc REST credentials do not match its dedicated service principal")

    redis_conf = root / "redis" / "redis.conf"
    if stat.S_IMODE(redis_conf.stat().st_mode) & 0o077:
        fail("redis.conf must be mode 0600")
    matches = re.findall(r"(?m)^requirepass\s+(\S+)\s*$", redis_conf.read_text(encoding="utf-8"))
    if matches != [redis["REDIS_PASSWORD"]]:
        fail("redis.conf must contain exactly the redis.env password")

    if caddy["CADDY_IDENTITY_MODE"] != "institutional-unique":
        fail("hospital activation requires CADDY_IDENTITY_MODE=institutional-unique")
    users_file = root / "caddy" / "auth" / "users.caddy"
    roles_file = root / "caddy" / "auth" / "roles.caddy"
    dicom_access_file = root / "caddy" / "auth" / "dicom-access.caddy"
    approval_file = root / "caddy" / "auth" / "identity-approval.txt"
    for path in (users_file, roles_file, dicom_access_file, approval_file):
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            fail(f"identity file must exist with mode 0600: {path}")
    users: set[str] = set()
    for line_no, raw in enumerate(users_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not BCRYPT.fullmatch(parts[1]) or parts[0] in users:
            fail(f"{users_file}:{line_no}: expected unique USER BCRYPT_HASH")
        if int(parts[1].split("$")[2]) < 12:
            fail(f"{users_file}:{line_no}: bcrypt cost must be at least 12")
        if parts[0] in {"clinical", "meld-admin", "meld-auditor"}:
            fail("shared bring-up role accounts are forbidden at hospital activation")
        users.add(parts[0])
    if len(users) < 3:
        fail("at least three unique institutional identities are required")
    role_text = roles_file.read_text(encoding="utf-8")
    access_text = dicom_access_file.read_text(encoding="utf-8")
    admin_users: set[str] = set()
    for user in users:
        role_match = re.search(rf'(?m)^{re.escape(user)}\s+"([^"]+)"$', role_text)
        if role_match is None:
            fail(f"roles.caddy has no explicit role mapping for {user}")
        access_match = re.search(rf'(?m)^{re.escape(user)}\s+"(allow|deny)"$', access_text)
        if access_match is None:
            fail(f"dicom-access.caddy has no explicit decision for {user}")
        roles = set(role_match.group(1).split())
        unknown_roles = roles - {"submitter", "reviewer", "admin", "auditor"}
        if unknown_roles:
            fail(f"roles.caddy has an unsupported role for {user}")
        if "admin" in roles:
            admin_users.add(user)
        expected_access = "allow" if roles.intersection({"reviewer", "admin"}) else "deny"
        if access_match.group(1) != expected_access:
            fail(f"DICOM access for {user} is inconsistent with reviewer/admin roles")
    if len(admin_users) < 3:
        fail("cohort building requires three distinct institutional admin identities")
    approval_lines = [line.strip() for line in approval_file.read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
    if not approval_lines or approval_lines[0] != "INSTITUTIONAL_UNIQUE_IDENTITY_APPROVED":
        fail("hospital IAM/security approval token is absent")

    cert, key = root / "tls" / "tls.crt", root / "tls" / "tls.key"
    if not cert.is_file() or not key.is_file():
        fail("institutional TLS certificate/key are missing")
    if stat.S_IMODE(key.stat().st_mode) & 0o077:
        fail("TLS private key must be mode 0600")
    if openssl("x509", "-checkend", "2592000", "-noout", "-in", str(cert)).returncode:
        fail("TLS certificate is invalid or expires in less than 30 days")
    if openssl("x509", "-checkhost", caddy["SITE_ADDRESS"], "-noout", "-in", str(cert)).returncode:
        fail("TLS certificate does not cover SITE_ADDRESS")
    cert_pub = openssl("x509", "-pubkey", "-noout", "-in", str(cert)).stdout
    key_pub = openssl("pkey", "-pubout", "-in", str(key)).stdout
    if not cert_pub or cert_pub != key_pub:
        fail("TLS certificate and private key do not match")

    immudb_public = root / "trust" / "immudb-signing-public.pem"
    if (not immudb_public.is_file()
            or openssl("pkey", "-pubin", "-in", str(immudb_public), "-noout").returncode):
        fail("pinned immudb signing public key is absent or invalid")
    if api["MELD7T_IMMUDB_ROOT_STATE_PATH"] != "/var/lib/meld7t/immudb-state/api.root":
        fail("API immudb root state must use its dedicated persistent volume")
    if api["MELD7T_IMMUDB_PUBLIC_KEY_PATH"] != "/run/secrets/immudb-public-key.pem":
        fail("API immudb public key must use the read-only secret mount")
    if worker["MELD7T_IMMUDB_ROOT_STATE_PATH"] == api["MELD7T_IMMUDB_ROOT_STATE_PATH"]:
        fail("API and worker immudb root state files must be distinct")
    if not Path(worker["MELD7T_IMMUDB_ROOT_STATE_PATH"]).is_absolute():
        fail("worker immudb root state path must be absolute")
    if worker["MELD7T_IMMUDB_PUBLIC_KEY_PATH"] != str(immudb_public):
        fail("worker must use the installed pinned immudb signing public key")

    manifest_digest = api["MELD7T_RELEASE_MANIFEST_DIGEST"].lower().removeprefix("sha256:")
    if not HEX64.fullmatch(manifest_digest):
        fail("invalid release manifest digest")
    if worker["MELD7T_RELEASE_MANIFEST_DIGEST"].lower().removeprefix("sha256:") != manifest_digest:
        fail("API and worker release manifest digests differ")
    if runtime.get("MELD7T_RELEASE_MANIFEST_DIGEST", "").lower().removeprefix("sha256:") != manifest_digest:
        fail("runtime-images.env release digest does not match API/worker")
    if runtime.get("MELD7T_GIT_SHA") != worker["MELD7T_GIT_SHA"]:
        fail("runtime-images.env and worker git revisions differ")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", worker["MELD7T_GIT_SHA"]) is None:
        fail("worker git revision is not a full commit hash")
    if HEX64.fullmatch(worker["MELD7T_OS_CHECKSUM"]) is None:
        fail("worker OS checksum is not a 64-character ostree checksum")
    if HEX64.fullmatch(runtime["MELD7T_MAP_SCRIPT_SHA256"]) is None:
        fail("runtime MAP script digest is not a 64-character SHA-256")
    if HEX64.fullmatch(runtime["MELD7T_HIPPUNFOLD_CACHE_SHA256"]) is None:
        fail("runtime HippUnfold cache digest is not a 64-character SHA-256")
    if HEX64.fullmatch(runtime["MELD7T_HARMONIZATION_INVENTORY_SHA256"]) is None:
        fail("runtime expected harmonization inventory digest is not a 64-character SHA-256")
    if (runtime["MELD7T_HARMONIZATION_INVENTORY_SHA256"]
            != release["MELD7T_HARMONIZATION_INVENTORY_SHA256"]):
        fail("runtime harmonization inventory digest differs from the signed release value")

    images = load_lock(args.image_lock)
    mapping = {"MELD7T_PKG_IMAGE": "pkg", "MELD7T_MELD_IMAGE": "meld_graph",
               "MELD7T_HIPPUNFOLD_IMAGE": "hippunfold", "MELD7T_MAP_IMAGE": "spm"}
    for variable, role in mapping.items():
        if runtime.get(variable) != images.get(role):
            fail(f"{variable} is not the signed {role} digest")

    staging = Path(worker["MELD7T_DICOM_STAGING"])
    meld_data = Path(worker["MELD7T_MELD_DATA"])
    if not staging.is_absolute() or meld_data not in staging.parents:
        fail("DICOM staging must be an absolute child of local MELD data")
    print("production configuration relationships validated")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"validate-production-config: {exc}", file=sys.stderr)
        raise SystemExit(1)
