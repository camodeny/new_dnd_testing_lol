"""Loot boxes — issue #463.

The AI DM awards a sealed box whose pool it generated (items of varying
rarity); the character's player opens it, and code draws the contents and
writes them to the sheet. Boxes are earned through play only — never sold.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

LOOT_BOX_STATUSES = ("sealed", "opened")


class LootBox(Base):
    """One sealed (then opened) loot box owned by one character.

    ``pool`` is the DM-generated candidate items; ``draws`` how many the
    opener receives. ``contents`` stays null until opening, when code
    records the drawn items and coins exactly once.
    """

    __tablename__ = "loot_boxes"
    __table_args__ = (
        UniqueConstraint("campaign_id", "award_key", name="uq_loot_boxes_award_key"),
        CheckConstraint("status IN ('sealed', 'opened')", name="ck_loot_boxes_status"),
        CheckConstraint("draws BETWEEN 1 AND 6", name="ck_loot_boxes_draws"),
        Index("ix_loot_boxes_character", "character_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="SET NULL"), nullable=True
    )
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Stable per (award effect, recipient): a replayed commit never awards twice.
    award_key: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="sealed", server_default="sealed")
    draws: Mapped[int] = mapped_column(Integer, nullable=False)
    loot_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    character_level: Mapped[int] = mapped_column(Integer, nullable=False)
    pool: Mapped[list] = mapped_column(JSONB, nullable=False)
    contents: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True
    )
