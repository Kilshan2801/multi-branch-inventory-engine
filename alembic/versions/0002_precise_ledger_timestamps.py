"""precise timestamps on stock_movements and audit_logs

now() returns the transaction start time, so every row written by one request
got the same created_at. clock_timestamp() gives the real time of each insert,
which keeps the ledger and audit trail in true chronological order.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("stock_movements", "audit_logs")


def upgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "created_at", server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "created_at", server_default=sa.text("now()"))
