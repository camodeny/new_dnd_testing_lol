"""Authoritative ask_character_sheet evidence tool — issue #224.

Returns the canonical CharacterMechanics DTO via the #203 EvidenceResult
contract. No LLM math; no frontend JSON re-parsing; authorization-scoped.

Campaign scoping (review fix): targets are resolved through authoritative
campaign membership, not arbitrary Character UUIDs or all characters owned by
audience users. A character must be owned by a CampaignMember of the
requested campaign (and for party/current_player, be associated with the
campaign via at least one PlayerSubmission when submissions exist). Cross-
campaign IDs are rejected before any AuthorizationScope is stamped.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

from app.dm.context import AuthorizationScope, ContextAudience, SourceRef
from app.dm.contract import EvidenceRequest
from app.dm.evidence import EvidenceResult
from app.observability.tracing import structured_log

logger = logging.getLogger(__name__)


def _campaign_member_user_ids(db: Any, campaign_id: uuid.UUID) -> set[uuid.UUID]:
    from models import CampaignMember

    rows = db.execute(select(CampaignMember.user_id).where(CampaignMember.campaign_id == campaign_id)).scalars().all()
    return set(rows)


def _campaign_character_ids_via_submissions(db: Any, campaign_id: uuid.UUID) -> set[uuid.UUID] | None:
    """Distinct character_ids that have at least one PlayerSubmission in this campaign.

    Returns None if the campaign has no submissions at all (so caller can fall back
    to membership-only scoping for pre-submission characters).
    """
    from models import PlayerSubmission

    rows = db.execute(
        select(PlayerSubmission.character_id).where(
            PlayerSubmission.campaign_id == campaign_id,
            PlayerSubmission.character_id.is_not(None),
        ).distinct()
    ).scalars().all()
    # distinct returns UUIDs
    ids = {r for r in rows if r is not None}
    if not ids:
        # Check if any submissions exist at all (even without character)
        any_sub = db.execute(select(PlayerSubmission.id).where(PlayerSubmission.campaign_id == campaign_id).limit(1)).scalar_one_or_none()
        if any_sub is None:
            return None  # no submissions yet — no signal
    return ids


def _resolve_character_ids_for_scope(
    request: EvidenceRequest,
    audience: ContextAudience,
    db: Any,
) -> list[uuid.UUID]:
    """Resolve evidence scope to concrete character UUIDs, scoped to audience campaign."""
    from models import CampaignMember, Character

    try:
        campaign_uuid = uuid.UUID(str(audience.campaign_id))
    except Exception:
        return []

    member_user_ids = _campaign_member_user_ids(db, campaign_uuid)
    # campaign must exist and have members; otherwise no targets
    if not member_user_ids:
        return []

    # Helper to verify a single character is campaign-authorized
    def _is_campaign_authorized(char: Character) -> bool:
        if char.owner_id not in member_user_ids:
            return False
        # If campaign has submissions, require the character to have appeared in it
        # (prevents leaking a user's unrelated character from another campaign).
        # Only enforce when submissions exist; otherwise allow membership-only.
        campaign_char_ids = _campaign_character_ids_via_submissions(db, campaign_uuid)
        if campaign_char_ids is not None and len(campaign_char_ids) > 0:
            # If this character has never submitted in this campaign, it's not yet
            # authoritative for this campaign — treat as not found for party/current_player,
            # but for explicit character_id we still reject.
            if char.id not in campaign_char_ids:
                # For explicit character_id, reject; for party scopes caller filters differently
                # We return False here so explicit scope returns empty; party/current_player
                # will filter via submission set directly.
                return False
        return True

    # character_id scope — single explicit char, must be campaign-authorized
    if request.scope == "character_id":
        if request.character_id is None:
            return []
        try:
            cid = uuid.UUID(str(request.character_id))
        except Exception:
            return []
        char = db.get(Character, cid)
        if char is None:
            return []
        if not _is_campaign_authorized(char):
            return []
        # Also enforce audience authorization: for private include_private, owner must be in audience; for campaign, owner must be member (already)
        if request.include_private and str(char.owner_id) not in audience.user_ids:
            # Private data requested but character not owned by audience user — not authorized
            # Let caller handle via status; we still filter out
            return []
        return [cid]

    # party — all campaign members' characters that are campaign-associated
    if request.scope == "party":
        # Prefer submission-linked characters when they exist
        campaign_char_ids = _campaign_character_ids_via_submissions(db, campaign_uuid)
        if campaign_char_ids is not None and len(campaign_char_ids) > 0:
            # Return only submission-linked characters that are owned by members
            rows = db.execute(select(Character).where(Character.id.in_(campaign_char_ids))).scalars().all()
            # Double-check owner still member (in case character ownership changed)
            return [r.id for r in rows if r.owner_id in member_user_ids]
        # No submissions yet — fall back to characters owned by campaign members (bounded)
        rows = db.execute(select(Character).where(Character.owner_id.in_(member_user_ids)).limit(8)).scalars().all()
        return [r.id for r in rows]

    # current_player — characters owned by audience users that are campaign-associated
    if request.scope == "current_player":
        audience_user_ids: set[uuid.UUID] = set()
        for uid in audience.user_ids:
            try:
                audience_user_ids.add(uuid.UUID(uid))
            except Exception:
                continue
        # Intersect with campaign members — audience must be subset, but verify
        audience_user_ids = audience_user_ids.intersection(member_user_ids)
        if not audience_user_ids:
            return []
        campaign_char_ids = _campaign_character_ids_via_submissions(db, campaign_uuid)
        if campaign_char_ids is not None and len(campaign_char_ids) > 0:
            rows = db.execute(
                select(Character).where(
                    Character.id.in_(campaign_char_ids),
                    Character.owner_id.in_(audience_user_ids),
                )
            ).scalars().all()
            return [r.id for r in rows][:8]
        rows = db.execute(select(Character).where(Character.owner_id.in_(audience_user_ids)).limit(8)).scalars().all()
        return [r.id for r in rows][:8]

    return []


def handle_ask_character_sheet(
    request: EvidenceRequest,
    audience: ContextAudience,
    *,
    db: Any | None = None,
) -> EvidenceResult:
    """Evidence handler for ask_character_sheet — returns CharacterMechanics DTO.

    Authorization: private sheet data only when audience is private and caller authorized for those user_ids.
    Campaign scoping is enforced before any AuthorizationScope is stamped.
    """
    t0 = time.monotonic()

    if request.tool != "ask_character_sheet":
        return EvidenceResult(
            request_id=request.id,
            tool=request.tool,
            status="tool_failure",
            sources=[],
            visibility="campaign",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload=None,
            error=f"handler called for wrong tool {request.tool!r}",
        )

    if db is None:
        return EvidenceResult(
            request_id=request.id,
            tool=request.tool,
            status="unknown",
            sources=[SourceRef(source_type="dnd5e_character_sheet", source_id=request.id, source_version="no_db", provenance={"tool": request.tool, "stub": True})],
            visibility="campaign",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload={"note": "no db available for character mechanics", "request_id": request.id},
            result_count=0,
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    visibility: str = "private" if request.include_private else "campaign"
    if request.include_private and audience.audience != "private":
        return EvidenceResult(
            request_id=request.id,
            tool=request.tool,
            status="unauthorized",
            sources=[],
            visibility="private",  # type: ignore[arg-type]
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload=None,
            error="private sheet data not authorized for this audience",
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    character_ids = _resolve_character_ids_for_scope(request, audience, db)
    if not character_ids:
        return EvidenceResult(
            request_id=request.id,
            tool=request.tool,
            status="missing",
            sources=[],
            visibility=visibility,  # type: ignore[arg-type]
            authorization=AuthorizationScope(
                campaign_id=audience.campaign_id,
                thread_ids=[audience.thread_id],
                user_ids=list(audience.user_ids) if visibility == "private" else [],
            ),
            payload=None,
            error="no characters found for requested scope",
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    from app.rules.mechanics import MechanicsError, get_character_mechanics_for_sheet
    from models import Dnd5eCharacterSheet

    results_payload: list[dict[str, Any]] = []
    sources: list[SourceRef] = []
    auth_user_ids: list[str] = []
    has_error = False
    error_msg: str | None = None
    has_success = False

    for cid in character_ids[:4]:
        sheet = db.execute(
            select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == cid).order_by(Dnd5eCharacterSheet.updated_at.desc())
        ).scalars().first()
        if sheet is None:
            continue
        try:
            mechanics = get_character_mechanics_for_sheet(sheet)
            dump = mechanics.model_dump(mode="json")
            results_payload.append(dump)
            has_success = True
            sources.append(SourceRef(
                source_type="dnd5e_character_sheet",
                source_id=str(sheet.id),
                source_version=sheet.updated_at.isoformat() if getattr(sheet, "updated_at", None) else "unknown",
                campaign_revision=None,
                provenance={"tool": request.tool, "character_id": str(cid), "question": request.question or ""},
            ))
            if visibility == "private":
                auth_user_ids.append(str(sheet.owner_id))
        except MechanicsError as exc:
            has_error = True
            error_msg = f"mechanics error {exc.code}: {exc}"
            sources.append(SourceRef(
                source_type="dnd5e_character_sheet",
                source_id=str(sheet.id) if sheet and getattr(sheet, "id", None) else str(cid),
                source_version=sheet.updated_at.isoformat() if sheet and getattr(sheet, "updated_at", None) else "unknown",
                provenance={"tool": request.tool, "error_code": exc.code, "field": exc.field or ""},
            ))
            results_payload.append({
                "character_id": str(cid),
                "error": {"code": exc.code, "field": exc.field, "message": str(exc), "details": exc.details},
            })
        except Exception as exc:
            has_error = True
            error_msg = str(exc)[:400]
            results_payload.append({"character_id": str(cid), "error": {"code": "tool_failure", "message": str(exc)[:400]}})

    if not results_payload:
        return EvidenceResult(
            request_id=request.id,
            tool=request.tool,
            status="missing",
            sources=sources,
            visibility=visibility,  # type: ignore[arg-type]
            authorization=AuthorizationScope(
                campaign_id=audience.campaign_id,
                thread_ids=[audience.thread_id],
                user_ids=sorted(set(auth_user_ids)) if visibility == "private" else [],
            ),
            payload=None,
            error=error_msg or "no sheets found",
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    auth = AuthorizationScope(
        campaign_id=audience.campaign_id,
        thread_ids=[audience.thread_id],
        user_ids=sorted(set(auth_user_ids)) if visibility == "private" else [],
    )
    if visibility == "private" and not auth.user_ids:
        auth = AuthorizationScope(
            campaign_id=audience.campaign_id,
            thread_ids=[audience.thread_id],
            user_ids=list(audience.user_ids),
        )

    latency_ms = (time.monotonic() - t0) * 1000

    payload: dict[str, Any]
    if len(results_payload) == 1:
        payload = results_payload[0]
    else:
        payload = {"characters": results_payload, "count": len(results_payload)}

    # Fail-closed: if every result is an error, propagate failure; partial success stays ok but errors visible per-character
    if has_error and not has_success:
        # All failed due to MechanicsError — block authoritative use
        status: str = "tool_failure"
    elif has_error:
        status = "ok"
    else:
        status = "ok"

    structured_log(
        logger,
        logging.INFO,
        "character_sheet_evidence",
        request_id=request.id,
        scope=request.scope,
        include_private=bool(request.include_private),
        character_count=len(results_payload),
        source_ids=[f"{s.source_type}:{s.source_id}@{s.source_version}" for s in sources],
        latency_ms=round(latency_ms, 2),
        status=status,
        visibility=visibility,
    )

    return EvidenceResult(
        request_id=request.id,
        tool=request.tool,
        status=status,  # type: ignore[arg-type]
        sources=sources,
        visibility=visibility,  # type: ignore[arg-type]
        authorization=auth,
        payload=payload,
        result_count=len(results_payload),
        latency_ms=latency_ms,
    )
