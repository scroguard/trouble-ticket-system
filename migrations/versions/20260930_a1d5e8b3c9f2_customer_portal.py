"""customer portal: emailed sign-in links and customer sessions

Revision ID: a1d5e8b3c9f2
Revises: f7c3a9e2b6d8
Create Date: 2026-09-30 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1d5e8b3c9f2'
down_revision: str | None = 'f7c3a9e2b6d8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('customer_login_tokens',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customer_login_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_customer_login_tokens_token_hash'))
    )
    op.create_index('ix_customer_login_tokens_email_created', 'customer_login_tokens', ['email', 'created_at'])
    op.create_index('ix_customer_login_tokens_ip_created', 'customer_login_tokens', ['ip_address', 'created_at'])
    op.create_table('customer_sessions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('user_agent', sa.String(length=512), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customer_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_customer_sessions_token_hash'))
    )
    op.create_index(op.f('ix_customer_sessions_email'), 'customer_sessions', ['email'])
    op.create_index(op.f('ix_customer_sessions_expires_at'), 'customer_sessions', ['expires_at'])


def downgrade() -> None:
    op.drop_index(op.f('ix_customer_sessions_expires_at'), table_name='customer_sessions')
    op.drop_index(op.f('ix_customer_sessions_email'), table_name='customer_sessions')
    op.drop_table('customer_sessions')
    op.drop_index('ix_customer_login_tokens_ip_created', table_name='customer_login_tokens')
    op.drop_index('ix_customer_login_tokens_email_created', table_name='customer_login_tokens')
    op.drop_table('customer_login_tokens')
