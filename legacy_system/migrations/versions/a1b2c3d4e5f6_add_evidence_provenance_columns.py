"""add evidence_provenance columns

Revision ID: a1b2c3d4e5f6
Revises: f8b2d109e23c
Create Date: 2026-07-16 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'f8b2d109e23c'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('campaign_memory_logs', sa.Column('evidence_status', sa.String(50), nullable=True))
    op.add_column('campaign_memory_logs', sa.Column('provenance_json', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('campaign_memory_logs', 'provenance_json')
    op.drop_column('campaign_memory_logs', 'evidence_status')
