"""Adventures transport — APIRouter.

Canonical lifecycle (open/list/complete-current, issue #260) lives in
``app.campaigns.router``. This module carries the additive issue #263
surface on top of the same canonical service/model:

- POST .../adventures/{adventure_id}/complete — explicit-target completion
  that also derives the AdventureSummary (best-effort, never blocks).
- GET .../summary — durable historical summary (owner/DM-only).
- GET .../recap — visibility-filtered player recap (member-readable).
- POST .../summaries/generate + .../summaries/mark-stale — retry/repair.
- /api/cron/adventure-closing — best-effort closing sweep (issue #260).

AI-only DM: all lifecycle/repair mutations are owner-only; list/recap stay
member-readable. Single canonical path, real Supabase JWT (resolve_profile).
"""

from __future__ import annotations

import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adventures.service import (
    AdventureError,
    complete_adventure,
    finalize_adventure_derived,
    generate_summary,
    mark_stale,
    project_recap,
)
from app.campaigns.events import RevisionConflictError
from app.campaigns.service import (
    CampaignArchivedError,
    is_campaign_member,
    parse_campaign_id,
)
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from database import get_db
from models.campaigns import Adventure, AdventureEpilogue, AdventureSummary, Campaign

router = APIRouter()
logger = logging.getLogger(__name__)


def _campaign_or_404(db: Session, cid: uuid_lib.UUID) -> Campaign:
    camp = db.get(Campaign, cid)
    if camp is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return camp


def _require_member(db: Session, camp: Campaign, profile) -> None:
    if camp.owner_id != profile.id and not is_campaign_member(db, camp.id, profile.id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")


def _require_owner(camp: Campaign, profile) -> None:
    """DM-declared lifecycle mutations are owner/DM-only.

    Ordinary campaign members (role=player) may read the recap projection
    but must never complete adventures, choose outcomes, or drive
    repair/regeneration of canonical derived state.
    """
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the campaign owner (DM) can perform this action")


def _parse_ids(campaign_id: str, adventure_id: str | None = None):
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    aid = None
    if adventure_id is not None:
        try:
            aid = uuid_lib.UUID(str(adventure_id))
        except ValueError:
            raise HTTPException(status_code=404, detail="Invalid adventure id")
    return cid, aid


def _adventure_or_404(db: Session, cid, aid) -> Adventure:
    adv = db.get(Adventure, aid)
    if adv is None or adv.campaign_id != cid:
        raise HTTPException(status_code=404, detail="Adventure not found")
    return adv


def _summary_or_404(db: Session, adv: Adventure) -> AdventureSummary:
    row = db.execute(
        select(AdventureSummary).where(AdventureSummary.adventure_id == adv.id)
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Adventure summary not found")
    return row


def _opt_uuid(raw) -> uuid_lib.UUID | None:
    if not raw:
        return None
    try:
        return uuid_lib.UUID(str(raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid source id")


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/complete")
def complete_adventure_endpoint(
    campaign_id: str, adventure_id: str, payload: dict,
    request: Request, response: Response, db: Session = Depends(get_db),
):
    """DM-declared completion of an explicit adventure + derived summary.

    Funnels through the canonical ``complete_adventure`` service (same code
    path as /current/complete and the staged DM effect). The legacy
    ``outcome_reason`` body field maps to the canonical member-visible
    ``public_summary``; the DM-private ``reason`` stays owner-visible and
    never enters the recap. Derived summary generation is best-effort and
    never blocks the authoritative completion.
    """
    from app.adventures.service import (
        AdventureAlreadyCompletedError,
        AdventureNotFoundError,
    )

    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_owner(camp, profile)
    adv = _adventure_or_404(db, cid, aid)
    body = payload or {}
    operation_id = str(body.get("operation_id") or "").strip() or None
    if adv.status == "completed":
        # Idempotent replay: same completion operation reuses the existing
        # row + derived summary instead of duplicating.
        if operation_id is not None and adv.operation_id not in (None, operation_id):
            raise HTTPException(status_code=409, detail=f"Adventure {aid} is already completed")
        row = db.execute(
            select(AdventureSummary).where(AdventureSummary.adventure_id == adv.id)
        ).scalars().first()
        if row is None:
            from app.adventures.service import _ensure_summary_placeholder

            row = _ensure_summary_placeholder(db, adv)
            db.commit()
            db.refresh(row)
        return {"adventure": adv.to_dict(), "summary": row.to_dict(), "idempotent": True}
    if "expected_revision" not in body:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        expected = int(body["expected_revision"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    idempotency_key = require_idempotency_key(request, operation_id)

    def _execute():
        try:
            completed, event = complete_adventure(
                db, cid,
                outcome=str(body.get("outcome") or ""),
                public_summary=body.get("outcome_reason"),
                adventure_id=aid,
                operation_id=operation_id or idempotency_key,
                actor_id=profile.id,
                expected_revision=expected,
                source_turn_id=_opt_uuid(body.get("source_turn_id")),
                commit=False,
            )
        except AdventureNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except AdventureAlreadyCompletedError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            if isinstance(exc, RevisionConflictError):
                raise HTTPException(
                    status_code=409, detail=str(exc),
                    headers={"X-Current-Revision": str(exc.actual_revision)},
                )
            if isinstance(exc, CampaignArchivedError):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc))
        # Bind the authoritative end cursor and derive the summary through the
        # shared finalizer (same step every completion path runs; best-effort,
        # never rolls back the authoritative completion).
        fresh_campaign = db.get(Campaign, cid)
        row = finalize_adventure_derived(
            db, completed,
            event_sequence=event.sequence if event is not None else None,
            revision=fresh_campaign.revision if fresh_campaign is not None else None,
            actor_id=profile.id,
        )
        db.flush()
        return {
            "adventure": completed.to_dict(),
            "event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
            "summary": row.to_dict() if row is not None else None,
        }

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="adventure.complete", scope_type="adventure", scope_id=aid,
            payload=body, execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        )


@router.get("/api/campaigns/{campaign_id}/adventures/{adventure_id}/summary")
def get_summary(campaign_id: str, adventure_id: str, request: Request, db: Session = Depends(get_db)):
    """Durable historical summary (derived; events/facts outrank it).

    Owner/DM-only: the historical summary compresses hidden source evidence
    (dm_only/private) for retrieval/context. Members use the /recap
    projection, which is visibility-filtered.
    """
    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_owner(camp, profile)
    adv = _adventure_or_404(db, cid, aid)
    row = _summary_or_404(db, adv)
    return {"adventure": adv.to_dict(), "summary": row.to_dict()}


@router.get("/api/campaigns/{campaign_id}/adventures/{adventure_id}/recap")
def get_recap(campaign_id: str, adventure_id: str, request: Request, db: Session = Depends(get_db)):
    """Review Adventure — player-facing recap projection (visibility-filtered).

    Available after continuation/archive as long as the viewer is authorized
    (campaign member); records a view.
    """
    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
    adv = _adventure_or_404(db, cid, aid)
    row = _summary_or_404(db, adv)
    projected = project_recap(db, adv, row, viewer_id=profile.id)
    db.commit()
    return projected


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/summaries/generate")
def retry_generate(campaign_id: str, adventure_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    """Async-style retry/regeneration of derived work (never blocks completion)."""
    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_owner(camp, profile)
    adv = _adventure_or_404(db, cid, aid)
    body = payload or {}
    row = generate_summary(
        db, adv, actor_id=profile.id,
        force_fail=bool(body.get("force_fail")),
    )
    return {"adventure": adv.to_dict(), "summary": row.to_dict()}


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/summaries/mark-stale")
def mark_stale_endpoint(
    campaign_id: str, adventure_id: str, payload: dict, request: Request, db: Session = Depends(get_db),
):
    """Repair/retcon hook: mark the derived artifact stale so it rebuilds."""
    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_owner(camp, profile)
    adv = _adventure_or_404(db, cid, aid)
    body = payload or {}
    try:
        row = mark_stale(db, adv.id, reason=str(body.get("reason") or "repair/retcon"))
    except AdventureError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if body.get("regenerate"):
        row = generate_summary(db, adv, actor_id=profile.id)
    return {"adventure": adv.to_dict(), "summary": row.to_dict()}


@router.get("/api/cron/adventure-closing")
def adventure_closing_cron_get(request: Request, db: Session = Depends(get_db)):
    """Drive pending adventure closing work via the idempotent worker fence."""
    from app.outbox.router import _require_cron_secret

    _require_cron_secret(request.headers.get("authorization"))
    from app.adventures.service import run_adventure_closing_sweep

    result = run_adventure_closing_sweep(db)
    logger.info(
        "adventure closing cron executed=%s failed=%s",
        len(result.get("executed", [])),
        len(result.get("failed", [])),
    )
    return {"ok": True, "sweep": result}


@router.post("/api/cron/adventure-closing")
def adventure_closing_cron_post(request: Request, db: Session = Depends(get_db)):
    return adventure_closing_cron_get(request=request, db=db)


# ── Optional player epilogues (issue #262) ────────────────────────────────────
#
# Canonical post-adventure play: players optionally submit what their PCs do
# next (or skip) after DM-declared completion. Simple entries resolve
# immediately into canonical ``adventure.epilogue`` domain events;
# adjudicated entries wait for the owning player's deterministic roll.
# The DM never authors voluntary PC choices — submit/roll accept only the
# character's owning player (enforced in the service).


def _epilogue_error_response(exc: Exception):
    from app.adventures.epilogues import (
        EpilogueAuthorizationError,
        EpilogueDuplicateError,
        EpilogueStateError,
    )

    if isinstance(exc, EpilogueAuthorizationError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (EpilogueStateError, EpilogueDuplicateError)):
        raise HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RevisionConflictError):
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        )
    if isinstance(exc, CampaignArchivedError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc))


def _require_int(body: dict, name: str) -> int:
    try:
        return int(body[name])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be an integer")


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/epilogues/open")
def open_epilogues_endpoint(campaign_id: str, adventure_id: str, request: Request, db: Session = Depends(get_db)):
    """Open the optional epilogue phase for a completed adventure (owner/DM-only)."""
    from app.adventures.epilogues import epilogue_stats, open_epilogues

    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_owner(camp, profile)
    _adventure_or_404(db, cid, aid)
    try:
        adv = open_epilogues(db, cid, aid)
        db.refresh(adv)
    except Exception as exc:  # noqa: BLE001 — mapped to status codes below
        _epilogue_error_response(exc)
    return {"adventure": adv.to_dict(), "stats": epilogue_stats(db, aid)}


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/epilogues/submit")
def submit_epilogue_endpoint(
    campaign_id: str, adventure_id: str, payload: dict,
    request: Request, db: Session = Depends(get_db),
):
    """Submit the caller's voluntary epilogue choice for their own PC.

    Body: character_id, content, visibility (public|private),
    needs_adjudication + roll_spec ({roll_kind, ability_or_skill, label, dc})
    for mechanically uncertain actions, optional operation_id for idempotent
    retry, and required expected_revision (canonical fictional mutation).
    """
    from app.adventures.epilogues import submit_epilogue

    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
    _adventure_or_404(db, cid, aid)
    body = payload or {}
    try:
        character_id = uuid_lib.UUID(str(body.get("character_id") or ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="character_id must be a UUID")
    if "expected_revision" not in body:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    expected = _require_int(body, "expected_revision")
    try:
        row, event = submit_epilogue(
            db, cid, aid,
            user_id=profile.id,
            character_id=character_id,
            content=str(body.get("content") or ""),
            visibility=str(body.get("visibility") or "public"),
            needs_adjudication=bool(body.get("needs_adjudication")),
            roll_spec=body.get("roll_spec"),
            operation_id=(str(body.get("operation_id") or "").strip() or None),
            expected_revision=expected,
        )
    except Exception as exc:  # noqa: BLE001 — mapped to status codes below
        _epilogue_error_response(exc)
    return {
        "epilogue": row.to_dict(),
        "event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
    }


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/epilogues/{epilogue_id}/roll")
def fulfill_epilogue_roll_endpoint(
    campaign_id: str, adventure_id: str, epilogue_id: str, payload: dict,
    request: Request, db: Session = Depends(get_db),
):
    """Fulfill the caller's human roll for their PC's adjudicated epilogue.

    Body: die_value (1–20), modifier (-10…+30), required expected_revision.
    Code-owned arithmetic decides success; the outcome commits canonically.
    """
    from app.adventures.epilogues import fulfill_epilogue_roll

    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
    _adventure_or_404(db, cid, aid)
    try:
        eid = uuid_lib.UUID(str(epilogue_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid epilogue id")
    body = payload or {}
    if "expected_revision" not in body:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    expected = _require_int(body, "expected_revision")
    try:
        die = int(body.get("die_value"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="die_value must be an integer between 1 and 20")
    try:
        modifier = int(body.get("modifier", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="modifier must be an integer")
    # Ownership check BEFORE any mutation: the epilogue row must belong to the
    # route's campaign/adventure, otherwise a caller could resolve an epilogue
    # in another adventure/campaign through this route (the mutation commits).
    existing = db.get(AdventureEpilogue, eid)
    if (
        existing is None
        or str(existing.adventure_id) != str(aid)
        or str(existing.campaign_id) != str(cid)
    ):
        raise HTTPException(status_code=404, detail="Epilogue not found")
    try:
        row, event = fulfill_epilogue_roll(
            db, eid,
            user_id=profile.id,
            die_value=die,
            modifier=modifier,
            expected_revision=expected,
        )
    except Exception as exc:  # noqa: BLE001 — mapped to status codes below
        _epilogue_error_response(exc)
    if str(row.adventure_id) != str(aid) or str(row.campaign_id) != str(cid):
        raise HTTPException(status_code=404, detail="Epilogue not found")
    return {
        "epilogue": row.to_dict(),
        "event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
    }


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/epilogues/skip")
def skip_epilogue_endpoint(
    campaign_id: str, adventure_id: str, payload: dict,
    request: Request, db: Session = Depends(get_db),
):
    """Record an explicit decline for a PC (owner of the PC, or campaign owner)."""
    from app.adventures.epilogues import skip_epilogue

    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
    _adventure_or_404(db, cid, aid)
    body = payload or {}
    try:
        character_id = uuid_lib.UUID(str(body.get("character_id") or ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="character_id must be a UUID")
    try:
        row = skip_epilogue(db, cid, aid, user_id=profile.id, character_id=character_id)
    except Exception as exc:  # noqa: BLE001 — mapped to status codes below
        _epilogue_error_response(exc)
    return {"epilogue": row.to_dict()}


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/epilogues/close")
def close_epilogues_endpoint(campaign_id: str, adventure_id: str, request: Request, db: Session = Depends(get_db)):
    """Close the epilogue phase (owner/DM-only). Partial participation is fine."""
    from app.adventures.epilogues import close_epilogues

    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_owner(camp, profile)
    _adventure_or_404(db, cid, aid)
    try:
        stats = close_epilogues(db, cid, aid)
    except Exception as exc:  # noqa: BLE001 — mapped to status codes below
        _epilogue_error_response(exc)
    return {"stats": stats}


@router.get("/api/campaigns/{campaign_id}/adventures/{adventure_id}/epilogues")
def list_epilogues_endpoint(campaign_id: str, adventure_id: str, request: Request, db: Session = Depends(get_db)):
    """Visibility-filtered epilogue roster + participation stats (member-readable)."""
    from app.adventures.epilogues import epilogue_stats, list_epilogues

    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
    _adventure_or_404(db, cid, aid)
    entries = list_epilogues(
        db, aid, viewer_id=profile.id, is_owner=(camp.owner_id == profile.id)
    )
    return {"epilogues": entries, "stats": epilogue_stats(db, aid)}
