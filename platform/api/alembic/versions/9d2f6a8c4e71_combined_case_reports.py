"""Add immutable, versioned combined case reports.

Revision ID: 9d2f6a8c4e71
Revises: 4b7e2c9a1d53
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql


revision = "9d2f6a8c4e71"
down_revision = "4b7e2c9a1d53"
branch_labels = None
depends_on = None


def _enum(name: str, *values: str):
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("combined report migration requires PostgreSQL")

    report_kind = postgresql.ENUM(
        "preliminary", "final", name="casereportkind",
    )
    report_status = postgresql.ENUM(
        "queued", "generating", "ready", "failed", name="casereportstatus",
    )
    report_kind.create(bind, checkfirst=True)
    report_status.create(bind, checkfirst=True)
    text_type = sqlmodel.sql.sqltypes.AutoString()
    json_type = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "case_reports",
        sa.Column("id", text_type, nullable=False),
        sa.Column("case_id", text_type, nullable=False),
        sa.Column("recipe_id", text_type, nullable=False),
        sa.Column("kind", _enum("casereportkind", "preliminary", "final"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _enum("casereportstatus", "queued", "generating", "ready",
                                  "failed"), nullable=False),
        sa.Column("snapshot", json_type, nullable=False),
        sa.Column("snapshot_sha256", text_type, nullable=False),
        sa.Column("branding", json_type, nullable=False),
        sa.Column("requested_by", text_type, nullable=False),
        sa.Column("report_path", text_type, nullable=True),
        sa.Column("artifact_manifest", json_type, nullable=True),
        sa.Column("last_error", text_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_case_report_version_positive"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "kind", "version",
                            name="uq_case_report_kind_version"),
    )
    op.create_index("ix_case_reports_case_id", "case_reports", ["case_id"])
    op.create_index("ix_case_reports_recipe_id", "case_reports", ["recipe_id"])
    op.create_index("ix_case_reports_status", "case_reports", ["status"])
    op.create_index("ix_case_report_status_updated", "case_reports", ["status", "updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("combined report migration requires PostgreSQL")
    op.drop_index("ix_case_report_status_updated", table_name="case_reports")
    op.drop_index("ix_case_reports_status", table_name="case_reports")
    op.drop_index("ix_case_reports_recipe_id", table_name="case_reports")
    op.drop_index("ix_case_reports_case_id", table_name="case_reports")
    op.drop_table("case_reports")
    postgresql.ENUM(name="casereportstatus").drop(bind, checkfirst=True)
    postgresql.ENUM(name="casereportkind").drop(bind, checkfirst=True)
