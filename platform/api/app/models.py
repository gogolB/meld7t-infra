"""Data model (spec §8, §15, §25.1) — detector-plural, modality-plural.

A `Case` (one patient study in Orthanc) holds N `Series`. From the confirmed series + chosen
workup, a `Recipe` is built = a list of `Run`s, one per (detector × source series). Each `Run` is
the IMMUTABLE unit of review (§15): it yields one `Result` with N `Cluster`s, tracked by `Job`
stage rows and pinned `Provenance`. Adjudication references a specific run. MELD is
detector_id=meld_fcd — one row among many, never a privileged case.meld_result.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# JSON column that degrades to generic JSON on sqlite (tests) but uses JSONB on postgres.
def _json_col(*, nullable: bool = True) -> Column:
    return Column(
        JSONB().with_variant(__import__("sqlalchemy").JSON(), "sqlite"),
        nullable=nullable,
    )


# ---------------------------------------------------------------- enums

class CaseStatus(str, Enum):
    created = "created"
    series_pending = "series_pending"          # awaiting series-role confirmation (§16)
    series_confirmed = "series_confirmed"
    recipe_pending = "recipe_pending"
    recipe_confirmed = "recipe_confirmed"
    queued = "queued"
    running = "running"
    review_ready = "review_ready"
    adjudicated = "adjudicated"
    mdt_promoted = "mdt_promoted"
    archived = "archived"
    blocked = "blocked"                          # contraindication hard-block (§19)
    failed = "failed"


class SeriesRole(str, Enum):
    t1_uni = "t1_uni"                            # MP2RAGE UNI (needs O'Brien clean)
    t1_inv1 = "t1_inv1"
    t1_inv2 = "t1_inv2"
    t1_mprage = "t1_mprage"                      # conventional AX MPRAGE
    flair = "flair"
    t2 = "t2"                                     # 3D T2 SPACE (HS arm)
    unknown = "unknown"


class Workup(str, Enum):
    fcd = "fcd"
    hs = "hs"
    both = "both"


class DetectorId(str, Enum):
    meld_fcd = "meld_fcd"                         # built, validated
    map = "map"                                   # built (§25.4)
    hippunfold = "hippunfold"                     # built (§25.5)
    qt2 = "qt2"                                    # pending
    aid_hs = "aid_hs"                             # pending


class RunStatus(str, Enum):
    created = "created"
    queued = "queued"
    preprocessing = "preprocessing"
    qc_pending = "qc_pending"
    inference = "inference"
    packaging = "packaging"
    review_ready = "review_ready"
    adjudicated = "adjudicated"
    failed = "failed"
    failed_oom = "failed_oom"                     # explicit, not silent CPU retry (§6, §18)
    blocked = "blocked"
    pending = "pending"                           # detector declared but not yet integrated (§25.7)


class Device(str, Enum):
    gpu = "gpu"
    cpu = "cpu"


class HarmonizationStatus(str, Enum):
    unassigned = "unassigned"
    proposed = "proposed"
    confirmed = "confirmed"
    not_required = "not_required"
    blocked = "blocked"


class HarmonizationProfileStatus(str, Enum):
    draft = "draft"
    validated = "validated"
    active = "active"
    retired = "retired"


class OutboxStatus(str, Enum):
    pending = "pending"
    publishing = "publishing"
    published = "published"
    failed = "failed"


# ---------------------------------------------------------------- tables


class HarmonizationProfile(SQLModel, table=True):
    """Versioned scanner/protocol harmonization contract.

    ``selector`` contains only acquisition metadata matching rules. ``artifact_manifest`` contains
    local, hash-addressed model/control artifacts carried in the signed offline release. Profiles
    are immutable once active; a change is represented by a new ``(code, version)`` row.
    """

    __tablename__ = "harmonization_profiles"
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_harmonization_profile_code_version"),
        CheckConstraint("version > 0", name="ck_harmonization_profile_version_positive"),
        # The database, rather than a request-time read, is the final concurrency guard.  Two
        # administrators may activate different versions of the same code simultaneously.
        Index(
            "uq_harmonization_profile_one_active_code",
            "code",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    code: str = Field(index=True)
    version: int = 1
    name: str
    method: str                         # e.g. meld_combat, map_normative, identity/research
    detector_id: Optional[DetectorId] = None
    selector: dict = Field(sa_column=_json_col(nullable=False))
    artifact_manifest: dict = Field(sa_column=_json_col(nullable=False))
    parameters: dict = Field(default_factory=dict, sa_column=_json_col(nullable=False))
    status: HarmonizationProfileStatus = Field(default=HarmonizationProfileStatus.draft)
    created_by: str
    validated_by: Optional[str] = None
    validation_summary: Optional[dict] = Field(default=None, sa_column=_json_col())
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
    validated_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))


class HarmonizationAssignment(SQLModel, table=True):
    """Researcher-confirmed profile assignment for one case/detector/source series."""

    __tablename__ = "harmonization_assignments"
    __table_args__ = (
        UniqueConstraint("case_id", "detector_id", "source_series_uid",
                         name="uq_harmonization_assignment_target"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    case_id: str = Field(foreign_key="cases.id", index=True)
    profile_id: str = Field(foreign_key="harmonization_profiles.id", index=True)
    detector_id: DetectorId
    source_series_uid: str
    acquisition_fingerprint: str = Field(index=True)
    status: HarmonizationStatus = Field(default=HarmonizationStatus.proposed)
    proposal_score: Optional[int] = None
    proposal_reasons: Optional[list] = Field(default=None, sa_column=_json_col())
    confirmed_by: Optional[str] = None
    override_reason: Optional[str] = None
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
    confirmed_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))

class Case(SQLModel, table=True):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("orthanc_study_uid", name="uq_cases_orthanc_study_uid"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    pseudonym: str
    created_by: str = Field(index=True)
    assigned_to: Optional[str] = Field(default=None, index=True)
    orthanc_study_uid: Optional[str] = Field(default=None, index=True)
    dicom_path: Optional[str] = None            # local DICOM staging (worker input; §2.3)
    staging_id: Optional[str] = Field(default=None, index=True)
    status: CaseStatus = Field(default=CaseStatus.created)
    workup: Optional[Workup] = None
    contraindications: Optional[dict] = Field(default=None, sa_column=_json_col())
    scanner_fingerprint: Optional[str] = Field(default=None, index=True)
    harmonization_status: HarmonizationStatus = Field(default=HarmonizationStatus.unassigned)
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))


class Series(SQLModel, table=True):
    __tablename__ = "series"
    __table_args__ = (
        UniqueConstraint("case_id", "orthanc_series_uid", name="uq_series_case_uid"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    case_id: str = Field(foreign_key="cases.id", index=True)
    orthanc_series_uid: str
    series_description: Optional[str] = None
    modality: Optional[str] = None
    proposed_role: SeriesRole = Field(default=SeriesRole.unknown)   # tag-based proposal (§16)
    confirmed_role: Optional[SeriesRole] = None                     # submitter confirms/overrides
    image_type: Optional[list] = Field(default=None, sa_column=_json_col())
    geometry: Optional[dict] = Field(default=None, sa_column=_json_col())
    acquisition: Optional[dict] = Field(default=None, sa_column=_json_col())
    fingerprint: Optional[str] = Field(default=None, index=True)
    instance_count: Optional[int] = None
    active: bool = Field(default=True, index=True)
    last_seen_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))


class Recipe(SQLModel, table=True):
    __tablename__ = "recipes"
    __table_args__ = (
        UniqueConstraint("case_id", "version", name="uq_recipes_case_version"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    case_id: str = Field(foreign_key="cases.id", index=True)
    workup: Workup
    spec: list = Field(sa_column=_json_col(nullable=False))  # immutable detector/source entries
    version: int = 1
    spec_hash: Optional[str] = Field(default=None, index=True)
    supersedes: Optional[str] = Field(default=None, foreign_key="recipes.id")
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
    confirmed_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))


class Run(SQLModel, table=True):
    """The immutable unit of review (§15). One per (detector × source series)."""
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("logical_key", name="uq_runs_logical_key"),
        CheckConstraint("attempt >= 0", name="ck_runs_attempt_nonnegative"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    case_id: str = Field(foreign_key="cases.id", index=True)
    recipe_id: str = Field(foreign_key="recipes.id", index=True)
    detector_id: DetectorId
    detector_version: Optional[str] = None
    source_role: Optional[SeriesRole] = None
    source_series_uid: Optional[str] = None
    params: Optional[dict] = Field(default=None, sa_column=_json_col())
    logical_key: str = Field(index=True)
    status: RunStatus = Field(default=RunStatus.created)
    device: Optional[Device] = None
    attempt: int = 0
    claimed_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    heartbeat_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    lease_expires_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    claim_token: Optional[str] = Field(default=None, index=True)
    execution_contract: Optional[dict] = Field(default=None, sa_column=_json_col())
    completed_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    adjudicated_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    status_reason: Optional[str] = None
    source_manifest: Optional[dict] = Field(default=None, sa_column=_json_col())
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
    superseded_by: Optional[str] = Field(default=None, foreign_key="runs.id")


class Job(SQLModel, table=True):
    """Per-stage execution tracking for a run (§8)."""
    __tablename__ = "jobs"
    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    stage: str
    status: str = "queued"
    device: Optional[Device] = None
    started_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    finished_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    retry_count: int = 0
    error: Optional[str] = None
    workdir: Optional[str] = None


class Result(SQLModel, table=True):
    __tablename__ = "results"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_results_run_id"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    orthanc_study_uid: Optional[str] = None     # the packaged (derived) study OHIF opens
    orthanc_t1_uid: Optional[str] = None
    orthanc_seg_uid: Optional[str] = None
    orthanc_probmap_uid: Optional[str] = None
    report_path: Optional[str] = None
    harmo_code: Optional[str] = None
    n_clusters: int = 0
    output_manifest: Optional[dict] = Field(default=None, sa_column=_json_col())
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))


class Cluster(SQLModel, table=True):
    __tablename__ = "clusters"
    __table_args__ = (
        UniqueConstraint("result_id", "index", name="uq_clusters_result_index"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    result_id: str = Field(foreign_key="results.id", index=True)
    index: int
    hemi: Optional[str] = None
    location: Optional[str] = None
    size: Optional[float] = None
    confidence: Optional[float] = None
    saliency: Optional[dict] = Field(default=None, sa_column=_json_col())


class Adjudication(SQLModel, table=True):
    """Append-only reviewer read; also written to the immudb ledger (§24, §26)."""
    __tablename__ = "adjudications"
    __table_args__ = (
        CheckConstraint("confidence IS NULL OR (confidence >= 1 AND confidence <= 5)",
                        name="ck_adjudication_confidence_range"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    reviewer: str
    ts: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
    agree: Optional[bool] = None
    confidence: Optional[int] = None
    ground_truth: Optional[str] = None
    notes: Optional[str] = None
    supersedes: Optional[str] = Field(default=None, foreign_key="adjudications.id")


class Provenance(SQLModel, table=True):
    __tablename__ = "provenance"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_provenance_run_id"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    os_checksum: Optional[str] = None
    image_digests: Optional[dict] = Field(default=None, sa_column=_json_col())
    tool_versions: Optional[dict] = Field(default=None, sa_column=_json_col())
    git_sha: Optional[str] = None
    params: Optional[dict] = Field(default=None, sa_column=_json_col())
    input_series_uid: Optional[str] = None
    recon_provenance: Optional[dict] = Field(default=None, sa_column=_json_col())
    source_manifest: Optional[dict] = Field(default=None, sa_column=_json_col())
    harmonization: Optional[dict] = Field(default=None, sa_column=_json_col())
    output_hashes: Optional[dict] = Field(default=None, sa_column=_json_col())
    release_manifest_digest: Optional[str] = None
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))


class OutboxEvent(SQLModel, table=True):
    """Durable handoff from committed workflow state to Redis/Arq or other side effects."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonnegative"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    dedupe_key: str = Field(index=True)
    topic: str = Field(index=True)
    aggregate_type: str
    aggregate_id: str = Field(index=True)
    payload: dict = Field(sa_column=_json_col(nullable=False))
    status: OutboxStatus = Field(default=OutboxStatus.pending, index=True)
    attempts: int = 0
    available_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
    published_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    lease_until: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    last_error: Optional[str] = None


class AuditRecord(SQLModel, table=True):
    """Local index/mirror of the immudb ledger writes (§26). immudb is the tamper-evident store;
    this table is a queryable convenience carrying the hash chain + immudb tx id."""
    __tablename__ = "audit"
    __table_args__ = (
        UniqueConstraint("sequence", name="uq_audit_sequence"),
        UniqueConstraint("immudb_tx_id", name="uq_audit_immudb_tx_id"),
        CheckConstraint("sequence > 0", name="ck_audit_sequence_positive"),
        CheckConstraint("ledger_attempts >= 0", name="ck_audit_ledger_attempts_nonnegative"),
        CheckConstraint(
            "ledger_status IN ('pending', 'verified', 'failed')",
            name="ck_audit_ledger_status",
        ),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    sequence: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    ts: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False))
    actor: str
    action: str
    entity_type: str
    entity_id: str
    payload: Optional[dict] = Field(default=None, sa_column=_json_col())
    payload_hash: str
    prev_hash: Optional[str] = None
    immudb_tx_id: Optional[int] = Field(
        default=None, sa_column=Column(BigInteger, nullable=True))
    local_status: str = "postgres_hash_chain"
    ledger_status: str = Field(default="pending", index=True)
    ledger_last_error: Optional[str] = None
    ledger_attempts: int = 0
    ledger_verified_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
