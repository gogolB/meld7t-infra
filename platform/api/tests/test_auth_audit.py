"""Focused authentication, authorization, and audit-integrity tests."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.requests import Request

from app import audit, auth
from app.config import LocalAuthToken, Settings
from app.models import AuditRecord, OutboxEvent, OutboxStatus
from app.models import Case
from app.routes import _authorize_case


LONG_USER_TOKEN = "user-token-" + "a" * 48
LONG_SERVICE_TOKEN = "service-token-" + "b" * 48
SERVER_DB = "postgresql+psycopg://meld:" + "d" * 40 + "@postgres:5432/meld"
SERVER_REDIS = "redis://:" + "r" * 40 + "@redis:6379/0"
SERVER_IMMUDB_PASSWORD = SecretStr("i" * 40)
SERVER_AUDIT_HMAC_KEY = SecretStr("h" * 40)
SERVER_IMMUDB_STATE = "/var/lib/meld7t-audit/root.state"
SERVER_IMMUDB_PUBLIC_KEY = "/run/meld7t/immudb-signing-public.pem"
SERVER_RELEASE_DIGEST = "a" * 64
SERVER_HARMONIZATION_ORTHANC_PASSWORD = SecretStr("o" * 40)


def _settings(**overrides) -> Settings:
    # Explicit values isolate these constructor tests from the API suite's test-mode environment.
    values = {
        "deployment_mode": "research",
        "auth_dev_bypass": False,
        "db_url": SERVER_DB,
        "redis_url": SERVER_REDIS,
        "immudb_password": SERVER_IMMUDB_PASSWORD,
        "audit_hmac_key": SERVER_AUDIT_HMAC_KEY,
        "audit_require_immudb": False,  # documented research degraded-mode override
        "release_manifest_digest": SERVER_RELEASE_DIGEST,
        "harmonization_orthanc_password": SERVER_HARMONIZATION_ORTHANC_PASSWORD,
        "auth_local_tokens": [_user_credential()],
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _request(headers: dict[str, str] | None = None, peer: str = "127.0.0.1") -> Request:
    raw_headers = [
        (name.lower().encode("ascii"), value.encode("utf-8"))
        for name, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/test",
        "raw_path": b"/api/test",
        "query_string": b"",
        "headers": raw_headers,
        "client": (peer, 12345),
        "server": ("testserver", 443),
    })


def _user_credential() -> LocalAuthToken:
    return LocalAuthToken(
        subject="researcher@example.test",
        token=SecretStr(LONG_USER_TOKEN),
        roles={"submitter", "reviewer"},
    )


def _service_credential() -> LocalAuthToken:
    return LocalAuthToken(
        subject="worker-01",
        token=SecretStr(LONG_SERVICE_TOKEN),
        roles={"service"},
        service=True,
    )


def test_research_mode_rejects_anonymous(monkeypatch):
    monkeypatch.setattr(auth, "settings", _settings())
    with pytest.raises(HTTPException) as raised:
        auth.get_principal(_request())
    assert raised.value.status_code == 401


def test_service_intake_can_be_assigned_to_named_submitter():
    case = Case(
        pseudonym="ASSIGNED", created_by="service:intake",
        assigned_to="user:researcher@example.test", orthanc_study_uid="1.2.840.99",
    )
    assigned = auth.Principal(
        subject="researcher@example.test", roles=frozenset({auth.Role.submitter}),
        auth_method="bearer_token", request_id="assigned-test",
    )
    _authorize_case(assigned, case, mutate=True)
    unassigned = auth.Principal(
        subject="someone-else@example.test", roles=frozenset({auth.Role.submitter}),
        auth_method="bearer_token", request_id="unassigned-test",
    )
    with pytest.raises(HTTPException, match="case access denied"):
        _authorize_case(unassigned, case, mutate=True)
    intake = auth.Principal(
        subject="different-intake", roles=frozenset({auth.Role.service}),
        auth_method="service_token", request_id="intake-test", service=True,
    )
    with pytest.raises(HTTPException, match="case access denied"):
        _authorize_case(intake, case)


def test_bearer_and_service_tokens_create_typed_principals(monkeypatch):
    monkeypatch.setattr(
        auth,
        "settings",
        _settings(auth_local_tokens=[_user_credential(), _service_credential()]),
    )
    user = auth.get_principal(_request({
        "Authorization": f"Bearer {LONG_USER_TOKEN}",
        "X-Request-ID": "test-request-123",
    }))
    assert user.subject == "researcher@example.test"
    assert user.roles == frozenset({auth.Role.submitter, auth.Role.reviewer})
    assert user.actor == "user:researcher@example.test"
    assert user.request_id == "test-request-123"

    service = auth.get_principal(_request({"X-Service-Token": LONG_SERVICE_TOKEN}))
    assert service.service is True
    assert service.auth_method == "service_token"
    assert auth.require_service(service) is service
    with pytest.raises(HTTPException) as raised:
        auth.require_admin(user)
    assert raised.value.status_code == 403


def test_proxy_headers_only_from_allowlisted_peer(monkeypatch):
    monkeypatch.setattr(
        auth,
        "settings",
        _settings(
            auth_trusted_proxy_networks=["10.70.0.0/24"],
            auth_proxy_shared_secret=SecretStr("proxy-secret-" + "p" * 40),
        ),
    )
    headers = {
        "X-Remote-User": "reviewer@example.test",
        "X-Remote-Roles": "reviewer,auditor",
        "X-MELD-Proxy-Secret": "proxy-secret-" + "p" * 40,
    }
    principal = auth.get_principal(_request(headers, peer="10.70.0.8"))
    assert principal.auth_method == "trusted_proxy"
    assert principal.roles == frozenset({auth.Role.reviewer, auth.Role.auditor})

    with pytest.raises(HTTPException) as raised:
        auth.get_principal(_request(headers, peer="10.71.0.8"))
    assert raised.value.status_code == 401


def test_development_bypass_must_be_deliberate(monkeypatch):
    with pytest.raises(ValidationError):
        _settings(auth_dev_bypass=True)

    dev = Settings(
        _env_file=None,
        deployment_mode="development",
        auth_dev_bypass=True,
        harmonization_required=False,
        audit_require_immudb=False,
    )
    monkeypatch.setattr(auth, "settings", dev)
    principal = auth.get_principal(_request())
    assert principal.auth_method == "development_bypass"
    assert auth.Role.admin in principal.roles
    assert dev.is_development is True
    assert dev.is_server_mode is False
    assert dev.harmonization_required is False

    research = _settings(harmonization_required=True)
    assert research.is_server_mode is True
    assert research.harmonization_required is True
    assert research.harmonization_root == "/data/harmonization"


def test_bootstrap_authorization_requires_empty_expected_inventory():
    with pytest.raises(ValidationError, match="requires an empty"):
        _settings(
            harmonization_cohort_bootstrap_allowed=True,
            harmonization_expected_profiles=[{
                "code": "HSITE", "version": 1, "detector_id": "meld_fcd",
                "document_sha256": "f" * 64,
            }],
        )


def test_production_rejects_placeholder_secrets_and_missing_identity():
    with pytest.raises(ValidationError, match="db_url"):
        Settings(
            _env_file=None,
            deployment_mode="research",
            auth_dev_bypass=False,
            db_url="sqlite:///not-allowed.db",
            redis_url=SERVER_REDIS,
            immudb_password=SERVER_IMMUDB_PASSWORD,
            audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
            audit_require_immudb=False,
        )
    with pytest.raises(ValidationError, match="placeholder password"):
        Settings(
            _env_file=None,
            deployment_mode="research",
            auth_dev_bypass=False,
            db_url="postgresql+psycopg://meld:meld@postgres:5432/meld",
            redis_url=SERVER_REDIS,
            immudb_password=SERVER_IMMUDB_PASSWORD,
            audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
            audit_require_immudb=False,
        )
    with pytest.raises(ValidationError, match="immudb_password"):
        Settings(
            _env_file=None,
            deployment_mode="research",
            auth_dev_bypass=False,
            db_url=SERVER_DB,
            redis_url=SERVER_REDIS,
            immudb_password=SecretStr("immudb"),
            audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
            audit_require_immudb=False,
        )
    with pytest.raises(ValidationError, match="redis_url"):
        Settings(
            _env_file=None,
            deployment_mode="research",
            auth_dev_bypass=False,
            db_url=SERVER_DB,
            redis_url="redis://redis:6379/0",
            immudb_password=SERVER_IMMUDB_PASSWORD,
            audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
            audit_require_immudb=False,
            auth_local_tokens=[_user_credential()],
        )

    with pytest.raises(ValidationError, match="trusted proxy|local token"):
        Settings(
            _env_file=None,
            deployment_mode="production",
            auth_dev_bypass=False,
            db_url=SERVER_DB,
            redis_url=SERVER_REDIS,
            immudb_password=SERVER_IMMUDB_PASSWORD,
            harmonization_orthanc_password=SERVER_HARMONIZATION_ORTHANC_PASSWORD,
            audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
            audit_require_immudb=True,
        )

    configured = Settings(
        _env_file=None,
        deployment_mode="production",
        auth_dev_bypass=False,
        db_url=SERVER_DB,
        redis_url=SERVER_REDIS,
        immudb_password=SERVER_IMMUDB_PASSWORD,
        harmonization_orthanc_password=SERVER_HARMONIZATION_ORTHANC_PASSWORD,
        audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
        audit_require_immudb=True,
        immudb_root_state_path=SERVER_IMMUDB_STATE,
        immudb_public_key_path=SERVER_IMMUDB_PUBLIC_KEY,
        release_manifest_digest=SERVER_RELEASE_DIGEST,
        auth_local_tokens=[_user_credential()],
    )
    assert configured.deployment_mode == "production"

    with pytest.raises(ValidationError, match="audit_require_immudb"):
        Settings(
            _env_file=None,
            deployment_mode="production",
            auth_dev_bypass=False,
            db_url=SERVER_DB,
            redis_url=SERVER_REDIS,
            immudb_password=SERVER_IMMUDB_PASSWORD,
            harmonization_orthanc_password=SERVER_HARMONIZATION_ORTHANC_PASSWORD,
            audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
            audit_require_immudb=False,
            immudb_root_state_path=SERVER_IMMUDB_STATE,
            immudb_public_key_path=SERVER_IMMUDB_PUBLIC_KEY,
            release_manifest_digest=SERVER_RELEASE_DIGEST,
            auth_local_tokens=[_user_credential()],
        )


def test_server_proxy_network_and_secret_must_be_paired():
    with pytest.raises(ValidationError, match="requires both"):
        _settings(auth_trusted_proxy_networks=["10.70.0.0/24"])
    with pytest.raises(ValidationError, match="requires both"):
        _settings(auth_proxy_shared_secret=SecretStr("p" * 40))


def test_server_requires_signed_release_identity():
    with pytest.raises(ValidationError, match="release_manifest_digest"):
        _settings(release_manifest_digest=None)


def test_server_defaults_require_harmonization_and_immudb(monkeypatch):
    monkeypatch.delenv("MELD7T_AUDIT_REQUIRE_IMMUDB", raising=False)
    monkeypatch.delenv("MELD7T_HARMONIZATION_REQUIRED", raising=False)
    monkeypatch.delenv("MELD7T_AUTH_DEV_BYPASS", raising=False)
    configured = Settings(
        _env_file=None,
        deployment_mode="research",
        db_url=SERVER_DB,
        redis_url=SERVER_REDIS,
        immudb_password=SERVER_IMMUDB_PASSWORD,
        harmonization_orthanc_password=SERVER_HARMONIZATION_ORTHANC_PASSWORD,
        audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
        release_manifest_digest=SERVER_RELEASE_DIGEST,
        auth_local_tokens=[_user_credential()],
    )
    assert configured.audit_require_immudb is True
    assert configured.harmonization_required is True


def test_normal_production_worker_does_not_need_cohort_store_credential():
    # The normal worker imports app.config for shared queue/audit contracts but is deliberately
    # denied the separate harmonization Orthanc secret.
    configured = Settings(
        _env_file=None,
        deployment_mode="production",
        auth_dev_bypass=False,
        db_url=SERVER_DB,
        redis_url=SERVER_REDIS,
        immudb_password=SERVER_IMMUDB_PASSWORD,
        audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
        audit_require_immudb=True,
        immudb_root_state_path=SERVER_IMMUDB_STATE,
        immudb_public_key_path=SERVER_IMMUDB_PUBLIC_KEY,
        release_manifest_digest=SERVER_RELEASE_DIGEST,
        harmonization_required=True,
        auth_local_tokens=[_service_credential()],
    )
    assert configured.harmonization_required is True
    assert "harmonization_orthanc_password" not in configured.model_fields_set
    with pytest.raises(ValueError, match="API harmonization Orthanc password"):
        configured.require_harmonization_orthanc_credentials()

    with pytest.raises(ValidationError, match="harmonization Orthanc password"):
        Settings(
            _env_file=None,
            deployment_mode="production",
            auth_dev_bypass=False,
            db_url=SERVER_DB,
            redis_url=SERVER_REDIS,
            immudb_password=SERVER_IMMUDB_PASSWORD,
            harmonization_orthanc_password=SecretStr("change-me"),
            audit_hmac_key=SERVER_AUDIT_HMAC_KEY,
            audit_require_immudb=True,
            immudb_root_state_path=SERVER_IMMUDB_STATE,
            immudb_public_key_path=SERVER_IMMUDB_PUBLIC_KEY,
            release_manifest_digest=SERVER_RELEASE_DIGEST,
            auth_local_tokens=[_service_credential()],
        )


@pytest.fixture
def audit_session(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class _MemoryLedger:
    def __init__(self, *, available: bool = True):
        self.available = available
        self.values: dict[tuple[str, int], str] = {}

    def verified_set(self, key: str, value: str) -> int | None:
        if not self.available:
            return None
        tx_id = len(self.values) + 1
        self.values[(key, tx_id)] = value
        return tx_id

    def verify_entry(self, key: str, value: str, tx_id: int) -> audit.LedgerVerification:
        if not self.available:
            return audit.LedgerVerification("unavailable", tx_id, "offline")
        if self.values.get((key, tx_id)) != value:
            return audit.LedgerVerification("mismatch", tx_id, "value differs")
        return audit.LedgerVerification("verified", tx_id)


def test_v2_chain_hashes_identity_timestamp_sequence_and_full_payload(
    audit_session: Session, monkeypatch
):
    ledger = _MemoryLedger()
    monkeypatch.setattr(audit, "_immu", ledger)
    first = audit.record(
        audit_session,
        actor="user:alice",
        action="case.create",
        entity_type="case",
        entity_id="case-1",
        payload={"nested": {"notes": "complete payload"}},
    )
    second = audit.record(
        audit_session,
        actor="service:worker",
        action="run.start",
        entity_type="run",
        entity_id="run-1",
        payload={"detector": "meld"},
    )
    audit_session.commit()

    assert first.payload["_audit"]["sequence"] == 1
    assert second.payload["_audit"]["sequence"] == 2
    assert (first.sequence, second.sequence) == (1, 2)
    assert first.ledger_status == second.ledger_status == "verified"
    assert first.payload["_audit"]["local_status"] == "postgres_hash_chain"
    verification = audit.verify_chain(audit_session)
    assert verification["ok"] is True
    assert verification["fully_verified"] is True
    assert verification["immudb"]["status"] == "verified"

    # Timestamp and ID are part of the v2 digest, in addition to the complete stored payload.
    original_hash = first.payload_hash
    first.ts = first.ts + timedelta(seconds=1)
    assert audit._expected_hash(first, audit.GENESIS) != original_hash


def test_local_only_audit_never_claims_immudb_success(audit_session: Session, monkeypatch):
    monkeypatch.setattr(audit, "_immu", _MemoryLedger(available=False))
    row = audit.record(
        audit_session,
        actor="user:alice",
        action="report.read",
        entity_type="run",
        entity_id="run-2",
    )
    audit_session.commit()
    assert row.immudb_tx_id is None

    verification = audit.verify_chain(audit_session)
    assert verification["ok"] is True  # local integrity; immudb is optional in test settings
    assert verification["fully_verified"] is False
    assert verification["immudb"]["status"] == "not_mirrored"
    assert verification["immudb"]["missing"] == 1


def test_pending_audit_can_be_reconciled_from_durable_state(
    audit_session: Session, monkeypatch
):
    ledger = _MemoryLedger(available=False)
    monkeypatch.setattr(audit, "_immu", ledger)
    row = audit.record(
        audit_session,
        actor="user:alice",
        action="case.read",
        entity_type="case",
        entity_id="case-reconcile",
    )
    audit_session.commit()
    event = audit_session.exec(select(OutboxEvent).where(
        OutboxEvent.aggregate_id == row.id)).one()
    assert row.ledger_status == "pending"
    assert event.status == OutboxStatus.pending

    ledger.available = True
    audit.mirror_record(audit_session, row.id)
    audit_session.commit()
    audit_session.refresh(row)
    assert row.ledger_status == "verified"
    assert row.immudb_tx_id is not None


def test_chain_detects_tampering(audit_session: Session, monkeypatch):
    monkeypatch.setattr(audit, "_immu", _MemoryLedger())
    row = audit.record(
        audit_session,
        actor="user:alice",
        action="case.read",
        entity_type="case",
        entity_id="case-3",
        payload={"purpose": "research"},
    )
    audit_session.commit()
    row.action = "case.delete"
    audit_session.add(row)
    audit_session.commit()

    verification = audit.verify_chain(audit_session, verify_immudb=False)
    assert verification["ok"] is False
    assert verification["reason"] == "hash_mismatch"


def test_immudb_connection_failure_is_not_cached_as_success(monkeypatch):
    connector = audit._Immudb()
    attempts = 0
    connected = SimpleNamespace(shutdown=lambda: None)

    def connect():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("offline")
        return connected

    monkeypatch.setattr(connector, "_connect", connect)
    monkeypatch.setattr(audit.settings, "audit_require_immudb", False)
    assert connector.client() is None
    monkeypatch.setattr(audit.settings, "audit_require_immudb", True)
    assert connector.client() is connected
    assert attempts == 2


def test_ledger_health_is_readiness_safe(monkeypatch):
    connector = audit._Immudb()
    connected = SimpleNamespace(
        health=lambda: {"status": True},
        currentState=lambda: {"tx_id": 12},
        shutdown=lambda: None,
    )
    monkeypatch.setattr(connector, "_connect", lambda: connected)
    monkeypatch.setattr(audit, "_immu", connector)
    monkeypatch.setattr(audit.settings, "audit_require_immudb", True)
    result = audit.ledger_health()
    assert result == {
        "ready": True,
        "required": True,
        "status": "healthy",
        "proof_state": "verified_state_returned",
    }


def test_authenticated_audit_actor_cannot_come_from_request_body(
    audit_session: Session, monkeypatch
):
    monkeypatch.setattr(audit, "_immu", _MemoryLedger(available=False))
    principal = auth.Principal(
        subject="reviewer@example.test",
        roles=frozenset({auth.Role.reviewer}),
        auth_method="bearer_token",
        request_id="request-55",
    )
    audit.record_authenticated(
        audit_session,
        principal=principal,
        action="adjudication.create",
        entity_type="run",
        entity_id="run-55",
        payload={"reviewer": "forged-body-name"},
    )
    audit_session.commit()
    row = audit_session.exec(select(AuditRecord)).one()
    assert row.actor == "user:reviewer@example.test"
    assert row.payload["event"]["request_id"] == "request-55"
