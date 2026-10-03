"""World evidence packets, audience gates, and evidence-tool result envelopes.

Shared by structured retrieval (#212) and semantic recall (#213): one typed
packet shape with stable source identity, epistemic state, preserved
visibility, and code-owned provenance; one set of per-record audience gates
so every retrieval path authorizes identically; one result envelope for the
#203 evidence tools.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.schema import coerce_uuid
from app.threads.service import can_read_thread, parse_thread_id
from app.visibility.access import is_campaign_participant, is_world_authority, may_user_receive
from app.visibility.policy import RECORD_VISIBILITIES, evidence_bundle_visibility, visible_to_viewer
from models.campaigns import Campaign, CampaignDomainEvent
from models.dm import DmTurn
from models.threads import CampaignThread, PlayerSubmission

RETRIEVAL_SOURCE = "world_retrieval_212"


# ── Typed evidence packet ────────────────────────────────────────────────────

@dataclass
class EvidencePacket:
    """One normalized authoritative evidence item.

    ``source_type``/``source_id``/``source_version`` form the stable source
    identity used by validators and audit tooling. ``retrieval_rank`` is the
    deterministic retrieval order.
    ``revealable`` is meaningful in player-facing mode (True = authorized
    for the viewer); in DM-internal mode it is None (deferred to later
    projection) while ``visibility`` is always preserved.
    """

    source_type: str
    source_id: str
    source_version: str
    content: dict[str, Any]
    epistemic_state: str | None
    visibility: str
    campaign_id: str
    revision_or_sequence: int | None
    provenance: dict[str, Any]
    retrieval_rank: int = 0
    retrieval_score: float = 0.0
    revealable: bool | None = None
    denial_reason: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "content": self.content,
            "epistemic_state": self.epistemic_state,
            "visibility": self.visibility,
            "campaign_id": self.campaign_id,
            "revision_or_sequence": self.revision_or_sequence,
            "provenance": self.provenance,
            "retrieval_rank": self.retrieval_rank,
            "retrieval_score": self.retrieval_score,
            "revealable": self.revealable,
            "denial_reason": self.denial_reason,
            "created_at": self.created_at,
        }

    def to_source_ref(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "campaign_revision": self.revision_or_sequence,
            "provenance": dict(self.provenance),
        }


def packet_source_ids(packets: list[EvidencePacket]) -> list[str]:
    return sorted({f"{p.source_type}:{p.source_id}@{p.source_version}" for p in packets})


def packet_provenance(
    record: Any, *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Code-owned provenance built only from persisted record fields."""
    prov: dict[str, Any] = {"retrieved_by": RETRIEVAL_SOURCE}
    for attr in ("source_turn_id", "source_attempt_id", "source_event_id",
                 "actor_id", "operation_id", "idempotency_key"):
        value = getattr(record, attr, None)
        if value is not None:
            prov[attr] = str(value)
    stored = getattr(record, "provenance", None)
    if isinstance(stored, dict):
        for key, value in stored.items():
            prov.setdefault(f"record_{key}", value)
    if extra:
        for key, value in extra.items():
            prov.setdefault(key, value)
    return prov


def version_of(record: Any, *, fallback: str = "1") -> str:
    version = getattr(record, "version", None)
    if version is not None:
        return f"v{int(version)}"
    updated = getattr(record, "updated_at", None)
    if updated is not None:
        try:
            return updated.isoformat()
        except Exception:
            pass
    return fallback


def _created_iso(record: Any) -> str | None:
    created = getattr(record, "created_at", None)
    try:
        return created.isoformat() if created else None
    except Exception:
        return None


def resolve_campaign(db: Session, campaign_id: Any) -> Campaign:
    cid = coerce_uuid(campaign_id, field="campaign_id")
    campaign = db.get(Campaign, cid)
    if campaign is None:
        raise ValueError(f"Campaign {cid} not found")
    return campaign


def resolve_viewers(viewer_user_id: Any) -> list[uuid.UUID]:
    """Normalize one viewer id or a list of them (fail-closed on garbage)."""
    if viewer_user_id is None:
        return []
    raw = viewer_user_id if isinstance(viewer_user_id, (list, tuple)) else [viewer_user_id]
    viewers: list[uuid.UUID] = []
    for value in raw:
        try:
            viewers.append(coerce_uuid(value, field="viewer_user_id"))
        except ValueError:
            continue
    seen: set[str] = set()
    unique: list[uuid.UUID] = []
    for viewer in viewers:
        if str(viewer) not in seen:
            seen.add(str(viewer))
            unique.append(viewer)
    return unique


# ── Packet builders (one per record kind; visibility always preserved) ───────

def entity_packet(entity: Any, campaign_id: uuid.UUID, rank: int,
                   *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="world_entity",
        source_id=str(entity.id),
        source_version=version_of(entity),
        content=entity.to_dict(),
        epistemic_state=None,
        visibility=str(getattr(entity, "visibility", "campaign")),
        campaign_id=str(campaign_id),
        revision_or_sequence=None,
        provenance=packet_provenance(entity),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(entity),
    )


def relation_packet(relation: Any, campaign_id: uuid.UUID, rank: int,
                     *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="world_relation",
        source_id=str(relation.id),
        source_version=version_of(relation),
        content=relation.to_dict(),
        epistemic_state=str(getattr(relation, "epistemic_state", "claimed")),
        visibility=str(getattr(relation, "visibility", "dm_only")),
        campaign_id=str(campaign_id),
        revision_or_sequence=None,
        provenance=packet_provenance(relation),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(relation),
    )


def fact_packet(fact: Any, campaign_id: uuid.UUID, rank: int,
                 *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="world_fact",
        source_id=str(fact.id),
        source_version=version_of(fact),
        content=fact.to_dict(),
        epistemic_state=str(getattr(fact, "epistemic_state", "claimed")),
        visibility=str(getattr(fact, "visibility", "dm_only")),
        campaign_id=str(campaign_id),
        revision_or_sequence=None,
        provenance=packet_provenance(fact),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(fact),
    )


def event_packet(event: CampaignDomainEvent, rank: int,
                  *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="domain_event",
        source_id=str(event.id),
        source_version=f"seq{int(event.sequence)}",
        content=event.to_dict(),
        epistemic_state=None,
        visibility=str(getattr(event, "visibility", "public")),
        campaign_id=str(event.campaign_id),
        revision_or_sequence=int(event.sequence),
        provenance=packet_provenance(event, extra={"event_type": event.event_type}),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(event),
    )


def turn_packet(turn: DmTurn, rank: int, *,
                 revealable: bool | None,
                 submissions: list[dict[str, Any]] | None = None,
                 records: dict[str, list[str]] | None = None) -> EvidencePacket:
    audience = str(getattr(turn, "audience", "campaign") or "campaign")
    visibility = audience if audience in RECORD_VISIBILITIES else "campaign"
    content = turn.to_dict()
    if submissions is not None:
        content = {**content, "submissions": submissions}
    if records is not None:
        content = {**content, "established_records": records}
    return EvidencePacket(
        source_type="source_turn",
        source_id=str(turn.id),
        source_version=version_of(turn),
        content=content,
        epistemic_state=None,
        visibility=visibility,
        campaign_id=str(turn.campaign_id),
        revision_or_sequence=int(getattr(turn, "source_revision", 0) or 0),
        provenance=packet_provenance(turn, extra={"thread_id": str(getattr(turn, "thread_id", ""))}),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(turn),
    )


def scene_packet(scene: Any, rank: int, *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="scene",
        source_id=str(scene.campaign_id),
        source_version=f"r{int(getattr(scene, 'revision', 0) or 0)}",
        content=scene.to_dict(),
        epistemic_state=None,
        visibility=str(getattr(scene, "visibility", "campaign")),
        campaign_id=str(scene.campaign_id),
        revision_or_sequence=int(getattr(scene, "revision", 0) or 0),
        provenance=packet_provenance(scene),
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=_created_iso(scene),
    )


def knowledge_packet(entry: dict[str, Any], rank: int,
                      *, revealable: bool | None) -> EvidencePacket:
    return EvidencePacket(
        source_type="knowledge",
        source_id=str(entry.get("knowledge_id", f"knowledge:{rank}")),
        source_version="1",
        content=dict(entry),
        epistemic_state=str(entry.get("knowledge_state", "believes")),
        visibility=str(entry.get("visibility", "dm_only") or "dm_only"),
        campaign_id=str(entry.get("campaign_id", "")),
        revision_or_sequence=None,
        provenance={"retrieved_by": RETRIEVAL_SOURCE,
                    "subject_entity_id": str(entry.get("subject_entity_id", "")),
                    "target_kind": str(entry.get("target_kind", "")),
                    "target_id": str(entry.get("target_id", ""))},
        retrieval_rank=rank,
        retrieval_score=float(1000 - rank),
        revealable=revealable,
        created_at=None,
    )


# ── Audience gates ───────────────────────────────────────────────────────────

def authorize_world_record(
    db: Session, campaign: Campaign, kind: str, record_id: uuid.UUID,
    viewers: list[uuid.UUID], *, dm_internal: bool,
) -> tuple[bool, str | None]:
    """Return (allowed, denial_reason) for one world record.

    DM-internal mode never denies here — visibility metadata travels on the
    packet for later projection. Player-facing mode requires membership plus
    an explicit ``may_user_receive`` allow for EVERY listed viewer
    (intersection: the most restrictive viewer wins, fail closed).
    """
    if dm_internal:
        return True, None
    if not viewers:
        return False, "viewer_required"
    for viewer in viewers:
        if not is_campaign_participant(db, campaign, viewer):
            return False, "not_campaign_member"
        verdict = may_user_receive(db, campaign, kind, record_id, viewer)
        if not verdict.get("allowed"):
            return False, str(verdict.get("reason", "denied"))
    return True, None


def event_visible_player_facing(db: Session, campaign: Campaign,
                                  event: CampaignDomainEvent,
                                  viewers: list[uuid.UUID]) -> tuple[bool, str | None]:
    """Member feed rule mirroring ``list_campaign_events``: public events,
    plus a viewer's own actor events. Anything else stays hidden.

    Every player-facing viewer must first pass campaign membership — an
    arbitrary user UUID plus a known campaign ID must not read that
    campaign's public timeline.
    """
    if not viewers:
        return False, "viewer_required"
    for viewer in viewers:
        if not is_campaign_participant(db, campaign, viewer):
            return False, "not_campaign_member"
    if str(getattr(event, "visibility", "public")) == "public":
        return True, None
    actor = getattr(event, "actor_id", None)
    if actor is not None and any(str(actor) == str(viewer) for viewer in viewers):
        return True, None
    return False, "event_not_visible"


def _thread_readable_for_viewer(
    db: Session, campaign: Campaign, raw_thread_id: Any, viewer: uuid.UUID,
) -> bool:
    """Thread-ACL check for source-turn/submission evidence (read-only).

    Uses the centralized ``can_read_thread`` primitive: shared campaign
    threads require campaign membership; private threads require explicit
    thread membership (owner status alone never grants private access).
    Private content must live on a resolvable thread row — an unresolvable
    thread reference denies fail-closed. (This helper is only invoked for
    private-audience records; shared-audience legacy ``"main"`` turns
    without a durable thread row never reach it.)
    """
    raw = "" if raw_thread_id is None else str(raw_thread_id)
    if not raw or raw == "main":
        thread = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == campaign.id,
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        if thread is None:
            return False
        try:
            return bool(can_read_thread(db, campaign.id, thread.id, viewer))
        except Exception:
            return False
    try:
        tid = parse_thread_id(raw)
    except Exception:
        return False
    try:
        return bool(can_read_thread(db, campaign.id, tid, viewer))
    except Exception:
        return False


def turn_gate(
    db: Session, campaign: Campaign, turn: DmTurn,
    viewers: list[uuid.UUID], *, dm_internal: bool,
) -> tuple[bool, str | None]:
    if dm_internal:
        return True, None
    if not viewers:
        return False, "viewer_required"
    for viewer in viewers:
        if not is_campaign_participant(db, campaign, viewer):
            return False, "not_campaign_member"
    audience = str(getattr(turn, "audience", "campaign") or "campaign")
    if audience == "private":
        for viewer in viewers:
            if not _thread_readable_for_viewer(
                db, campaign, getattr(turn, "thread_id", None), viewer
            ):
                return False, "turn_not_visible"
        return True, None
    return True, None


def submission_gate(
    db: Session, campaign: Campaign, submission: PlayerSubmission,
    viewers: list[uuid.UUID], *, dm_internal: bool,
) -> tuple[bool, str | None]:
    if dm_internal:
        return True, None
    if not viewers:
        return False, "viewer_required"
    for viewer in viewers:
        if not is_campaign_participant(db, campaign, viewer):
            return False, "not_campaign_member"
    audience = str(getattr(submission, "audience", "campaign") or "campaign")
    if audience == "private":
        for viewer in viewers:
            if not _thread_readable_for_viewer(
                db, campaign, getattr(submission, "thread_id", None), viewer
            ):
                return False, "submission_not_visible"
    return True, None


def scene_gate(
    db: Session, campaign: Campaign, scene: Any,
    viewers: list[uuid.UUID], *, dm_internal: bool,
) -> tuple[bool, str | None]:
    if dm_internal:
        return True, None
    if not viewers:
        return False, "viewer_required"
    authority = any(is_world_authority(campaign, viewer) for viewer in viewers)
    member = all(is_campaign_participant(db, campaign, viewer) for viewer in viewers)
    if not member or not visible_to_viewer(scene.visibility, authority):
        return False, "scene_not_visible"
    return True, None


# ── #203 evidence-tool result envelope ───────────────────────────────────────

def audience_viewers(audience: Any) -> list[str]:
    try:
        return list(getattr(audience, "user_ids", None) or [])
    except Exception:
        return []


def audience_authorization(audience: Any) -> dict[str, Any]:
    try:
        thread_id = str(getattr(audience, "thread_id", ""))
    except Exception:
        thread_id = ""
    try:
        campaign_id = str(getattr(audience, "campaign_id", ""))
    except Exception:
        campaign_id = ""
    return {"campaign_id": campaign_id, "thread_ids": [thread_id] if thread_id else []}


def tool_result(
    audience: Any, *, status: str, packets: list[EvidencePacket],
    payload: dict[str, Any], dm_internal: bool,
) -> dict[str, Any]:
    """Evidence-tool envelope: sources, bundle visibility, and authorization."""
    visibility = evidence_bundle_visibility((p.visibility for p in packets), dm_internal=dm_internal)
    authorization = audience_authorization(audience)
    if visibility == "private":
        authorization["user_ids"] = audience_viewers(audience)
    return {
        "status": status,
        "sources": [p.to_source_ref() for p in packets],
        "visibility": visibility,
        "authorization": authorization,
        "payload": payload,
        "result_count": len(packets),
    }
