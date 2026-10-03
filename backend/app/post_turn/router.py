"""Post-turn cron endpoints — issue #216.

Follows the dm-execute cron pattern: an external scheduler (Supabase Cron
or equivalent) drives pending post-turn work through the worker layer, and
drains pending world semantic index rows on the same tick.
Shared CRON_SECRET auth guard (``app.deps.cron``).
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
    """Execute outstanding post-turn runs, then pending semantic index rows.

    Semantic indexing is derived work: its failure never fails the sweep.
    """
    from app.deps.cron import require_cron_secret

    require_cron_secret(request.headers.get("authorization"))
    from app.post_turn.service import run_post_turn_sweep
    from app.world.semantic_index import run_semantic_index_sweep

    result = run_post_turn_sweep(db)
    logger.info(
        "post_turn cron executed=%s failed=%s",
        len(result.get("executed", [])),
        len(result.get("failed", [])),
    )
    try:
        semantic = run_semantic_index_sweep(db)
    except Exception as exc:  # noqa: BLE001 — derived index work is best-effort
        db.rollback()
        logger.warning("semantic index sweep failed error=%s", exc)
        semantic = {"indexed": [], "failed": [{"error": str(exc)[:300]}]}
    logger.info(
        "semantic index cron indexed=%s failed=%s",
        len(semantic["indexed"]),
        len(semantic["failed"]),
    )
    return {"ok": True, "sweep": result, "semantic_index": semantic}


@router.post("/post-turn")
def post_turn_cron_post(request: Request, db: Session = Depends(get_db)):
    return post_turn_cron_get(request=request, db=db)
