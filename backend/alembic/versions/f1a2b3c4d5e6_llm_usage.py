"""llm_usage ledger (E1)

Revision ID: f1a2b3c4d5e6
Revises: e3f5a6b7c8d9
Create Date: 2026-10-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e3f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'llm_usage',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('session_id', sa.Uuid(), nullable=True),
        sa.Column('task_id', sa.Uuid(), nullable=True),
        sa.Column('provider_id', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=True),
        sa.Column('purpose', sa.String(), nullable=False),
        sa.Column('prompt_hit', sa.Integer(), nullable=True),
        sa.Column('prompt_miss', sa.Integer(), nullable=True),
        sa.Column('prompt_total', sa.Integer(), nullable=False),
        sa.Column('completion', sa.Integer(), nullable=False),
        sa.Column('reasoning', sa.Integer(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('tier', sa.String(), nullable=False),
        sa.Column('estimated', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_llm_usage_created_at'), 'llm_usage', ['created_at'], unique=False)
    op.create_index(op.f('ix_llm_usage_session_id'), 'llm_usage', ['session_id'], unique=False)
    op.create_index(op.f('ix_llm_usage_task_id'), 'llm_usage', ['task_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_llm_usage_task_id'), table_name='llm_usage')
    op.drop_index(op.f('ix_llm_usage_session_id'), table_name='llm_usage')
    op.drop_index(op.f('ix_llm_usage_created_at'), table_name='llm_usage')
    op.drop_table('llm_usage')
