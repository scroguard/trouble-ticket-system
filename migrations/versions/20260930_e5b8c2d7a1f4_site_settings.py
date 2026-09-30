"""admin-editable site settings (site name)

Revision ID: e5b8c2d7a1f4
Revises: d4a9e1c3f7b2
Create Date: 2026-09-30 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e5b8c2d7a1f4'
down_revision: str | None = 'd4a9e1c3f7b2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('site_settings',
    sa.Column('id', sa.SmallInteger(), nullable=False),
    sa.Column('site_name', sa.String(length=100), server_default='Support Desk', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by_id', sa.Integer(), nullable=True),
    sa.CheckConstraint('id = 1', name=op.f('ck_site_settings_single_row')),
    sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], name=op.f('fk_site_settings_updated_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_site_settings'))
    )
    op.execute("INSERT INTO site_settings (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table('site_settings')
