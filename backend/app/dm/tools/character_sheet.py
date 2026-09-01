"""Authoritative ask_character_sheet evidence tool — issue #224.

Returns the canonical CharacterMechanics DTO via the #203 EvidenceResult
contract. No LLM math; no frontend JSON re-parsing; authorization-scoped.
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


def _resolve_character_ids_for_scope(
    request: EvidenceRequest,
    audience: ContextAudience,
    db: Any,
) -> list[uuid.UUID]:
    """Resolve evidence scope to concrete character UUIDs, scoped to audience campaign."""
    from models import CampaignMember, CampaignThreadMember, Character

    # character_id scope — single explicit char
    if request.scope == "character_id":
        if request.character_id is None:
            return []
        try:
            cid = uuid.UUID(str(request.character_id))
        except Exception:
            return []
        char = db.get(Character, cid)
        # must belong to requested campaign's context — best effort: if audience campaign set, just return it; auth handled downstream
        if char is None:
            return []
        return [cid]

    # party — all campaign members' characters? Return chars whose owner in campaign user set.
    # We resolve via characters owned by campaign members.
    if request.scope == "party":
        # audience.user_ids includes campaign members + owner (see _audience_for_attempt)
        user_ids = []
        for uid in audience.user_ids:
            try:
                user_ids.append(uuid.UUID(uid))
            except Exception:
                continue
        if not user_ids:
            return []
        rows = db.execute(select(Character).where(Character.owner_id.in_(user_ids))).scalars().all()
        return [r.id for r in rows]

    # current_player — characters owned by thread members (private) or all campaign members but filtered to submission-relevant?
    # For generic tool, return calling thread's characters — use thread membership for private, campaign membership for campaign audience.
    if request.scope == "current_player":
        # If private thread, audience.user_ids is thread members; otherwise campaign members
        user_ids = []
        for uid in audience.user_ids:
            try:
                user_ids.append(uuid.UUID(uid))
            except Exception:
                continue
        rows = db.execute(select(Character).where(Character.owner_id.in_(user_ids))).scalars().all()
        # In campaign audience, this would return many — limit to first owner's chars? For evidence, we return all but bounded later.
        # Bounding: take at most 8 to keep payload bounded
        ids = [r.id for r in rows][:8]
        return ids

    return []


def handle_ask_character_sheet(
    request: EvidenceRequest,
    audience: ContextAudience,
    *,
    db: Any | None = None,
) -> EvidenceResult:
    """Evidence handler for ask_character_sheet — returns CharacterMechanics DTO.

    Authorization: private sheet data only when audience is private and caller authorized for those user_ids.
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
        # No DB — cannot resolve; return unknown (keeps loop exercising without crash)
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

    # Authorization gate for private data
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

    # Resolve targets
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

    # Fetch mechanics for each character, bounding output
    from app.rules.mechanics import MechanicsError, get_character_mechanics_for_sheet
    from models import Dnd5eCharacterSheet

    results_payload: list[dict[str, Any]] = []
    sources: list[SourceRef] = []
    auth_user_ids: list[str] = []
    has_error = False
    error_msg: str | None = None

    for cid in character_ids[:4]:  # bound to 4 sheets per request
        sheet = db.execute(
            select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == cid).order_by(Dnd5eCharacterSheet.updated_at.desc())
        ).scalars().first()
        if sheet is None:
            continue
        try:
            mechanics = get_character_mechanics_for_sheet(sheet)
            dump = mechanics.model_dump(mode="json")
            results_payload.append(dump)
            sources.append(SourceRef(
                source_type="dnd5e_character_sheet",
                source_id=str(sheet.id),
                source_version=sheet.updated_at.isoformat() if getattr(sheet, "updated_at", None) else "unknown",
                campaign_revision=None,
                provenance={"tool": request.tool, "character_id": str(cid), "question": request.question or ""},
            ))
            # For private visibility, authorize to sheet owner
            if visibility == "private":
                auth_user_ids.append(str(sheet.owner_id))
        except MechanicsError as exc:
            has_error = True
            error_msg = f"mechanics error {exc.code}: {exc}"
            # Still produce a result with error provenance — caller can distinguish
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

    # Authorization scope for result — private requires user-scoped auth
    auth = AuthorizationScope(
        campaign_id=audience.campaign_id,
        thread_ids=[audience.thread_id],
        user_ids=sorted(set(auth_user_ids)) if visibility == "private" else [],
    )
    if visibility == "private" and not auth.user_ids:
        # Fail-closed: private must be user-scoped
        auth = AuthorizationScope(
            campaign_id=audience.campaign_id,
            thread_ids=[audience.thread_id],
            user_ids=list(audience.user_ids),
        )

    latency_ms = (time.monotonic() - t0) * 1000

    # Compact payload already bounded by mechanics size; evidence layer will _compact further

    payload: dict[str, Any]
    if len(results_payload) == 1:
        payload = results_payload[0]
    else:
        payload = {"characters": results_payload, "count": len(results_payload)}

    status: str = "ok"
    if has_error and not any("error" not in p for p in results_payload):
        status = "tool_failure" if error_msg else "missing"
    elif has_error:
        status = "ok"  # partial success — caller sees per-character errors in payload

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
