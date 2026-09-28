"""Explicit audited repairs, DM adjudication, directives, retcon propagation — issue #221.

Consumes #220 consistency incidents; never silently rewrites canon. Every
meaningful repair records before/after/evidence/reason/source. Deterministic
stale derived state (summaries/embeddings) repairs without DM adjudication;
ambiguous conflicts route through a bounded DM repair decision. Retcons
preserve superseded prior truth and propagate to dependent derived state.
Secret repairs never broaden visibility or auto-grant character knowledge.
Duplicate execution is idempotent via (campaign, repair_key) + operation id.

Authority split (the AI is the only DM):
- Deterministic code owns authorization, visibility scope, identity exact
  matches, dice/state arithmetic, and idempotency. Decision models only
  choose among code-supplied repair outcomes for ambiguous conflicts.
- If no defensible repair exists the conflict stays explicit (deferred).

Secret safety: evidence/directives are DM/operator-scoped; player-facing
correction projections reveal only audience-allowed text.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.decisions import (
    ACTIVE,
    ESCALATE,
    CandidateRecord,
    ChoiceResult,
    DecisionClassPolicy,
    DecisionFrame,
    DecisionService,
    build_frame,
    build_record,
    evaluate_execution,
    is_escape_id,
    record_fail_soft,
    register_policy,
    to_decision_request,
)
from app.observability.tracing import structured_log
from models.repair import REPAIR_TYPES, CampaignRepair, RepairDirective
from models.world import CLOCK_EVALUABLE_STATUSES, CLOCK_STATUSES, CLOCK_TERMINAL_STATUSES

logger = logging.getLogger(__name__)

# ── Repair-adjudication decision role ─────────────────────────────────────────

REPAIR_DECISION_CLASS = "campaign_repair"
REPAIR_QUESTION_ID = "select_repair_outcome"
REPAIR_FRAME_INSTRUCTIONS = (
    "Select only the supplied repair outcome matching the authorized evidence. "
    "APPLY_REPAIR applies the proposed deterministic fix. MERGE merges a duplicate "
    "entity into its canonical target. KEEP_DISTINCT keeps similar names as separate "
    "entities. RETCON explicitly replaces prior truth while preserving history. "
    "DEFER leaves the conflict explicit when evidence is insufficient. "
    "Never invent records outside the supplied evidence."
)
REPAIR_FRAME_SCHEMA_VERSION = 1

APPLY_REPAIR = "APPLY_REPAIR"
MERGE = "MERGE"
KEEP_DISTINCT = "KEEP_DISTINCT"
RETCON = "RETCON"
DEFER = "DEFER"

register_policy(DecisionClassPolicy(
    decision_class=REPAIR_DECISION_CLASS,
    min_probability_direct=.8, min_confidence_direct=.75,
    min_margin_direct=.2, near_tie_margin=.2,
    max_risk_for_direct="standard", allow_direct_when_irreversible=False,
))

REPAIR_POLICY = {
    "decision_class": REPAIR_DECISION_CLASS,
    "question_id": REPAIR_QUESTION_ID,
    "frame_schema": REPAIR_FRAME_SCHEMA_VERSION,
}

# Statuses
PENDING = "pending"
ADJUDICATION_REQUIRED = "adjudication_required"
APPLYING = "applying"
APPLIED = "applied"
FAILED = "failed"
DEFERRED = "deferred"
RETCONNED = "retconned"

TERMINAL_STATUSES = frozenset({APPLIED, RETCONNED})

# Visibility ranks: repair must never widen (narrowing or equal only).
_VIS_RANK = {"dm_only": 0, "private": 1, "campaign": 2, "public": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _repair_key(incident_id: Any | None, repair_type: str, fingerprint: str) -> str:
    raw = f"{incident_id or 'direct'}:{repair_type}:{fingerprint}"
    return f"rep221-{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def _utcnow_iso() -> str:
    return _now().isoformat()


def _as_uuid(value: Any, *, field: str = "id") -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid {field} {value!r}") from exc


# ── Creation (idempotent) ─────────────────────────────────────────────────────

def create_repair(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    repair_type: str,
    proposed_changes: list[dict[str, Any]],
    incident_id: uuid.UUID | None = None,
    conflicting_records: list[dict[str, Any]] | None = None,
    evidence: dict[str, Any] | None = None,
    before_state: dict[str, Any] | None = None,
    reason: str = "",
    source: str = "system",
    actor_id: str | None = None,
    requires_player_visible_correction: bool = False,
    player_visible_correction: dict[str, Any] | None = None,
    is_retcon: bool = False,
    retcon_of_id: uuid.UUID | None = None,
    fingerprint: str = "",
    operation_id: str | None = None,
    commit: bool = True,
) -> tuple[CampaignRepair, bool]:
    """Create (or reuse) a repair record. Returns (row, created)."""
    campaign_id = campaign_id if isinstance(campaign_id, uuid.UUID) else uuid.UUID(str(campaign_id))
    if repair_type not in REPAIR_TYPES:
        raise ValueError(f"unknown repair_type {repair_type!r}")
    key = _repair_key(incident_id, repair_type, fingerprint or _fingerprint(proposed_changes))
    existing = db.execute(select(CampaignRepair).where(
        CampaignRepair.campaign_id == campaign_id,
        CampaignRepair.repair_key == key,
    )).scalars().first()
    if existing is not None:
        return existing, False
    domains = sorted({str(c.get("domain") or "unknown") for c in (proposed_changes or [])})
    row = CampaignRepair(
        id=uuid.uuid4(), campaign_id=campaign_id,
        incident_id=incident_id, repair_key=key, repair_type=repair_type,
        status=PENDING, detection_path="dm_adjudicated" if source == "dm_adjudicated" else "automatic",
        affected_domains=domains,
        conflicting_records=list(conflicting_records or []),
        evidence=dict(evidence or {}),
        before_state=dict(before_state or {}),
        proposed_changes=list(proposed_changes or []),
        applied_changes=[],
        reason=reason[:4000],
        source=source if source in ("system", "dm_adjudicated", "operator") else "system",
        actor_id=actor_id[:128] if actor_id else None,
        requires_player_visible_correction=1 if requires_player_visible_correction else 0,
        player_visible_correction=_player_safe_correction(player_visible_correction),
        visibility="dm_only",
        is_retcon=1 if is_retcon else 0,
        retcon_of_id=retcon_of_id,
        operation_id=operation_id[:128] if operation_id else None,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        dup = db.execute(select(CampaignRepair).where(
            CampaignRepair.campaign_id == campaign_id,
            CampaignRepair.repair_key == key,
        )).scalars().first()
        if dup is None:
            raise
        return dup, False
    if commit:
        db.commit()
    else:
        db.flush()
    structured_log(logger, logging.INFO, "campaign_repair_created",
                   campaign_id=str(campaign_id), repair_id=str(row.id),
                   repair_type=repair_type, domains=domains)
    return row, True


def _fingerprint(changes: list[dict[str, Any]] | None) -> str:
    import json as _json
    try:
        return hashlib.sha256(_json.dumps(changes or [], sort_keys=True, default=str).encode()).hexdigest()[:16]
    except Exception:
        return "manual"


def get_repair(db: Session, campaign_id: uuid.UUID, repair_id: uuid.UUID) -> CampaignRepair | None:
    try:
        rid = _as_uuid(repair_id, field="repair_id")
    except ValueError:
        return None
    row = db.get(CampaignRepair, rid)
    if row is None or row.campaign_id != campaign_id:
        return None
    return row


# ── Silent vs player-visible correction ───────────────────────────────────────

def needs_player_visible_correction(
    *,
    evidence: dict[str, Any] | None = None,
    conflicting_records: list[dict[str, Any]] | None = None,
    proposed_changes: list[dict[str, Any]] | None = None,
) -> bool:
    """Decide silent vs player-visible correction.

    Player-visible history (member-visible committed events, current
    campaign-visible summaries, visible scene/character state) that already
    exposed the wrong value would confuse players if silently changed — those
    repairs require an explicit correction. Pure DM-only residue repairs
    silently.
    """
    ev = dict(evidence or {})
    if ev.get("player_visible_exposure"):
        return True
    for rec in (conflicting_records or []):
        vis = str((rec or {}).get("visibility") or "")
        if vis in ("campaign", "public"):
            return True
    for change in (proposed_changes or []):
        if isinstance(change, dict) and change.get("player_visible"):
            return True
    return False


def _player_safe_correction(correction: dict[str, Any] | None) -> dict[str, Any] | None:
    if not correction:
        return None
    text = str(correction.get("correction_text") or "").strip()
    if not text:
        return None
    scope = correction.get("scope") or "campaign"
    if scope not in ("campaign", "public"):
        scope = "campaign"
    return {
        "correction_text": text[:2000],
        "scope": scope,
    }


def _correction_scope(
    *,
    evidence: dict[str, Any] | None = None,
    conflicting_records: list[dict[str, Any]] | None = None,
    proposed_changes: list[dict[str, Any]] | None = None,
) -> str | None:
    """Member-visible scope for a player correction, or None when secret.

    Returns ``"campaign"`` only when member-visible history already exposed
    the wrong value (campaign/public conflicting records, explicit
    player-visible exposure, or an explicitly player-visible change).
    All-restricted (dm_only/private) incidents stay silent: no scope, no
    player correction, no directive.
    """
    ev = dict(evidence or {})
    if ev.get("player_visible_exposure"):
        return "campaign"
    for rec in (conflicting_records or []):
        if str((rec or {}).get("visibility") or "") in ("campaign", "public"):
            return "campaign"
    for change in (proposed_changes or []):
        if isinstance(change, dict) and change.get("player_visible"):
            return "campaign"
    return None


def _explicit_correction_text(repair: CampaignRepair) -> str | None:
    """Only explicitly supplied correction text — never reason/evidence prose.

    DM reasoning and raw evidence may contain secrets; deriving a
    player-facing correction from them would leak. Callers must supply
    ``player_visible_correction.correction_text`` or
    ``evidence.correction_text`` explicitly.
    """
    corr = dict(repair.player_visible_correction or {})
    text = str(corr.get("correction_text") or "").strip()
    if text:
        return text[:2000]
    ev = dict(repair.evidence or {})
    text = str(ev.get("correction_text") or "").strip()
    return text[:2000] if text else None


# ── Secret safety ─────────────────────────────────────────────────────────────

def _assert_no_visibility_widening(before_vis: Any, after_vis: Any) -> None:
    if before_vis is None or after_vis is None:
        return
    b = _VIS_RANK.get(str(before_vis), -1)
    a = _VIS_RANK.get(str(after_vis), -1)
    if b < 0 or a < 0:
        raise ValueError(f"unknown visibility transition {before_vis!r} -> {after_vis!r}")
    if a > b:
        raise ValueError(
            f"repair must not broaden visibility ({before_vis!r} -> {after_vis!r}); "
            "secret repair stays secret-safe"
        )


# ── Typed deterministic handlers ──────────────────────────────────────────────

def _handle_entity(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    from app.world.service import get_entity_strict, normalize_visibility

    entity = get_entity_strict(db, campaign_id, _as_uuid(change["target_id"], field="target_id"))
    before = {"status": entity.status, "summary": entity.summary,
              "visibility": entity.visibility, "details": dict(entity.details or {})}
    patch = dict(change.get("patch") or {})
    if "visibility" in patch:
        new_vis = normalize_visibility(patch["visibility"])
        _assert_no_visibility_widening(entity.visibility, new_vis)
        entity.visibility = new_vis
    if "status" in patch:
        from app.world.service import validate_entity_status
        entity.status = validate_entity_status(patch["status"])
    if "summary" in patch:
        entity.summary = str(patch["summary"])[:4000] if patch["summary"] is not None else None
    if "details" in patch and isinstance(patch["details"], dict):
        details = dict(entity.details or {})
        details.update({k: v for k, v in patch["details"].items()})
        details["last_repair_operation"] = operation_id
        entity.details = details
    entity.revision = int(entity.revision or 1) + 1
    if operation_id:
        entity.operation_id = operation_id[:128]
    db.add(entity)
    db.flush()
    from app.world import semantic as _semantic
    _semantic.mark_stale(db, campaign_id, "world_entity", entity.id, reason="repair", commit=False)
    return {"before": before, "after": {"status": entity.status, "summary": entity.summary,
                                        "visibility": entity.visibility, "revision": entity.revision}}


def _handle_entity_merge(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    from app.world.service import get_entity_strict
    from app.world.identity import supersede_entity

    duplicate = get_entity_strict(db, campaign_id, _as_uuid(change["duplicate_id"], field="duplicate_id"))
    canonical = get_entity_strict(db, campaign_id, _as_uuid(change["canonical_id"], field="canonical_id"))
    if duplicate.superseded_by_id is not None:
        if duplicate.superseded_by_id == canonical.id:
            return {"before": {"duplicate_id": str(duplicate.id), "already_merged": True},
                    "after": {"canonical_id": str(canonical.id), "already_merged": True}}
        raise ValueError("duplicate entity already merged into a different canonical target")
    # Secret safety: merging must not leak hidden aliases into a wider audience.
    _assert_no_visibility_widening("dm_only" if duplicate.visibility == "dm_only" else "campaign",
                                   "dm_only" if canonical.visibility == "dm_only" else "campaign")
    before = {"duplicate_id": str(duplicate.id), "duplicate_status": duplicate.status,
              "canonical_id": str(canonical.id)}
    supersede_entity(db, duplicate, canonical, provenance={
        "repair_operation": operation_id, "reason": str(change.get("reason") or "duplicate_merge"),
    })
    db.flush()
    return {"before": before, "after": {"duplicate_id": str(duplicate.id),
                                       "superseded_by_id": str(duplicate.superseded_by_id),
                                       "canonical_id": str(canonical.id)}}


def _handle_fact(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None,
                 retcon: bool = False) -> dict[str, Any]:
    from models.campaigns import Campaign
    from app.world.knowledge import get_fact_strict, supersede_fact_inline

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    row = get_fact_strict(db, campaign_id, _as_uuid(change["target_id"], field="target_id"))
    before = {"content": row.content, "epistemic_state": row.epistemic_state,
              "status": row.status, "visibility": row.visibility, "version": row.version}
    patch = dict(change.get("patch") or {})
    if "visibility" in patch:
        _assert_no_visibility_widening(row.visibility, patch["visibility"])
    new_row, _created = supersede_fact_inline(
        db, campaign, row.id,
        content=patch.get("content"),
        epistemic_state="retconned" if retcon else patch.get("epistemic_state"),
        visibility=patch.get("visibility"),
        provenance={"repair_reason": str(change.get("reason") or ("retcon" if retcon else "fact_correction")),
                    "repair_operation": operation_id},
        operation_id=operation_id,
        idempotency_key=f"repair-{operation_id}-{row.id}" if operation_id else None,
    )
    db.flush()
    # Dependent derived state: summaries + embeddings for this fact go stale.
    # Strict: a stale-marking failure fails the repair (retryable) rather
    # than reporting applied while dependents still serve the old truth.
    from app.world import summaries as _summaries
    from app.world import semantic as _semantic
    _summaries.note_source_repair(db, campaign_id, reason="fact_repaired")
    _semantic.mark_stale(db, campaign_id, "world_fact", row.id, reason="repair", commit=False)
    _semantic.mark_stale(db, campaign_id, "world_fact", new_row.id, reason="repair", commit=False)
    return {"before": before, "after": {"fact_id": str(new_row.id), "content": new_row.content,
                                       "epistemic_state": new_row.epistemic_state,
                                       "status": new_row.status, "version": new_row.version,
                                       "supersedes_id": str(new_row.supersedes_id) if new_row.supersedes_id else None}}


def _handle_relation(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None,
                     retcon: bool = False) -> dict[str, Any]:
    from models.campaigns import Campaign
    from app.world.knowledge import get_relation_strict, supersede_relation_inline

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    row = get_relation_strict(db, campaign_id, _as_uuid(change["target_id"], field="target_id"))
    before = {"relation_type": row.relation_type, "epistemic_state": row.epistemic_state,
              "status": row.status, "visibility": row.visibility, "version": row.version}
    patch = dict(change.get("patch") or {})
    if "visibility" in patch:
        _assert_no_visibility_widening(row.visibility, patch["visibility"])
    kwargs: dict[str, Any] = {"operation_id": operation_id,
                              "provenance": {"repair_reason": str(change.get("reason") or ("retcon" if retcon else "relation_correction")),
                                             "repair_operation": operation_id}}
    if operation_id:
        kwargs["idempotency_key"] = f"repair-{operation_id}-{row.id}"
    for field in ("relation_type", "epistemic_state", "visibility", "object_label"):
        if field in patch:
            kwargs[field] = patch[field]
    if retcon and "epistemic_state" not in patch:
        kwargs["epistemic_state"] = "retconned"
    new_row, _created = supersede_relation_inline(db, campaign, row.id, **kwargs)
    db.flush()
    from app.world import summaries as _summaries
    from app.world import semantic as _semantic
    _summaries.note_source_repair(db, campaign_id, reason="relation_repaired")
    _semantic.mark_stale(db, campaign_id, "world_relation", row.id, reason="repair", commit=False)
    return {"before": before, "after": {"relation_id": str(new_row.id), "version": new_row.version,
                                       "status": new_row.status}}


def _handle_event_metadata(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    from models.campaigns import CampaignDomainEvent

    eid = _as_uuid(change["target_id"], field="target_id")
    event = db.get(CampaignDomainEvent, eid)
    if event is None or event.campaign_id != campaign_id:
        raise ValueError("repair target event not found in this campaign")
    before = {"event_type": event.event_type, "provenance": dict(event.provenance or {})}
    patch = dict(change.get("patch") or {})
    # Only metadata/provenance may be repaired — never rewrite the visible payload silently.
    if "payload" in patch:
        raise ValueError("repair must not rewrite committed event payload; use explicit retcon")
    if "provenance" in patch and isinstance(patch["provenance"], dict):
        prov = dict(event.provenance or {})
        prov.update(patch["provenance"])
        if operation_id:
            prov["last_repair_operation"] = operation_id
        event.provenance = prov
    db.add(event)
    db.flush()
    return {"before": before, "after": {"event_type": event.event_type, "provenance": dict(event.provenance or {})}}


def _handle_summary(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    """Deterministic stale-derived repair: mark stale so rebuild converges."""
    from app.world import summaries as _summaries

    before = {"action": "mark_stale"}
    count = _summaries.mark_summaries_stale(
        db, campaign_id,
        from_sequence=change.get("from_sequence"), to_sequence=change.get("to_sequence"),
        reason=str(change.get("reason") or "repair_invalidated"),
        commit=False,
    )
    return {"before": before, "after": {"marked_stale": count}}


def _handle_scene(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    from models.campaigns import Campaign
    from app.world.service import UNSET, apply_scene_update_inline, get_current_scene

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    scene = get_current_scene(db, campaign_id)
    before = scene.to_dict() if scene is not None else {"scene": None}
    patch = dict(change.get("patch") or {})
    if "visibility" in patch:
        _assert_no_visibility_widening(scene.visibility if scene else "campaign", patch["visibility"])
    kwargs: dict[str, Any] = {"new_revision": int(campaign.revision or 0) + 1, "operation_id": operation_id}
    sentinel_map = {"location_entity_id": UNSET, "location_name": None, "fictional_time": None,
                    "fictional_time_details": None, "present_actors": None, "environment": None,
                    "visibility": None, "source_turn_id": None, "source_attempt_id": None}
    for field, default in sentinel_map.items():
        if field in patch:
            value = patch[field]
            if field == "location_entity_id" and value is None:
                kwargs[field] = None  # explicit clear
            else:
                kwargs[field] = value
        elif field == "location_entity_id":
            kwargs[field] = UNSET
    updated = apply_scene_update_inline(db, campaign, **kwargs)
    db.flush()
    return {"before": before, "after": updated.to_dict()}


def _handle_character_state(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    """Repair character sheet hooks with authoritative campaign association.

    The sheet carries no campaign FK, so association is proven through
    ownership: the sheet must belong to its character
    (``sheet.character_id == character.id`` with matching ``owner_id``),
    and that owner must be the campaign owner or an explicit campaign
    member. Cross-campaign or owner-mismatched sheets are rejected.
    """
    from models.campaigns import Campaign, CampaignMember
    from models.characters import Character, Dnd5eCharacterSheet
    from sqlalchemy import select as _select

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    patch = dict(change.get("patch") or {})
    character_id = change.get("character_id") or change.get("target_id")
    cid = _as_uuid(character_id, field="character_id")
    character = db.get(Character, cid)
    if character is None or character.is_deleted:
        raise ValueError("character not found for repair target")
    sheet = db.scalar(_select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == cid))
    if sheet is None:
        raise ValueError("character sheet not found for repair target")
    if sheet.character_id != character.id:
        raise ValueError("character sheet does not belong to the repair target character")
    if sheet.owner_id != character.owner_id:
        raise ValueError("character sheet owner does not match the character owner")
    if character.owner_id != campaign.owner_id:
        member = db.scalar(_select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.user_id == character.owner_id,
        ))
        if member is None:
            raise ValueError("character owner is not a member of this campaign")
    before: dict[str, Any] = {}
    allowed = {"hit_points_current", "hit_points_max", "hit_points_temp", "conditions",
               "death_save_successes", "death_save_failures", "exhaustion_level"}
    for field, value in patch.items():
        if field not in allowed:
            raise ValueError(f"character_state repair field {field!r} is not a repairable hook")
        before[field] = getattr(sheet, field)
        setattr(sheet, field, value)
    db.add(sheet)
    db.flush()
    return {"before": before, "after": {k: getattr(sheet, k) for k in before}}


def _handle_knowledge(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    """Repair a knower's epistemic stance — never auto-grants human visibility."""
    from models.campaigns import Campaign
    from app.world.epistemics import assert_knowledge_inline

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    if change.get("grant_visibility"):
        raise ValueError("repair must not auto-grant visibility after a leak; use an explicit grant workflow")
    before = {"subject": change.get("subject_entity_id"), "target": change.get("target_id"),
              "knowledge_state": change.get("previous_state")}
    row, _created = assert_knowledge_inline(
        db, campaign,
        subject_kind=str(change.get("subject_kind") or "character"),
        subject_entity_id=change["subject_entity_id"],
        target_kind=str(change.get("target_kind") or "fact"),
        target_id=change["target_id"],
        knowledge_state=str(change.get("knowledge_state") or "believes"),
        acquisition_source="repair",
        visibility=str(change.get("visibility") or "dm_only"),
        operation_id=operation_id,
        idempotency_key=f"repair-{operation_id}-{change.get('subject_entity_id')}-{change.get('target_id')}" if operation_id else None,
    )
    db.flush()
    return {"before": before, "after": {"knowledge_id": str(row.id), "knowledge_state": row.knowledge_state}}


def _handle_visibility_grant(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    """Only revocation (narrowing) is a safe repair default; grants need an explicit actor."""
    from models.campaigns import Campaign
    from app.world.epistemics import revoke_visibility_inline

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    action = str(change.get("action") or "revoke")
    if action == "grant":
        raise ValueError("repair must not automatically grant character knowledge after a leak")
    if action != "revoke":
        raise ValueError(f"unknown visibility_grant repair action {action!r}")
    ok = revoke_visibility_inline(
        db, campaign,
        target_kind=str(change["target_kind"]), target_id=change["target_id"],
        grantee_user_id=change["grantee_user_id"],
        operation_id=operation_id,
    )
    db.flush()
    return {"before": {"action": "revoke"}, "after": {"revoked": bool(ok)}}


def _handle_npc(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    from models.campaigns import Campaign
    from app.world.npcs import UNSET as _NPC_UNSET, apply_npc_state_inline, get_npc_state

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    existing = get_npc_state(db, campaign_id, change["target_id"])
    before = existing.to_dict() if existing is not None else {"npc": None}
    patch = dict(change.get("patch") or {})
    kwargs: dict[str, Any] = {"new_revision": int(campaign.revision or 0) + 1, "operation_id": operation_id}
    for field in ("role", "goals", "disposition", "resources", "current_activity",
                  "location_entity_id", "location_name", "importance", "depth", "field_visibility"):
        kwargs[field] = patch.get(field, _NPC_UNSET)
    updated = apply_npc_state_inline(db, campaign, change["target_id"], **kwargs)
    db.flush()
    return {"before": before, "after": updated.to_dict()}


def _handle_clock(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    """Repair clock progress/status against real clock domain constraints.

    - ``progress`` is a non-negative tick count bounded by ``threshold``
      while the clock is evaluable (active/ticking); over-threshold while
      evaluable is exactly the #220 deterministic contradiction, so repair
      must not recreate it.
    - Terminal clocks (completed/retired) are terminal: repair may move a
      clock into them but never back out into evaluable/pending work.
    - ``threshold`` and ``evaluated_through_sequence`` are owned by clock
      creation/evaluation, not repair, and are never rewritten here.
    """
    from models.world import CampaignClock

    cid = _as_uuid(change["target_id"], field="target_id")
    clock = db.get(CampaignClock, cid)
    if clock is None or clock.campaign_id != campaign_id:
        raise ValueError("repair target clock not found in this campaign")
    before = {"progress": clock.progress, "status": clock.status, "revision": clock.revision}
    patch = dict(change.get("patch") or {})
    if "threshold" in patch or "evaluated_through_sequence" in patch:
        raise ValueError("clock threshold/evaluated_through_sequence are not repairable fields")
    new_progress = int(clock.progress or 0)
    if "progress" in patch:
        try:
            new_progress = int(patch["progress"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"clock progress must be an integer, got {patch['progress']!r}") from exc
    new_status = str(patch["status"]) if "status" in patch else clock.status
    if new_status not in CLOCK_STATUSES:
        raise ValueError(f"unknown clock status {patch.get('status')!r}")
    if int(clock.threshold or 0) < 1:
        raise ValueError("clock threshold is corrupt; refusing repair")
    if new_progress < 0:
        raise ValueError("clock progress must be non-negative")
    if new_status in CLOCK_EVALUABLE_STATUSES and new_progress > int(clock.threshold):
        raise ValueError(
            f"clock progress {new_progress} exceeds threshold {clock.threshold} "
            f"while status {new_status!r} is evaluable"
        )
    if clock.status in CLOCK_TERMINAL_STATUSES and new_status not in CLOCK_TERMINAL_STATUSES:
        raise ValueError(
            f"terminal clock {clock.status!r} cannot be reopened to {new_status!r} via repair"
        )
    clock.progress = new_progress
    clock.status = new_status
    clock.revision = int(clock.revision or 1) + 1
    if operation_id:
        clock.operation_id = operation_id[:128]
    db.add(clock)
    db.flush()
    return {"before": before, "after": {"progress": clock.progress, "status": clock.status, "revision": clock.revision}}


def _handle_embedding(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    from app.world import semantic as _semantic

    action = str(change.get("action") or "mark_stale")
    if action == "mark_stale":
        count = _semantic.mark_stale(db, campaign_id, change["source_type"], change["source_id"],
                                     reason=str(change.get("reason") or "repair"), commit=False)
        return {"before": {"action": action}, "after": {"marked_stale": count}}
    if action == "refresh":
        row = _semantic.index_source_record(db, campaign_id, change["source_type"], change["source_id"], commit=False)
        return {"before": {"action": action}, "after": {"indexed": row.to_dict() if row else None}}
    raise ValueError(f"unknown embedding repair action {action!r}")


def _handle_projection(db: Session, campaign_id: uuid.UUID, change: dict[str, Any], *, operation_id: str | None) -> dict[str, Any]:
    """Projection refresh is derived/rebuildable: stale-mark affected summaries + embeddings."""
    from app.world import summaries as _summaries
    from app.world import semantic as _semantic

    count = _summaries.note_source_repair(db, campaign_id, reason=str(change.get("reason") or "projection_repaired")) or 0
    refreshed = 0
    for ref in (change.get("embedding_refs") or []):
        refreshed += int(_semantic.mark_stale(db, campaign_id, ref.get("source_type"), ref.get("source_id"),
                                              reason="repair", commit=False) or 0)
    return {"before": {"action": "projection_refresh"}, "after": {"summaries_noted": count, "embeddings_staled": refreshed}}


_HANDLERS = {
    "entity": _handle_entity,
    "entity_merge": _handle_entity_merge,
    "relation": _handle_relation,
    "fact": _handle_fact,
    "event_metadata": _handle_event_metadata,
    "summary": _handle_summary,
    "scene": _handle_scene,
    "character_state": _handle_character_state,
    "knowledge": _handle_knowledge,
    "visibility_grant": _handle_visibility_grant,
    "npc_state": _handle_npc,
    "clock": _handle_clock,
    "embedding": _handle_embedding,
    "projection": _handle_projection,
}

REGISTERED_DOMAINS = frozenset(_HANDLERS)


# ── Bounded DM repair adjudication ────────────────────────────────────────────

# Repair types that must never apply without an approved bounded DM decision.
_AMBIGUOUS_TYPES = frozenset({"ambiguous"})
# Decision outcomes that count as approval to apply.
_APPROVED_OUTCOMES = frozenset({APPLY_REPAIR, MERGE, KEEP_DISTINCT, RETCON})


def _adjudication_approval(repair: CampaignRepair) -> str | None:
    """Approved outcome recorded on the repair, or None.

    Approval requires the repair to have gone through the bounded DM path
    (``detection_path == "dm_adjudicated"``) with a recorded non-DEFER
    selection. Guessing from a pending/system repair is never approval.
    """
    if repair.detection_path != "dm_adjudicated":
        return None
    dist = repair.decision_distribution or {}
    for outcome in (APPLY_REPAIR, MERGE, KEEP_DISTINCT, RETCON):
        if dist.get(outcome):
            return outcome
    return None

@dataclass
class RepairCandidate:
    candidate_id: str
    label: str
    changes: list[dict[str, Any]] | None = None


def build_repair_frame(
    *,
    repair_type: str,
    evidence: dict[str, Any],
    candidates: list[RepairCandidate],
    target_ref: str = "",
) -> DecisionFrame:
    records = tuple(
        CandidateRecord(id=c.candidate_id, label=c.label, source="repair:outcome")
        for c in candidates
    )
    safe_evidence = {k: v for k, v in dict(evidence or {}).items() if k != "secrets"}
    return build_frame(
        decision_class=REPAIR_DECISION_CLASS,
        question_id=REPAIR_QUESTION_ID,
        instructions=REPAIR_FRAME_INSTRUCTIONS,
        state={"frame_schema": REPAIR_FRAME_SCHEMA_VERSION, "repair_type": repair_type,
               "target_ref": target_ref, "evidence": safe_evidence,
               "candidate_count": len(records)},
        state_revision=f"repair:{repair_type}:{target_ref}",
        candidates=records,
        include_escapes=True,
    )


@dataclass
class RepairVerdict:
    selected_id: str
    failure: str | None = None
    record: Any = None
    provider: str | None = None
    model: str | None = None


def decide_repair(frame: DecisionFrame, service: DecisionService, *, session_factory: Any = None) -> RepairVerdict:
    try:
        response = service.decide(to_decision_request(frame))
    except Exception as exc:
        logger.warning("repair judgment failed error=%s", exc)
        return RepairVerdict(DEFER, failure=f"{type(exc).__name__}: {exc}"[:500])
    result = response.results.get(frame.question_id)
    if not isinstance(result, ChoiceResult):
        return RepairVerdict(DEFER, failure="malformed: missing choice result",
                             provider=getattr(response, "provider", None),
                             model=getattr(response, "model", None))
    if is_escape_id(result.selected_id) or result.selected_id == DEFER:
        record = build_record(frame, result, evaluate_execution(
            frame, result.selected_id, dict(result.probabilities), result.confidence, verified=True),
            provider=response.provider, model=response.model or "unknown", mode=ACTIVE,
            trace_id=response.trace_id, operation_id=response.operation_id,
            campaign_id=None, latency_ms=response.latency_ms, verified=True)
        record_fail_soft(session_factory, record)
        return RepairVerdict(DEFER, record=record, provider=response.provider, model=response.model)
    try:
        if result.selected_id not in {c.id for c in frame.candidates}:
            raise ValueError(f"unknown candidate {result.selected_id!r}")
        verdict = evaluate_execution(frame, result.selected_id, dict(result.probabilities),
                                     result.confidence, verified=True)
    except Exception as exc:
        return RepairVerdict(DEFER, failure=f"{type(exc).__name__}: {exc}"[:500],
                             provider=response.provider, model=response.model)
    record = build_record(frame, result, verdict, provider=response.provider,
                          model=response.model or "unknown", mode=ACTIVE,
                          trace_id=response.trace_id, operation_id=response.operation_id,
                          campaign_id=None, latency_ms=response.latency_ms, verified=True)
    record_fail_soft(session_factory, record)
    if verdict.directive == ESCALATE:
        return RepairVerdict(DEFER, record=record, provider=response.provider, model=response.model)
    return RepairVerdict(result.selected_id, record=record, provider=response.provider, model=response.model)


def adjudicate_repair(
    db: Session,
    repair: CampaignRepair,
    *,
    decision_service: DecisionService,
    session_factory: Any = None,
    candidates: list[RepairCandidate] | None = None,
    commit: bool = True,
) -> RepairVerdict:
    """Route an ambiguous conflict through a bounded DM repair decision.

    A non-DEFER selection binds to the repair: the chosen candidate's
    changes (when the candidate carries any) replace the proposed changes
    and are revalidated against the registered handler domains before the
    repair becomes applicable. RETCON selections additionally flag the
    repair as a retcon; MERGE/KEEP_DISTINCT retype it accordingly. DEFER
    (or any failure/escape) leaves the conflict explicit.
    """
    options = candidates or [
        RepairCandidate(APPLY_REPAIR, "Apply the proposed deterministic fix"),
        RepairCandidate(RETCON, "Explicit retcon preserving prior history"),
        RepairCandidate(DEFER, "Defer; leave the conflict explicit"),
    ]
    frame = build_repair_frame(repair_type=repair.repair_type, evidence=repair.evidence or {},
                               candidates=options, target_ref=str(repair.id))
    verdict = decide_repair(frame, decision_service, session_factory=session_factory)
    repair.decision_policy = dict(REPAIR_POLICY)
    repair.decision_model = verdict.model[:128] if verdict.model else None
    repair.decision_distribution = {verdict.selected_id: 1}
    repair.detection_path = "dm_adjudicated"
    if verdict.selected_id == DEFER:
        repair.status = ADJUDICATION_REQUIRED if verdict.failure else DEFERRED
        if verdict.failure:
            repair.error = verdict.failure[:2000]
    else:
        selected = next((c for c in options if c.candidate_id == verdict.selected_id), None)
        if selected is None:
            repair.status = ADJUDICATION_REQUIRED
            repair.error = f"decision selected unknown candidate {verdict.selected_id!r}"[:2000]
        else:
            bind_error = _bind_adjudicated_outcome(repair, verdict.selected_id, selected)
            if bind_error is not None:
                repair.status = FAILED
                repair.error = bind_error[:2000]
                repair.retry_count = int(repair.retry_count or 0) + 1
            else:
                repair.status = PENDING
                repair.error = None
    db.add(repair)
    db.flush()
    if commit:
        db.commit()
    else:
        db.flush()
    return verdict


def _bind_adjudicated_outcome(
    repair: CampaignRepair, selected_id: str, selected: RepairCandidate,
) -> str | None:
    """Bind the DM-selected outcome to the repair. Returns an error or None."""
    if selected.changes is not None:
        repair.proposed_changes = list(selected.changes)
        repair.affected_domains = sorted(
            {str(c.get("domain") or "unknown") for c in (repair.proposed_changes or [])}
        )
    if selected_id == RETCON:
        repair.is_retcon = 1
        repair.repair_type = "retcon"
    elif selected_id == MERGE:
        repair.repair_type = "entity_merge"
    elif selected_id == KEEP_DISTINCT:
        repair.repair_type = "entity_keep_distinct"
    for change in (repair.proposed_changes or []):
        domain = str((change or {}).get("domain") or "")
        if domain not in _HANDLERS:
            return f"adjudicated outcome binds unknown repair domain {domain!r}"
    if selected_id == MERGE:
        merges = [c for c in (repair.proposed_changes or [])
                  if str((c or {}).get("domain")) == "entity_merge"]
        if not merges:
            return "MERGE outcome requires an entity_merge change"
        for change in merges:
            if not change.get("duplicate_id") or not change.get("canonical_id"):
                return "MERGE outcome requires duplicate_id and canonical_id"
            if str(change["duplicate_id"]) == str(change["canonical_id"]):
                return "MERGE outcome requires distinct duplicate and canonical entities"
    if selected_id != KEEP_DISTINCT and not (repair.proposed_changes or []):
        return f"{selected_id} outcome has no bound changes to apply"
    return None


# ── Apply (transactional, idempotent, retryable) ──────────────────────────────

def _fail_repair(
    db: Session, repair: CampaignRepair, message: str, *, commit: bool,
) -> dict[str, Any]:
    repair.status = FAILED
    repair.error = message[:2000]
    repair.retry_count = int(repair.retry_count or 0) + 1
    db.add(repair)
    db.flush()
    if commit:
        db.commit()
    structured_log(logger, logging.WARNING, "campaign_repair_failed",
                   campaign_id=str(repair.campaign_id), repair_id=str(repair.id),
                   error=repair.error)
    return {"status": FAILED, "error": repair.error, "repair_id": str(repair.id),
            "retry_count": repair.retry_count}


def apply_repair(
    db: Session,
    campaign_id: uuid.UUID,
    repair_id: uuid.UUID,
    *,
    operation_id: str | None = None,
    actor_id: str | None = None,
    correction_text: str | None = None,
    correction_scope: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Apply one repair's proposed changes through typed handlers.

    Authoritative gating: ``ambiguous`` repairs (and any repair sitting in a
    deferred/adjudication-required state) never apply without a recorded
    approved bounded DM outcome; the decision binds the exact changes.
    Player-visible corrections require explicit campaign-scoped correction
    text — reason/evidence prose is never leaked into player output.

    Transactional and idempotent: handlers, the player directive, the
    linked-incident resolution, and the repair row finalize inside one
    savepoint. Any failure rolls everything back and leaves the repair
    failed/retryable — a failed incident resolve or derived invalidation
    can never report ``applied``. Re-applying a terminal repair returns the
    stored result without touching state.
    """
    campaign_id = campaign_id if isinstance(campaign_id, uuid.UUID) else uuid.UUID(str(campaign_id))
    try:
        rid = _as_uuid(repair_id, field="repair_id")
    except ValueError as exc:
        raise ValueError(f"invalid repair id {repair_id!r}") from exc
    repair = get_repair(db, campaign_id, rid)
    if repair is None:
        raise ValueError("repair not found in this campaign")
    # Operator-supplied correction text (recoverable path when the stored
    # repair lacks explicit player-safe text). Sanitized, never derived.
    if correction_text is not None:
        repair.player_visible_correction = _player_safe_correction(
            {"correction_text": correction_text, "scope": correction_scope or "campaign"})
        repair.requires_player_visible_correction = 1
        db.add(repair)
        db.flush()
    for change in (repair.proposed_changes or []):
        domain = str((change or {}).get("domain") or "")
        if domain not in _HANDLERS:
            return _fail_repair(db, repair, f"unknown repair domain {domain!r}", commit=commit)
    if repair.status in (ADJUDICATION_REQUIRED, DEFERRED):
        repair.error = ("repair is deferred/awaiting adjudication; "
                        "re-adjudicate before apply")[:2000]
        db.add(repair)
        db.flush()
        if commit:
            db.commit()
        return {"status": repair.status, "error": repair.error, "repair_id": str(repair.id)}
    if repair.repair_type in _AMBIGUOUS_TYPES and _adjudication_approval(repair) is None:
        return _fail_repair(
            db, repair,
            "ambiguous repair requires an approved bounded DM adjudication before apply",
            commit=commit,
        )
    if repair.status in TERMINAL_STATUSES:
        return {"status": repair.status, "repair_id": str(repair.id),
                "applied_changes": list(repair.applied_changes or []), "duplicate": True}

    # Player-correction preflight (fail closed BEFORE touching state): a
    # required player-visible correction needs explicit campaign-scoped
    # text; reason/evidence prose is never an acceptable substitute.
    requires_correction = bool(repair.requires_player_visible_correction) or needs_player_visible_correction(
        evidence=repair.evidence, conflicting_records=repair.conflicting_records,
        proposed_changes=repair.proposed_changes,
    )
    if requires_correction:
        scope = _correction_scope(
            evidence=repair.evidence, conflicting_records=repair.conflicting_records,
            proposed_changes=repair.proposed_changes,
        )
        text = _explicit_correction_text(repair)
        if scope != "campaign" or not text:
            return _fail_repair(
                db, repair,
                "player-visible correction requires explicit campaign-scoped "
                "correction_text; refusing to derive player output from "
                "reason/evidence or to broadcast restricted scope",
                commit=commit,
            )
        repair.requires_player_visible_correction = 1
        repair.player_visible_correction = {"correction_text": text, "scope": "campaign"}

    repair.status = APPLYING
    if operation_id:
        repair.operation_id = operation_id[:128]
    if actor_id:
        repair.actor_id = actor_id[:128]
    db.add(repair)
    db.flush()

    # Capture before-state for every touched domain (audit completeness).
    before_state = dict(repair.before_state or {})
    applied: list[dict[str, Any]] = []
    savepoint = db.begin_nested()
    try:
        is_retcon = bool(repair.is_retcon) or repair.repair_type == "retcon"
        for change in (repair.proposed_changes or []):
            domain = str(change.get("domain"))
            handler = _HANDLERS[domain]
            if domain in ("fact", "relation") and is_retcon:
                result = handler(db, campaign_id, change, operation_id=operation_id or repair.operation_id, retcon=True)
            else:
                result = handler(db, campaign_id, change, operation_id=operation_id or repair.operation_id)
            applied.append({"domain": domain, "target": change.get("target_id") or change.get("duplicate_id"),
                            "before": result.get("before"), "after": result.get("after")})
            before_state.setdefault(domain, []).append(result.get("before"))
        repair.before_state = before_state
        repair.applied_changes = applied
        repair.status = RETCONNED if (bool(repair.is_retcon) or repair.repair_type == "retcon") else APPLIED
        repair.error = None
        repair.resolved_at = _now()
        if requires_correction:
            _ensure_directive(db, repair, commit=False)
        _resolve_linked_incident(db, repair, commit=False)
        db.add(repair)
        db.flush()
        savepoint.commit()
    except Exception as exc:
        try:
            savepoint.rollback()
        except Exception:
            pass
        return _fail_repair(db, repair, f"{type(exc).__name__}: {exc}", commit=commit)

    db.add(repair)
    db.flush()
    if commit:
        db.commit()
    structured_log(logger, logging.INFO, "campaign_repair_applied",
                   campaign_id=str(campaign_id), repair_id=str(repair.id),
                   repair_type=repair.repair_type, domains=list(repair.affected_domains or []),
                   retcon=bool(repair.is_retcon))
    return {"status": repair.status, "repair_id": str(repair.id),
            "applied_changes": applied, "directive_created": bool(repair.requires_player_visible_correction)}


def apply_retcon(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    incident_id: uuid.UUID | None = None,
    proposed_changes: list[dict[str, Any]],
    evidence: dict[str, Any] | None = None,
    conflicting_records: list[dict[str, Any]] | None = None,
    reason: str = "",
    correction_text: str = "",
    correction_scope: str | None = None,
    source: str = "dm_adjudicated",
    actor_id: str | None = None,
    fingerprint: str = "",
    operation_id: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Explicit retcon: preserve superseded truth, propagate to dependents, direct the DM.

    Player-visible correction is scoped, never defaulted: a ``campaign``
    correction (plus DM directive) is created only when member-visible
    history already exposed the old value or ``correction_scope="campaign"``
    is passed explicitly. Secret retcons stay silent — the correction text
    (when given) is kept in DM-only evidence and no player payload is
    produced.
    """
    ev = dict(evidence or {})
    scope = correction_scope or _correction_scope(
        evidence=ev, conflicting_records=conflicting_records,
        proposed_changes=proposed_changes,
    )
    if correction_text:
        ev["correction_text"] = correction_text[:2000]
    player_correction: dict[str, Any] | None = None
    requires_correction = False
    if scope == "campaign" and correction_text:
        ev["player_visible_exposure"] = True
        requires_correction = True
        player_correction = {"correction_text": correction_text[:2000], "scope": "campaign"}
    repair, created = create_repair(
        db, campaign_id, repair_type="retcon", proposed_changes=proposed_changes,
        incident_id=incident_id, conflicting_records=conflicting_records, evidence=ev,
        reason=reason or "explicit retcon preserving prior history",
        source=source, actor_id=actor_id,
        requires_player_visible_correction=requires_correction,
        player_visible_correction=player_correction,
        is_retcon=True, fingerprint=fingerprint or "retcon", operation_id=operation_id, commit=False,
    )
    if not created and repair.status in TERMINAL_STATUSES:
        return {"status": repair.status, "repair_id": str(repair.id), "duplicate": True,
                "applied_changes": list(repair.applied_changes or [])}
    return apply_repair(db, campaign_id, repair.id, operation_id=operation_id, actor_id=actor_id, commit=commit)


def _resolve_linked_incident(db: Session, repair: CampaignRepair, *, commit: bool = True) -> None:
    """Resolve the linked #220 incident inside the repair transaction.

    Strict: a missing/cross-campaign incident or any persistence failure
    raises so the repair fails instead of reporting ``applied`` while its
    incident stays open.
    """
    if repair.incident_id is None:
        return
    from models.post_turn import PostTurnConsistencyIncident
    incident = db.get(PostTurnConsistencyIncident, repair.incident_id)
    if incident is None:
        raise ValueError("linked consistency incident not found")
    if incident.campaign_id != repair.campaign_id:
        raise ValueError("linked consistency incident belongs to another campaign")
    if incident.status != "resolved":
        incident.status = "resolved"
        incident.resolved_at = _now()
        db.add(incident)
        db.flush()
    if commit:
        db.flush()


# ── Forward-DM directives (one-time consumption) ──────────────────────────────

def _ensure_directive(db: Session, repair: CampaignRepair, *, commit: bool = True) -> RepairDirective:
    key = f"dir-{repair.repair_key}"
    existing = db.execute(select(RepairDirective).where(
        RepairDirective.campaign_id == repair.campaign_id,
        RepairDirective.directive_key == key,
    )).scalars().first()
    if existing is not None:
        return existing
    safe = _player_safe_correction(repair.player_visible_correction)
    if safe is not None and str(safe.get("scope") or "") not in ("campaign", "public"):
        safe = None
    row = RepairDirective(
        id=uuid.uuid4(), campaign_id=repair.campaign_id, repair_id=repair.id,
        directive_key=key, status="open", audience="campaign",
        payload={"repair_id": str(repair.id), "repair_type": repair.repair_type,
                 "reason": (repair.reason or "")[:2000],
                 "evidence_refs": list(repair.conflicting_records or []),
                 "instruction": str((repair.evidence or {}).get("dm_instruction")
                                    or repair.reason or "Reconcile the corrected detail.")[:2000]},
        player_safe_payload=safe,
        visibility="dm_only",
        operation_id=repair.operation_id,
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
    return row


def create_directive_for_repair(
    db: Session,
    repair: CampaignRepair,
    *,
    audience: str = "campaign",
    thread_id: uuid.UUID | None = None,
    dm_instruction: str = "",
    commit: bool = True,
) -> tuple[RepairDirective, bool]:
    key = f"dir-{repair.repair_key}"
    existing = db.execute(select(RepairDirective).where(
        RepairDirective.campaign_id == repair.campaign_id,
        RepairDirective.directive_key == key,
    )).scalars().first()
    if existing is not None:
        return existing, False
    safe = _player_safe_correction(repair.player_visible_correction)
    if safe is not None and str(safe.get("scope") or "") not in ("campaign", "public"):
        safe = None
    row = RepairDirective(
        id=uuid.uuid4(), campaign_id=repair.campaign_id, repair_id=repair.id,
        directive_key=key, status="open", audience=audience, thread_id=thread_id,
        payload={"repair_id": str(repair.id), "repair_type": repair.repair_type,
                 "reason": (repair.reason or "")[:2000],
                 "evidence_refs": list(repair.conflicting_records or []),
                 "instruction": (dm_instruction or repair.reason or "Reconcile the corrected detail.")[:2000]},
        player_safe_payload=safe,
        visibility="dm_only",
        operation_id=repair.operation_id,
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
    return row, True


def list_open_directives(db: Session, campaign_id: uuid.UUID, *, thread_id: uuid.UUID | None = None) -> list[RepairDirective]:
    query = select(RepairDirective).where(
        RepairDirective.campaign_id == campaign_id,
        RepairDirective.status == "open",
    ).order_by(RepairDirective.created_at.asc())
    rows = list(db.execute(query).scalars().all())
    if thread_id is None:
        return rows
    return [r for r in rows if r.thread_id is None or r.thread_id == thread_id]


def build_repair_directive_context_records(
    db: Session, campaign_id: uuid.UUID, *, thread_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Typed REPAIR_DIRECTIVES lane records for the #202 context assembler."""
    try:
        directives = list_open_directives(db, campaign_id, thread_id=thread_id)
    except Exception:
        return []
    records: list[dict[str, Any]] = []
    for directive in directives:
        payload = dict(directive.payload or {})
        records.append({
            "record_id": f"repair-directive:{directive.id}",
            "value": {"directive_id": str(directive.id), "repair_id": str(directive.repair_id),
                      "instruction": payload.get("instruction"),
                      "reason": payload.get("reason"),
                      "evidence_refs": payload.get("evidence_refs") or []},
            "source_type": "repair_directive", "source_id": str(directive.id),
            "source_version": directive.created_at.isoformat() if directive.created_at else "1",
            "visibility": "dm_only", "use": "adjudication_only",
            "required": True, "priority": 100,
        })
    return records


def consume_directive(db: Session, campaign_id: uuid.UUID, directive_id: uuid.UUID, *, commit: bool = True) -> dict[str, Any]:
    try:
        did = _as_uuid(directive_id, field="directive_id")
    except ValueError as exc:
        raise ValueError(f"invalid directive id {directive_id!r}") from exc
    row = db.get(RepairDirective, did)
    if row is None or row.campaign_id != campaign_id:
        raise ValueError("repair directive not found in this campaign")
    if row.status == "consumed":
        return {"status": "consumed", "directive_id": str(row.id), "duplicate": True}
    if row.status == "closed":
        return {"status": "closed", "directive_id": str(row.id), "duplicate": True}
    row.status = "consumed"
    row.consumed_at = _now()
    db.add(row)
    db.flush()
    if commit:
        db.commit()
    return {"status": "consumed", "directive_id": str(row.id)}


def close_directive(db: Session, campaign_id: uuid.UUID, directive_id: uuid.UUID, *, commit: bool = True) -> dict[str, Any]:
    try:
        did = _as_uuid(directive_id, field="directive_id")
    except ValueError as exc:
        raise ValueError(f"invalid directive id {directive_id!r}") from exc
    row = db.get(RepairDirective, did)
    if row is None or row.campaign_id != campaign_id:
        raise ValueError("repair directive not found in this campaign")
    if row.status == "closed":
        return {"status": "closed", "directive_id": str(row.id), "duplicate": True}
    row.status = "closed"
    row.closed_at = _now()
    db.add(row)
    db.flush()
    if commit:
        db.commit()
    return {"status": "closed", "directive_id": str(row.id)}


# ── Observability ─────────────────────────────────────────────────────────────

def get_repair_stats(db: Session, campaign_id: uuid.UUID) -> dict[str, Any]:
    repairs = list(db.execute(select(CampaignRepair).where(
        CampaignRepair.campaign_id == campaign_id)).scalars().all())
    directives = list(db.execute(select(RepairDirective).where(
        RepairDirective.campaign_id == campaign_id)).scalars().all())
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    automatic = 0
    adjudicated = 0
    retcons = 0
    resolution_ms: list[int] = []
    for repair in repairs:
        by_type[repair.repair_type] = by_type.get(repair.repair_type, 0) + 1
        by_status[repair.status] = by_status.get(repair.status, 0) + 1
        if repair.detection_path == "dm_adjudicated":
            adjudicated += 1
        else:
            automatic += 1
        if bool(repair.is_retcon) or repair.repair_type == "retcon":
            retcons += 1
        if repair.resolved_at is not None and repair.created_at is not None:
            try:
                resolution_ms.append(int((repair.resolved_at - repair.created_at).total_seconds() * 1000))
            except Exception:
                pass
    domains: dict[str, int] = {}
    for repair in repairs:
        for domain in (repair.affected_domains or []):
            domains[str(domain)] = domains.get(str(domain), 0) + 1
    return {
        "repairs": len(repairs),
        "by_type": by_type,
        "by_status": by_status,
        "automatic": automatic,
        "dm_adjudicated": adjudicated,
        "affected_domains": domains,
        "evidence_refs": sum(len(list(r.conflicting_records or [])) for r in repairs),
        "time_to_resolution_ms": {
            "count": len(resolution_ms),
            "p50": sorted(resolution_ms)[len(resolution_ms) // 2] if resolution_ms else None,
        },
        "repeated_failures": sum(int(r.retry_count or 0) for r in repairs),
        "directives": {
            "open": sum(1 for d in directives if d.status == "open"),
            "consumed": sum(1 for d in directives if d.status == "consumed"),
            "closed": sum(1 for d in directives if d.status == "closed"),
        },
        "retcons": retcons,
    }
