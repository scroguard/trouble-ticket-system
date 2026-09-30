"""v2 API: 'new'/'pending' ticket statuses, login sessions, login lockout

Revision ID: c8e2a4f6b1d3
Revises: b3f1c7d2e9a4
Create Date: 2026-09-29 23:40:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c8e2a4f6b1d3'
down_revision: str | None = 'b3f1c7d2e9a4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ACTIVE = "status IN ('open', 'in_progress', 'pending_customer')"
NEW_ACTIVE = "status IN ('new', 'open', 'in_progress', 'pending')"


def _partial_indexes(active: str) -> None:
    op.create_index(
        'ix_tickets_unassigned_active', 'tickets', [sa.literal_column('created_at DESC')],
        postgresql_where=sa.text(f"assigned_to_id IS NULL AND {active}"),
    )
    op.create_index(
        'ix_tickets_priority_active', 'tickets',
        [sa.literal_column('priority DESC'), 'created_at'], postgresql_where=sa.text(active),
    )


def _drop_partial_indexes() -> None:
    op.drop_index('ix_tickets_unassigned_active', table_name='tickets')
    op.drop_index('ix_tickets_priority_active', table_name='tickets')


def upgrade() -> None:
    # A new enum value can't be used in the transaction that adds it, so commit it first.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'new' BEFORE 'open'")
    op.execute("ALTER TYPE ticket_status RENAME VALUE 'pending_customer' TO 'pending'")
    op.alter_column('tickets', 'status', server_default='new')
    _drop_partial_indexes()
    _partial_indexes(NEW_ACTIVE)

    op.add_column('users', sa.Column('failed_login_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('users', sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True))

    op.create_table('user_sessions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('user_agent', sa.String(length=512), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_user_sessions_token_hash'))
    )
    op.create_index(op.f('ix_user_sessions_expires_at'), 'user_sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_user_sessions_user_id'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_expires_at'), table_name='user_sessions')
    op.drop_table('user_sessions')
    op.drop_column('users', 'locked_until')
    op.drop_column('users', 'failed_login_attempts')

    # PostgreSQL can't drop an enum value: rebuild the type without 'new'.
    _drop_partial_indexes()
    op.execute("UPDATE tickets SET status = 'open' WHERE status = 'new'")
    op.execute("ALTER TYPE ticket_status RENAME VALUE 'pending' TO 'pending_customer'")
    op.execute("ALTER TABLE tickets ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE ticket_status RENAME TO ticket_status_v2")
    op.execute(
        "CREATE TYPE ticket_status AS ENUM "
        "('open', 'in_progress', 'pending_customer', 'resolved', 'closed')"
    )
    op.execute(
        "ALTER TABLE tickets ALTER COLUMN status TYPE ticket_status USING status::text::ticket_status"
    )
    op.execute("DROP TYPE ticket_status_v2")
    op.alter_column('tickets', 'status', server_default='open')
    _partial_indexes(OLD_ACTIVE)
