"""add memory_anchors column

Revision ID: f8b2d109e23c
Revises: e7b9f872e411
Create Date: 2026-07-08 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f8b2d109e23c'
down_revision = 'e7b9f872e411'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('campaign_sessions', sa.Column('memory_anchors', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('campaign_sessions', 'memory_anchors')
