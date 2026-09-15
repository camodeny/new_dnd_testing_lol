"""Post-turn cron endpoints — issue #216.

Follows the dm-execute cron pattern: an external scheduler (Supabase Cron
or equivalent) drives pending post-turn work through the worker layer.
Same CRON_SECRET auth guard as the outbox relay cron.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.params import Depends
from sqlalchemy.orm import Session

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["cron"])


@router.get("/post-turn")
def post_turn_cron_get(request: Request, db: Session = Depends(get_db)):
    """Execute outstanding post-turn runs via the idempotent worker fence."""
    from app.outbox.router import _require_cron_secret

    _require_cron_secret(request.headers.get("authorization"))
    from app.post_turn.service import run_post_turn_sweep

    result = run_post_turn_sweep(db)
    logger.info(
        "post_turn cron executed=%s failed=%s",
        len(result.get("executed", [])),
        len(result.get("failed", [])),
    )
    return {"ok": True, "sweep": result}


@router.post("/post-turn")
def post_turn_cron_post(request: Request, db: Session = Depends(get_db)):
    return post_turn_cron_get(request=request, db=db)
