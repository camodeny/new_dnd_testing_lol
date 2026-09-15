"""world relations + epistemic facts — issue #210 (additive).

Revision ID: c41d210f9a07
Revises: b7e21f4c90d2

Additive only: creates world_relations + world_facts +
world_fact_entity_refs. Does not touch existing tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c41d210f9a07"
down_revision: Union[str, Sequence[str], None] = "b7e21f4c90d2"
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
        "world_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("object_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("object_label", sa.String(length=256), nullable=True),
        sa.Column("epistemic_state", sa.String(length=32), nullable=False, server_default=sa.text("'claimed'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default=sa.text("'dm_only'")),
        sa.Column("grants", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["subject_entity_id"], ["world_entities.id"]),
        sa.ForeignKeyConstraint(["object_entity_id"], ["world_entities.id"]),
        sa.ForeignKeyConstraint(["supersedes_id"], ["world_relations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["world_relations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_world_relations_campaign_idempotency"),
    )
    op.create_index("ix_world_relations_campaign_status", "world_relations", ["campaign_id", "status"])
    op.create_index("ix_world_relations_campaign_subject", "world_relations", ["campaign_id", "subject_entity_id"])
    op.create_index("ix_world_relations_campaign_object", "world_relations", ["campaign_id", "object_entity_id"])
    op.create_index("ix_world_relations_campaign_type", "world_relations", ["campaign_id", "relation_type"])
    op.create_index("ix_world_relations_source_turn", "world_relations", ["campaign_id", "source_turn_id"])
    op.create_index("ix_world_relations_source_event", "world_relations", ["campaign_id", "source_event_id"])

    op.create_table(
        "world_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("epistemic_state", sa.String(length=32), nullable=False, server_default=sa.text("'claimed'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default=sa.text("'dm_only'")),
        sa.Column("grants", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["supersedes_id"], ["world_facts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["world_facts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_world_facts_campaign_idempotency"),
    )
    op.create_index("ix_world_facts_campaign_status", "world_facts", ["campaign_id", "status"])
    op.create_index("ix_world_facts_campaign_epistemic", "world_facts", ["campaign_id", "epistemic_state"])
    op.create_index("ix_world_facts_source_turn", "world_facts", ["campaign_id", "source_turn_id"])
    op.create_index("ix_world_facts_source_event", "world_facts", ["campaign_id", "source_event_id"])

    op.create_table(
        "world_fact_entity_refs",
        sa.Column("fact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["world_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["world_entities.id"]),
        sa.PrimaryKeyConstraint("fact_id", "entity_id"),
    )
    op.create_index("ix_world_fact_entity_refs_campaign_entity", "world_fact_entity_refs", ["campaign_id", "entity_id"])
    op.create_index("ix_world_fact_entity_refs_fact", "world_fact_entity_refs", ["fact_id"])


def downgrade() -> None:
    op.drop_index("ix_world_fact_entity_refs_fact", table_name="world_fact_entity_refs")
    op.drop_index("ix_world_fact_entity_refs_campaign_entity", table_name="world_fact_entity_refs")
    op.drop_table("world_fact_entity_refs")
    op.drop_index("ix_world_facts_source_event", table_name="world_facts")
    op.drop_index("ix_world_facts_source_turn", table_name="world_facts")
    op.drop_index("ix_world_facts_campaign_epistemic", table_name="world_facts")
    op.drop_index("ix_world_facts_campaign_status", table_name="world_facts")
    op.drop_table("world_facts")
    op.drop_index("ix_world_relations_source_event", table_name="world_relations")
    op.drop_index("ix_world_relations_source_turn", table_name="world_relations")
    op.drop_index("ix_world_relations_campaign_type", table_name="world_relations")
    op.drop_index("ix_world_relations_campaign_object", table_name="world_relations")
    op.drop_index("ix_world_relations_campaign_subject", table_name="world_relations")
    op.drop_index("ix_world_relations_campaign_status", table_name="world_relations")
    op.drop_table("world_relations")
