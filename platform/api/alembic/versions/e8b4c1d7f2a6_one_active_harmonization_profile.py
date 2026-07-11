"""Enforce one active harmonization profile per versioned code.

Revision ID: e8b4c1d7f2a6
Revises: d6a3b8f2e4c1
"""
from alembic import op
import sqlalchemy as sa


revision = "e8b4c1d7f2a6"
down_revision = "d6a3b8f2e4c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL is the only supported server database.  A partial unique index closes the race
    # between two transactions activating different versions of one logical profile code.
    op.create_index(
        "uq_harmonization_profile_one_active_code",
        "harmonization_profiles",
        ["code"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_harmonization_profile_one_active_code",
        table_name="harmonization_profiles",
    )
