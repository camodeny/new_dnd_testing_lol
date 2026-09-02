"""Compatibility marker for databases deployed before the baseline squash."""

from typing import Sequence, Union


revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """The existing pre-squash schema is already present."""


def downgrade() -> None:
    """Compatibility marker; the old schema is not removed here."""
