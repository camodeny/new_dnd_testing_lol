"""add optional clock completion criteria

Revision ID: c3d4e5f6a1b2
Revises: b2c3d4e5f6a1
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a1b2'
down_revision = 'b2c3d4e5f6a1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('campaign_clocks', sa.Column('completion_criteria', sa.JSON(), nullable=True))
    op.add_column('campaign_clocks', sa.Column('completion_state', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('campaign_clocks', 'completion_state')
    op.drop_column('campaign_clocks', 'completion_criteria')
