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

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# JSON column that degrades to generic JSON on sqlite (tests) but uses JSONB on postgres.
def _json_col() -> Column:
    return Column(JSONB().with_variant(__import__("sqlalchemy").JSON(), "sqlite"))


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
    map = "map"                                   # pending (§25.4)
    hippunfold = "hippunfold"                     # pending (§25.5)
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


# ---------------------------------------------------------------- tables

class Case(SQLModel, table=True):
    __tablename__ = "cases"
    id: str = Field(default_factory=_uuid, primary_key=True)
    pseudonym: str
    orthanc_study_uid: Optional[str] = Field(default=None, index=True)
    dicom_path: Optional[str] = None            # local DICOM staging (worker input; §2.3)
    status: CaseStatus = Field(default=CaseStatus.created)
    workup: Optional[Workup] = None
    contraindications: Optional[dict] = Field(default=None, sa_column=_json_col())
    created_at: datetime = Field(default_factory=_now)


class Series(SQLModel, table=True):
    __tablename__ = "series"
    id: str = Field(default_factory=_uuid, primary_key=True)
    case_id: str = Field(foreign_key="cases.id", index=True)
    orthanc_series_uid: str
    series_description: Optional[str] = None
    modality: Optional[str] = None
    proposed_role: SeriesRole = Field(default=SeriesRole.unknown)   # tag-based proposal (§16)
    confirmed_role: Optional[SeriesRole] = None                     # submitter confirms/overrides
    image_type: Optional[list] = Field(default=None, sa_column=_json_col())
    geometry: Optional[dict] = Field(default=None, sa_column=_json_col())


class Recipe(SQLModel, table=True):
    __tablename__ = "recipes"
    id: str = Field(default_factory=_uuid, primary_key=True)
    case_id: str = Field(foreign_key="cases.id", index=True)
    workup: Workup
    spec: list = Field(sa_column=_json_col())      # [{detector_id, source_role, source_series_uid, params, status}]
    created_at: datetime = Field(default_factory=_now)
    confirmed_at: Optional[datetime] = None


class Run(SQLModel, table=True):
    """The immutable unit of review (§15). One per (detector × source series)."""
    __tablename__ = "runs"
    id: str = Field(default_factory=_uuid, primary_key=True)
    case_id: str = Field(foreign_key="cases.id", index=True)
    recipe_id: str = Field(foreign_key="recipes.id", index=True)
    detector_id: DetectorId
    detector_version: Optional[str] = None
    source_role: Optional[SeriesRole] = None
    source_series_uid: Optional[str] = None
    params: Optional[dict] = Field(default=None, sa_column=_json_col())
    status: RunStatus = Field(default=RunStatus.created)
    device: Optional[Device] = None
    created_at: datetime = Field(default_factory=_now)
    superseded_by: Optional[str] = Field(default=None, foreign_key="runs.id")


class Job(SQLModel, table=True):
    """Per-stage execution tracking for a run (§8)."""
    __tablename__ = "jobs"
    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    stage: str
    status: str = "queued"
    device: Optional[Device] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    retry_count: int = 0
    error: Optional[str] = None
    workdir: Optional[str] = None


class Result(SQLModel, table=True):
    __tablename__ = "results"
    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    orthanc_study_uid: Optional[str] = None     # the packaged (derived) study OHIF opens
    orthanc_t1_uid: Optional[str] = None
    orthanc_seg_uid: Optional[str] = None
    orthanc_probmap_uid: Optional[str] = None
    report_path: Optional[str] = None
    harmo_code: Optional[str] = None
    n_clusters: int = 0
    created_at: datetime = Field(default_factory=_now)


class Cluster(SQLModel, table=True):
    __tablename__ = "clusters"
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
    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    reviewer: str
    ts: datetime = Field(default_factory=_now)
    agree: Optional[bool] = None
    confidence: Optional[int] = None
    ground_truth: Optional[str] = None
    notes: Optional[str] = None
    supersedes: Optional[str] = Field(default=None, foreign_key="adjudications.id")


class Provenance(SQLModel, table=True):
    __tablename__ = "provenance"
    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    os_checksum: Optional[str] = None
    image_digests: Optional[dict] = Field(default=None, sa_column=_json_col())
    tool_versions: Optional[dict] = Field(default=None, sa_column=_json_col())
    git_sha: Optional[str] = None
    params: Optional[dict] = Field(default=None, sa_column=_json_col())
    input_series_uid: Optional[str] = None
    recon_provenance: Optional[dict] = Field(default=None, sa_column=_json_col())


class AuditRecord(SQLModel, table=True):
    """Local index/mirror of the immudb ledger writes (§26). immudb is the tamper-proof store;
    this table is a queryable convenience carrying the hash chain + immudb tx id."""
    __tablename__ = "audit"
    id: str = Field(default_factory=_uuid, primary_key=True)
    ts: datetime = Field(default_factory=_now)
    actor: str
    action: str
    entity_type: str
    entity_id: str
    payload: Optional[dict] = Field(default=None, sa_column=_json_col())
    payload_hash: str
    prev_hash: Optional[str] = None
    immudb_tx_id: Optional[int] = None
