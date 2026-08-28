"""add campaigns, members, invites

Revision ID: b7c9e2a4f1d3
Revises: 94fb722d0dcc
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b7c9e2a4f1d3"
down_revision: Union[str, Sequence[str], None] = "94fb722d0dcc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # campaigns — idempotent: only create if not exists (handles local create_all pre-existing)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = set(inspector.get_table_names())

    if "campaigns" not in existing:
        op.create_table(
            "campaigns",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("random_seed", sa.String(length=128), nullable=True),
            sa.Column("required_players", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("loot_mode", sa.String(length=32), nullable=False, server_default="frequent_gamble"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("campaigns_owner_idx", "campaigns", ["owner_id"])
        op.execute(sa.text("""
            create trigger campaigns_updated_at
              before update on public.campaigns
              for each row execute function public.handle_updated_at();
        """))

    if "campaign_members" not in existing:
        op.create_table(
            "campaign_members",
            sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False, server_default="player"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("campaign_id", "user_id"),
        )
        op.create_index("campaign_members_user_idx", "campaign_members", ["user_id"])

    if "campaign_invites" not in existing:
        op.create_table(
            "campaign_invites",
            sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("code", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("campaign_id"),
            sa.UniqueConstraint("code"),
        )


def downgrade() -> None:
    op.execute(sa.text("drop trigger if exists campaigns_updated_at on public.campaigns"))
    op.drop_table("campaign_invites")
    op.drop_index("campaign_members_user_idx", table_name="campaign_members")
    op.drop_table("campaign_members")
    op.drop_index("campaigns_owner_idx", table_name="campaigns")
    op.drop_table("campaigns")
