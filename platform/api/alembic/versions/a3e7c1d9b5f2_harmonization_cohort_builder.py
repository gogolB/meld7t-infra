"""Add on-server harmonization cohort builder state.

Revision ID: a3e7c1d9b5f2
Revises: f9c5d2e8a3b7
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql


revision = "a3e7c1d9b5f2"
down_revision = "f9c5d2e8a3b7"
branch_labels = None
depends_on = None


def _enum(name: str, *values: str):
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("harmonization cohort migration requires PostgreSQL")

    cohort_status = postgresql.ENUM(
        "draft", "cohort_ready", "frozen", "archived",
        name="harmonizationcohortstatus",
    )
    build_status = postgresql.ENUM(
        "queued", "building", "qc_review", "validated", "active", "failed", "cancelled",
        name="harmonizationbuildstatus",
    )
    upload_status = postgresql.ENUM(
        "receiving", "staged", "importing", "imported", "failed",
        name="harmonizationuploadstatus",
    )
    cohort_status.create(bind, checkfirst=True)
    build_status.create(bind, checkfirst=True)
    upload_status.create(bind, checkfirst=True)
    json_type = postgresql.JSONB(astext_type=sa.Text())
    text_type = sqlmodel.sql.sqltypes.AutoString()

    op.create_table(
        "harmonization_cohorts",
        sa.Column("id", text_type, nullable=False),
        sa.Column("name", text_type, nullable=False),
        sa.Column("site_code", text_type, nullable=False),
        sa.Column("profile_code", text_type, nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("source_role", _enum("seriesrole", "t1_uni", "t1_inv1", "t1_inv2",
                                       "t1_mprage", "flair", "t2", "unknown"), nullable=False),
        sa.Column("selector", json_type, nullable=False),
        sa.Column("min_controls", sa.Integer(), nullable=False),
        sa.Column("cv_folds", sa.Integer(), nullable=False),
        sa.Column("status", _enum("harmonizationcohortstatus", "draft", "cohort_ready",
                                  "frozen", "archived"), nullable=False),
        sa.Column("demographics_manifest", json_type, nullable=True),
        sa.Column("frozen_manifest", json_type, nullable=True),
        sa.Column("created_by", text_type, nullable=False),
        sa.Column("approved_by", text_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("min_controls >= 20", name="ck_harmonization_cohort_min_controls"),
        sa.CheckConstraint("cv_folds >= 2 AND cv_folds <= 10",
                           name="ck_harmonization_cohort_cv_folds"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_code", "profile_version",
                            name="uq_harmonization_cohort_profile_version"),
    )
    op.create_index("ix_harmonization_cohorts_site_code", "harmonization_cohorts", ["site_code"])
    op.create_index("ix_harmonization_cohorts_profile_code", "harmonization_cohorts",
                    ["profile_code"])

    op.create_table(
        "harmonization_cohort_studies",
        sa.Column("id", text_type, nullable=False),
        sa.Column("cohort_id", text_type, nullable=False),
        sa.Column("orthanc_study_uid", text_type, nullable=False),
        sa.Column("subject_key_hmac", text_type, nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", text_type, nullable=True),
        sa.Column("acquisition_fingerprint", text_type, nullable=False),
        sa.Column("acquisition", json_type, nullable=False),
        sa.Column("series_manifest", json_type, nullable=False),
        sa.Column("study_sha256", text_type, nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cohort_id"], ["harmonization_cohorts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cohort_id", "orthanc_study_uid",
                            name="uq_harmonization_cohort_study_uid"),
        sa.UniqueConstraint("cohort_id", "subject_key_hmac",
                            name="uq_harmonization_cohort_subject"),
    )
    op.create_index("ix_harmonization_cohort_studies_cohort_id",
                    "harmonization_cohort_studies", ["cohort_id"])
    op.create_index("ix_harmonization_cohort_studies_subject_key_hmac",
                    "harmonization_cohort_studies", ["subject_key_hmac"])
    op.create_index("ix_harmonization_cohort_studies_acquisition_fingerprint",
                    "harmonization_cohort_studies", ["acquisition_fingerprint"])

    op.create_table(
        "harmonization_demographics",
        sa.Column("id", text_type, nullable=False),
        sa.Column("cohort_id", text_type, nullable=False),
        sa.Column("subject_key_hmac", text_type, nullable=False),
        sa.Column("age", sa.Float(), nullable=False),
        sa.Column("sex", text_type, nullable=False),
        sa.ForeignKeyConstraint(["cohort_id"], ["harmonization_cohorts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cohort_id", "subject_key_hmac",
                            name="uq_harmonization_demographic_subject"),
    )
    op.create_index("ix_harmonization_demographics_cohort_id", "harmonization_demographics",
                    ["cohort_id"])
    op.create_index("ix_harmonization_demographics_subject_key_hmac",
                    "harmonization_demographics", ["subject_key_hmac"])

    op.create_table(
        "harmonization_uploads",
        sa.Column("id", text_type, nullable=False),
        sa.Column("cohort_id", text_type, nullable=False),
        sa.Column("filename", text_type, nullable=False),
        sa.Column("content_type", text_type, nullable=False),
        sa.Column("storage_key", text_type, nullable=False),
        sa.Column("total_size", sa.BigInteger(), nullable=False),
        sa.Column("received_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", text_type, nullable=False),
        sa.Column("status", _enum("harmonizationuploadstatus", "receiving", "staged",
                                  "importing", "imported", "failed"), nullable=False),
        sa.Column("created_by", text_type, nullable=False),
        sa.Column("last_error", text_type, nullable=True),
        sa.Column("import_result", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("staging_cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mapping_redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("total_size > 0", name="ck_harmonization_upload_total_size"),
        sa.CheckConstraint("received_size >= 0", name="ck_harmonization_upload_received_size"),
        sa.CheckConstraint("received_size <= total_size",
                           name="ck_harmonization_upload_received_within_total"),
        sa.ForeignKeyConstraint(["cohort_id"], ["harmonization_cohorts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_harmonization_uploads_cohort_id", "harmonization_uploads", ["cohort_id"])
    op.create_index("ix_harmonization_upload_status_updated", "harmonization_uploads",
                    ["status", "updated_at"])

    op.create_table(
        "harmonization_builds",
        sa.Column("id", text_type, nullable=False),
        sa.Column("cohort_id", text_type, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", _enum("harmonizationbuildstatus", "queued", "building",
                                  "qc_review", "validated", "active", "failed", "cancelled"),
                  nullable=False),
        sa.Column("stage", text_type, nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("builder_image_digest", text_type, nullable=False),
        sa.Column("builder_adapter_sha256", text_type, nullable=False),
        sa.Column("acceptance_criteria", json_type, nullable=False),
        sa.Column("cv_plan", json_type, nullable=True),
        sa.Column("qc_report", json_type, nullable=True),
        sa.Column("artifact_manifest", json_type, nullable=True),
        sa.Column("rejection_summary", json_type, nullable=True),
        sa.Column("profile_id", text_type, nullable=True),
        sa.Column("error_code", text_type, nullable=True),
        sa.Column("initiated_by", text_type, nullable=False),
        sa.Column("validated_by", text_type, nullable=True),
        sa.Column("activated_by", text_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["cohort_id"], ["harmonization_cohorts.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["harmonization_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cohort_id", "attempt", name="uq_harmonization_build_attempt"),
        sa.CheckConstraint("attempt > 0", name="ck_harmonization_build_attempt"),
        sa.CheckConstraint("progress >= 0 AND progress <= 100",
                           name="ck_harmonization_build_progress"),
    )
    op.create_index("ix_harmonization_builds_cohort_id", "harmonization_builds", ["cohort_id"])
    op.create_index("ix_harmonization_build_status_lease", "harmonization_builds",
                    ["status", "lease_expires_at"])
    op.create_index(
        "uq_harmonization_build_one_live", "harmonization_builds",
        [sa.text("(cohort_id IS NOT NULL)")], unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'building', 'qc_review', 'validated')"
        ),
    )

    op.create_table(
        "harmonization_fold_results",
        sa.Column("id", text_type, nullable=False),
        sa.Column("build_id", text_type, nullable=False),
        sa.Column("fold_index", sa.Integer(), nullable=False),
        sa.Column("train_count", sa.Integer(), nullable=False),
        sa.Column("holdout_count", sa.Integer(), nullable=False),
        sa.Column("membership_hmac_sha256", text_type, nullable=False),
        sa.Column("status", text_type, nullable=False),
        sa.Column("metrics", json_type, nullable=False),
        sa.Column("resource_usage", json_type, nullable=False),
        sa.ForeignKeyConstraint(["build_id"], ["harmonization_builds.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("build_id", "fold_index", name="uq_harmonization_fold_index"),
        sa.CheckConstraint("fold_index >= 0", name="ck_harmonization_fold_index"),
        sa.CheckConstraint("train_count > 0", name="ck_harmonization_fold_train_count"),
        sa.CheckConstraint("holdout_count > 0", name="ck_harmonization_fold_holdout_count"),
        sa.CheckConstraint("status IN ('passed', 'failed')",
                           name="ck_harmonization_fold_status"),
    )
    op.create_index("ix_harmonization_fold_results_build_id", "harmonization_fold_results",
                    ["build_id"])

    op.create_table(
        "acquisition_observations",
        sa.Column("id", text_type, nullable=False),
        sa.Column("detector_id", _enum("detectorid", "meld_fcd", "map", "hippunfold",
                                       "qt2", "aid_hs"), nullable=False),
        sa.Column("source_role", _enum("seriesrole", "t1_uni", "t1_inv1", "t1_inv2",
                                       "t1_mprage", "flair", "t2", "unknown"), nullable=False),
        sa.Column("acquisition_fingerprint", text_type, nullable=False),
        sa.Column("acquisition", json_type, nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("coverage_status", text_type, nullable=False),
        sa.Column("profile_id", text_type, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["harmonization_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("detector_id", "source_role", "acquisition_fingerprint",
                            name="uq_acquisition_observation_contract"),
    )
    op.create_index("ix_acquisition_observations_acquisition_fingerprint",
                    "acquisition_observations", ["acquisition_fingerprint"])


def downgrade() -> None:
    op.drop_table("acquisition_observations")
    op.drop_table("harmonization_fold_results")
    op.drop_table("harmonization_builds")
    op.drop_table("harmonization_uploads")
    op.drop_table("harmonization_demographics")
    op.drop_table("harmonization_cohort_studies")
    op.drop_table("harmonization_cohorts")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for enum_name in (
            "harmonizationuploadstatus", "harmonizationbuildstatus",
            "harmonizationcohortstatus",
        ):
            postgresql.ENUM(name=enum_name).drop(bind, checkfirst=True)
