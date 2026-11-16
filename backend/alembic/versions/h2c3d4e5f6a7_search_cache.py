"""search cache (S1)

Revision ID: h2c3d4e5f6a7
Revises: g1b2c3d4e5f6
Create Date: 2026-10-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'h2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'g1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'search_cache',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index(op.f('ix_search_cache_created_at'), 'search_cache', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_search_cache_created_at'), table_name='search_cache')
    op.drop_table('search_cache')
