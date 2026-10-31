"""scheduler job urgency and off-peak deferral (E8)

Revision ID: g1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-10-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'g1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('scheduler_jobs') as batch:
        batch.add_column(sa.Column('urgent', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('defer_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('scheduler_jobs') as batch:
        batch.drop_column('defer_until')
        batch.drop_column('urgent')
