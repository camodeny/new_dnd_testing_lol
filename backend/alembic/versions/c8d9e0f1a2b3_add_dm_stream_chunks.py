"""persist DM stream chunks independently of client connections — issue 197

Revision ID: c8d9e0f1a2b3
Revises: b2c3d4e5f6a7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dm_streams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="streaming", nullable=False),
        sa.Column("audience", sa.String(32), server_default="campaign", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("first_chunk_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_chunk_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=True),
        sa.Column("completion_reason", sa.String(64), nullable=True),
        sa.Column("abandonment_reason", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("operation_id", sa.String(128), nullable=True),
        sa.Column("final_text", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('streaming', 'completed', 'abandoned', 'failed')", name="ck_dm_streams_status"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["campaign_threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "attempt_id", name="uq_dm_streams_turn_attempt"),
    )
    op.create_index("ix_dm_streams_campaign_id", "dm_streams", ["campaign_id"])
    op.create_index("ix_dm_streams_thread_id", "dm_streams", ["thread_id"])
    op.create_index("ix_dm_streams_turn_id", "dm_streams", ["turn_id"])
    op.create_index("ix_dm_streams_attempt_id", "dm_streams", ["attempt_id"])
    op.create_index("ix_dm_streams_trace_id", "dm_streams", ["trace_id"])
    op.create_index("ix_dm_streams_operation_id", "dm_streams", ["operation_id"])
    op.create_index("ix_dm_streams_campaign_thread", "dm_streams", ["campaign_id", "thread_id"])

    op.create_table(
        "dm_stream_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("sequence >= 0", name="ck_dm_stream_chunks_nonnegative_sequence"),
        sa.ForeignKeyConstraint(["stream_id"], ["dm_streams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stream_id", "sequence", name="uq_dm_stream_chunks_stream_sequence"),
    )
    op.create_index("ix_dm_stream_chunks_stream_id", "dm_stream_chunks", ["stream_id"])


def downgrade() -> None:
    op.drop_index("ix_dm_stream_chunks_stream_id", table_name="dm_stream_chunks")
    op.drop_table("dm_stream_chunks")
    op.drop_index("ix_dm_streams_campaign_thread", table_name="dm_streams")
    op.drop_index("ix_dm_streams_operation_id", table_name="dm_streams")
    op.drop_index("ix_dm_streams_trace_id", table_name="dm_streams")
    op.drop_index("ix_dm_streams_attempt_id", table_name="dm_streams")
    op.drop_index("ix_dm_streams_turn_id", table_name="dm_streams")
    op.drop_index("ix_dm_streams_thread_id", table_name="dm_streams")
    op.drop_index("ix_dm_streams_campaign_id", table_name="dm_streams")
    op.drop_table("dm_streams")
