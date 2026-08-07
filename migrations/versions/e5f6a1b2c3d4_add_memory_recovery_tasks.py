"""add memory recovery task table

Revision ID: e5f6a1b2c3d4
Revises: c3d4e5f6a1b2
Create Date: 2026-08-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6a1b2c3d4'
down_revision = 'c3d4e5f6a1b2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'session_memory_recovery_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=True),
        sa.Column('player_message_id', sa.Integer(), nullable=False),
        sa.Column('dm_message_id', sa.Integer(), nullable=True),
        sa.Column('trace_id', sa.String(length=160), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('error_stage', sa.String(length=40), nullable=True),
        sa.Column('error_code', sa.String(length=80), nullable=True),
        sa.Column('error_text', sa.Text(), nullable=True),
        sa.Column('patch_json', sa.Text(), nullable=True),
        sa.Column('context_json', sa.Text(), nullable=True),
        sa.Column('memory_applied', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error_text', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaign.id'], ),
        sa.ForeignKeyConstraint(['dm_message_id'], ['session_messages.id'], ),
        sa.ForeignKeyConstraint(['player_message_id'], ['session_messages.id'], ),
        sa.ForeignKeyConstraint(['session_id'], ['campaign_sessions.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('campaign_id', 'player_message_id', name='uq_memory_recovery_campaign_player'),
    )
    op.create_index(op.f('ix_session_memory_recovery_tasks_campaign_id'), 'session_memory_recovery_tasks', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_session_memory_recovery_tasks_session_id'), 'session_memory_recovery_tasks', ['session_id'], unique=False)
    op.create_index(op.f('ix_session_memory_recovery_tasks_player_message_id'), 'session_memory_recovery_tasks', ['player_message_id'], unique=False)
    op.create_index(op.f('ix_session_memory_recovery_tasks_status'), 'session_memory_recovery_tasks', ['status'], unique=False)
    op.create_index(op.f('ix_session_memory_recovery_tasks_created_at'), 'session_memory_recovery_tasks', ['created_at'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_session_memory_recovery_tasks_created_at'), table_name='session_memory_recovery_tasks')
    op.drop_index(op.f('ix_session_memory_recovery_tasks_status'), table_name='session_memory_recovery_tasks')
    op.drop_index(op.f('ix_session_memory_recovery_tasks_player_message_id'), table_name='session_memory_recovery_tasks')
    op.drop_index(op.f('ix_session_memory_recovery_tasks_session_id'), table_name='session_memory_recovery_tasks')
    op.drop_index(op.f('ix_session_memory_recovery_tasks_campaign_id'), table_name='session_memory_recovery_tasks')
    op.drop_table('session_memory_recovery_tasks')
