"""add shared/private thread model — issue 195

Revision ID: f6a8b2c1d4e5
Revises: e5f9a2b6c1d4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f6a8b2c1d4e5"
down_revision: Union[str, Sequence[str], None] = "e5f9a2b6c1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_threads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_type", sa.String(32), server_default="campaign", nullable=False),
        sa.Column("title", sa.String(128), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("thread_type IN ('campaign', 'private')", name="ck_campaign_threads_type"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["profiles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "id", name="uq_campaign_threads_campaign_id"),
    )
    op.create_index("ix_campaign_threads_campaign_id", "campaign_threads", ["campaign_id"])
    op.create_index("ix_campaign_threads_created_by", "campaign_threads", ["created_by"])

    op.create_table(
        "campaign_thread_members",
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), server_default="member", nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["campaign_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id", "user_id"),
        sa.UniqueConstraint("thread_id", "user_id", name="uq_campaign_thread_members_identity"),
    )

    # Backfill: one shared campaign thread per existing campaign, and migrate
    # any existing player_submissions.thread_id='main' to the new durable id.
    conn = op.get_bind()
    # Use raw SQL that works on both Postgres and SQLite
    campaigns = conn.execute(sa.text("SELECT id, owner_id FROM campaigns")).fetchall()
    for campaign_id, owner_id in campaigns:
        # Skip if thread already exists (idempotent upgrade)
        existing = conn.execute(
            sa.text("SELECT id FROM campaign_threads WHERE campaign_id = :cid AND thread_type = 'campaign'"),
            {"cid": str(campaign_id)},
        ).fetchone()
        if existing is not None:
            shared_id = existing[0]
        else:
            import uuid as _uuid
            shared_id = str(_uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO campaign_threads (id, campaign_id, thread_type, title, created_by) "
                    "VALUES (:id, :cid, 'campaign', 'Campaign', :created_by)"
                ),
                {"id": shared_id, "cid": str(campaign_id), "created_by": str(owner_id) if owner_id else None},
            )
        # Migrate submissions that still use the legacy 'main' sentinel
        conn.execute(
            sa.text(
                "UPDATE player_submissions SET thread_id = :tid "
                "WHERE campaign_id = :cid AND thread_id = 'main'"
            ),
            {"tid": shared_id, "cid": str(campaign_id)},
        )


def downgrade() -> None:
    op.drop_table("campaign_thread_members")
    op.drop_index("ix_campaign_threads_created_by", table_name="campaign_threads")
    op.drop_index("ix_campaign_threads_campaign_id", table_name="campaign_threads")
    op.drop_table("campaign_threads")
