"""Mark system-originated player submissions (issue #246 opener).

Revision ID: s246start01
Revises: e927softdelchar
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "s246start01"
down_revision: Union[str, Sequence[str], None] = "e927softdelchar"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OPENING_OOC_TEXT = (
    "[Campaign start #246] Begin the adventure: describe the opening scene, "
    "introduce each party member by name, and ask what the party does."
)


def upgrade() -> None:
    op.add_column(
        "player_submissions",
        sa.Column("source", sa.String(64), nullable=True),
    )
    # Backfill #246 openers staged before the marker existed. Player-authored
    # rows keep NULL (player-sent). Match is exact on the deterministic
    # opener text, which players never type verbatim.
    op.execute(
        sa.text(
            "UPDATE player_submissions SET source = 'campaign-start-246' "
            "WHERE source IS NULL AND raw_content = :text"
        ).bindparams(text=_OPENING_OOC_TEXT)
    )


def downgrade() -> None:
    op.drop_column("player_submissions", "source")
