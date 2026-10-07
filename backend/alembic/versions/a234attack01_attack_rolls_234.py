"""Attack and damage roll linkage on player roll requests (issue #234).

Revision ID: a234attack01
Revises: drop249contest

Attack requests carry the target and the sheet attack used; damage requests
link back to the hit attack and carry the dice code derived from the weapon.
Fulfillments store the code-owned resolution (hit/miss, resolved damage).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a234attack01"
down_revision = "drop249contest"
branch_labels = None
depends_on = None

_OLD_KINDS = "roll_kind IN ('check','save','attack','ability','initiative','other')"
_NEW_KINDS = "roll_kind IN ('check','save','attack','damage','ability','initiative','other')"


def upgrade() -> None:
    op.add_column("player_roll_requests", sa.Column("target_kind", sa.String(8), nullable=True))
    op.add_column("player_roll_requests", sa.Column("target_id", sa.String(64), nullable=True))
    op.add_column("player_roll_requests", sa.Column("attack_name", sa.String(120), nullable=True))
    op.add_column("player_roll_requests", sa.Column("damage_dice", sa.String(32), nullable=True))
    op.add_column(
        "player_roll_requests",
        sa.Column(
            "attack_request_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("player_roll_requests.id", ondelete="CASCADE", name="fk_player_roll_requests_attack_request"),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_player_roll_requests_attack_damage", "player_roll_requests", ["attack_request_id"], unique=True,
    )
    op.drop_constraint("ck_player_roll_requests_kind", "player_roll_requests", type_="check")
    op.create_check_constraint("ck_player_roll_requests_kind", "player_roll_requests", _NEW_KINDS)
    op.add_column(
        "player_roll_fulfillments",
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_roll_fulfillments", "resolution")
    op.execute("DELETE FROM player_roll_requests WHERE roll_kind = 'damage'")
    op.drop_constraint("ck_player_roll_requests_kind", "player_roll_requests", type_="check")
    op.create_check_constraint("ck_player_roll_requests_kind", "player_roll_requests", _OLD_KINDS)
    op.drop_index("uq_player_roll_requests_attack_damage", table_name="player_roll_requests")
    op.drop_column("player_roll_requests", "attack_request_id")
    op.drop_column("player_roll_requests", "damage_dice")
    op.drop_column("player_roll_requests", "attack_name")
    op.drop_column("player_roll_requests", "target_id")
    op.drop_column("player_roll_requests", "target_kind")
