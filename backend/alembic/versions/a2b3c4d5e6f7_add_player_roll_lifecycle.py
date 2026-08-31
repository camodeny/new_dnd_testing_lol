"""add durable player-owned roll lifecycle — issue 204

Revision ID: a2b3c4d5e6f7
Revises: c1d2e3f4a5b6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dm_turn_attempts", sa.Column("roll_evidence", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.drop_index("uq_dm_turns_active_per_thread", table_name="dm_turns")
    op.execute(sa.text("CREATE UNIQUE INDEX uq_dm_turns_active_per_thread ON dm_turns (campaign_id, thread_id) WHERE status IN ('pending','awaiting_roll','streaming','failed_visible')"))
    op.create_table(
        "player_roll_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_key", sa.String(48), nullable=False),
        sa.Column("requested_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("roll_kind", sa.String(24), nullable=False),
        sa.Column("ability_or_skill", sa.String(64), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("advantage_state", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("reason_public", sa.String(600), nullable=False),
        sa.Column("dc_private", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("replacement_of_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["dm_turns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["dm_turn_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_user_id"], ["profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["replacement_of_id"], ["player_roll_requests.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("turn_id", "request_key", name="uq_player_roll_requests_turn_key"),
        sa.CheckConstraint("status IN ('pending','fulfilled','cancelled','replaced')", name="ck_player_roll_requests_status"),
        sa.CheckConstraint("roll_kind IN ('check','save','attack','ability','initiative','other')", name="ck_player_roll_requests_kind"),
        sa.CheckConstraint("advantage_state IN ('normal','advantage','disadvantage')", name="ck_player_roll_requests_advantage"),
    )
    for name, cols in [
        ("ix_player_roll_requests_campaign_id", ["campaign_id"]), ("ix_player_roll_requests_thread_id", ["thread_id"]),
        ("ix_player_roll_requests_turn_id", ["turn_id"]), ("ix_player_roll_requests_attempt_id", ["attempt_id"]),
        ("ix_player_roll_requests_requested_user_id", ["requested_user_id"]), ("ix_player_roll_requests_character_id", ["character_id"]),
        ("ix_player_roll_requests_status", ["status"]),
        ("ix_player_roll_requests_campaign_thread_status", ["campaign_id", "thread_id", "status"]),
        ("ix_player_roll_requests_player_status", ["requested_user_id", "status"]),
    ]:
        op.create_index(name, "player_roll_requests", cols)
    op.create_table(
        "player_roll_fulfillments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("roll_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submitted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="public"),
        sa.Column("raw_rolls", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("modifier", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("raw_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["roll_request_id"], ["player_roll_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submitted_by"], ["profiles.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("roll_request_id", name="uq_player_roll_fulfillments_request"),
        sa.CheckConstraint("source IN ('app','physical')", name="ck_player_roll_fulfillments_source"),
        sa.CheckConstraint("visibility IN ('public','private')", name="ck_player_roll_fulfillments_visibility"),
    )
    op.create_index("ix_player_roll_fulfillments_roll_request_id", "player_roll_fulfillments", ["roll_request_id"])
    op.create_index("ix_player_roll_fulfillments_submitted_by", "player_roll_fulfillments", ["submitted_by"])


def downgrade() -> None:
    op.drop_table("player_roll_fulfillments")
    op.drop_table("player_roll_requests")
    op.drop_index("uq_dm_turns_active_per_thread", table_name="dm_turns")
    op.execute(sa.text("CREATE UNIQUE INDEX uq_dm_turns_active_per_thread ON dm_turns (campaign_id, thread_id) WHERE status IN ('pending','streaming','failed_visible')"))
    op.drop_column("dm_turn_attempts", "roll_evidence")
