"""Compatibility marker for the pre-baseline rules-corpus revision."""

from typing import Sequence, Union


revision: str = "f3a1c9d8e2b4"
down_revision: Union[str, Sequence[str], None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """The baseline migration completes the rules schema idempotently."""


def downgrade() -> None:
    """Compatibility marker; the old schema is not removed here."""
