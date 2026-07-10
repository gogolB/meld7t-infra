"""Immutable audit ledger (spec §24, §26).

Every consequential event — case state transitions, recipe/run creation, adjudications, view/
access — is appended here. Design: immudb is the tamper-proof Merkle-backed store; a mirror row
in Postgres (AuditRecord) carries an application hash chain (record_hash = H(payload ‖ prev_hash))
plus the immudb tx id, so the chain is independently verifiable AND queryable with plain SQL.
Corrections are NEW appended entries, never edits.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session, select

from .config import settings
from .models import AuditRecord

GENESIS = "0" * 64
# Constant key for the append serialization lock. The hash chain is a read-modify-write
# (read last hash → insert linked record); without serialization two concurrent writers — e.g.
# MELD and HippUnfold both emitting run.start at once now that detectors run concurrently (§18) —
# read the same prev_hash and fork the chain. A transaction-scoped advisory lock serializes ALL
# appends across every connection (API + worker) until each writer commits.
_AUDIT_LOCK_KEY = 0x6D656C6461      # "melda"


def _serialize_appends(session: Session) -> None:
    """Block until this transaction owns the audit append lock (Postgres only; sqlite serializes
    writes itself, so it's a harmless no-op there / in tests)."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_LOCK_KEY})


def _canonical(payload: Optional[dict]) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)


def _hash(payload: Optional[dict], prev_hash: str) -> str:
    return hashlib.sha256((_canonical(payload) + prev_hash).encode()).hexdigest()


class _Immudb:
    """Lazy immudb client; None if unreachable (chain still recorded in Postgres)."""

    def __init__(self) -> None:
        self._client = None
        self._tried = False

    def client(self):
        if self._tried:
            return self._client
        self._tried = True
        try:
            from immudb import ImmudbClient

            c = ImmudbClient(f"{settings.immudb_host}:{settings.immudb_port}")
            c.login(settings.immudb_user, settings.immudb_password, database=settings.immudb_db)
            self._client = c
        except Exception:
            if settings.audit_require_immudb:
                raise
            self._client = None
        return self._client

    def verified_set(self, key: str, value: str) -> Optional[int]:
        c = self.client()
        if c is None:
            return None
        try:
            tx = c.verifiedSet(key.encode(), value.encode())
            return int(getattr(tx, "id", getattr(tx, "txId", 0)) or 0) or None
        except Exception:
            if settings.audit_require_immudb:
                raise
            return None


_immu = _Immudb()


def record(session: Session, *, actor: str, action: str, entity_type: str,
           entity_id: str, payload: Optional[dict] = None) -> AuditRecord:
    _serialize_appends(session)                       # serialize concurrent appends (§18/§24)
    prev = session.exec(
        select(AuditRecord).order_by(AuditRecord.ts.desc()).limit(1)
    ).first()
    prev_hash = prev.payload_hash if prev else GENESIS

    body = {"actor": actor, "action": action, "entity_type": entity_type,
            "entity_id": entity_id, "payload": payload}
    payload_hash = _hash(body, prev_hash)

    rec = AuditRecord(actor=actor, action=action, entity_type=entity_type,
                      entity_id=entity_id, payload=payload,
                      payload_hash=payload_hash, prev_hash=prev_hash)
    rec.immudb_tx_id = _immu.verified_set(
        f"audit:{rec.id}", _canonical({**body, "hash": payload_hash, "prev": prev_hash}))
    session.add(rec)
    session.flush()
    return rec


def verify_chain(session: Session) -> dict:
    """Recompute the Postgres hash chain end-to-end; report the first break if any."""
    rows = session.exec(select(AuditRecord).order_by(AuditRecord.ts)).all()
    prev_hash = GENESIS
    for i, r in enumerate(rows):
        body = {"actor": r.actor, "action": r.action, "entity_type": r.entity_type,
                "entity_id": r.entity_id, "payload": r.payload}
        expect = _hash(body, prev_hash)
        if r.prev_hash != prev_hash or r.payload_hash != expect:
            return {"ok": False, "broken_at": i, "record_id": r.id, "count": len(rows)}
        prev_hash = r.payload_hash
    return {"ok": True, "count": len(rows)}


def rebuild_chain(session: Session) -> dict:
    """Re-link the Postgres mirror's hash chain over the current records, in ts order.

    Maintenance only — for after legitimate dev-data cleanup or to heal a pre-fix concurrency fork.
    It re-derives prev_hash/payload_hash from each record's UNCHANGED content (actor/action/
    entity/payload), so no event is altered or invented; immudb retains the original immutable log.
    Returns how many links were repaired."""
    _serialize_appends(session)
    rows = session.exec(select(AuditRecord).order_by(AuditRecord.ts)).all()
    prev_hash, repaired = GENESIS, 0
    for r in rows:
        body = {"actor": r.actor, "action": r.action, "entity_type": r.entity_type,
                "entity_id": r.entity_id, "payload": r.payload}
        new_hash = _hash(body, prev_hash)
        if r.prev_hash != prev_hash or r.payload_hash != new_hash:
            r.prev_hash, r.payload_hash = prev_hash, new_hash
            session.add(r)
            repaired += 1
        prev_hash = r.payload_hash
    session.commit()
    return {"ok": True, "count": len(rows), "repaired": repaired}
