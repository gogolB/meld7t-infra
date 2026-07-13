"""Add resumable routine case DICOM uploads.

Revision ID: 4b7e2c9a1d53
Revises: a3e7c1d9b5f2
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql


revision = "4b7e2c9a1d53"
down_revision = "a3e7c1d9b5f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("routine case upload migration requires PostgreSQL")
    status = postgresql.ENUM(
        "receiving", "staged", "importing", "ready", "failed",
        name="caseuploadstatus",
    )
    status.create(bind, checkfirst=True)
    text_type = sqlmodel.sql.sqltypes.AutoString()
    json_type = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "case_uploads",
        sa.Column("id", text_type, nullable=False),
        sa.Column("case_id", text_type, nullable=True),
        sa.Column("pseudonym", text_type, nullable=False),
        sa.Column("filename", text_type, nullable=False),
        sa.Column("content_type", text_type, nullable=False),
        sa.Column("storage_key", text_type, nullable=False),
        sa.Column("total_size", sa.BigInteger(), nullable=False),
        sa.Column("received_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", text_type, nullable=False),
        sa.Column("status", postgresql.ENUM(
            "receiving", "staged", "importing", "ready", "failed",
            name="caseuploadstatus", create_type=False), nullable=False),
        sa.Column("created_by", text_type, nullable=False),
        sa.Column("last_error", text_type, nullable=True),
        sa.Column("import_result", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("staging_cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("total_size > 0", name="ck_case_upload_total_size"),
        sa.CheckConstraint("received_size >= 0", name="ck_case_upload_received_size"),
        sa.CheckConstraint("received_size <= total_size",
                           name="ck_case_upload_received_within_total"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", name="uq_case_upload_case_id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_case_uploads_case_id", "case_uploads", ["case_id"])
    op.create_index("ix_case_uploads_created_by", "case_uploads", ["created_by"])
    op.create_index("ix_case_uploads_status", "case_uploads", ["status"])
    op.create_index("ix_case_upload_status_updated", "case_uploads", ["status", "updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("routine case upload migration requires PostgreSQL")
    op.drop_index("ix_case_upload_status_updated", table_name="case_uploads")
    op.drop_index("ix_case_uploads_status", table_name="case_uploads")
    op.drop_index("ix_case_uploads_created_by", table_name="case_uploads")
    op.drop_index("ix_case_uploads_case_id", table_name="case_uploads")
    op.drop_table("case_uploads")
    postgresql.ENUM(name="caseuploadstatus").drop(bind, checkfirst=True)
