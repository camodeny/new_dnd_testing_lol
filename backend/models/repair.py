"""Durable audited repairs + forward-DM directives — issue #221.

Two tables:
- CampaignRepair: one auditable repair per detected contradiction. Carries
  issue/source linkage, conflicting records, evidence, before state,
  proposed/applied changes, status, actor/source, correction-visibility
  decision, and retcon lineage. Old truth is never deleted by repair —
  handlers supersede/retire rows and preserve provenance.
- RepairDirective: one forward-DM instruction per repair that needs
  player-visible reconciliation. Open directives enter the next relevant
  forward-DM context lane (#202) and are consumed/closed exactly once.

Both tables are DM/operator-scoped by default; player-facing projections
reveal only what the audience may know (secret-safe by construction).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


REPAIR_STATUSES = frozenset({
    "pending", "adjudication_required", "applying", "applied", "failed", "deferred", "retconned",
})

REPAIR_TYPES = frozenset({
    "deterministic_derived",
    "scene_field",
    "entity_merge",
    "entity_keep_distinct",
    "fact_correction",
    "clock_fix",
    "knowledge_fix",
    "npc_fix",
    "summary_rebuild",
    "embedding_refresh",
    "ambiguous",
    "retcon",
})

REPAIR_SOURCES = frozenset({"system", "dm_adjudicated", "operator"})

DIRECTIVE_STATUSES = frozenset({"open", "consumed", "closed"})


class CampaignRepair(Base):
    """One auditable repair — issue #221."""

    __tablename__ = "campaign_repairs"
    __table_args__ = (
        UniqueConstraint("campaign_id", "repair_key", name="uq_campaign_repairs_campaign_key"),
        Index("ix_campaign_repairs_campaign_status", "campaign_id", "status"),
        Index("ix_campaign_repairs_campaign_incident", "campaign_id", "incident_id"),
        CheckConstraint(
            "status IN ('pending','adjudication_required','applying','applied','failed','deferred','retconned')",
            name="ck_campaign_repairs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("post_turn_consistency_incidents.id", ondelete="SET NULL"), nullable=True
    )
    repair_key: Mapped[str] = mapped_column(String(128), nullable=False)
    repair_type: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic_derived")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    detection_path: Mapped[str] = mapped_column(String(16), nullable=False, default="automatic", server_default="automatic")
    affected_domains: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    conflicting_records: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    before_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proposed_changes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    applied_changes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="system", server_default="system")
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requires_player_visible_correction: Mapped[bool] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    player_visible_correction: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="dm_only", server_default="dm_only")
    is_retcon: Mapped[bool] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retcon_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_repairs.id", ondelete="SET NULL"), nullable=True
    )
    decision_policy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    decision_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_distribution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self, *, include_evidence: bool = True):
        out = {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "incident_id": str(self.incident_id) if self.incident_id else None,
            "repair_key": self.repair_key,
            "repair_type": self.repair_type,
            "status": self.status,
            "detection_path": self.detection_path,
            "affected_domains": list(self.affected_domains or []),
            "conflicting_records": list(self.conflicting_records or []),
            "before_state": dict(self.before_state or {}),
            "proposed_changes": list(self.proposed_changes or []),
            "applied_changes": list(self.applied_changes or []),
            "reason": self.reason,
            "source": self.source,
            "actor_id": self.actor_id,
            "requires_player_visible_correction": bool(self.requires_player_visible_correction),
            "player_visible_correction": dict(self.player_visible_correction or {}) if self.player_visible_correction else None,
            "visibility": self.visibility,
            "is_retcon": bool(self.is_retcon),
            "retcon_of_id": str(self.retcon_of_id) if self.retcon_of_id else None,
            "decision_policy": dict(self.decision_policy or {}),
            "decision_model": self.decision_model,
            "decision_distribution": dict(self.decision_distribution or {}),
            "operation_id": self.operation_id,
            "error": self.error,
            "retry_count": self.retry_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
        if include_evidence:
            out["evidence"] = dict(self.evidence or {})
        return out

    def player_projection(self) -> dict | None:
        """Audience-safe correction: only what players may know.

        Returns None unless a member-visible (campaign/public) correction
        is required with explicit correction text. Never includes DM-only
        evidence, before-state secrets, reason prose, or knowledge grants.
        """
        if not self.requires_player_visible_correction or not self.player_visible_correction:
            return None
        safe = dict(self.player_visible_correction or {})
        if str(safe.get("scope") or "") not in ("campaign", "public"):
            return None
        text = str(safe.get("correction_text") or "").strip()
        if not text:
            return None
        return {
            "repair_id": str(self.id),
            "correction_text": text[:2000],
            "scope": str(safe.get("scope")),
        }


class RepairDirective(Base):
    """One forward-DM reconciliation instruction — issue #221 / lane #202.

    ``open`` directives are assembled into the next relevant forward-DM
    context packet; the DM consumes exactly one directive per reconciliation
    (``consumed``), and closure (``closed``) happens once the correction has
    been narrated or explicitly reconciled. Payload is DM-scoped; the
    optional ``player_safe_payload`` holds the audience-safe correction.
    """

    __tablename__ = "repair_directives"
    __table_args__ = (
        UniqueConstraint("campaign_id", "directive_key", name="uq_repair_directives_campaign_key"),
        Index("ix_repair_directives_campaign_status", "campaign_id", "status"),
        Index("ix_repair_directives_campaign_repair", "campaign_id", "repair_id"),
        CheckConstraint(
            "status IN ('open','consumed','closed')",
            name="ck_repair_directives_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repair_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_repairs.id", ondelete="CASCADE"), nullable=False
    )
    directive_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    audience: Mapped[str] = mapped_column(String(16), nullable=False, default="campaign", server_default="campaign")
    thread_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    player_safe_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="dm_only", server_default="dm_only")
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self, *, include_payload: bool = True):
        out = {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "repair_id": str(self.repair_id),
            "directive_key": self.directive_key,
            "status": self.status,
            "audience": self.audience,
            "thread_id": str(self.thread_id) if self.thread_id else None,
            "visibility": self.visibility,
            "operation_id": self.operation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "consumed_at": self.consumed_at.isoformat() if self.consumed_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }
        if include_payload:
            out["payload"] = dict(self.payload or {})
            out["player_safe_payload"] = dict(self.player_safe_payload or {}) if self.player_safe_payload else None
        return out
