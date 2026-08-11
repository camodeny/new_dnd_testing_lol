"""add campaign world identity mapping table

Revision ID: d6e7f8a9b0c1
Revises: e5f6a1b2c3d4
Create Date: 2026-08-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd6e7f8a9b0c1'
down_revision = 'e5f6a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'campaign_world_identities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('graph_entity_id', sa.String(length=100), nullable=False),
        sa.Column('actor_id', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaign.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('campaign_id', 'actor_id', name='uq_world_identity_campaign_actor'),
        sa.UniqueConstraint('campaign_id', 'graph_entity_id', name='uq_world_identity_campaign_entity'),
    )
    op.create_index(op.f('ix_campaign_world_identities_campaign_id'), 'campaign_world_identities', ['campaign_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_campaign_world_identities_campaign_id'), table_name='campaign_world_identities')
    op.drop_table('campaign_world_identities')
