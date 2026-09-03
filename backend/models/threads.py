"""Threads and submissions domain models."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class CampaignThread(Base):
    """Durable thread identity for live-table messaging — issue #195."""

    __tablename__ = "campaign_threads"
    __table_args__ = (
        CheckConstraint("thread_type IN ('campaign', 'private')", name="ck_campaign_threads_type"),
        Index(
            "uq_campaign_threads_one_campaign_per_campaign",
            "campaign_id",
            unique=True,
            postgresql_where=text("thread_type = 'campaign'"),
            sqlite_where=text("thread_type = 'campaign'"),
        ),
        Index(
            "uq_campaign_threads_private_key",
            "campaign_id",
            "private_key",
            unique=True,
            postgresql_where=text("private_key IS NOT NULL"),
            sqlite_where=text("private_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    thread_type: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    private_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    private_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self, *, include_members: bool = False, members=None):
        result = {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "thread_type": self.thread_type,
            "private_kind": self.private_kind,
            "title": self.title,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_members and members is not None:
            result["members"] = [m.to_dict() for m in members]
        return result


class CampaignThreadMember(Base):
    __tablename__ = "campaign_thread_members"
    __table_args__ = (UniqueConstraint("thread_id", "user_id", name="uq_campaign_thread_members_identity"),)
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaign_threads.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member", server_default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {"thread_id": str(self.thread_id), "user_id": str(self.user_id), "role": self.role, "joined_at": self.joined_at.isoformat() if self.joined_at else None}


class PlayerSubmission(Base):
    __tablename__ = "player_submissions"
    __table_args__ = (
        UniqueConstraint("campaign_id", "thread_id", "sequence", name="uq_player_submissions_campaign_thread_sequence"),
        CheckConstraint("sequence > 0", name="ck_player_submissions_positive_sequence"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False, index=True)
    character_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("characters.id", ondelete="RESTRICT"), nullable=True, index=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, default="main", server_default="main")
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted", server_default="accepted", index=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self, segments=None):
        result = {"id": str(self.id), "campaign_id": str(self.campaign_id), "user_id": str(self.user_id), "character_id": str(self.character_id) if self.character_id else None, "thread_id": self.thread_id, "audience": self.audience, "sequence": self.sequence, "raw_content": self.raw_content, "resolution_status": self.resolution_status, "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None, "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None}
        if segments is not None:
            result["segments"] = [segment.to_dict() for segment in segments]
        return result


class PlayerSubmissionSegment(Base):
    __tablename__ = "player_submission_segments"
    __table_args__ = (
        UniqueConstraint("submission_id", "position", name="uq_player_submission_segments_position"),
        CheckConstraint("position >= 0", name="ck_player_submission_segments_nonnegative_position"),
        CheckConstraint("segment_type IN ('ic', 'ooc')", name="ck_player_submission_segments_type"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("player_submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_type: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    def to_dict(self):
        return {"position": self.position, "type": self.segment_type, "text": self.text}
