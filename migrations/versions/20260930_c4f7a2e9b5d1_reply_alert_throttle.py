"""customer reply alerts: remember when staff were last alerted per ticket

Revision ID: c4f7a2e9b5d1
Revises: b6e2f4a8d1c7
Create Date: 2026-09-30 22:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c4f7a2e9b5d1'
down_revision: str | None = 'b6e2f4a8d1c7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tickets', sa.Column('customer_reply_alerted_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('tickets', 'customer_reply_alerted_at')
