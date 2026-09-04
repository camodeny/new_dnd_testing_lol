"""Outbox relay cron endpoint — issue #331.

Serverless-safe trigger for the transactional outbox relay: Vercel Cron
(or any external scheduler) hits ``GET /api/cron/outbox-relay`` on a
schedule, which claims one batch via ``process_outbox_batch()`` and
publishes via ``envelope_for_outbox``. This is the production path that
keeps outbox rows from sitting ``pending`` forever on serverless, where
the lifespan background loop may not persist between invocations.

Auth: when ``CRON_SECRET`` is set (Vercel Cron sends
``Authorization: Bearer <CRON_SECRET>``), the secret is required.
When unset (local/dev/test), the endpoint is open.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["cron"])


def _require_cron_secret(authorization: str | None) -> None:
    expected = os.getenv("CRON_SECRET")
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/outbox-relay")
def outbox_relay_cron_get(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    _require_cron_secret(authorization)
    from app.outbox.relay import run_outbox_relay_once

    result = run_outbox_relay_once(db=db, claimed_by=os.getenv("OUTBOX_RELAY_CLAIMED_BY", "relay-cron"))
    logger.info(
        "outbox relay cron claimed=%s succeeded=%s failed=%s",
        result.get("claimed"),
        result.get("succeeded"),
        result.get("failed"),
    )
    return {"ok": True, "relay": result}


@router.post("/outbox-relay")
def outbox_relay_cron_post(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    return outbox_relay_cron_get(db=db, authorization=authorization)
