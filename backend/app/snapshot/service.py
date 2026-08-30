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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.runtime.threads import (
    ThreadAuthorizationError,
    ThreadNotFoundError,
    can_read_thread,
    get_campaign_thread,
    get_or_create_campaign_thread,
    list_threads_for_user,
    parse_thread_id,
    resolve_thread_id,
)
from models import Campaign, PlayerSubmission, PlayerSubmissionSegment

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
    inserts outbox/domain events. Thread creation for the shared thread is
    the only durable write, committed by the caller (router) if needed.

    Raises:
        SnapshotNotFoundError: campaign or thread not found (private threads
            hidden as 404 when unauthorized).
        SnapshotAuthorizationError: not authorized to read campaign/thread.
        ValueError: invalid pagination.
        SnapshotProjectionError: partial failure (caller should map to 500
            rather than returning a misleading partial snapshot).
    """
    started = time.monotonic()
    # Resolve limit early for validation
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

        # Resolve thread id (durably, but don't commit here)
        raw_thread = thread_id if thread_id is not None else "main"
        try:
            resolved_tid = resolve_thread_id(db, campaign_id, raw_thread, created_by=viewer_id)
        except ThreadNotFoundError as exc:
            raise SnapshotNotFoundError(str(exc)) from exc
        except ThreadAuthorizationError as exc:
            raise SnapshotAuthorizationError(str(exc)) from exc

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

        # Ensure durable shared-thread existence is flushed; router will commit.
        # For snapshot callers that use service directly in tests, thread is visible
        # within same transaction; outer commit persists it if newly created.

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

        # DM turn/stream state — placeholder hook (issue says include active DM turn/stream state
        # and hooks for structured surfaces added later). No durable DM turn model yet;
        # return a stable idle state so clients can rely on shape.
        dm_state = {
            "status": "idle",
            "streaming": False,
            "active_turn": None,
            "turn_id": None,
            "started_at": None,
        }

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
