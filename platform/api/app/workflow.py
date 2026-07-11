"""Explicit workflow invariants shared by routes and background reconciliation."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .models import CaseStatus, OutboxEvent, Run, RunStatus


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.created: frozenset({RunStatus.queued, RunStatus.blocked, RunStatus.pending}),
    RunStatus.queued: frozenset({RunStatus.preprocessing, RunStatus.failed, RunStatus.blocked}),
    RunStatus.preprocessing: frozenset({RunStatus.qc_pending, RunStatus.inference, RunStatus.failed}),
    RunStatus.qc_pending: frozenset({RunStatus.inference, RunStatus.blocked, RunStatus.failed}),
    RunStatus.inference: frozenset({RunStatus.packaging, RunStatus.failed, RunStatus.failed_oom}),
    RunStatus.packaging: frozenset({RunStatus.review_ready, RunStatus.failed}),
    RunStatus.review_ready: frozenset({RunStatus.adjudicated}),
    RunStatus.adjudicated: frozenset(),
    RunStatus.failed: frozenset({RunStatus.queued}),
    RunStatus.failed_oom: frozenset({RunStatus.queued}),
    RunStatus.blocked: frozenset(),
    RunStatus.pending: frozenset(),
}


RECIPE_MUTABLE_CASE_STATES = frozenset({
    CaseStatus.series_confirmed, CaseStatus.recipe_pending, CaseStatus.recipe_confirmed,
    CaseStatus.failed, CaseStatus.review_ready, CaseStatus.adjudicated,
})


def logical_run_key(recipe_id: str, entry_id: str) -> str:
    return hashlib.sha256(f"{recipe_id}:{entry_id}".encode()).hexdigest()


def run_input_contract_hash(*, recipe_id: str, recipe_spec_hash: str,
                            logical_key: str, detector_id: str,
                            source_role: str | None, source_series_uid: str | None,
                            params: dict) -> str:
    """Hash every database field allowed to determine a run's scientific input."""
    body = {
        "schema_version": 1,
        "recipe_id": recipe_id,
        "recipe_spec_hash": recipe_spec_hash,
        "logical_key": logical_key,
        "detector_id": detector_id,
        "source_role": source_role,
        "source_series_uid": source_series_uid,
        "params": params,
    }
    return hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def transition_run(run: Run, target: RunStatus, *, reason: str | None = None) -> None:
    if target == run.status:
        return
    if target not in RUN_TRANSITIONS.get(run.status, frozenset()):
        raise ValueError(f"invalid run transition {run.status.value} -> {target.value}")
    run.status = target
    run.status_reason = reason
    if target in {RunStatus.review_ready, RunStatus.adjudicated, RunStatus.failed,
                  RunStatus.failed_oom, RunStatus.blocked}:
        run.completed_at = datetime.now(timezone.utc)


def assert_case_state(case_status: CaseStatus, allowed: set[CaseStatus] | frozenset[CaseStatus],
                      action: str) -> None:
    if case_status not in allowed:
        # The host worker shares the API's pure contract/model package but intentionally does not
        # install the HTTP server stack.  Keep FastAPI at this route-only call site so importing
        # hashing and transition helpers in the offline worker remains dependency-complete.
        from fastapi import HTTPException
        raise HTTPException(409, f"cannot {action} while case is {case_status.value}")


def run_outbox_event(run: Run) -> OutboxEvent:
    return OutboxEvent(
        dedupe_key=f"run.enqueue:{run.logical_key}:attempt:{run.attempt}",
        topic="run.enqueue",
        aggregate_type="run",
        aggregate_id=run.id,
        payload={"run_id": run.id, "attempt": run.attempt},
    )
