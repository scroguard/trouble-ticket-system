"""v1.1 priority levels: rename 'normal' -> 'medium', add triage index

Revision ID: b3f1c7d2e9a4
Revises: aa9ba08e9ff9
Create Date: 2026-09-29 23:10:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b3f1c7d2e9a4'
down_revision: str | None = 'aa9ba08e9ff9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE = "status IN ('open', 'in_progress', 'pending_customer')"


def upgrade() -> None:
    # In-place rename (PG 10+): existing rows follow automatically and the enum's
    # sort order (low < medium < high < urgent) is preserved.
    op.execute("ALTER TYPE ticket_priority RENAME VALUE 'normal' TO 'medium'")
    op.alter_column('tickets', 'priority', server_default='medium')
    op.create_index(
        'ix_tickets_priority_active', 'tickets',
        [sa.literal_column('priority DESC'), 'created_at'],
        unique=False, postgresql_where=sa.text(ACTIVE),
    )


def downgrade() -> None:
    op.drop_index('ix_tickets_priority_active', table_name='tickets', postgresql_where=sa.text(ACTIVE))
    op.execute("ALTER TYPE ticket_priority RENAME VALUE 'medium' TO 'normal'")
    op.alter_column('tickets', 'priority', server_default='normal')
