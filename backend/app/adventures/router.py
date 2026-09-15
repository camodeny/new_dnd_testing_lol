"""Adventure-closing cron endpoint — issue #260.

Follows the post-turn cron pattern: an external scheduler drives pending
adventure closing work (recap/reward follow-ups) through the idempotent
worker fence. Same CRON_SECRET auth guard as the other cron endpoints.
Best-effort by design — failures never invalidate committed completions.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.params import Depends
from sqlalchemy.orm import Session

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["cron"])


@router.get("/adventure-closing")
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


@router.post("/adventure-closing")
def adventure_closing_cron_post(request: Request, db: Session = Depends(get_db)):
    return adventure_closing_cron_get(request=request, db=db)
