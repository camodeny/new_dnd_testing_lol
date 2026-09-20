"""authoritative map geometry, terrain, placements, movement ledger — issue #232 (additive).

Revision ID: f232mapgeom01
Revises: e2130a17c213

Additive only: encounter_maps (one grid row per encounter) plus
encounter_terrain_zones (DM-authored terrain rectangles),
encounter_placements (durable token positions), and encounter_moves
(idempotent movement ledger). No existing tables or rows touched.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f232mapgeom01"
down_revision: Union[str, Sequence[str], None] = "e2130a17c213"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "encounter_maps",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("diagonal_policy", sa.String(24), nullable=False, server_default="no_corner_cut"),
        sa.Column("background_art_ref", sa.String(512), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("encounter_id", name="uq_encounter_maps_encounter"),
        sa.CheckConstraint(
            "diagonal_policy IN ('no_corner_cut', 'allow_corner_cut')",
            name="ck_encounter_maps_diagonal_policy",
        ),
    )
    op.create_index("ix_encounter_maps_campaign", "encounter_maps", ["campaign_id"])

    op.create_table(
        "encounter_terrain_zones",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("map_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounter_maps.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("rect_col", sa.Integer(), nullable=False),
        sa.Column("rect_row", sa.Integer(), nullable=False),
        sa.Column("rect_width", sa.Integer(), nullable=False),
        sa.Column("rect_height", sa.Integer(), nullable=False),
        sa.Column("cost_multiplier", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("label", sa.String(160), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="public"),
        # Explicit author order: later zones win on overlap. Same-transaction
        # rows share created_at while UUID ids carry no author order.
        sa.Column("zone_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('blocked', 'difficult', 'open')", name="ck_terrain_zones_kind"),
        sa.CheckConstraint(
            "visibility IN ('public', 'dm_only')",
            name="ck_terrain_zones_visibility",
        ),
    )
    op.create_index("ix_terrain_zones_map", "encounter_terrain_zones", ["map_id"])

    op.create_table(
        "encounter_placements",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("participant_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounter_participants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("col", sa.Integer(), nullable=False),
        sa.Column("row", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("encounter_id", "participant_id", name="uq_placements_encounter_participant"),
    )
    op.create_index("ix_placements_encounter", "encounter_placements", ["encounter_id"])

    op.create_table(
        "encounter_moves",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("encounter_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("participant_id", sa.UUID(as_uuid=True), sa.ForeignKey("encounter_participants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("movement_mode", sa.String(16), nullable=False, server_default="walk"),
        sa.Column("from_col", sa.Integer(), nullable=False),
        sa.Column("from_row", sa.Integer(), nullable=False),
        sa.Column("to_col", sa.Integer(), nullable=False),
        sa.Column("to_row", sa.Integer(), nullable=False),
        sa.Column("cost_squares", sa.Integer(), nullable=False),
        sa.Column("cost_feet", sa.Integer(), nullable=False),
        sa.Column("path", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("turn_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("encounter_id", "operation_id", name="uq_moves_encounter_operation"),
    )
    op.create_index("ix_moves_encounter", "encounter_moves", ["encounter_id"])


def downgrade() -> None:
    op.drop_table("encounter_moves")
    op.drop_table("encounter_placements")
    op.drop_table("encounter_terrain_zones")
    op.drop_table("encounter_maps")
