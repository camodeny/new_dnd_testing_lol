"""world scene + canonical entities — issue #209 (additive).

Revision ID: 9c1d209a44aa
Revises: 2a04bc8c83ba

Additive only: creates world_entities + campaign_current_scenes.
Does not touch the squashed baseline.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "9c1d209a44aa"
down_revision: Union[str, Sequence[str], None] = "2a04bc8c83ba"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name if conn is not None else "postgresql"
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
        if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
            SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
            SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    op.create_table(
        "world_entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default=sa.text("'campaign'")),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_world_entities_campaign_idempotency"),
    )
    op.create_index("ix_world_entities_campaign_id", "world_entities", ["campaign_id"])
    op.create_index("ix_world_entities_campaign_type", "world_entities", ["campaign_id", "entity_type"])
    op.create_index("ix_world_entities_campaign_status", "world_entities", ["campaign_id", "status"])

    op.create_table(
        "campaign_current_scenes",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("location_name", sa.String(length=256), nullable=True),
        sa.Column("fictional_time", sa.String(length=256), nullable=True),
        sa.Column("fictional_time_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("present_actors", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("environment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default=sa.text("'campaign'")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["location_entity_id"], ["world_entities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("campaign_id"),
    )
    op.create_index(
        "ix_campaign_current_scenes_location_entity",
        "campaign_current_scenes", ["location_entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_current_scenes_location_entity", table_name="campaign_current_scenes")
    op.drop_table("campaign_current_scenes")
    op.drop_index("ix_world_entities_campaign_status", table_name="world_entities")
    op.drop_index("ix_world_entities_campaign_type", table_name="world_entities")
    op.drop_index("ix_world_entities_campaign_id", table_name="world_entities")
    op.drop_table("world_entities")
