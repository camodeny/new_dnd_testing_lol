"""Realtime HTTP API — issue #198.

- Private channel authorization check for Supabase Realtime subscriptions.
- Metrics / observability for publish failures, reconciliations, etc.
- Realtime outage never blocks authoritative writes — this router is read-only
  for subscription auth and metrics; it does not participate in submission
  acceptance transactions.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.campaigns.service import is_campaign_member, parse_campaign_id
from app.deps.auth import resolve_profile
from app.realtime.channels import live_table_channel, parse_live_table_channel
from app.realtime.service import get_realtime_metrics
from app.runtime.threads import ThreadAuthorizationError, ThreadNotFoundError, assert_can_read_thread, parse_thread_id
from database import get_db
from models.campaigns import Campaign

router = APIRouter()
logger = logging.getLogger(__name__)


def _authorized_campaign(db: Session, campaign_id: str, user_id: uuid.UUID) -> Campaign:
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid campaign id") from exc
    campaign = db.get(Campaign, cid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != user_id and not is_campaign_member(db, cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    return campaign


@router.get("/api/campaigns/{campaign_id}/realtime/channels")
def list_realtime_channels(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    """Return private realtime channels the caller is authorized to subscribe to.

    This is the audience-safe listing — private threads are hidden unless the
    caller is an explicit member. The returned channel names can be used with
    Supabase Realtime (`supabase.channel(name).subscribe()`).
    """
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    from app.runtime.threads import list_threads_for_user

    threads = list_threads_for_user(db, campaign.id, profile.id)
    channels = [
        {
            "thread_id": str(t.id),
            "thread_type": t.thread_type,
            "channel": live_table_channel(campaign.id, t.id),
        }
        for t in threads
    ]
    # Also include campaign revision for reconciliation convenience
    revision = int(campaign.revision) if campaign.revision is not None else 0
    return {"channels": channels, "revision": revision, "campaign_id": str(campaign.id)}


@router.post("/api/campaigns/{campaign_id}/realtime/authorize")
def authorize_realtime_channel(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    """Authorize a specific realtime channel subscription.

    Payload: {"channel": "live-table:campaign:<cid>:thread:<tid>"}
    or {"thread_id": "<uuid>"} for convenience.

    Returns 200 if authorized, 403 if campaign member but not thread member,
    404 if campaign/thread not found or private thread hidden.
    """
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)

    channel = payload.get("channel") if isinstance(payload, dict) else None
    thread_id_raw = payload.get("thread_id") if isinstance(payload, dict) else None

    tid: uuid.UUID | None = None
    if thread_id_raw:
        try:
            tid = parse_thread_id(str(thread_id_raw))
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
    elif channel:
        parsed = parse_live_table_channel(str(channel))
        if parsed is None:
            raise HTTPException(status_code=422, detail="Invalid channel format")
        cid_parsed, tid_parsed = parsed
        if str(cid_parsed) != str(campaign.id):
            raise HTTPException(status_code=403, detail="Channel does not belong to this campaign")
        tid = tid_parsed
    else:
        raise HTTPException(status_code=422, detail="channel or thread_id is required")

    assert tid is not None
    try:
        assert_can_read_thread(db, campaign.id, tid, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    authorized_channel = live_table_channel(campaign.id, tid)
    logger.info("realtime channel authorized campaign_id=%s thread_id=%s user_id=%s channel=%s", campaign.id, tid, profile.id, authorized_channel)
    # Count as subscription attempt for observability (accepted)
    from app.realtime.service import _inc

    _inc("subscription_count")
    return {"authorized": True, "channel": authorized_channel, "thread_id": str(tid)}


@router.get("/api/realtime/metrics")
def get_metrics(request: Request, db: Session = Depends(get_db)):
    """Observability: publish failures, subscription/reconnect counts, etc."""
    # Require auth but not campaign membership — allow global view for authenticated users.
    resolve_profile(request, db)
    return get_realtime_metrics()


@router.get("/api/campaigns/{campaign_id}/realtime/token")
def get_realtime_token(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    """Convenience: return channel + resume token for snapshot→subscribe flow.

    Client flow (race-safe):
      1. subscribe to channels (buffer events)
      2. fetch snapshot (get revision + high water)
      3. reconcile buffered events with snapshot (drop already-in-snapshot)

    This endpoint returns the expected channels plus the current snapshot's
    realtime_resume_token so the client can detect gaps.
    """
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    thread_id_raw = request.query_params.get("thread_id")
    from app.snapshot.service import build_live_table_snapshot

    try:
        snapshot = build_live_table_snapshot(
            db,
            campaign.id,
            profile.id,
            thread_id=thread_id_raw,
            limit=1,
        )
        # rollback read-only tx
        try:
            if db.in_transaction():
                db.rollback()
        except SQLAlchemyError as exc:
            logger.warning(
                "realtime token read-only rollback failed campaign_id=%s error=%s",
                campaign.id,
                exc,
            )
    except Exception as exc:
        from app.snapshot.service import SnapshotNotFoundError, SnapshotAuthorizationError

        if isinstance(exc, SnapshotNotFoundError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, SnapshotAuthorizationError):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail="Failed to build snapshot") from exc

    active_tid = snapshot.get("active_thread_id")
    channels = []
    if active_tid:
        try:
            tid_uuid = uuid.UUID(str(active_tid))
            # only if authorized
            assert_can_read_thread(db, campaign.id, tid_uuid, profile.id)
            channels.append(live_table_channel(campaign.id, tid_uuid))
        except (ValueError, ThreadNotFoundError, ThreadAuthorizationError) as exc:
            logger.info(
                "realtime token channel omitted campaign_id=%s thread_id=%s reason=%s",
                campaign.id,
                active_tid,
                type(exc).__name__,
            )

    return {
        "channels": channels,
        "active_thread_id": active_tid,
        "revision": snapshot.get("revision"),
        "realtime_resume_token": snapshot.get("reconciliation", {}).get("realtime_resume_token"),
        "snapshot": {
            "revision": snapshot.get("revision"),
            "reconciliation": snapshot.get("reconciliation"),
        },
    }
