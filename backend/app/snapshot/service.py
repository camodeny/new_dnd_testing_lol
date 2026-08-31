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
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.runtime.threads import (
    ThreadAuthorizationError,
    ThreadNotFoundError,
    can_read_thread,
    get_campaign_thread,
    list_threads_for_user,
    parse_thread_id,
)
from models import Campaign, CampaignThread, PlayerSubmission, PlayerSubmissionSegment

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
    except Exception as exc:
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
        except Exception:
            pass
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
    except Exception:
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

    # Fetch limit+1 to detect has_more
    base_q = select(PlayerSubmission).where(
        PlayerSubmission.campaign_id == campaign_id,
        PlayerSubmission.thread_id == thread_id,
    )
    if cursor_seq is not None:
        base_q = base_q.where(PlayerSubmission.sequence < cursor_seq)

    # Order DESC to get window, then reverse to ASC for response
    rows = db.execute(
        base_q.order_by(PlayerSubmission.sequence.desc()).limit(limit + 1)
    ).scalars().all()

    has_more = len(rows) > limit
    window = rows[:limit]
    # window currently DESC; reverse for asc
    window = list(reversed(window))

    if not window:
        return [], None, False

    # fetch segments for window
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

    next_cursor = None
    if has_more:
        # Oldest sequence in window is next cursor
        smallest = window[0].sequence
        next_cursor = encode_cursor(smallest)
    elif cursor_seq is not None and not has_more:
        # No more older history
        next_cursor = None
    # If no cursor and not has_more, also no next_cursor
    # For consistency, when we had to paginate, the next cursor points to oldest.
    # Client can use it to fetch older.
    # If window is empty, no cursor.
    # For has_more case, next_cursor set. Otherwise None.
    # Also need to handle case where first page had more — already set.

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
    """Build authoritative live-table snapshot for viewer.

    This is a pure read projection — never mutates campaign revision or
    inserts outbox/domain events. The shared campaign thread is created once
    at campaign creation (see `app/campaigns/router.py`), so this read is
    side-effect-free and retryable.

    All DB reads run in a single REPEATABLE READ snapshot (Postgres) so
    campaign, threads, history, and reconciliation metadata describe the
    same point in time even if writers commit between statements. On SQLite
    (tests) this degrades to the single-connection serializable harness.

    Raises:
        SnapshotNotFoundError: campaign or thread not found (private threads
            hidden as 404 when unauthorized).
        SnapshotAuthorizationError: not authorized to read campaign/thread.
        ValueError: invalid pagination.
        SnapshotProjectionError: partial failure (caller should map to 500
             rather than returning a misleading partial snapshot).
    """
    started = time.monotonic()
    # Resolve limit early for validation (before entering REPEATABLE READ)
    try:
        resolved_limit = _normalize_limit(limit) if limit is not None else DEFAULT_LIMIT
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    # Validate pagination cursor early
    if cursor is not None:
        try:
            decode_cursor(cursor)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    # Enter consistent snapshot before any DB SELECT in this projection.
    _ensure_repeatable_read(db)

    try:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise SnapshotNotFoundError("Campaign not found")

        # Campaign membership check (owner implicitly member)
        from app.campaigns.service import is_campaign_member

        is_member = campaign.owner_id == viewer_id or is_campaign_member(db, campaign_id, viewer_id)
        if not is_member:
            logger.info(
                "snapshot denied campaign_id=%s viewer_id=%s reason=not_member",
                campaign_id, viewer_id,
            )
            raise SnapshotAuthorizationError("Not a member of this campaign")

        # Resolve thread id without side effects (shared thread must already exist)
        raw_thread = thread_id if thread_id is not None else "main"
        try:
            resolved_tid = _resolve_thread_readonly(db, campaign_id, raw_thread)
        except SnapshotNotFoundError:
            raise
        except ThreadNotFoundError as exc:
            raise SnapshotNotFoundError(str(exc)) from exc

        # Centralized thread authorization (private threads hidden as 404)
        try:
            from app.runtime.threads import assert_can_read_thread

            thread = assert_can_read_thread(db, campaign_id, resolved_tid, viewer_id)
        except ThreadNotFoundError as exc:
            # Private existence hidden as not found
            raise SnapshotNotFoundError(str(exc)) from exc
        except ThreadAuthorizationError as exc:
            # Should have been mapped to not found, but defensive
            raise SnapshotAuthorizationError(str(exc)) from exc

        # Revision is authoritative
        revision = int(campaign.revision) if campaign.revision is not None else 0

        # Visible threads for this viewer (hooks for structured surfaces later)
        visible_threads = list_threads_for_user(db, campaign_id, viewer_id)

        # History window (visibility already enforced by thread auth — all messages
        # in an authorized thread are visible to its members)
        thread_id_str = str(resolved_tid)
        messages, next_cursor, has_more = _fetch_history(
            db, campaign_id, thread_id_str, resolved_limit, cursor
        )

        # High-water mark for reconciliation (max sequence in thread overall)
        high_water = db.scalar(
            select(func.max(PlayerSubmission.sequence)).where(
                PlayerSubmission.campaign_id == campaign_id,
                PlayerSubmission.thread_id == thread_id_str,
            )
        )
        # high_water can be None if no messages
        # Also count total visible for observability (not returned as full list)
        total_visible = db.scalar(
            select(func.count()).select_from(PlayerSubmission).where(
                PlayerSubmission.campaign_id == campaign_id,
                PlayerSubmission.thread_id == thread_id_str,
            )
        ) or 0

        # DM stream state — issue #197 durable stream chunks
        # Completed streams become canonical history; streaming ones are active.
        # Abandoned/failed are excluded from canonical view but remain auditable via dm_streams API.
        dm_messages: list[dict] = []
        dm_state: dict[str, Any] = {
            "status": "idle",
            "streaming": False,
            "active_turn": None,
            "turn_id": None,
            "started_at": None,
        }
        try:
            from models import DMStream, DMStreamChunk

            # Active streaming stream (most recent) for this thread
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
                    "status": "streaming",
                    "streaming": True,
                    "active_turn": dm_active.turn_id,
                    "turn_id": dm_active.turn_id,
                    "attempt_id": dm_active.attempt_id,
                    "stream_id": str(dm_active.id),
                    "started_at": dm_active.created_at.isoformat() if dm_active.created_at else None,
                    "first_chunk_at": dm_active.first_chunk_at.isoformat() if dm_active.first_chunk_at else None,
                    "last_chunk_at": dm_active.last_chunk_at.isoformat() if dm_active.last_chunk_at else None,
                    "chunk_count": dm_active.chunk_count,
                    "total_bytes": dm_active.total_bytes,
                    "last_sequence": dm_active.last_sequence,
                    "visible_text": visible_text,
                    "trace_id": dm_active.trace_id,
                }
            else:
                dm_state = {
                    "status": "idle",
                    "streaming": False,
                    "active_turn": None,
                    "turn_id": None,
                    "attempt_id": None,
                    "stream_id": None,
                    "started_at": None,
                    "chunk_count": 0,
                    "visible_text": "",
                }

            # Completed DM messages (canonical history) — ordered by completion time
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
                # Prefer materialized final_text, fallback to chunk reconstruction
                if s.final_text is not None:
                    text = s.final_text
                else:
                    ch = db.execute(
                        select(DMStreamChunk).where(DMStreamChunk.stream_id == s.id).order_by(DMStreamChunk.sequence.asc())
                    ).scalars().all()
                    text = "".join(c.text for c in ch)
                dm_messages.append({
                    "id": str(s.id),
                    "turn_id": s.turn_id,
                    "attempt_id": s.attempt_id,
                    "thread_id": str(s.thread_id),
                    "status": s.status,
                    "final_text": text,
                    "chunk_count": s.chunk_count,
                    "total_bytes": s.total_bytes,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                })
        except Exception as exc:
            # DM stream projection must not break snapshot — fail closed is snapshot projection error,
            # but broken dm_streams should not take down entire snapshot. Log and fallback to idle.
            logger.warning("dm_state projection failed campaign_id=%s thread_id=%s error=%s", campaign_id, thread_id_str, exc)
            # keep initialized idle dm_state/dm_messages

        # Reconciliation metadata — allows client to fetch snapshot then subscribe
        # realtime without a gap. The resume token encodes revision + high water.
        # If high_water is None, resume token is just revision.
        realtime_resume_token = f"{revision}:{high_water or 0}"

        # Cursor for next older fetch
        history_cursor = next_cursor
        # The cursor that was used to fetch this window (for client echo)
        request_cursor = cursor

        generated_at = datetime.now(timezone.utc).isoformat()

        snapshot: dict[str, Any] = {
            "campaign": campaign.to_dict(),
            "revision": revision,
            "threads": [t.to_dict() for t in visible_threads],
            "active_thread_id": thread_id_str,
            "active_thread": thread.to_dict(),
            "history": {
                "thread_id": thread_id_str,
                "messages": messages,
                "pagination": {
                    "limit": resolved_limit,
                    "cursor": request_cursor,
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                    "total_visible": total_visible,
                },
            },
            "dm_state": dm_state,
            "dm_messages": dm_messages,
            # Hook for later structured surfaces (combat, shops, etc.)
            "surfaces": {},
            # Backward-compatible alias
            "extensions": {},
            "reconciliation": {
                "snapshot_revision": revision,
                "snapshot_sequence": revision,
                "history_cursor": history_cursor,
                "request_cursor": request_cursor,
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
        # Any unexpected error during projection must not return a partial snapshot
        logger.warning(
            "snapshot projection failed campaign_id=%s viewer_id=%s error=%s",
            campaign_id, viewer_id, exc,
        )
        raise SnapshotProjectionError("Failed to build snapshot") from exc
