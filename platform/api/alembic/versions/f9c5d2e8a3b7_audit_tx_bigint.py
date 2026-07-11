"""Use 64-bit immudb transaction identifiers.

Revision ID: f9c5d2e8a3b7
Revises: e8b4c1d7f2a6
"""
from alembic import op
import sqlalchemy as sa


revision = "f9c5d2e8a3b7"
down_revision = "e8b4c1d7f2a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "audit", "immudb_tx_id",
        existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=True,
    )


def downgrade() -> None:
    # A downgrade is unsafe once any value exceeds PostgreSQL INTEGER. Refuse explicitly instead
    # of truncating immutable-ledger references.
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM audit
            WHERE immudb_tx_id > 2147483647 OR immudb_tx_id < -2147483648
          ) THEN
            RAISE EXCEPTION 'cannot downgrade: immudb transaction ID exceeds INTEGER';
          END IF;
        END $$;
    """))
    op.alter_column(
        "audit", "immudb_tx_id",
        existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=True,
    )
