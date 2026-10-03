"""Realtime HTTP API — issue #198.

- Channel discovery + private channel authorization check for Supabase
  Realtime subscriptions.
- Realtime outage never blocks authoritative writes — this router is read-only
  for subscription auth; it does not participate in submission acceptance
  transactions.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps.campaign import campaign_for
from app.deps.auth import current_profile
from app.realtime.channels import live_table_channel, parse_live_table_channel
from app.threads.service import ThreadAuthorizationError, ThreadNotFoundError, assert_can_read_thread, parse_thread_id
from database import get_db
from models.campaigns import Campaign

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/campaigns/{campaign_id}/realtime/channels")
def list_realtime_channels(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    """Return private realtime channels the caller is authorized to subscribe to.

    This is the audience-safe listing — private threads are hidden unless the
    caller is an explicit member. The returned channel names can be used with
    Supabase Realtime (`supabase.channel(name).subscribe()`).
    """
    from app.threads.service import list_threads_for_user

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
def authorize_realtime_channel(
    payload: dict,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    """Authorize a specific realtime channel subscription.

    Payload: {"channel": "live-table:campaign:<cid>:thread:<tid>"}
    or {"thread_id": "<uuid>"} for convenience.

    Returns 200 if authorized, 403 if campaign member but not thread member,
    404 if campaign/thread not found or private thread hidden.
    """
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
