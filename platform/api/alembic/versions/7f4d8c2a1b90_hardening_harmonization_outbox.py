"""hardening_harmonization_outbox

Revision ID: 7f4d8c2a1b90
Revises: ac9817973224
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql


revision: str = "7f4d8c2a1b90"
down_revision: Union[str, None] = "ac9817973224"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(name: str, *values: str):
    return postgresql.ENUM(*values, name=name, create_type=False)


def _assert_no_duplicates(table: str, columns: str, label: str) -> None:
    op.execute(sa.text(f"""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM {table} GROUP BY {columns} HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'migration blocked: duplicate {label} rows must be reconciled first';
          END IF;
        END $$;
    """))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("production hardening migration requires PostgreSQL")

    harmonization_status = postgresql.ENUM(
        "unassigned", "proposed", "confirmed", "not_required", "blocked",
        name="harmonizationstatus")
    profile_status = postgresql.ENUM(
        "draft", "validated", "active", "retired", name="harmonizationprofilestatus")
    outbox_status = postgresql.ENUM(
        "pending", "publishing", "published", "failed", name="outboxstatus")
    harmonization_status.create(bind, checkfirst=True)
    profile_status.create(bind, checkfirst=True)
    outbox_status.create(bind, checkfirst=True)

    op.create_table(
        "harmonization_profiles",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("code", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("method", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("detector_id", _enum("detectorid", "meld_fcd", "map", "hippunfold",
                                       "qt2", "aid_hs"), nullable=True),
        sa.Column("selector", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifact_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", _enum("harmonizationprofilestatus", "draft", "validated", "active",
                                   "retired"), nullable=False),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("validated_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("validation_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_harmonization_profile_version_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", "version", name="uq_harmonization_profile_code_version"),
    )
    op.create_index("ix_harmonization_profiles_code", "harmonization_profiles", ["code"])

    op.add_column("cases", sa.Column("staging_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("cases", sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.execute("UPDATE cases SET created_by = 'legacy-import' WHERE created_by IS NULL")
    op.alter_column("cases", "created_by", nullable=False)
    op.add_column("cases", sa.Column("scanner_fingerprint", sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=True))
    op.add_column("cases", sa.Column("harmonization_status",
                                      _enum("harmonizationstatus", "unassigned", "proposed",
                                            "confirmed", "not_required", "blocked"),
                                      server_default="unassigned", nullable=False))
    op.create_index("ix_cases_staging_id", "cases", ["staging_id"])
    op.create_index("ix_cases_created_by", "cases", ["created_by"])
    op.create_index("ix_cases_scanner_fingerprint", "cases", ["scanner_fingerprint"])

    op.add_column("series", sa.Column("acquisition", postgresql.JSONB(astext_type=sa.Text()),
                                       nullable=True))
    op.add_column("series", sa.Column("fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("series", sa.Column("instance_count", sa.Integer(), nullable=True))
    op.create_index("ix_series_fingerprint", "series", ["fingerprint"])
    _assert_no_duplicates("series", "case_id, orthanc_series_uid", "case/series UID")
    op.create_unique_constraint("uq_series_case_uid", "series", ["case_id", "orthanc_series_uid"])

    op.create_table(
        "harmonization_assignments",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("case_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("profile_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("detector_id", _enum("detectorid", "meld_fcd", "map", "hippunfold",
                                       "qt2", "aid_hs"), nullable=False),
        sa.Column("source_series_uid", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acquisition_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", _enum("harmonizationstatus", "unassigned", "proposed", "confirmed",
                                   "not_required", "blocked"), nullable=False),
        sa.Column("proposal_score", sa.Integer(), nullable=True),
        sa.Column("proposal_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confirmed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("override_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["harmonization_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "detector_id", "source_series_uid",
                            name="uq_harmonization_assignment_target"),
    )
    op.create_index("ix_harmonization_assignments_case_id", "harmonization_assignments", ["case_id"])
    op.create_index("ix_harmonization_assignments_profile_id", "harmonization_assignments",
                    ["profile_id"])
    op.create_index("ix_harmonization_assignments_acquisition_fingerprint",
                    "harmonization_assignments", ["acquisition_fingerprint"])

    op.add_column("recipes", sa.Column("version", sa.Integer(), server_default="1", nullable=False))
    op.add_column("recipes", sa.Column("spec_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("recipes", sa.Column("supersedes", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM recipes WHERE spec IS NULL) THEN
            RAISE EXCEPTION 'migration blocked: recipe spec must be reconstructed before upgrade';
          END IF;
        END $$;
    """))
    op.alter_column("recipes", "spec", nullable=False)
    op.execute("UPDATE recipes SET spec_hash = 'legacy-md5:' || md5(spec::text) WHERE spec_hash IS NULL")
    _assert_no_duplicates("recipes", "case_id, version", "case/recipe version")
    op.create_unique_constraint("uq_recipes_case_version", "recipes", ["case_id", "version"])
    op.create_index("ix_recipes_spec_hash", "recipes", ["spec_hash"])
    op.create_foreign_key("fk_recipes_supersedes", "recipes", "recipes", ["supersedes"], ["id"])

    op.add_column("runs", sa.Column("logical_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("runs", sa.Column("attempt", sa.Integer(), server_default="0", nullable=False))
    op.add_column("runs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("status_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("runs", sa.Column("source_manifest", postgresql.JSONB(astext_type=sa.Text()),
                                     nullable=True))
    op.execute("UPDATE runs SET logical_key = md5(recipe_id || ':' || id) WHERE logical_key IS NULL")
    op.alter_column("runs", "logical_key", nullable=False)
    op.create_unique_constraint("uq_runs_logical_key", "runs", ["logical_key"])
    op.create_check_constraint("ck_runs_attempt_nonnegative", "runs", "attempt >= 0")
    op.create_index("ix_runs_logical_key", "runs", ["logical_key"])

    _assert_no_duplicates("results", "run_id", "result/run")
    op.create_unique_constraint("uq_results_run_id", "results", ["run_id"])
    op.add_column("results", sa.Column("output_manifest", postgresql.JSONB(astext_type=sa.Text()),
                                        nullable=True))

    _assert_no_duplicates("clusters", "result_id, index", "result/cluster index")
    op.create_unique_constraint("uq_clusters_result_index", "clusters", ["result_id", "index"])

    _assert_no_duplicates("provenance", "run_id", "provenance/run")
    op.create_unique_constraint("uq_provenance_run_id", "provenance", ["run_id"])
    op.add_column("provenance", sa.Column("source_manifest", postgresql.JSONB(astext_type=sa.Text()),
                                           nullable=True))
    op.add_column("provenance", sa.Column("harmonization", postgresql.JSONB(astext_type=sa.Text()),
                                           nullable=True))
    op.add_column("provenance", sa.Column("output_hashes", postgresql.JSONB(astext_type=sa.Text()),
                                           nullable=True))
    op.add_column("provenance", sa.Column("release_manifest_digest",
                                           sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("provenance", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE provenance SET created_at = now() WHERE created_at IS NULL")
    op.alter_column("provenance", "created_at", nullable=False)

    op.create_check_constraint(
        "ck_adjudication_confidence_range", "adjudications",
        "confidence IS NULL OR (confidence >= 1 AND confidence <= 5)")

    op.add_column("audit", sa.Column("sequence", sa.BigInteger(), nullable=True))
    op.execute(sa.text("""
        WITH ordered AS (
          SELECT id, row_number() OVER (ORDER BY ts, id) AS sequence FROM audit
        )
        UPDATE audit SET sequence = ordered.sequence FROM ordered WHERE audit.id = ordered.id
    """))
    op.alter_column("audit", "sequence", nullable=False)
    op.add_column("audit", sa.Column("local_status", sqlmodel.sql.sqltypes.AutoString(),
                                      server_default="postgres_hash_chain", nullable=False))
    op.add_column("audit", sa.Column("ledger_status", sqlmodel.sql.sqltypes.AutoString(),
                                      server_default="pending", nullable=False))
    op.execute("UPDATE audit SET ledger_status = 'verified' WHERE immudb_tx_id IS NOT NULL")
    op.add_column("audit", sa.Column("ledger_last_error", sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=True))
    op.add_column("audit", sa.Column("ledger_attempts", sa.Integer(), server_default="0",
                                      nullable=False))
    op.add_column("audit", sa.Column("ledger_verified_at", sa.DateTime(timezone=True),
                                      nullable=True))
    _assert_no_duplicates("audit", "sequence", "audit sequence")
    op.create_unique_constraint("uq_audit_sequence", "audit", ["sequence"])
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM audit WHERE immudb_tx_id IS NOT NULL
            GROUP BY immudb_tx_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'migration blocked: duplicate audit immudb transaction IDs';
          END IF;
        END $$;
    """))
    op.create_unique_constraint("uq_audit_immudb_tx_id", "audit", ["immudb_tx_id"])
    op.create_check_constraint("ck_audit_sequence_positive", "audit", "sequence > 0")
    op.create_check_constraint("ck_audit_ledger_attempts_nonnegative", "audit",
                               "ledger_attempts >= 0")
    op.create_check_constraint("ck_audit_ledger_status", "audit",
                               "ledger_status IN ('pending', 'verified', 'failed')")
    op.create_index("ix_audit_sequence", "audit", ["sequence"])
    op.create_index("ix_audit_ledger_status", "audit", ["ledger_status"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("dedupe_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("topic", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("aggregate_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("aggregate_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", _enum("outboxstatus", "pending", "publishing", "published", "failed"),
                  nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
    )
    op.create_index("ix_outbox_events_dedupe_key", "outbox_events", ["dedupe_key"])
    op.create_index("ix_outbox_events_topic", "outbox_events", ["topic"])
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    # Preserve already-queued research work when upgrading an active pre-outbox installation.
    op.execute(sa.text("""
        INSERT INTO outbox_events (
          id, dedupe_key, topic, aggregate_type, aggregate_id, payload, status, attempts,
          available_at, created_at
        )
        SELECT
          'legacy-' || md5(id),
          'run.enqueue:' || logical_key || ':attempt:' || attempt::text,
          'run.enqueue', 'run', id,
          jsonb_build_object('run_id', id, 'attempt', attempt),
          'pending'::outboxstatus, 0, now(), now()
        FROM runs WHERE status = 'queued'::runstatus
        ON CONFLICT (dedupe_key) DO NOTHING
    """))

    # Existing application timestamps were written as UTC but stored without timezone.
    for table, column in (
        ("cases", "created_at"), ("recipes", "created_at"), ("recipes", "confirmed_at"),
        ("runs", "created_at"), ("results", "created_at"), ("adjudications", "ts"),
        ("jobs", "started_at"), ("jobs", "finished_at"), ("audit", "ts"),
    ):
        nullable = column in {"confirmed_at", "started_at", "finished_at"}
        op.alter_column(table, column, type_=sa.DateTime(timezone=True), nullable=nullable,
                        postgresql_using=f"{column} AT TIME ZONE 'UTC'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("production hardening migration requires PostgreSQL")
    # Restore the original timezone-naive UTC columns before removing hardening additions.
    for table, column in (
        ("cases", "created_at"), ("recipes", "created_at"), ("recipes", "confirmed_at"),
        ("runs", "created_at"), ("results", "created_at"), ("adjudications", "ts"),
        ("jobs", "started_at"), ("jobs", "finished_at"), ("audit", "ts"),
    ):
        nullable = column in {"confirmed_at", "started_at", "finished_at"}
        op.alter_column(table, column, type_=sa.DateTime(timezone=False), nullable=nullable,
                        postgresql_using=f"{column} AT TIME ZONE 'UTC'")
    op.drop_table("outbox_events")
    op.drop_index("ix_audit_ledger_status", table_name="audit")
    op.drop_index("ix_audit_sequence", table_name="audit")
    op.drop_constraint("ck_audit_ledger_status", "audit", type_="check")
    op.drop_constraint("ck_audit_ledger_attempts_nonnegative", "audit", type_="check")
    op.drop_constraint("ck_audit_sequence_positive", "audit", type_="check")
    op.drop_constraint("uq_audit_immudb_tx_id", "audit", type_="unique")
    op.drop_constraint("uq_audit_sequence", "audit", type_="unique")
    for column in ("ledger_verified_at", "ledger_attempts", "ledger_last_error",
                   "ledger_status", "local_status", "sequence"):
        op.drop_column("audit", column)
    op.drop_constraint("ck_adjudication_confidence_range", "adjudications", type_="check")
    for column in ("created_at", "release_manifest_digest", "output_hashes", "harmonization",
                   "source_manifest"):
        op.drop_column("provenance", column)
    op.drop_constraint("uq_provenance_run_id", "provenance", type_="unique")
    op.drop_column("results", "output_manifest")
    op.drop_constraint("uq_results_run_id", "results", type_="unique")
    op.drop_constraint("uq_clusters_result_index", "clusters", type_="unique")
    op.drop_index("ix_runs_logical_key", table_name="runs")
    op.drop_constraint("ck_runs_attempt_nonnegative", "runs", type_="check")
    op.drop_constraint("uq_runs_logical_key", "runs", type_="unique")
    for column in ("source_manifest", "status_reason", "completed_at", "claimed_at", "attempt",
                   "logical_key"):
        op.drop_column("runs", column)
    op.drop_constraint("fk_recipes_supersedes", "recipes", type_="foreignkey")
    op.drop_constraint("uq_recipes_case_version", "recipes", type_="unique")
    op.drop_index("ix_recipes_spec_hash", table_name="recipes")
    op.alter_column("recipes", "spec", nullable=True)
    for column in ("supersedes", "spec_hash", "version"):
        op.drop_column("recipes", column)
    op.drop_table("harmonization_assignments")
    op.drop_constraint("uq_series_case_uid", "series", type_="unique")
    op.drop_index("ix_series_fingerprint", table_name="series")
    for column in ("instance_count", "fingerprint", "acquisition"):
        op.drop_column("series", column)
    op.drop_index("ix_cases_scanner_fingerprint", table_name="cases")
    op.drop_index("ix_cases_created_by", table_name="cases")
    op.drop_index("ix_cases_staging_id", table_name="cases")
    for column in ("harmonization_status", "scanner_fingerprint", "created_by", "staging_id"):
        op.drop_column("cases", column)
    op.drop_table("harmonization_profiles")
    for enum_name in ("outboxstatus", "harmonizationprofilestatus", "harmonizationstatus"):
        postgresql.ENUM(name=enum_name).drop(bind, checkfirst=True)
