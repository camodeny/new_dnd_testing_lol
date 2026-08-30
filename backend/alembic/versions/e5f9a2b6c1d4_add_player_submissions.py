"""add durable ordered player submissions — issue 194

Revision ID: e5f9a2b6c1d4
Revises: d4e8f1a7b2c9
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e5f9a2b6c1d4"
down_revision: Union[str, Sequence[str], None] = "d4e8f1a7b2c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("thread_id", sa.String(128), server_default="main", nullable=False),
        sa.Column("audience", sa.String(32), server_default="campaign", nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("resolution_status", sa.String(32), server_default="accepted", nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sequence > 0", name="ck_player_submissions_positive_sequence"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "thread_id", "sequence", name="uq_player_submissions_campaign_thread_sequence"),
    )
    op.create_index("ix_player_submissions_campaign_id", "player_submissions", ["campaign_id"])
    op.create_index("ix_player_submissions_user_id", "player_submissions", ["user_id"])
    op.create_index("ix_player_submissions_character_id", "player_submissions", ["character_id"])
    op.create_index("ix_player_submissions_resolution_status", "player_submissions", ["resolution_status"])
    op.create_table(
        "player_submission_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("segment_type", sa.String(8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_player_submission_segments_nonnegative_position"),
        sa.CheckConstraint("segment_type IN ('ic', 'ooc')", name="ck_player_submission_segments_type"),
        sa.ForeignKeyConstraint(["submission_id"], ["player_submissions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("submission_id", "position", name="uq_player_submission_segments_position"),
    )
    op.create_index("ix_player_submission_segments_submission_id", "player_submission_segments", ["submission_id"])


def downgrade() -> None:
    op.drop_index("ix_player_submission_segments_submission_id", table_name="player_submission_segments")
    op.drop_table("player_submission_segments")
    op.drop_index("ix_player_submissions_resolution_status", table_name="player_submissions")
    op.drop_index("ix_player_submissions_character_id", table_name="player_submissions")
    op.drop_index("ix_player_submissions_user_id", table_name="player_submissions")
    op.drop_index("ix_player_submissions_campaign_id", table_name="player_submissions")
    op.drop_table("player_submissions")
