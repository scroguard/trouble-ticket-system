"""outbound email log: thread replies to automated emails, cap acknowledgements

Revision ID: d8b1e6f3a7c2
Revises: c4f7a2e9b5d1
Create Date: 2026-09-30 23:30:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd8b1e6f3a7c2'
down_revision: str | None = 'c4f7a2e9b5d1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('outbound_emails',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('message_id', sa.String(length=998), nullable=False),
    sa.Column('ticket_id', sa.BigInteger(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('recipient', sa.String(length=320), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name=op.f('fk_outbound_emails_ticket_id_tickets'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbound_emails')),
    sa.UniqueConstraint('message_id', name=op.f('uq_outbound_emails_message_id'))
    )
    op.create_index(op.f('ix_outbound_emails_ticket_id'), 'outbound_emails', ['ticket_id'])
    op.create_index('ix_outbound_emails_recipient_kind_created', 'outbound_emails', ['recipient', 'kind', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_outbound_emails_recipient_kind_created', table_name='outbound_emails')
    op.drop_index(op.f('ix_outbound_emails_ticket_id'), table_name='outbound_emails')
    op.drop_table('outbound_emails')
