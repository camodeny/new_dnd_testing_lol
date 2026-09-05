"""DM turns, streams, and player-roll domain models."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class DMStream(Base):
    __tablename__ = "dm_streams"
    __table_args__ = (
        UniqueConstraint("turn_id", "attempt_id", name="uq_dm_streams_turn_attempt"),
        CheckConstraint("status IN ('streaming', 'completed', 'abandoned', 'failed')", name="ck_dm_streams_status"),
        Index("ix_dm_streams_campaign_thread", "campaign_id", "thread_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaign_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="streaming", server_default="streaming")
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    first_chunk_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_chunk_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    abandonment_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self, *, include_final: bool = True):
        return {"id": str(self.id), "campaign_id": str(self.campaign_id), "thread_id": str(self.thread_id), "turn_id": self.turn_id, "attempt_id": self.attempt_id, "status": self.status, "audience": self.audience, "created_at": self.created_at.isoformat() if self.created_at else None, "updated_at": self.updated_at.isoformat() if self.updated_at else None, "first_chunk_at": self.first_chunk_at.isoformat() if self.first_chunk_at else None, "last_chunk_at": self.last_chunk_at.isoformat() if self.last_chunk_at else None, "completed_at": self.completed_at.isoformat() if self.completed_at else None, "abandoned_at": self.abandoned_at.isoformat() if self.abandoned_at else None, "chunk_count": self.chunk_count, "total_bytes": self.total_bytes, "last_sequence": self.last_sequence, "completion_reason": self.completion_reason, "abandonment_reason": self.abandonment_reason, "trace_id": self.trace_id, "operation_id": self.operation_id, "final_text": self.final_text if include_final else None}


class DMStreamChunk(Base):
    __tablename__ = "dm_stream_chunks"
    __table_args__ = (UniqueConstraint("stream_id", "sequence", name="uq_dm_stream_chunks_stream_sequence"), CheckConstraint("sequence >= 0", name="ck_dm_stream_chunks_nonnegative_sequence"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stream_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("dm_streams.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {"id": str(self.id), "stream_id": str(self.stream_id), "sequence": self.sequence, "text": self.text, "byte_length": self.byte_length, "created_at": self.created_at.isoformat() if self.created_at else None}


class DmTurn(Base):
    __tablename__ = "dm_turns"
    __table_args__ = (
        Index("ix_dm_turns_campaign_thread_status", "campaign_id", "thread_id", "status"),
        Index("ix_dm_turns_campaign_status", "campaign_id", "status"),
        Index("uq_dm_turns_active_per_thread", "campaign_id", "thread_id", unique=True, postgresql_where=text("status IN ('pending','awaiting_roll','streaming','failed_visible')"), sqlite_where=text("status IN ('pending','awaiting_roll','streaming','failed_visible')")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    input_set_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    submission_ids: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list)
    current_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    streaming_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assembly_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assembly_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    streaming_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abandonment_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    time_waiting_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_executing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commit_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {"id": str(self.id), "campaign_id": str(self.campaign_id), "thread_id": self.thread_id, "audience": self.audience, "status": self.status, "source_revision": self.source_revision, "input_set_revision": self.input_set_revision, "submission_ids": self.submission_ids or [], "current_attempt_id": str(self.current_attempt_id) if self.current_attempt_id else None, "streaming_attempt_id": str(self.streaming_attempt_id) if self.streaming_attempt_id else None, "assembly_window_start": self.assembly_window_start.isoformat() if self.assembly_window_start else None, "assembly_window_end": self.assembly_window_end.isoformat() if self.assembly_window_end else None, "streaming_started_at": self.streaming_started_at.isoformat() if self.streaming_started_at else None, "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None, "committed_at": self.committed_at.isoformat() if self.committed_at else None, "abandoned_at": self.abandoned_at.isoformat() if self.abandoned_at else None, "abandonment_reason": self.abandonment_reason, "time_waiting_ms": self.time_waiting_ms, "time_executing_ms": self.time_executing_ms, "commit_duration_ms": self.commit_duration_ms, "created_at": self.created_at.isoformat() if self.created_at else None, "updated_at": self.updated_at.isoformat() if self.updated_at else None}


class DmTurnAttempt(Base):
    __tablename__ = "dm_turn_attempts"
    __table_args__ = (UniqueConstraint("turn_id", "attempt_number", name="uq_dm_turn_attempts_turn_number"), Index("ix_dm_turn_attempts_turn_status", "turn_id", "status"), Index("ix_dm_turn_attempts_campaign_status", "campaign_id", "status"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("dm_turns.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared", server_default="prepared", index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    audience: Mapped[str] = mapped_column(String(32), nullable=False, default="campaign", server_default="campaign")
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    input_set_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    submission_ids: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list)
    parent_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("dm_turn_attempts.id", ondelete="SET NULL"), nullable=True)
    worker_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("worker_executions.id", ondelete="SET NULL"), nullable=True, index=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    streaming_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assembly_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assembly_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    roll_evidence: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list)
    staged_effects: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list)
    contract_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    stream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("dm_streams.id", ondelete="SET NULL"), nullable=True, index=True)
    commit_operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abandonment_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self, *, include_private_roll_evidence: bool = False):
        evidence = self.roll_evidence or []
        if not include_private_roll_evidence:
            evidence = []
            for source in self.roll_evidence or []:
                item = dict(source)
                item.pop("dc_private", None)
                fulfillment = item.get("fulfillment")
                if isinstance(fulfillment, dict) and fulfillment.get("visibility") == "private":
                    item["fulfillment"] = {key: fulfillment.get(key) for key in ("id", "roll_request_id", "submitted_by", "source", "visibility", "submitted_at")}
                evidence.append(item)
        return {"id": str(self.id), "turn_id": str(self.turn_id), "attempt_number": self.attempt_number, "status": self.status, "campaign_id": str(self.campaign_id), "thread_id": self.thread_id, "audience": self.audience, "source_revision": self.source_revision, "input_set_revision": self.input_set_revision, "submission_ids": self.submission_ids or [], "parent_attempt_id": str(self.parent_attempt_id) if self.parent_attempt_id else None, "worker_job_id": str(self.worker_job_id) if self.worker_job_id else None, "invalidation_reason": self.invalidation_reason, "invalidated_at": self.invalidated_at.isoformat() if self.invalidated_at else None, "streaming_started_at": self.streaming_started_at.isoformat() if self.streaming_started_at else None, "last_error": self.last_error, "error_class": self.error_class, "retry_count": self.retry_count or 0, "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None, "assembly_window_start": self.assembly_window_start.isoformat() if self.assembly_window_start else None, "assembly_window_end": self.assembly_window_end.isoformat() if self.assembly_window_end else None, "processing_duration_ms": self.processing_duration_ms, "started_at": self.started_at.isoformat() if self.started_at else None, "completed_at": self.completed_at.isoformat() if self.completed_at else None, "result": self.result, "roll_evidence": evidence, "staged_effects": self.staged_effects or [], "contract_snapshot": self.contract_snapshot, "stream_id": str(self.stream_id) if self.stream_id else None, "commit_operation_id": self.commit_operation_id, "abandoned_at": self.abandoned_at.isoformat() if self.abandoned_at else None, "abandonment_reason": self.abandonment_reason, "created_at": self.created_at.isoformat() if self.created_at else None}


class PlayerRollRequest(Base):
    __tablename__ = "player_roll_requests"
    __table_args__ = (
        UniqueConstraint("turn_id", "request_key", name="uq_player_roll_requests_turn_key"),
        CheckConstraint("status IN ('pending','fulfilled','cancelled','replaced')", name="ck_player_roll_requests_status"),
        CheckConstraint("roll_kind IN ('check','save','attack','ability','initiative','other')", name="ck_player_roll_requests_kind"),
        CheckConstraint("advantage_state IN ('normal','advantage','disadvantage')", name="ck_player_roll_requests_advantage"),
        Index("ix_player_roll_requests_campaign_thread_status", "campaign_id", "thread_id", "status"),
        Index("ix_player_roll_requests_player_status", "requested_user_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("dm_turns.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("dm_turn_attempts.id", ondelete="CASCADE"), nullable=False, index=True)
    request_key: Mapped[str] = mapped_column(String(48), nullable=False)
    requested_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False, index=True)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("characters.id", ondelete="RESTRICT"), nullable=False, index=True)
    roll_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    ability_or_skill: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    advantage_state: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", server_default="normal")
    reason_public: Mapped[str] = mapped_column(String(600), nullable=False)
    dc_private: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending", index=True)
    replacement_of_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("player_roll_requests.id", ondelete="SET NULL"), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self, *, include_private: bool = False):
        value = {"id": str(self.id), "campaign_id": str(self.campaign_id), "thread_id": self.thread_id, "turn_id": str(self.turn_id), "attempt_id": str(self.attempt_id), "request_key": self.request_key, "requested_user_id": str(self.requested_user_id), "character_id": str(self.character_id), "roll_kind": self.roll_kind, "ability_or_skill": self.ability_or_skill, "label": self.label, "advantage_state": self.advantage_state, "reason_public": self.reason_public, "status": self.status, "replacement_of_id": str(self.replacement_of_id) if self.replacement_of_id else None, "requested_at": self.requested_at.isoformat() if self.requested_at else None, "fulfilled_at": self.fulfilled_at.isoformat() if self.fulfilled_at else None, "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None}
        if include_private:
            value["dc_private"] = self.dc_private
        return value


class PlayerRollFulfillment(Base):
    __tablename__ = "player_roll_fulfillments"
    __table_args__ = (UniqueConstraint("roll_request_id", name="uq_player_roll_fulfillments_request"), CheckConstraint("source IN ('app','physical')", name="ck_player_roll_fulfillments_source"), CheckConstraint("visibility IN ('public','private')", name="ck_player_roll_fulfillments_visibility"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    roll_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("player_roll_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    submitted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="public", server_default="public")
    raw_rolls: Mapped[list | None] = mapped_column(JSONB, nullable=False, default=list)
    modifier: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self, *, include_private: bool = False):
        value = {"id": str(self.id), "roll_request_id": str(self.roll_request_id), "submitted_by": str(self.submitted_by), "source": self.source, "visibility": self.visibility, "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None}
        if include_private or self.visibility == "public":
            value.update(raw_rolls=self.raw_rolls or [], modifier=self.modifier, total=self.total, raw_metadata=self.raw_metadata)
        return value
