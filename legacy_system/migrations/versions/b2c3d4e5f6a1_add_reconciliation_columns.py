"""add reconciliation columns

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-07-17 22:56:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a1'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('automation_runs', sa.Column('reconciliation_player_message_id', sa.String(length=120), nullable=True))
    op.add_column('automation_runs', sa.Column('reconciliation_timeout_phase', sa.String(length=40), nullable=True))
    op.add_column('automation_runs', sa.Column('reconciliation_timeout_error', sa.Text(), nullable=True))
    op.add_column('automation_runs', sa.Column('reconciliation_started_at', sa.DateTime(), nullable=True))
    op.add_column('automation_runs', sa.Column('reconciliation_deadline', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('automation_runs', 'reconciliation_deadline')
    op.drop_column('automation_runs', 'reconciliation_started_at')
    op.drop_column('automation_runs', 'reconciliation_timeout_error')
    op.drop_column('automation_runs', 'reconciliation_timeout_phase')
    op.drop_column('automation_runs', 'reconciliation_player_message_id')
