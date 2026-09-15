"""Adventures transport — APIRouter. Issue #263."""

from __future__ import annotations

import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adventures.service import (
    AdventureError,
    complete_adventure,
    create_adventure,
    generate_summary,
    mark_stale,
    project_recap,
)
from app.campaigns.events import RevisionConflictError
from app.campaigns.service import is_campaign_member, parse_campaign_id
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from database import get_db
from models.adventures import Adventure, AdventureSummary
from models.campaigns import Campaign

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


@router.post("/api/campaigns/{campaign_id}/adventures")
def open_adventure(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    cid, _ = _parse_ids(campaign_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
    body = payload or {}
    raw_start = body.get("start_sequence")
    try:
        start_sequence = int(raw_start) if raw_start is not None else int(camp.revision or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="start_sequence must be an integer")
    if start_sequence < 0:
        raise HTTPException(status_code=400, detail="start_sequence must be non-negative")
    try:
        adv = create_adventure(
            db, cid,
            title=body.get("title", "Untitled Adventure"),
            start_sequence=start_sequence,
            idempotency_key=str((payload or {}).get("operation_id") or "").strip() or None,
            extra={"opened_by": str(profile.id)},
        )
    except AdventureError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"adventure": adv.to_dict()}


@router.get("/api/campaigns/{campaign_id}/adventures")
def list_adventures(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    cid, _ = _parse_ids(campaign_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
    rows = db.execute(
        select(Adventure).where(Adventure.campaign_id == cid).order_by(Adventure.created_at.asc())
    ).scalars().all()
    return {"adventures": [a.to_dict() for a in rows]}


@router.post("/api/campaigns/{campaign_id}/adventures/{adventure_id}/complete")
def complete_adventure_endpoint(
    campaign_id: str, adventure_id: str, payload: dict,
    request: Request, response: Response, db: Session = Depends(get_db),
):
    """DM-declared completion. Derived summary failure never blocks this."""
    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
    adv = _adventure_or_404(db, cid, aid)
    if adv.status == "completed":
        row = db.execute(
            select(AdventureSummary).where(AdventureSummary.adventure_id == adv.id)
        ).scalars().first()
        return {"adventure": adv.to_dict(), "summary": row.to_dict() if row else None, "idempotent": True}
    body = payload or {}
    if "expected_revision" not in body:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        expected = int(body["expected_revision"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    operation_id = str(body.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _execute():
        try:
            completed, event = complete_adventure(
                db, cid, aid,
                outcome=str(body.get("outcome") or ""),
                outcome_reason=body.get("outcome_reason"),
                expected_revision=expected,
                operation_id=operation_id or idempotency_key,
                actor_id=profile.id,
                source_turn_id=_opt_uuid(body.get("source_turn_id")),
                source_attempt_id=_opt_uuid(body.get("source_attempt_id")),
                commit=False,
            )
        except AdventureError as exc:
            msg = str(exc)
            if msg == "Adventure not found":
                raise HTTPException(status_code=404, detail=msg)
            raise HTTPException(status_code=400, detail=msg)
        row = db.execute(
            select(AdventureSummary).where(AdventureSummary.adventure_id == completed.id)
        ).scalars().first()
        return {
            "adventure": completed.to_dict(),
            "event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
            "summary": row.to_dict() if row else None,
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


def _opt_uuid(raw) -> uuid_lib.UUID | None:
    if not raw:
        return None
    try:
        return uuid_lib.UUID(str(raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid source id")


@router.get("/api/campaigns/{campaign_id}/adventures/{adventure_id}/summary")
def get_summary(campaign_id: str, adventure_id: str, request: Request, db: Session = Depends(get_db)):
    """Durable historical summary (derived; events/facts outrank it)."""
    profile = resolve_profile(request, db)
    cid, aid = _parse_ids(campaign_id, adventure_id)
    camp = _campaign_or_404(db, cid)
    _require_member(db, camp, profile)
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
    _require_member(db, camp, profile)
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
    _require_member(db, camp, profile)
    adv = _adventure_or_404(db, cid, aid)
    body = payload or {}
    try:
        row = mark_stale(db, adv.id, reason=str(body.get("reason") or "repair/retcon"))
    except AdventureError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if body.get("regenerate"):
        row = generate_summary(db, adv, actor_id=profile.id)
    return {"adventure": adv.to_dict(), "summary": row.to_dict()}
