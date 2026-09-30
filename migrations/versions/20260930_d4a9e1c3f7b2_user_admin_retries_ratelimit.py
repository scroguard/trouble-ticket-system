"""reply delivery retries, login-failure log (replaces per-user lockout columns)

Revision ID: d4a9e1c3f7b2
Revises: c8e2a4f6b1d3
Create Date: 2026-09-30 00:30:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'd4a9e1c3f7b2'
down_revision: str | None = 'c8e2a4f6b1d3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- reply retries
    op.add_column('ticket_comments', sa.Column('delivery_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('ticket_comments', sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True))
    op.drop_index('ix_ticket_comments_undelivered', table_name='ticket_comments')
    op.create_index(
        'ix_ticket_comments_next_attempt', 'ticket_comments', ['next_attempt_at'],
        postgresql_where=sa.text('next_attempt_at IS NOT NULL'),
    )
    # Replies left 'pending' by an earlier crash get picked up by the new sweep.
    op.execute("UPDATE ticket_comments SET next_attempt_at = now() WHERE delivery_status = 'pending'")

    # --- login rate limiting
    op.create_table('login_failures',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('attempted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_login_failures'))
    )
    op.create_index('ix_login_failures_email_attempted', 'login_failures', ['email', 'attempted_at'])
    op.create_index('ix_login_failures_ip_attempted', 'login_failures', ['ip_address', 'attempted_at'])
    op.drop_column('users', 'locked_until')
    op.drop_column('users', 'failed_login_attempts')


def downgrade() -> None:
    op.add_column('users', sa.Column('failed_login_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('users', sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True))
    op.drop_index('ix_login_failures_ip_attempted', table_name='login_failures')
    op.drop_index('ix_login_failures_email_attempted', table_name='login_failures')
    op.drop_table('login_failures')

    op.drop_index('ix_ticket_comments_next_attempt', table_name='ticket_comments')
    op.create_index(
        'ix_ticket_comments_undelivered', 'ticket_comments', ['delivery_status'],
        postgresql_where=sa.text("delivery_status IN ('pending', 'failed')"),
    )
    op.drop_column('ticket_comments', 'next_attempt_at')
    op.drop_column('ticket_comments', 'delivery_attempts')
