"""add bounded reclaim failure columns

Revision ID: f6a7b8c9d0e1
Revises: d6e7f8a9b0c1
Create Date: 2026-08-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'd6e7f8a9b0c1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('automation_runs', sa.Column('reclaim_failure_fingerprint', sa.String(length=160), nullable=True))
    op.add_column('automation_runs', sa.Column('reclaim_failure_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('automation_runs', sa.Column('reclaim_failure_attempt', sa.Integer(), nullable=True))
    op.add_column('automation_runs', sa.Column('reclaim_failure_stage', sa.String(length=120), nullable=True))
    op.add_column('automation_runs', sa.Column('reclaim_failure_error', sa.Text(), nullable=True))
    op.add_column('automation_runs', sa.Column('reclaim_failure_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('automation_runs', 'reclaim_failure_at')
    op.drop_column('automation_runs', 'reclaim_failure_error')
    op.drop_column('automation_runs', 'reclaim_failure_stage')
    op.drop_column('automation_runs', 'reclaim_failure_attempt')
    op.drop_column('automation_runs', 'reclaim_failure_count')
    op.drop_column('automation_runs', 'reclaim_failure_fingerprint')
