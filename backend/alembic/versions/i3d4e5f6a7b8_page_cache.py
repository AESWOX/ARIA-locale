"""page cache (S3)

Revision ID: i3d4e5f6a7b8
Revises: h2c3d4e5f6a7
Create Date: 2026-10-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'i3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'h2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'page_cache',
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('etag', sa.String(), nullable=True),
        sa.Column('last_modified', sa.String(), nullable=True),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('date', sa.String(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('status', sa.Integer(), nullable=False),
        sa.Column('content_type', sa.String(), nullable=False),
        sa.Column('final_url', sa.String(), nullable=False),
        sa.Column('truncated', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('url'),
    )
    op.create_index(op.f('ix_page_cache_created_at'), 'page_cache', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_page_cache_created_at'), table_name='page_cache')
    op.drop_table('page_cache')
