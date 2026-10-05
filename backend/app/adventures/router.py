"""Adventures transport — APIRouter.

- GET/POST .../adventures — list (member-readable) / open (issue #260).
- GET .../recap — visibility-filtered player recap (member-readable).
- POST .../summaries/generate + .../summaries/mark-stale — retry/repair.
- .../epilogues/* — optional player epilogues (issue #262).
- /api/cron/adventure-closing — best-effort closing sweep (issue #260).

The AI is the only DM: it alone completes adventures (``complete_adventure``
staged effect). The routes here are host actions (open/continue, epilogue
phase, summary repair) gated to the campaign owner; list/recap stay
member-readable.
"""

from __future__ import annotations

import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adventures.service import (
    AdventureAlreadyActiveError,
    get_current_adventure,
    list_adventures,
)
from app.adventures.summaries import (
    AdventureError,
    generate_summary,
    mark_stale,
    project_recap,
)
from app.campaigns.events import RevisionConflictError
from app.campaigns.service import CampaignArchivedError
from app.deps.auth import current_profile
from app.deps.campaign import campaign_for, parse_uuid_or_404, run_campaign_command
from app.deps.cron import require_cron_secret
from app.deps.idempotency import command_keys
from database import get_db
from models.campaigns import Adventure, AdventureEpilogue, AdventureSummary, Campaign

router = APIRouter()
logger = logging.getLogger(__name__)

#: Member-readable adventure routes.
adventure_reader = campaign_for("participant")
#: Host actions (open/continue, epilogue phase, summary repair) are
#: owner-only; ordinary members read the recap projection. No human
#: completes adventures or chooses outcomes — that is the AI DM's effect.
adventure_owner = campaign_for("owner", forbidden="Only the campaign owner can perform this action")

_ADVENTURE_OUTCOME_ERROR = (
    "outcome must be one of victory, failure, retreat, capture, death, tpk, villain_victory"
)


def _adventure_or_404(db: Session, campaign: Campaign, adventure_id: str) -> Adventure:
    aid = parse_uuid_or_404(adventure_id, "Invalid adventure id")
    adv = db.get(Adventure, aid)
    if adv is None or adv.campaign_id != campaign.id:
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


@router.get("/api/campaigns/{campaign_id}/adventures")
def list_adventures_endpoint(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("participant", forbidden="Not a campaign member")),
    db: Session = Depends(get_db),
):
    # Only the player-visible summary leaves the table; the DM's reason,
    # metadata, provenance ids, and closing bookkeeping never do — the owner
    # is a player too (issue #260 security, #470).
    current = get_current_adventure(db, campaign.id)
    return {
        "campaign_id": str(campaign.id),
        "campaign_status": campaign.status,
        "current_adventure_id": str(current.id) if current else None,
        "adventures": [a.to_public_dict() for a in list_adventures(db, campaign.id)],
    }


@router.post("/api/campaigns/{campaign_id}/adventures")
def start_adventure_endpoint(
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only the campaign owner can manage adventures")),
    db: Session = Depends(get_db),
):
    from app.adventures import service

    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata must be an object")
    _, idempotency_key = command_keys(request, payload)
    # Source-range boundary for derived summaries (issue #263): an explicit
    # start_sequence stays inclusive; the default is derived inside
    # start_adventure from the LOCKED campaign revision, never from the
    # pre-lock read above.
    raw_start = payload.get("start_sequence")
    start_sequence = None
    if raw_start is not None:
        try:
            start_sequence = int(raw_start)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="start_sequence must be an integer")
        if start_sequence < 0:
            raise HTTPException(status_code=400, detail="start_sequence must be non-negative")

    def _execute():
        try:
            adventure = service.start_adventure(
                db, campaign.id, title, adventure_metadata=metadata,
                start_sequence=start_sequence, commit=False,
            )
        except (AdventureAlreadyActiveError, CampaignArchivedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"adventure": adventure.to_dict(), "campaign_status": campaign.status}

    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="adventure.start", scope_type="campaign", scope_id=campaign.id,
        payload=payload, execute=_execute,
    )


@router.get("/api/campaigns/{campaign_id}/adventures/{adventure_id}/recap")
def get_recap(
    adventure_id: str,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_reader),
    db: Session = Depends(get_db),
):
    """Review Adventure — player-facing recap projection (visibility-filtered).

    Available after continuation/archive as long as the viewer is authorized
    (campaign member); records a view.
    """
    adv = _adventure_or_404(db, camp, adventure_id)
    row = _summary_or_404(db, adv)
    projected = project_recap(db, adv, row, viewer_id=profile.id)
    db.commit()
    return projected


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/summaries/generate")
def retry_generate(
    adventure_id: str,
    payload: dict,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_owner),
    db: Session = Depends(get_db),
):
    """Async-style retry/regeneration of derived work (never blocks completion)."""
    adv = _adventure_or_404(db, camp, adventure_id)
    body = payload or {}
    row = generate_summary(
        db, adv, actor_id=profile.id,
        force_fail=bool(body.get("force_fail")),
    )
    return {"adventure": adv.to_dict(), "summary": row.to_dict()}


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/summaries/mark-stale")
def mark_stale_endpoint(
    adventure_id: str,
    payload: dict,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_owner),
    db: Session = Depends(get_db),
):
    """Repair/retcon hook: mark the derived artifact stale so it rebuilds."""
    adv = _adventure_or_404(db, camp, adventure_id)
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
    require_cron_secret(request.headers.get("authorization"))
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
def open_epilogues_endpoint(
    adventure_id: str,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_owner),
    db: Session = Depends(get_db),
):
    """Open the optional epilogue phase for a completed adventure (owner host action)."""
    from app.adventures.epilogues import epilogue_stats, open_epilogues

    cid = camp.id
    adv = _adventure_or_404(db, camp, adventure_id)
    aid = adv.id
    try:
        adv = open_epilogues(db, cid, aid)
        db.refresh(adv)
    except Exception as exc:  # noqa: BLE001 — mapped to status codes below
        _epilogue_error_response(exc)
    return {"adventure": adv.to_dict(), "stats": epilogue_stats(db, aid)}


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/epilogues/submit")
def submit_epilogue_endpoint(
    adventure_id: str,
    payload: dict,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_reader),
    db: Session = Depends(get_db),
):
    """Submit the caller's voluntary epilogue choice for their own PC.

    Body: character_id, content, visibility (public|private),
    needs_adjudication + roll_spec ({roll_kind, ability_or_skill, label, dc})
    for mechanically uncertain actions, optional operation_id for idempotent
    retry, and required expected_revision (canonical fictional mutation).
    """
    from app.adventures.epilogues import submit_epilogue

    cid = camp.id
    adv = _adventure_or_404(db, camp, adventure_id)
    aid = adv.id
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
    adventure_id: str,
    epilogue_id: str,
    payload: dict,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_reader),
    db: Session = Depends(get_db),
):
    """Fulfill the caller's human roll for their PC's adjudicated epilogue.

    Body: die_value (1–20), modifier (-10…+30), required expected_revision.
    Code-owned arithmetic decides success; the outcome commits canonically.
    """
    from app.adventures.epilogues import fulfill_epilogue_roll

    cid = camp.id
    adv = _adventure_or_404(db, camp, adventure_id)
    aid = adv.id
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
    adventure_id: str,
    payload: dict,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_reader),
    db: Session = Depends(get_db),
):
    """Record an explicit decline for a PC (owner of the PC, or campaign owner)."""
    from app.adventures.epilogues import skip_epilogue

    cid = camp.id
    adv = _adventure_or_404(db, camp, adventure_id)
    aid = adv.id
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
def close_epilogues_endpoint(
    adventure_id: str,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_owner),
    db: Session = Depends(get_db),
):
    """Close the epilogue phase (owner host action). Partial participation is fine."""
    from app.adventures.epilogues import close_epilogues

    cid = camp.id
    adv = _adventure_or_404(db, camp, adventure_id)
    aid = adv.id
    try:
        stats = close_epilogues(db, cid, aid)
    except Exception as exc:  # noqa: BLE001 — mapped to status codes below
        _epilogue_error_response(exc)
    return {"stats": stats}


@router.get("/api/campaigns/{campaign_id}/adventures/{adventure_id}/epilogues")
def list_epilogues_endpoint(
    adventure_id: str,
    profile=Depends(current_profile),
    camp: Campaign = Depends(adventure_reader),
    db: Session = Depends(get_db),
):
    """Visibility-filtered epilogue roster + participation stats (member-readable)."""
    from app.adventures.epilogues import epilogue_stats, list_epilogues

    adv = _adventure_or_404(db, camp, adventure_id)
    aid = adv.id
    entries = list_epilogues(db, aid, viewer_id=profile.id)
    return {"epilogues": entries, "stats": epilogue_stats(db, aid)}
