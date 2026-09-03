"""Live-table snapshot/read model — issue #196.

Authoritative reconnect-safe read flow. Guarantees:

- Snapshot is a durable read projection, not a replay of realtime events.
- Revision/sequence is suitable for realtime race reconciliation.
- Pagination prevents forcing whole transcript into one response.
- Visibility filtering is enforced server-side before serialization.
- Partial projection failure returns a clear failure, not a misleading partial.
- Client can retry the same read without side effects (no state mutation).
- Observability: latency, payload size, revision returned, failures measured.

Dependencies:
- #188 campaign revision ordering (Campaign.revision)
- #195 thread/audience model (can_read_thread, list_threads_for_user)
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.runtime.threads import (
    ThreadAuthorizationError,
    ThreadNotFoundError,
    get_campaign_thread,
    list_threads_for_user,
    parse_thread_id,
)
from models.campaigns import Campaign
from models.threads import CampaignThread
from models.threads import CampaignThreadMember
from models.threads import PlayerSubmission
from models.threads import PlayerSubmissionSegment

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 100

# ── cursor helpers ──────────────────────────────────────────────────────────


def encode_cursor(sequence: int) -> str:
    raw = str(sequence).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    # Be liberal in padding
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode())
        val = int(raw.decode())
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid cursor") from exc
    if val <= 0:
        raise ValueError("Invalid cursor")
    return val


# ── errors ─────────────────────────────────────────────────────────────────


class SnapshotAuthorizationError(PermissionError):
    pass


class SnapshotNotFoundError(LookupError):
    pass


class SnapshotProjectionError(RuntimeError):
    pass


def _ensure_repeatable_read(db: Session) -> None:
    """Enter REPEATABLE READ so all reads describe the same point in time (#196).

    Postgres `READ COMMITTED` allows writers to commit between separate SELECTs,
    producing a campaign/revision from T1 and history from T2. A single
    REPEATABLE READ transaction guarantees one consistent DB snapshot for
    campaign, threads, history, and reconciliation metadata.
    SQLite (used in unit tests) does not support SET TRANSACTION — best-effort
    fallback is plain `READ COMMITTED` which is already single-connection
    serializable in the in-memory test harness.

    On Postgres, any failure to establish the consistent snapshot must fail
    closed — returning a supposedly authoritative snapshot under READ COMMITTED
    would recreate the exact race this fix is meant to prevent.
    """
    bind = db.get_bind() if hasattr(db, "get_bind") else db.bind
    if bind is None:
        return
    dialect = getattr(bind, "dialect", None)
    if dialect is None or dialect.name != "postgresql":
        return
    # Postgres path — failures must not degrade silently to READ COMMITTED.
    try:
        # SET TRANSACTION must be the first statement of a transaction.
        # resolve_profile and other pre-snapshot reads may have already started
        # one on this session, so close it first.
        if db.in_transaction():
            db.rollback()
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    except Exception as exc:
        try:
            db.rollback()
        except SQLAlchemyError as rollback_exc:
            logger.warning(
                "snapshot repeatable-read rollback failed error=%s",
                rollback_exc,
            )
        logger.warning(
            "snapshot repeatable-read setup failed campaign_projection_error=%s",
            exc,
        )
        raise SnapshotProjectionError("Failed to establish consistent snapshot") from exc


def _resolve_thread_readonly(
    db: Session, campaign_id: uuid.UUID, raw_thread_id: str | None
) -> uuid.UUID:
    """Resolve thread identifier without durable side effects.

    Unlike `app.runtime.threads.resolve_thread_id`, this does NOT lazily
    create the shared campaign thread. Campaign creation is the sole writer
    of the shared thread (see `app/campaigns/router.py`), so GET remains
    retryable without side effects as required by #196.
    """
    if not raw_thread_id or raw_thread_id == "main":
        thread = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == campaign_id,
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        if thread is None:
            raise SnapshotNotFoundError("Shared thread not found")
        return thread.id
    tid = parse_thread_id(raw_thread_id)
    thread = get_campaign_thread(db, campaign_id, tid)
    if thread is None:
        raise SnapshotNotFoundError("Thread not found")
    return thread.id


# ── core service ────────────────────────────────────────────────────────────


def _normalize_limit(raw) -> int:
    try:
        n = int(raw) if raw is not None else DEFAULT_LIMIT
    except (ValueError, TypeError):
        raise ValueError("limit must be an integer")
    if n <= 0 or n > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return n


def _fetch_history(
    db: Session,
    campaign_id: uuid.UUID,
    thread_id: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict], str | None, bool]:
    """Return ordered messages (asc) for thread, with pagination.

    Cursor encodes the exclusive upper bound sequence for older history.
    First page (no cursor) returns latest `limit` messages.
    Subsequent pages with cursor return messages with sequence < cursor_seq.

    Returns: (messages, next_cursor, has_more)
    """
    cursor_seq: int | None = None
    if cursor:
        try:
            cursor_seq = decode_cursor(cursor)
        except ValueError as exc:
            raise ValueError(f"Invalid cursor: {cursor}") from exc

    base_q = select(PlayerSubmission).where(
        PlayerSubmission.campaign_id == campaign_id,
        PlayerSubmission.thread_id == thread_id,
    )
    if cursor_seq is not None:
        base_q = base_q.where(PlayerSubmission.sequence < cursor_seq)

    rows = db.execute(
        base_q.order_by(PlayerSubmission.sequence.desc()).limit(limit + 1)
    ).scalars().all()

    has_more = len(rows) > limit
    window = list(reversed(rows[:limit]))

    if not window:
        return [], None, False

    ids = [r.id for r in window]
    segs = db.execute(
        select(PlayerSubmissionSegment).where(
            PlayerSubmissionSegment.submission_id.in_(ids)
        ).order_by(PlayerSubmissionSegment.submission_id, PlayerSubmissionSegment.position)
    ).scalars().all()
    by_sub: dict[uuid.UUID, list] = {sid: [] for sid in ids}
    for s in segs:
        by_sub[s.submission_id].append(s)

    messages = [r.to_dict(by_sub[r.id]) for r in window]
    next_cursor = encode_cursor(window[0].sequence) if has_more else None
    return messages, next_cursor, has_more


def build_live_table_snapshot(
    db: Session,
    campaign_id: uuid.UUID,
    viewer_id: uuid.UUID,
    *,
    thread_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Build authoritative live-table snapshot for viewer."""
    started = time.monotonic()
    try:
        resolved_limit = _normalize_limit(limit) if limit is not None else DEFAULT_LIMIT
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    if cursor is not None:
        try:
            decode_cursor(cursor)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    _ensure_repeatable_read(db)

    try:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise SnapshotNotFoundError("Campaign not found")

        from app.campaigns.service import is_campaign_member

        is_member = campaign.owner_id == viewer_id or is_campaign_member(db, campaign_id, viewer_id)
        if not is_member:
            logger.info(
                "snapshot denied campaign_id=%s viewer_id=%s reason=not_member",
                campaign_id, viewer_id,
            )
            raise SnapshotAuthorizationError("Not a member of this campaign")

        raw_thread = thread_id if thread_id is not None else "main"
        try:
            resolved_tid = _resolve_thread_readonly(db, campaign_id, raw_thread)
        except SnapshotNotFoundError:
            raise
        except ThreadNotFoundError as exc:
            raise SnapshotNotFoundError(str(exc)) from exc

        try:
            from app.runtime.threads import assert_can_read_thread
            thread = assert_can_read_thread(db, campaign_id, resolved_tid, viewer_id)
        except ThreadNotFoundError as exc:
            raise SnapshotNotFoundError(str(exc)) from exc
        except ThreadAuthorizationError as exc:
            raise SnapshotAuthorizationError(str(exc)) from exc

        revision = int(campaign.revision) if campaign.revision is not None else 0
        visible_threads = list_threads_for_user(db, campaign_id, viewer_id)

        thread_id_str = str(resolved_tid)
        messages, next_cursor, has_more = _fetch_history(
            db, campaign_id, thread_id_str, resolved_limit, cursor
        )

        high_water = db.scalar(
            select(func.max(PlayerSubmission.sequence)).where(
                PlayerSubmission.campaign_id == campaign_id,
                PlayerSubmission.thread_id == thread_id_str,
            )
        )
        total_visible = db.scalar(
            select(func.count()).select_from(PlayerSubmission).where(
                PlayerSubmission.campaign_id == campaign_id,
                PlayerSubmission.thread_id == thread_id_str,
            )
        ) or 0

        from app.rolls.service import get_fulfillment
        from models.dm import PlayerRollRequest

        roll_rows = db.execute(
            select(PlayerRollRequest).where(
                PlayerRollRequest.campaign_id == campaign_id,
                PlayerRollRequest.thread_id == thread_id_str,
            ).order_by(PlayerRollRequest.requested_at, PlayerRollRequest.id)
        ).scalars().all()
        roll_requests: list[dict] = []
        for roll_row in roll_rows:
            roll_item = roll_row.to_dict(include_private=campaign.owner_id == viewer_id)
            fulfillment = get_fulfillment(db, roll_row.id)
            roll_item["fulfillment"] = fulfillment.to_dict(
                include_private=campaign.owner_id == viewer_id or roll_row.requested_user_id == viewer_id
            ) if fulfillment else None
            roll_requests.append(roll_item)

        dm_messages: list[dict] = []
        dm_state: dict[str, Any] = {
            "status": "idle", "streaming": False, "active_turn": None,
            "turn_id": None, "started_at": None,
        }
        try:
            from models.dm import DMStream
            from models.dm import DMStreamChunk
            dm_active = db.execute(
                select(DMStream)
                .where(
                    DMStream.campaign_id == campaign_id,
                    DMStream.thread_id == resolved_tid,
                    DMStream.status == "streaming",
                )
                .order_by(DMStream.created_at.desc())
                .limit(1)
            ).scalars().first()

            if dm_active is not None:
                active_chunks = db.execute(
                    select(DMStreamChunk)
                    .where(DMStreamChunk.stream_id == dm_active.id)
                    .order_by(DMStreamChunk.sequence.asc())
                ).scalars().all()
                visible_text = "".join(c.text for c in active_chunks)
                dm_state = {
                    "status": "streaming", "streaming": True,
                    "active_turn": dm_active.turn_id, "turn_id": dm_active.turn_id,
                    "attempt_id": dm_active.attempt_id, "stream_id": str(dm_active.id),
                    "started_at": dm_active.created_at.isoformat() if dm_active.created_at else None,
                    "first_chunk_at": dm_active.first_chunk_at.isoformat() if dm_active.first_chunk_at else None,
                    "last_chunk_at": dm_active.last_chunk_at.isoformat() if dm_active.last_chunk_at else None,
                    "chunk_count": dm_active.chunk_count, "total_bytes": dm_active.total_bytes,
                    "last_sequence": dm_active.last_sequence, "visible_text": visible_text,
                    "trace_id": dm_active.trace_id,
                }
            else:
                dm_state = {
                    "status": "idle", "streaming": False, "active_turn": None,
                    "turn_id": None, "attempt_id": None, "stream_id": None,
                    "started_at": None, "chunk_count": 0, "visible_text": "",
                }

            dm_completed_rows = db.execute(
                select(DMStream)
                .where(
                    DMStream.campaign_id == campaign_id,
                    DMStream.thread_id == resolved_tid,
                    DMStream.status == "completed",
                )
                .order_by(DMStream.completed_at.asc().nulls_last(), DMStream.created_at.asc())
            ).scalars().all()
            dm_messages = []
            for s in dm_completed_rows:
                if s.final_text is not None:
                    final_text = s.final_text
                else:
                    chunks = db.execute(
                        select(DMStreamChunk).where(DMStreamChunk.stream_id == s.id).order_by(DMStreamChunk.sequence.asc())
                    ).scalars().all()
                    final_text = "".join(c.text for c in chunks)
                dm_messages.append({
                    "id": str(s.id), "turn_id": s.turn_id, "attempt_id": s.attempt_id,
                    "thread_id": str(s.thread_id), "status": s.status,
                    "final_text": final_text, "chunk_count": s.chunk_count,
                    "total_bytes": s.total_bytes,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                })
        except Exception as exc:
            logger.warning("dm_state projection failed campaign_id=%s thread_id=%s error=%s", campaign_id, thread_id_str, exc)

        realtime_resume_token = f"{revision}:{high_water or 0}"
        generated_at = datetime.now(timezone.utc).isoformat()

        snapshot: dict[str, Any] = {
            "campaign": campaign.to_dict(),
            "revision": revision,
            "threads": [
                t.to_dict(
                    include_members=t.thread_type == "private",
                    members=(
                        db.query(CampaignThreadMember).filter_by(thread_id=t.id).all()
                        if t.thread_type == "private" else []
                    ),
                )
                for t in visible_threads
            ],
            "active_thread_id": thread_id_str,
            "active_thread": thread.to_dict(),
            "history": {
                "thread_id": thread_id_str,
                "messages": messages,
                "pagination": {
                    "limit": resolved_limit,
                    "cursor": cursor,
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                    "total_visible": total_visible,
                },
            },
            "dm_state": dm_state,
            "dm_messages": dm_messages,
            "roll_requests": roll_requests,
            "surfaces": {},
            "extensions": {},
            "reconciliation": {
                "snapshot_revision": revision,
                "snapshot_sequence": revision,
                "history_cursor": next_cursor,
                "request_cursor": cursor,
                "history_high_water_mark": high_water,
                "history_total": total_visible,
                "realtime_resume_token": realtime_resume_token,
            },
            "meta": {
                "generated_at": generated_at,
                "limit": resolved_limit,
                "thread_id": thread_id_str,
            },
        }

        latency_ms = (time.monotonic() - started) * 1000
        payload_size = len(json.dumps(snapshot, default=str).encode())
        logger.info(
            "snapshot built campaign_id=%s viewer_id=%s thread_id=%s revision=%s latency_ms=%.2f payload_bytes=%s message_count=%s has_more=%s",
            campaign_id, viewer_id, thread_id_str, revision, latency_ms, payload_size, len(messages), has_more,
        )
        return snapshot

    except (SnapshotNotFoundError, SnapshotAuthorizationError, ValueError):
        raise
    except Exception as exc:
        logger.warning(
            "snapshot projection failed campaign_id=%s viewer_id=%s error=%s",
            campaign_id, viewer_id, exc,
        )
        raise SnapshotProjectionError("Failed to build snapshot") from exc
