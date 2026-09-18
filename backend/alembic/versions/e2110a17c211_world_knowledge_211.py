"""world knowledge + visibility grants — issue #211 (additive).

Revision ID: e2110a17c211
Revises: d3a263a1f263

Additive only: creates world_knowledge + world_visibility_grants. Does not
touch existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e2110a17c211"
down_revision: Union[str, Sequence[str], None] = "8f3a25300001"
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
        "world_knowledge",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("target_fact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_relation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("knowledge_state", sa.String(length=16), nullable=False, server_default=sa.text("'believes'")),
        sa.Column("acquisition_source", sa.String(length=64), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default=sa.text("'dm_only'")),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["world_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_fact_id"], ["world_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_relation_id"], ["world_relations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_entity_id"], ["world_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_world_knowledge_campaign_idempotency"),
    )
    op.create_index("ix_world_knowledge_campaign_subject", "world_knowledge", ["campaign_id", "subject_entity_id"])
    op.create_index("ix_world_knowledge_campaign_fact", "world_knowledge", ["campaign_id", "target_fact_id"])
    op.create_index("ix_world_knowledge_campaign_relation", "world_knowledge", ["campaign_id", "target_relation_id"])
    op.create_index("ix_world_knowledge_campaign_tentity", "world_knowledge", ["campaign_id", "target_entity_id"])

    op.create_table(
        "world_visibility_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grantee_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grantee_user_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_world_visibility_grants_campaign_idempotency"),
    )
    op.create_index("ix_world_visibility_grants_target", "world_visibility_grants", ["campaign_id", "target_kind", "target_id"])
    op.create_index("ix_world_visibility_grants_grantee", "world_visibility_grants", ["campaign_id", "grantee_user_id"])


def downgrade() -> None:
    op.drop_index("ix_world_visibility_grants_grantee", table_name="world_visibility_grants")
    op.drop_index("ix_world_visibility_grants_target", table_name="world_visibility_grants")
    op.drop_table("world_visibility_grants")
    op.drop_index("ix_world_knowledge_campaign_tentity", table_name="world_knowledge")
    op.drop_index("ix_world_knowledge_campaign_relation", table_name="world_knowledge")
    op.drop_index("ix_world_knowledge_campaign_fact", table_name="world_knowledge")
    op.drop_index("ix_world_knowledge_campaign_subject", table_name="world_knowledge")
    op.drop_table("world_knowledge")
