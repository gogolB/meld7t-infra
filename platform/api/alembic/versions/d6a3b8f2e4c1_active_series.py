"""Track the current Orthanc series set without deleting historical rows.

Revision ID: d6a3b8f2e4c1
Revises: c4f2a7e1d9b3
"""
from alembic import op
import sqlalchemy as sa


revision = "d6a3b8f2e4c1"
down_revision = "c4f2a7e1d9b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("series", sa.Column(
        "active", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("series", sa.Column(
        "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_series_active", "series", ["active"], unique=False)
    op.alter_column("series", "active", server_default=None)
    op.alter_column("series", "last_seen_at", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_series_active", table_name="series")
    op.drop_column("series", "last_seen_at")
    op.drop_column("series", "active")
