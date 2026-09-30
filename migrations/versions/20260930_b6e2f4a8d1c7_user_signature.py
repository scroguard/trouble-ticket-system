"""per-user reply signature

Revision ID: b6e2f4a8d1c7
Revises: a1d5e8b3c9f2
Create Date: 2026-09-30 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b6e2f4a8d1c7'
down_revision: str | None = 'a1d5e8b3c9f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('signature', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'signature')
