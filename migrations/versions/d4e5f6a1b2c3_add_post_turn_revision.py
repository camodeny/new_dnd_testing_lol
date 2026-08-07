"""add post_turn_revision to session_dm_turns

Revision ID: d4e5f6a1b2c3
Revises: c3d4e5f6a1b2
Create Date: 2026-08-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6a1b2c3'
down_revision = 'c3d4e5f6a1b2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('session_dm_turns', sa.Column('post_turn_revision', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('session_dm_turns', 'post_turn_revision')
