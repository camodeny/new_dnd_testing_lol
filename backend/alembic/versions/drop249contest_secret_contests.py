"""Drop unused secret contest table and roll-request linkage.

Revision ID: drop249contest
Revises: drop219221

``secret_contests`` (#249) had no production entry point. Downgrade restores
the empty table and the ``player_roll_requests.secret_contest_id`` column.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "drop249contest"
down_revision = "drop219221"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_player_roll_requests_secret_contest", table_name="player_roll_requests")
    op.drop_constraint("fk_player_roll_requests_secret_contest", "player_roll_requests", type_="foreignkey")
    op.drop_column("player_roll_requests", "secret_contest_id")
    op.drop_table("secret_contests")


def downgrade() -> None:
    op.create_table(
        "secret_contests",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("initiating_turn_id", sa.UUID(as_uuid=True), sa.ForeignKey("dm_turns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("initiating_attempt_id", sa.UUID(as_uuid=True), sa.ForeignKey("dm_turn_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("initiator_user_id", sa.UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("initiator_character_id", sa.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("contest_key", sa.String(48), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False, server_default="opposed"),
        sa.Column("dc_private", sa.Integer(), nullable=True),
        sa.Column("initiator_roll_request_id", sa.UUID(as_uuid=True), sa.ForeignKey("player_roll_requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_user_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("target_roll_request_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("hidden_cause", sa.Text(), nullable=True),
        sa.Column("reveal_on_success", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reveal_on_failure", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("success_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("failure_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("revealed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("resolved_event_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaign_domain_events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("operation_id", sa.String(128), nullable=True, index=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("initiating_turn_id", "contest_key", name="uq_secret_contests_turn_key"),
        sa.CheckConstraint("status IN ('pending','resolved','cancelled')", name="ck_secret_contests_status"),
        sa.CheckConstraint("mode IN ('opposed','target_vs_dc')", name="ck_secret_contests_mode"),
        sa.Index("ix_secret_contests_campaign_status", "campaign_id", "status"),
    )
    op.add_column(
        "player_roll_requests",
        sa.Column("secret_contest_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_player_roll_requests_secret_contest",
        "player_roll_requests", "secret_contests",
        ["secret_contest_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_player_roll_requests_secret_contest",
        "player_roll_requests", ["secret_contest_id"],
    )
