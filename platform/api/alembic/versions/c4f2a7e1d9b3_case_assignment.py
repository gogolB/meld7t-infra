"""Add explicit case ownership for approved intake handoff.

Revision ID: c4f2a7e1d9b3
Revises: b8e1a4d9c3f2
"""
from alembic import op
import sqlalchemy as sa


revision = "c4f2a7e1d9b3"
down_revision = "b8e1a4d9c3f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("assigned_to", sa.String(), nullable=True))
    # Preserve existing creator access. New service-intake cases may deliberately remain
    # unassigned until an administrator hands them to a named researcher.
    op.execute("UPDATE cases SET assigned_to = created_by")
    op.create_index("ix_cases_assigned_to", "cases", ["assigned_to"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cases_assigned_to", table_name="cases")
    op.drop_column("cases", "assigned_to")
