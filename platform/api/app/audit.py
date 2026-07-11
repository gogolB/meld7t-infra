"""Append-only audit mirror with an independently verifiable immudb copy.

Each v2 record hashes its sequence, immutable ID, timestamp, actor/action/entity fields, complete
stored payload, local durability marker, and previous hash.  PostgreSQL serializes appends with a
transaction-scoped advisory lock.  immudb success is recorded only when ``verifiedSet`` returns a
verified transaction ID; verification reads the exact key/value back with a proof-capable API.

PostgreSQL and immudb cannot form one atomic transaction. A null ``immudb_tx_id`` therefore means
exactly "not proven mirrored" and is reported as such. Durable ledger state and an outbox reconcile
transient optional-ledger failures; required-ledger deployments still fail the originating SQL
transaction closed when the synchronous proof write cannot be verified.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import func, text
from sqlmodel import Session, select

from .config import settings
from .models import AuditRecord, OutboxEvent, OutboxStatus

if TYPE_CHECKING:
    from .auth import Principal


GENESIS = "0" * 64
AUDIT_SCHEMA_VERSION = 2
_AUDIT_META_KEY = "_audit"
# Constant key for append serialization.  Without it concurrent writers can read the same previous
# hash and fork the chain.  SQLite serializes writes itself and is used only for tests/development.
_AUDIT_LOCK_KEY = 0x6D656C6461  # "melda"
_access_window_lock = threading.Lock()
_access_windows: dict[tuple[str, str, str, str], float] = {}


class AuditLedgerError(RuntimeError):
    """immudb could not prove an operation that policy required."""


@dataclass(frozen=True, slots=True)
class LedgerVerification:
    status: str
    tx_id: int | None = None
    detail: str | None = None


def _serialize_appends(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_LOCK_KEY})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sensitive_digest(value: Any) -> str:
    """Keyed digest for tamper correlation without dictionary-attackable PHI hashes."""
    return hmac.new(
        settings.audit_hmac_key.get_secret_value().encode(),
        _canonical(value).encode(),
        hashlib.sha256,
    ).hexdigest()


def _legacy_hash(payload: Optional[dict], prev_hash: str) -> str:
    # Retain verification compatibility for records created before schema v2.
    canonical = _canonical(payload or {})
    return hashlib.sha256((canonical + prev_hash).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _audit_meta(payload: Optional[dict]) -> dict | None:
    if not isinstance(payload, dict):
        return None
    meta = payload.get(_AUDIT_META_KEY)
    if not isinstance(meta, dict) or meta.get("schema_version") != AUDIT_SCHEMA_VERSION:
        return None
    return meta


def _body_for_record(record: AuditRecord) -> dict:
    """Return the exact v2 object whose canonical representation is hashed and mirrored."""
    meta = _audit_meta(record.payload)
    if meta is None:
        return {
            "actor": record.actor,
            "action": record.action,
            "entity_type": record.entity_type,
            "entity_id": record.entity_id,
            "payload": record.payload,
        }
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "sequence": meta["sequence"],
        "record_id": record.id,
        "timestamp": _timestamp(record.ts),
        "actor": record.actor,
        "action": record.action,
        "entity_type": record.entity_type,
        "entity_id": record.entity_id,
        "payload": record.payload,
        "prev_hash": record.prev_hash or GENESIS,
    }


def _expected_hash(record: AuditRecord, prev_hash: str) -> str:
    if _audit_meta(record.payload) is None:
        return _legacy_hash(_body_for_record(record), prev_hash)
    body = _body_for_record(record)
    # Use the chain value being verified rather than trusting a potentially modified column.
    body["prev_hash"] = prev_hash
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _ledger_value(record: AuditRecord) -> str:
    if _audit_meta(record.payload) is None:
        body = _body_for_record(record)
        return _canonical({**body, "hash": record.payload_hash,
                           "prev": record.prev_hash or GENESIS})
    return _canonical({"record": _body_for_record(record), "record_hash": record.payload_hash})


class _Immudb:
    """Lazy reconnecting immudb client with explicit verified outcomes.

    A failed connection is never cached as a successful/terminal state.  This fixes the prior bug
    where the first optional failure made later fail-closed calls return ``None`` silently.
    """

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.RLock()
        self.last_error: str | None = None

    def reset(self) -> None:
        """Drop the cached connection (also useful for tests and credential rotation)."""
        with self._lock:
            self._invalidate()
            self.last_error = None

    def _invalidate(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                pass

    def _connect(self):
        from immudb import ImmudbClient
        from immudb.client import PersistentRootService

        kwargs: dict[str, Any] = {"timeout": settings.immudb_timeout_seconds}
        if settings.immudb_root_state_path:
            kwargs["rs"] = PersistentRootService(settings.immudb_root_state_path)
        if settings.immudb_public_key_path:
            kwargs["publicKeyFile"] = settings.immudb_public_key_path
        client = ImmudbClient(f"{settings.immudb_host}:{settings.immudb_port}", **kwargs)
        client.login(
            settings.immudb_user,
            settings.immudb_password.get_secret_value(),
            database=settings.immudb_db,
        )
        return client

    def client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                self._client = self._connect()
                self.last_error = None
                return self._client
            except Exception as exc:
                self._invalidate()
                self.last_error = type(exc).__name__
                if settings.audit_require_immudb:
                    raise AuditLedgerError("immudb connection/login failed") from exc
                return None

    def verified_set(self, key: str, value: str) -> int | None:
        with self._lock:
            client = self.client()
            if client is None:
                return None
            try:
                encoded_key, encoded_value = key.encode("utf-8"), value.encode("utf-8")
                # The key is the immutable PostgreSQL audit UUID.  A crash can lose the Set
                # response after immudb committed it; read-before-write makes that retry a proof
                # check instead of appending another revision under the same key.
                from immudb.exceptions import ErrKeyNotFound
                try:
                    existing = client.verifiedGet(encoded_key)
                except ErrKeyNotFound:
                    existing = None
                if existing is not None:
                    existing_tx = int(
                        getattr(existing, "id", getattr(existing, "tx", 0)) or 0)
                    if (getattr(existing, "verified", None) is not True
                            or getattr(existing, "key", None) != encoded_key
                            or getattr(existing, "value", None) != encoded_value
                            or existing_tx <= 0):
                        raise AuditLedgerError(
                            "immudb audit key already exists with an unverified/different value"
                        )
                    self.last_error = None
                    return existing_tx
                response = client.verifiedSet(encoded_key, encoded_value)
                tx_id = int(getattr(response, "id", getattr(response, "txId", 0)) or 0)
                if tx_id <= 0 or getattr(response, "verified", None) is not True:
                    raise AuditLedgerError("immudb did not return a verified transaction")
                self.last_error = None
                return tx_id
            except Exception as exc:
                self._invalidate()
                self.last_error = type(exc).__name__
                if settings.audit_require_immudb:
                    if isinstance(exc, AuditLedgerError):
                        raise
                    raise AuditLedgerError("immudb verified write failed") from exc
                return None

    def verify_entry(self, key: str, value: str, tx_id: int) -> LedgerVerification:
        """Read an exact historical value using the strongest API exposed by immudb-py."""
        with self._lock:
            client = self.client()
            if client is None:
                return LedgerVerification("unavailable", tx_id, self.last_error)
            try:
                encoded_key = key.encode("utf-8")
                if hasattr(client, "verifiedGetAt"):
                    response = client.verifiedGetAt(encoded_key, tx_id)
                else:  # Defensive compatibility with older clients: still demands a proof result.
                    response = client.verifiedGet(encoded_key)
                response_tx = int(getattr(response, "id", getattr(response, "tx", 0)) or 0)
                verified = getattr(response, "verified", None) is True
                actual_key = getattr(response, "key", None)
                actual_value = getattr(response, "value", None)
                if not verified:
                    return LedgerVerification("unverified", tx_id, "proof flag was not true")
                if response_tx != tx_id:
                    return LedgerVerification("mismatch", tx_id, "transaction ID differs")
                if actual_key != encoded_key or actual_value != value.encode("utf-8"):
                    return LedgerVerification("mismatch", tx_id, "key/value differs")
                self.last_error = None
                return LedgerVerification("verified", tx_id)
            except Exception as exc:
                self._invalidate()
                self.last_error = type(exc).__name__
                if settings.audit_require_immudb:
                    raise AuditLedgerError("immudb proof verification failed") from exc
                return LedgerVerification("unavailable", tx_id, self.last_error)

    def health(self) -> dict:
        """Return a readiness-safe ledger health result without overstating proof support."""
        with self._lock:
            try:
                client = self.client()
            except AuditLedgerError as exc:
                return {
                    "ready": False,
                    "required": True,
                    "status": "unavailable",
                    "proof_state": "unavailable",
                    "detail": type(exc.__cause__ or exc).__name__,
                }
            if client is None:
                return {
                    "ready": not settings.audit_require_immudb,
                    "required": settings.audit_require_immudb,
                    "status": "degraded" if not settings.audit_require_immudb else "unavailable",
                    "proof_state": "unavailable",
                    "detail": self.last_error,
                }
            try:
                if hasattr(client, "health"):
                    client.health()
                else:
                    client.healthCheck()
                if hasattr(client, "currentState"):
                    state = client.currentState()
                    proof_state = "verified_state_returned" if state is not None else "missing"
                else:
                    proof_state = "unsupported"
                if proof_state == "missing":
                    raise AuditLedgerError("immudb returned no current proof state")
                self.last_error = None
                return {
                    "ready": True,
                    "required": settings.audit_require_immudb,
                    "status": "healthy",
                    "proof_state": proof_state,
                }
            except Exception as exc:
                self._invalidate()
                self.last_error = type(exc).__name__
                return {
                    "ready": not settings.audit_require_immudb,
                    "required": settings.audit_require_immudb,
                    "status": "unavailable" if settings.audit_require_immudb else "degraded",
                    "proof_state": "unavailable",
                    "detail": self.last_error,
                }


_immu = _Immudb()


def ledger_health() -> dict:
    """Health/readiness report for ``/readyz`` integration."""
    return _immu.health()


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: Optional[dict] = None,
) -> AuditRecord:
    """Append one event to the transactional PostgreSQL mirror and verified ledger.

    Callers own the surrounding database transaction.  If immudb is required, ledger failure raises
    ``AuditLedgerError`` so the request transaction can roll back.  In optional mode the local row
    remains explicit with a null ``immudb_tx_id`` and verification reports it as not mirrored.
    """
    _serialize_appends(session)
    previous = session.exec(
        select(AuditRecord).order_by(AuditRecord.sequence.desc()).limit(1)
    ).first()
    prev_hash = previous.payload_hash if previous else GENESIS
    count = int(session.exec(select(func.count(AuditRecord.id))).one())

    timestamp = datetime.now(timezone.utc)
    if previous is not None and timestamp <= _utc(previous.ts):
        timestamp = _utc(previous.ts) + timedelta(microseconds=1)
    stored_payload = {
        _AUDIT_META_KEY: {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "sequence": count + 1,
            # A committed row is a durable local hash-chain mirror.  Since callers own commit, a
            # rolled-back event leaves no row and therefore cannot falsely retain this marker.
            "local_status": "postgres_hash_chain",
        },
        "event": payload,
    }
    audit_record = AuditRecord(
        sequence=count + 1,
        ts=timestamp,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=stored_payload,
        payload_hash="",
        prev_hash=prev_hash,
        local_status="postgres_hash_chain",
        ledger_status="pending",
    )
    audit_record.payload_hash = _expected_hash(audit_record, prev_hash)

    # Flush locally first so DB constraints fail before an irreversible external append. The
    # outbox row commits atomically with this record and reconciles optional/degraded writes.
    session.add(audit_record)
    session.flush()
    event = OutboxEvent(
        dedupe_key=f"audit.mirror:{audit_record.id}",
        topic="audit.mirror",
        aggregate_type="audit",
        aggregate_id=audit_record.id,
        payload={"audit_record_id": audit_record.id},
    )
    audit_record.ledger_attempts += 1
    try:
        audit_record.immudb_tx_id = _immu.verified_set(
            f"audit:{audit_record.id}", _ledger_value(audit_record)
        )
    except AuditLedgerError as exc:
        audit_record.ledger_status = "failed"
        audit_record.ledger_last_error = type(exc.__cause__ or exc).__name__
        raise
    if audit_record.immudb_tx_id is not None:
        audit_record.ledger_status = "verified"
        audit_record.ledger_last_error = None
        audit_record.ledger_verified_at = datetime.now(timezone.utc)
        event.status = OutboxStatus.published
        event.published_at = audit_record.ledger_verified_at
    else:
        audit_record.ledger_status = "pending"
        audit_record.ledger_last_error = getattr(_immu, "last_error", None) or "unavailable"
        if settings.audit_require_immudb:
            raise AuditLedgerError("immudb write was not verified")
    session.add(audit_record)
    session.add(event)
    session.flush()
    return audit_record


def mirror_record(session: Session, record_id: str) -> AuditRecord:
    """Reconcile one committed audit record from the durable outbox.

    A retry after a crash first observes a previously committed ``verified`` state and becomes a
    no-op. Cross-store atomicity is impossible, but the local status can no longer remain silently
    unmirrored after a transient optional-ledger outage.
    """
    statement = select(AuditRecord).where(AuditRecord.id == record_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = session.exec(statement).first()
    if row is None:
        raise AuditLedgerError(f"audit record {record_id!r} does not exist")
    if row.ledger_status == "verified" and row.immudb_tx_id is not None:
        return row
    row.ledger_attempts += 1
    try:
        tx_id = _immu.verified_set(f"audit:{row.id}", _ledger_value(row))
    except AuditLedgerError as exc:
        row.ledger_status = "failed"
        row.ledger_last_error = type(exc.__cause__ or exc).__name__
        session.add(row)
        raise
    if tx_id is None:
        row.ledger_status = "failed"
        row.ledger_last_error = getattr(_immu, "last_error", None) or "unavailable"
        session.add(row)
        raise AuditLedgerError("immudb reconciliation did not return a verified transaction")
    row.immudb_tx_id = tx_id
    row.ledger_status = "verified"
    row.ledger_last_error = None
    row.ledger_verified_at = datetime.now(timezone.utc)
    session.add(row)
    return row


def _ledger_summary(rows: list[AuditRecord], *, verify_immudb: bool) -> dict:
    mirrored = sum(row.immudb_tx_id is not None for row in rows)
    missing = len(rows) - mirrored
    summary: dict[str, Any] = {
        "required": settings.audit_require_immudb,
        "status": "not_checked",
        "records": len(rows),
        "mirrored": mirrored,
        "missing": missing,
        "verified": 0,
        "states": {
            status: sum(row.ledger_status == status for row in rows)
            for status in ("pending", "verified", "failed")
        },
    }
    if not verify_immudb:
        return summary
    if not rows:
        summary["status"] = "empty"
        return summary
    if mirrored == 0:
        summary["status"] = "not_mirrored"
        return summary

    failures: list[dict] = []
    for row in rows:
        if row.immudb_tx_id is None:
            continue
        try:
            result = _immu.verify_entry(
                f"audit:{row.id}", _ledger_value(row), int(row.immudb_tx_id)
            )
        except AuditLedgerError as exc:
            # Verification is a diagnostic operation: required-ledger policy makes the final
            # result fail, but callers still need a structured report rather than an opaque 500.
            result = LedgerVerification(
                "unavailable", int(row.immudb_tx_id), type(exc.__cause__ or exc).__name__
            )
        if result.status == "verified":
            summary["verified"] += 1
            continue
        failures.append({"record_id": row.id, "tx_id": row.immudb_tx_id,
                         "status": result.status, "detail": result.detail})
        if result.status == "unavailable":
            # One shared connection failure makes further immediate checks unhelpful.
            break

    if failures:
        summary["status"] = failures[0]["status"]
        summary["failures"] = failures
    elif missing:
        summary["status"] = "incomplete"
    elif summary["verified"] == len(rows):
        summary["status"] = "verified"
    else:
        summary["status"] = "unverified"
    return summary


def verify_chain(session: Session, *, verify_immudb: bool = True) -> dict:
    """Verify the local chain and, by default, every recorded immudb transaction/proof.

    ``ok`` preserves its historical meaning (local chain integrity) when immudb is optional, while
    ``fully_verified`` is true only when both stores verify.  When immudb is required, ``ok`` also
    fails for missing/unverified ledger entries.  The response can therefore never be mistaken for
    an immudb success merely because the PostgreSQL chain is intact.
    """
    rows = list(session.exec(
        select(AuditRecord).order_by(AuditRecord.sequence)
    ).all())
    prev_hash = GENESIS
    local_statuses: dict[str, int] = {}
    local_error: dict | None = None
    for index, row in enumerate(rows):
        if row.sequence != index + 1 or row.local_status != "postgres_hash_chain":
            local_error = {"broken_at": index, "record_id": row.id,
                           "reason": "durable_sequence_or_status_mismatch"}
            break
        meta = _audit_meta(row.payload)
        if meta is not None:
            status_name = str(meta.get("local_status", "missing"))
            local_statuses[status_name] = local_statuses.get(status_name, 0) + 1
            if meta.get("sequence") != index + 1:
                local_error = {"broken_at": index, "record_id": row.id,
                               "reason": "sequence_mismatch"}
                break
        else:
            local_statuses["legacy"] = local_statuses.get("legacy", 0) + 1
        expected = _expected_hash(row, prev_hash)
        if row.prev_hash != prev_hash or row.payload_hash != expected:
            local_error = {"broken_at": index, "record_id": row.id,
                           "reason": "hash_mismatch"}
            break
        prev_hash = row.payload_hash

    local_ok = local_error is None
    ledger = _ledger_summary(rows, verify_immudb=verify_immudb)
    ledger_ok = ledger["status"] in {"verified", "empty"}
    overall_ok = local_ok and (ledger_ok or not settings.audit_require_immudb)
    response: dict[str, Any] = {
        "ok": overall_ok,
        "fully_verified": local_ok and ledger_ok,
        "count": len(rows),
        "local": {"ok": local_ok, "statuses": local_statuses},
        "immudb": ledger,
    }
    if local_error:
        response.update(local_error)
        response["local"].update(local_error)
    return response


def actor_from_principal(principal: "Principal") -> str:
    """Derive an audit actor exclusively from the authenticated server-side principal."""
    return principal.actor


def record_authenticated(
    session: Session,
    *,
    principal: "Principal",
    action: str,
    entity_type: str,
    entity_id: str,
    payload: Optional[dict] = None,
) -> AuditRecord:
    """Record a consequential event with authentication and correlation context."""
    return record(
        session,
        actor=actor_from_principal(principal),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload={
            "data": payload,
            "authentication": {
                "subject": principal.subject,
                "roles": sorted(role.value for role in principal.roles),
                "method": principal.auth_method,
                "service": principal.service,
            },
            "request_id": principal.request_id,
        },
    )


def record_access(
    session: Session,
    *,
    principal: "Principal",
    entity_type: str,
    entity_id: str,
    operation: str = "read",
    outcome: str = "allowed",
    detail: Optional[dict] = None,
) -> AuditRecord:
    """Record PHI/result access without copying the accessed content into the ledger."""
    return record_authenticated(
        session,
        principal=principal,
        action=f"access.{operation}",
        entity_type=entity_type,
        entity_id=entity_id,
        payload={"outcome": outcome, "detail": detail},
    )


def record_access_coalesced(
    session: Session,
    *,
    principal: "Principal",
    entity_type: str,
    entity_id: str,
    operation: str = "read",
    outcome: str = "allowed",
    detail: Optional[dict] = None,
    window_seconds: int = 300,
) -> AuditRecord | None:
    """Record high-frequency status/list access at most once per identity/window.

    Content reads and exports continue to use ``record_access`` for every request. This helper is
    only for polling endpoints whose reverse-proxy request log remains the per-request record.
    """
    key = (principal.actor, entity_type, entity_id, operation)
    now = time.monotonic()
    with _access_window_lock:
        previous = _access_windows.get(key)
        if previous is not None and now - previous < window_seconds:
            return None
        if len(_access_windows) > 10_000:
            cutoff = now - window_seconds
            for stale in [item for item, timestamp in _access_windows.items()
                          if timestamp < cutoff]:
                _access_windows.pop(stale, None)
        _access_windows[key] = now
    try:
        return record_access(
            session, principal=principal, entity_type=entity_type, entity_id=entity_id,
            operation=operation, outcome=outcome, detail=detail,
        )
    except Exception:
        with _access_window_lock:
            if _access_windows.get(key) == now:
                _access_windows.pop(key, None)
        raise


def rebuild_chain(session: Session) -> dict:
    """Repair a development-only local chain that has never been mirrored to immudb.

    Rewriting hashes already present in an immutable ledger would create a deceptive mirror, so
    this operation now refuses any dataset containing a ledger transaction.
    """
    _serialize_appends(session)
    rows = list(session.exec(
        select(AuditRecord).order_by(AuditRecord.sequence)
    ).all())
    if any(row.immudb_tx_id is not None for row in rows):
        raise AuditLedgerError("refusing to rebuild records already mirrored to immudb")
    prev_hash, repaired = GENESIS, 0
    for row in rows:
        new_hash = _expected_hash(row, prev_hash)
        if row.prev_hash != prev_hash or row.payload_hash != new_hash:
            row.prev_hash, row.payload_hash = prev_hash, new_hash
            session.add(row)
            repaired += 1
        prev_hash = row.payload_hash
    session.commit()
    return {"ok": True, "count": len(rows), "repaired": repaired,
            "warning": "local-only development repair"}
