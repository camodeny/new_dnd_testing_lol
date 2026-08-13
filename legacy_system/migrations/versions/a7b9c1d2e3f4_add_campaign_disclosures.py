"""add campaign disclosure registry table

Revision ID: a7b9c1d2e3f4
Revises: f6a7b8c9d0e1
Create Date: 2026-08-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7b9c1d2e3f4'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'campaign_disclosures',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.String(length=255), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=True),
        sa.Column('source', sa.String(length=80), nullable=True),
        sa.Column('source_message_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaign.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('campaign_id', 'item_id', name='uq_campaign_disclosure_item'),
    )
    op.create_index(op.f('ix_campaign_disclosures_campaign_id'), 'campaign_disclosures', ['campaign_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_campaign_disclosures_campaign_id'), table_name='campaign_disclosures')
    op.drop_table('campaign_disclosures')
