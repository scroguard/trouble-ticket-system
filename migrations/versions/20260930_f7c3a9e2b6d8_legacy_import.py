"""legacy help desk import: ticket/comment legacy_ref, 'import' message source

Revision ID: f7c3a9e2b6d8
Revises: e5b8c2d7a1f4
Create Date: 2026-09-30 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f7c3a9e2b6d8'
down_revision: str | None = 'e5b8c2d7a1f4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE message_source ADD VALUE IF NOT EXISTS 'import'")
    op.add_column('tickets', sa.Column('legacy_ref', sa.String(length=32), nullable=True))
    op.create_unique_constraint(op.f('uq_tickets_legacy_ref'), 'tickets', ['legacy_ref'])
    op.add_column('ticket_comments', sa.Column('legacy_ref', sa.String(length=64), nullable=True))
    op.create_unique_constraint(op.f('uq_ticket_comments_legacy_ref'), 'ticket_comments', ['legacy_ref'])


def downgrade() -> None:
    # Imported rows use the 'import' source; they must go before the enum value can.
    op.execute("DELETE FROM tickets WHERE legacy_ref IS NOT NULL")
    op.execute("DELETE FROM ticket_comments WHERE source = 'import'")
    op.drop_constraint(op.f('uq_ticket_comments_legacy_ref'), 'ticket_comments', type_='unique')
    op.drop_column('ticket_comments', 'legacy_ref')
    op.drop_constraint(op.f('uq_tickets_legacy_ref'), 'tickets', type_='unique')
    op.drop_column('tickets', 'legacy_ref')
    # PostgreSQL can't drop an enum value: rebuild message_source without 'import'.
    op.execute("ALTER TYPE message_source RENAME TO message_source_v6")
    op.execute("CREATE TYPE message_source AS ENUM ('email', 'web', 'system')")
    for table in ('tickets', 'ticket_comments'):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN source TYPE message_source "
            "USING source::text::message_source"
        )
    op.execute("DROP TYPE message_source_v6")
