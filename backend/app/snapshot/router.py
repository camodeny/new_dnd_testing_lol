"""Live-table snapshot transport — issue #196."""

from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.campaigns.service import parse_campaign_id
from app.deps.auth import resolve_profile
from app.snapshot.service import (
    SnapshotAuthorizationError,
    SnapshotNotFoundError,
    SnapshotProjectionError,
    build_live_table_snapshot,
)
from database import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/campaigns/{campaign_id}/snapshot")
def get_live_table_snapshot(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    started = time.monotonic()
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")

    thread_id = request.query_params.get("thread_id")
    # Support both thread_id and threadId for compat
    if thread_id is None:
        thread_id = request.query_params.get("threadId")
    limit_raw = request.query_params.get("limit")
    cursor = request.query_params.get("cursor")
    if cursor is not None:
        cursor = cursor.strip() or None
    # Also support before/after legacy? No, just cursor
    # Support pagination via 'before' as alias for cursor if provided
    if cursor is None:
        cursor = request.query_params.get("before")

    # Parse limit
    limit_val: int | None = None
    if limit_raw is not None:
        try:
            limit_val = int(limit_raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="limit must be an integer")

    try:
        snapshot = build_live_table_snapshot(
            db,
            cid,
            profile.id,
            thread_id=thread_id,
            limit=limit_val,
            cursor=cursor,
        )
        # Read-only projection — no commit. Rollback any REPEATABLE READ
        # read-only transaction so the session is clean for reuse.
        try:
            if db.in_transaction():
                db.rollback()
        except SQLAlchemyError as exc:
            logger.warning(
                "snapshot read-only rollback failed campaign_id=%s viewer_id=%s error=%s",
                cid, profile.id, exc,
            )
    except SnapshotNotFoundError as exc:
        logger.info(
            "snapshot not_found campaign_id=%s viewer_id=%s thread_id=%s",
            cid, profile.id, thread_id,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SnapshotAuthorizationError as exc:
        logger.info(
            "snapshot denied campaign_id=%s viewer_id=%s reason=%s",
            cid, profile.id, str(exc),
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SnapshotProjectionError as exc:
        logger.warning(
            "snapshot projection failure campaign_id=%s viewer_id=%s error=%s",
            cid, profile.id, exc,
        )
        raise HTTPException(status_code=500, detail="Failed to build snapshot") from exc

    latency_ms = (time.monotonic() - started) * 1000
    payload_bytes = len(json.dumps(snapshot, default=str).encode())

    # Observability: log latency, payload size, revision returned
    logger.info(
        "snapshot served campaign_id=%s viewer_id=%s thread_id=%s revision=%s latency_ms=%.2f payload_bytes=%s has_more=%s",
        cid,
        profile.id,
        snapshot.get("active_thread_id"),
        snapshot.get("revision"),
        latency_ms,
        payload_bytes,
        snapshot.get("history", {}).get("pagination", {}).get("has_more"),
    )

    # Add reconciliation headers so realtime can resume without gap
    from fastapi.responses import JSONResponse

    response = JSONResponse(content=snapshot)
    response.headers["X-Snapshot-Revision"] = str(snapshot.get("revision", ""))
    response.headers["X-Realtime-Resume-Token"] = str(
        snapshot.get("reconciliation", {}).get("realtime_resume_token", "")
    )
    if snapshot.get("history", {}).get("pagination", {}).get("next_cursor"):
        response.headers["X-Next-Cursor"] = str(snapshot["history"]["pagination"]["next_cursor"])
    # Compact latency header for client observability
    response.headers["X-Snapshot-Latency-Ms"] = f"{latency_ms:.2f}"
    return response
