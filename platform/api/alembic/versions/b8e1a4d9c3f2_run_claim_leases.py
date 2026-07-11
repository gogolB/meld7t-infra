"""run claim leases and immutable execution contracts

Revision ID: b8e1a4d9c3f2
Revises: 7f4d8c2a1b90
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b8e1a4d9c3f2"
down_revision = "7f4d8c2a1b90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("production migrations require PostgreSQL")

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM cases WHERE orthanc_study_uid IS NOT NULL
            GROUP BY orthanc_study_uid HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'duplicate Orthanc StudyInstanceUID case claims must be resolved';
          END IF;
        END $$
        """
    )
    op.create_unique_constraint(
        "uq_cases_orthanc_study_uid", "cases", ["orthanc_study_uid"]
    )

    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("claim_token", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("execution_contract", postgresql.JSONB(), nullable=True))
    op.add_column("runs", sa.Column("adjudicated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_runs_lease_expires_at", "runs", ["lease_expires_at"], unique=False)
    op.create_index("ix_runs_claim_token", "runs", ["claim_token"], unique=False)
    op.execute(
        "UPDATE runs SET lease_expires_at = now() "
        "WHERE status IN ('preprocessing', 'inference', 'packaging')"
    )
    # Legacy adjudicated rows used completed_at for both meanings. Preserve that timestamp as the
    # best available adjudication time without further changing historical execution metadata.
    op.execute("UPDATE runs SET adjudicated_at = completed_at WHERE status = 'adjudicated'")


def downgrade() -> None:
    op.drop_index("ix_runs_claim_token", table_name="runs")
    op.drop_index("ix_runs_lease_expires_at", table_name="runs")
    op.drop_column("runs", "adjudicated_at")
    op.drop_column("runs", "execution_contract")
    op.drop_column("runs", "claim_token")
    op.drop_column("runs", "lease_expires_at")
    op.drop_column("runs", "heartbeat_at")
    op.drop_constraint("uq_cases_orthanc_study_uid", "cases", type_="unique")
