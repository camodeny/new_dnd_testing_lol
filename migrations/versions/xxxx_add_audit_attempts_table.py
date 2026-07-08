"""add audit attempts table

Revision ID: xxxx_add_audit_attempts_table
Revises: 
Create Date: 2026-07-08 13:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'xxxx_add_audit_attempts_table'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'automation_run_audit_attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('cycle_id', sa.Integer(), nullable=False),
        sa.Column('auditor_job_id', sa.Integer(), nullable=True),
        sa.Column('cycle_number', sa.Integer(), nullable=False),
        sa.Column('phase', sa.String(length=40), nullable=False),
        sa.Column('attempt_source', sa.String(length=40), nullable=False, server_default='built_in_auditor'),
        sa.Column('auditor_slot', sa.Integer(), nullable=True),
        sa.Column('provider', sa.String(length=80), nullable=True),
        sa.Column('model', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('error_class', sa.String(length=120), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('raw_payload_json', sa.JSON(), nullable=True),
        sa.Column('normalized_payload_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['automation_runs.id'], ),
        sa.ForeignKeyConstraint(['cycle_id'], ['automation_run_audit_cycles.id'], ),
        sa.ForeignKeyConstraint(['auditor_job_id'], ['automation_run_auditor_jobs.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_automation_run_audit_attempts_run_id'), 'automation_run_audit_attempts', ['run_id'], unique=False)
    op.create_index(op.f('ix_automation_run_audit_attempts_cycle_id'), 'automation_run_audit_attempts', ['cycle_id'], unique=False)
    op.create_index(op.f('ix_automation_run_audit_attempts_auditor_job_id'), 'automation_run_audit_attempts', ['auditor_job_id'], unique=False)
    op.create_index(op.f('ix_automation_run_audit_attempts_created_at'), 'automation_run_audit_attempts', ['created_at'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_automation_run_audit_attempts_created_at'), table_name='automation_run_audit_attempts')
    op.drop_index(op.f('ix_automation_run_audit_attempts_auditor_job_id'), table_name='automation_run_audit_attempts')
    op.drop_index(op.f('ix_automation_run_audit_attempts_cycle_id'), table_name='automation_run_audit_attempts')
    op.drop_index(op.f('ix_automation_run_audit_attempts_run_id'), table_name='automation_run_audit_attempts')
    op.drop_table('automation_run_audit_attempts')
