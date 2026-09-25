"""Post-turn durable checkpoint + run records — issue #216."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class PostTurnCheckpoint(Base):
    """One durable processed-through sequence per campaign.

    processed_through_sequence is the highest campaign_domain_events.sequence
    whose required post-turn consolidation has durably succeeded. 0 means
    nothing consolidated yet. Only moves forward.
    """

    __tablename__ = "post_turn_checkpoints"
    __table_args__ = (
        CheckConstraint("processed_through_sequence >= 0", name="ck_post_turn_checkpoint_nonnegative"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    processed_through_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "campaign_id": str(self.campaign_id),
            "processed_through_sequence": self.processed_through_sequence,
            "updated_by_run_id": str(self.updated_by_run_id) if self.updated_by_run_id else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PostTurnRun(Base):
    """Durable record of one post-turn consolidation attempt over a sequence range.

    The run id doubles as the outbox/worker job_id so duplicate delivery
    across outbox relay -> queue -> worker maps to the same logical run.
    """

    __tablename__ = "post_turn_runs"
    __table_args__ = (
        UniqueConstraint("campaign_id", "from_sequence", "to_sequence", name="uq_post_turn_runs_campaign_range"),
        CheckConstraint("from_sequence >= 1", name="ck_post_turn_runs_from_gte_1"),
        CheckConstraint("to_sequence >= from_sequence", name="ck_post_turn_runs_to_gte_from"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','skipped')",
            name="ck_post_turn_runs_status",
        ),
        CheckConstraint(
            "trigger IN ('normal','force','critical','admin_skip')",
            name="ck_post_turn_runs_trigger",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    to_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", server_default="normal")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "from_sequence": self.from_sequence,
            "to_sequence": self.to_sequence,
            "trigger": self.trigger,
            "status": self.status,
            "attempts": self.attempts,
            "failure_reason": self.failure_reason,
            "operation_id": self.operation_id,
            "trace_id": self.trace_id,
            "result": self.result,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class PostTurnConsistencyIncident(Base):
    """Explicit post-turn consistency incident — issue #220.

    A detected contradiction between newly materialized state, completed
    visible turns, clocks, current scene, facts, and summaries. Incidents
    are recorded instead of silently normalizing canon away; the #221
    repair workflow consumes them (this verifier never rewrites canon).

    ``incident_key`` makes creation idempotent per (campaign, conflict,
    source range): re-verifying the same range reuses the row and bumps
    ``repeat_count``. ``status`` stays required-unresolved (``open``,
    ``deferred``, ``verifier_failed``) until repair marks it ``resolved``.

    Rows are DM/operator-only by default; player-facing correction is
    decided by repair.
    """

    __tablename__ = "post_turn_consistency_incidents"
    __table_args__ = (
        UniqueConstraint("campaign_id", "incident_key", name="uq_post_turn_incidents_campaign_key"),
        Index("ix_post_turn_incidents_campaign_status", "campaign_id", "status"),
        CheckConstraint(
            "status IN ('open','deferred','verifier_failed','resolved')",
            name="ck_post_turn_incidents_status",
        ),
        CheckConstraint(
            "detection_path IN ('deterministic','semantic','operational')",
            name="ck_post_turn_incidents_path",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    to_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    incident_key: Mapped[str] = mapped_column(String(128), nullable=False)
    incident_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="canon_conflict")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    detection_path: Mapped[str] = mapped_column(String(16), nullable=False, default="deterministic")
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    affected_records: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    decision_policy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    decision_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_distribution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    detection_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self, *, include_evidence: bool = True):
        out = {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "source_range": [self.from_sequence, self.to_sequence],
            "incident_key": self.incident_key,
            "incident_type": self.incident_type,
            "category": self.category,
            "severity": self.severity,
            "status": self.status,
            "detection_path": self.detection_path,
            "affected_records": list(self.affected_records or []),
            "decision_policy": dict(self.decision_policy or {}),
            "decision_model": self.decision_model,
            "decision_distribution": dict(self.decision_distribution or {}),
            "detection_latency_ms": self.detection_latency_ms,
            "repeat_count": self.repeat_count,
            "operation_id": self.operation_id,
            "error": self.error,
            "visibility": "dm_only",
            "repair_status": "needs_repair" if self.status != "resolved" else "resolved",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
        if include_evidence:
            out["evidence"] = dict(self.evidence or {})
        return out
