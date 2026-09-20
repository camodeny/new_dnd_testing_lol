"""canonical entity identity and aliases — issue #214.

Revision ID: a214identity01
Revises: a215npcstate01 (re-pointed after #408 merge to keep one linear head)
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a214identity01"
down_revision: Union[str, Sequence[str], None] = "a215npcstate01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("world_entities", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("world_entities", sa.Column("superseded_by_id", sa.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_world_entities_superseded_by", "world_entities", "world_entities", ["superseded_by_id"], ["id"], ondelete="SET NULL")
    op.create_table(
        "world_entity_aliases",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.UUID(as_uuid=True), sa.ForeignKey("world_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alias", sa.String(160), nullable=False),
        sa.Column("normalized_alias", sa.String(160), nullable=False),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="campaign"),
        sa.Column("provenance", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("campaign_id", "normalized_alias", name="uq_world_entity_alias_campaign_alias"),
    )
    op.create_index("ix_world_entity_alias_entity", "world_entity_aliases", ["entity_id"])


def downgrade() -> None:
    op.drop_index("ix_world_entity_alias_entity", table_name="world_entity_aliases")
    op.drop_table("world_entity_aliases")
    op.drop_constraint("fk_world_entities_superseded_by", "world_entities", type_="foreignkey")
    op.drop_column("world_entities", "superseded_by_id")
    op.drop_column("world_entities", "revision")
