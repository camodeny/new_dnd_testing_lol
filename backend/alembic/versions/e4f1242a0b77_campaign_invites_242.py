"""normalize campaign invites for lobby flow — issue #242.

Revision ID: e4f1242a0b77
Revises: d3a263a1f263

Pre-alpha canonical rebuild of ``campaign_invites``: one row per invite
(id PK) instead of one row per campaign (campaign_id PK), plus creator,
intended recipient metadata, status/expiry/revocation, accepted-count, and
email-delivery observability columns.

Existing rows are preserved: each keeps its code/campaign and gains an id.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e4f1242a0b77"
down_revision: Union[str, Sequence[str], None] = "d3a263a1f263"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) New id column; backfill existing rows, then promote to PK.
    op.add_column("campaign_invites", sa.Column("id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(sa.text("UPDATE campaign_invites SET id = gen_random_uuid() WHERE id IS NULL"))
    op.alter_column("campaign_invites", "id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_constraint("campaign_invites_pkey", "campaign_invites", type_="primary")
    op.create_primary_key("campaign_invites_pkey", "campaign_invites", ["id"])

    # 2) New domain columns.
    op.add_column("campaign_invites", sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("campaign_invites", sa.Column("intended_email", sa.String(length=320), nullable=True))
    op.add_column("campaign_invites", sa.Column("recipient_label", sa.String(length=128), nullable=True))
    op.add_column("campaign_invites", sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'active'")))
    op.add_column("campaign_invites", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaign_invites", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaign_invites", sa.Column("accepted_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("campaign_invites", sa.Column("last_delivery_status", sa.String(length=16), nullable=True))
    op.add_column("campaign_invites", sa.Column("last_delivery_error", sa.Text(), nullable=True))
    op.add_column("campaign_invites", sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "campaign_invites",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_foreign_key(
        "fk_campaign_invites_created_by",
        "campaign_invites",
        "profiles",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_campaign_invites_campaign_id", "campaign_invites", ["campaign_id"])
    op.create_index("ix_campaign_invites_created_by", "campaign_invites", ["created_by"])
    op.create_check_constraint(
        "ck_campaign_invites_status",
        "campaign_invites",
        "status IN ('active', 'revoked')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_campaign_invites_status", "campaign_invites", type_="check")
    op.drop_index("ix_campaign_invites_created_by", table_name="campaign_invites")
    op.drop_index("ix_campaign_invites_campaign_id", table_name="campaign_invites")
    op.drop_constraint("fk_campaign_invites_created_by", "campaign_invites", type_="foreignkey")
    op.drop_column("campaign_invites", "updated_at")
    op.drop_column("campaign_invites", "last_delivery_at")
    op.drop_column("campaign_invites", "last_delivery_error")
    op.drop_column("campaign_invites", "last_delivery_status")
    op.drop_column("campaign_invites", "accepted_count")
    op.drop_column("campaign_invites", "revoked_at")
    op.drop_column("campaign_invites", "expires_at")
    op.drop_column("campaign_invites", "status")
    op.drop_column("campaign_invites", "recipient_label")
    op.drop_column("campaign_invites", "intended_email")
    op.drop_column("campaign_invites", "created_by")
    # Best-effort restore of the old one-row-per-campaign shape: keep the
    # oldest invite per campaign, then re-key on campaign_id.
    op.execute(sa.text(
        "DELETE FROM campaign_invites a USING campaign_invites b "
        "WHERE a.campaign_id = b.campaign_id AND a.created_at > b.created_at"
    ))
    op.drop_constraint("campaign_invites_pkey", "campaign_invites", type_="primary")
    op.drop_column("campaign_invites", "id")
    op.create_primary_key("campaign_invites_pkey", "campaign_invites", ["campaign_id"])
