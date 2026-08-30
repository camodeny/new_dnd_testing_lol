"""add campaign revision and immutable domain events — issue 188

Revision ID: c9f5a2e6b1d4
Revises: a1b2c3d4e5f6
Create Date: 2026-08-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c9f5a2e6b1d4"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    # ── campaigns.revision ──────────────────────────────────────────────
    if "campaigns" in tables:
        cols = {c["name"] for c in inspector.get_columns("campaigns")}
        if "revision" not in cols:
            op.add_column(
                "campaigns",
                sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            )
            # backfill existing rows to 0 (already server_default, but be explicit)
            op.execute(sa.text("UPDATE campaigns SET revision = 0 WHERE revision IS NULL"))

    # ── campaign_domain_events ──────────────────────────────────────────
    if "campaign_domain_events" not in tables:
        op.create_table(
            "campaign_domain_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("operation_id", sa.String(length=128), nullable=True),
            sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("visibility", sa.String(length=32), nullable=False, server_default="public"),
            sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["actor_id"], ["profiles.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("campaign_id", "sequence", name="uq_campaign_domain_events_campaign_sequence"),
        )
        op.create_index(
            "ix_campaign_domain_events_campaign_id", "campaign_domain_events", ["campaign_id"]
        )
        op.create_index(
            "ix_campaign_domain_events_campaign_sequence",
            "campaign_domain_events",
            ["campaign_id", "sequence"],
            unique=True,
        )
        op.create_index(
            "ix_campaign_domain_events_actor_id", "campaign_domain_events", ["actor_id"]
        )
        op.create_index(
            "ix_campaign_domain_events_event_type", "campaign_domain_events", ["event_type"]
        )


def downgrade() -> None:
    op.drop_index("ix_campaign_domain_events_event_type", table_name="campaign_domain_events")
    op.drop_index("ix_campaign_domain_events_actor_id", table_name="campaign_domain_events")
    op.drop_index("ix_campaign_domain_events_campaign_sequence", table_name="campaign_domain_events")
    op.drop_index("ix_campaign_domain_events_campaign_id", table_name="campaign_domain_events")
    op.drop_table("campaign_domain_events")
    # keep revision column downgrade idempotent
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    if "campaigns" in tables:
        cols = {c["name"] for c in inspector.get_columns("campaigns")}
        if "revision" in cols:
            op.drop_column("campaigns", "revision")
