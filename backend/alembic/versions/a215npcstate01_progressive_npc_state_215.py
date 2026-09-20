"""progressive NPC state — issue #215 (additive).

Revision ID: a215npcstate01
Revises: f232mapgeom01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a215npcstate01"
down_revision: Union[str, Sequence[str], None] = "f232mapgeom01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn is not None and conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
        if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
            SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
            SQLiteTypeCompiler._patched_jsonb = True  # type: ignore
    op.create_table(
        "npc_states",
        sa.Column("entity_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(160), nullable=True),
        sa.Column("goals", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("disposition", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("resources", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("current_activity", sa.Text(), nullable=True),
        sa.Column("location_entity_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("location_name", sa.String(256), nullable=True),
        sa.Column("importance", sa.String(16), nullable=False, server_default="incidental"),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("field_visibility", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("state_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("campaign_revision", sa.Integer(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_turn_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("source_attempt_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("source_event_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["entity_id"], ["world_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["location_entity_id"], ["world_entities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("entity_id"),
        sa.CheckConstraint("importance IN ('incidental','supporting','major')", name="ck_npc_states_importance"),
        sa.CheckConstraint("depth >= 0", name="ck_npc_states_depth_nonnegative"),
        sa.CheckConstraint("state_revision >= 1", name="ck_npc_states_revision_positive"),
        sa.CheckConstraint("campaign_revision >= 1", name="ck_npc_states_campaign_revision_positive"),
    )
    op.create_index("ix_npc_states_campaign_importance", "npc_states", ["campaign_id", "importance"])
    op.create_index("ix_npc_states_campaign_location", "npc_states", ["campaign_id", "location_entity_id"])


def downgrade() -> None:
    op.drop_index("ix_npc_states_campaign_location", table_name="npc_states")
    op.drop_index("ix_npc_states_campaign_importance", table_name="npc_states")
    op.drop_table("npc_states")
