"""Durable DM stream/chunk persistence — issue #197.

Guarantees:
- Stream chunks are ordered and associated with one logical turn attempt.
- Duplicate append/retry cannot create duplicate visible text (idempotent).
- Client can fetch persisted chunks after disconnect and reconstruct visible text.
- Completed streams expose convenient final message/read representation.
- Failed/abandoned partial streams remain auditable but excluded from canonical history.
- Private stream chunks are never stored in a shared bucket (thread-scoped).
- Chunk write failure is not reported as visible until persistence succeeds.
- Observability: first chunk ts, count/bytes, last sequence, completion reason, failures.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.dm import DMStream
from models.dm import DMStreamChunk

logger = logging.getLogger(__name__)


class DMStreamNotFoundError(LookupError):
    pass


class DMStreamAuthorizationError(PermissionError):
    pass


class DMStreamConflictError(ValueError):
    """Out-of-order or conflicting chunk write."""
    pass


class DMStreamStateError(ValueError):
    """Invalid state transition (e.g. append after completion)."""
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_stream(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    thread_id: uuid.UUID,
    turn_id: str,
    attempt_id: str,
    audience: str = "campaign",
    trace_id: str | None = None,
    operation_id: str | None = None,
) -> DMStream:
    if not turn_id or not turn_id.strip():
        raise ValueError("turn_id is required")
    if not attempt_id or not attempt_id.strip():
        raise ValueError("attempt_id is required")
    turn_id = turn_id.strip()
    attempt_id = attempt_id.strip()
    # Resolve trace if not provided
    if trace_id is None:
        try:
            from app.observability.tracing import current_trace_id
            trace_id = current_trace_id()
        except Exception:
            trace_id = None

    stream = DMStream(
        campaign_id=campaign_id,
        thread_id=thread_id,
        turn_id=turn_id,
        attempt_id=attempt_id,
        status="streaming",
        audience=audience,
        chunk_count=0,
        total_bytes=0,
        last_sequence=None,
        trace_id=trace_id,
        operation_id=operation_id,
    )
    db.add(stream)
    try:
        db.flush()
    except IntegrityError as exc:
        # Duplicate turn+attempt — idempotent handle: return existing
        db.rollback()
        existing = db.execute(
            select(DMStream).where(DMStream.turn_id == turn_id, DMStream.attempt_id == attempt_id)
        ).scalars().first()
        if existing is not None:
            logger.info(
                "dm_stream duplicate create turn_id=%s attempt_id=%s existing_id=%s",
                turn_id, attempt_id, existing.id,
            )
            return existing
        raise DMStreamConflictError(f"Stream create conflict: {exc}") from exc

    logger.info(
        "dm_stream created campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s stream_id=%s trace_id=%s",
        campaign_id, thread_id, turn_id, attempt_id, stream.id, trace_id,
    )
    return stream


def get_stream(db: Session, stream_id: uuid.UUID) -> DMStream | None:
    return db.get(DMStream, stream_id)


def _get_stream_for_update(db: Session, stream_id: uuid.UUID) -> DMStream:
    stream = db.execute(
        select(DMStream).where(DMStream.id == stream_id).with_for_update()
    ).scalars().first()
    if stream is None:
        raise DMStreamNotFoundError("Stream not found")
    return stream


def append_chunk(
    db: Session,
    stream_id: uuid.UUID,
    sequence: int,
    text: str,
) -> DMStreamChunk:
    if sequence is None or sequence < 0:
        raise ValueError("sequence must be >= 0")
    if text is None:
        raise ValueError("text is required")
    # Allow empty text? Probably not meaningful but allow as valid chunk.
    # Enforce byte size limit similar to submission? Use 50k per chunk? Be permissive.
    byte_len = len(text.encode("utf-8"))

    stream = _get_stream_for_update(db, stream_id)

    if stream.status != "streaming":
        logger.info(
            "dm_stream append denied status=%s stream_id=%s sequence=%s reason=not_streaming",
            stream.status, stream_id, sequence,
        )
        raise DMStreamStateError(f"Cannot append to stream in status {stream.status}")

    # Idempotency: check existing chunk at this sequence
    existing = db.execute(
        select(DMStreamChunk).where(DMStreamChunk.stream_id == stream_id, DMStreamChunk.sequence == sequence)
    ).scalars().first()
    if existing is not None:
        if existing.text == text and existing.byte_length == byte_len:
            logger.info(
                "dm_stream append idempotent stream_id=%s sequence=%s byte_len=%s",
                stream_id, sequence, byte_len,
            )
            return existing
        logger.warning(
            "dm_stream append conflict stream_id=%s sequence=%s existing_bytes=%s new_bytes=%s",
            stream_id, sequence, existing.byte_length, byte_len,
        )
        raise DMStreamConflictError(
            f"Duplicate sequence {sequence} with different content"
        )

    # Ordered check: must be exactly next sequence
    expected = (int(stream.last_sequence) + 1) if stream.last_sequence is not None else 0
    if sequence != expected:
        logger.warning(
            "dm_stream append out_of_order stream_id=%s expected=%s got=%s chunk_count=%s last_sequence=%s",
            stream_id, expected, sequence, stream.chunk_count, stream.last_sequence,
        )
        raise DMStreamConflictError(f"Out of order chunk: expected {expected}, got {sequence}")

    chunk = DMStreamChunk(
        stream_id=stream_id,
        sequence=sequence,
        text=text,
        byte_length=byte_len,
    )
    db.add(chunk)
    # Update stream observability metrics — only after persistence succeeds.
    now = _now()
    if stream.first_chunk_at is None:
        stream.first_chunk_at = now
    stream.last_chunk_at = now
    stream.last_sequence = sequence
    stream.chunk_count = int(stream.chunk_count or 0) + 1
    stream.total_bytes = int(stream.total_bytes or 0) + byte_len

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        # Re-check idempotent race
        race = db.execute(
            select(DMStreamChunk).where(DMStreamChunk.stream_id == stream_id, DMStreamChunk.sequence == sequence)
        ).scalars().first()
        if race is not None and race.text == text:
            logger.info("dm_stream append race idempotent stream_id=%s sequence=%s", stream_id, sequence)
            # Need to re-load stream metrics? Already rolled back, so re-apply?
            # Caller will re-read. Return race as success.
            return race
        logger.warning("dm_stream chunk write failed stream_id=%s sequence=%s error=%s", stream_id, sequence, exc)
        raise DMStreamConflictError(f"Chunk persistence failed: {exc}") from exc
    except Exception as exc:
        # Must not be reported as successfully visible until persistence succeeds
        logger.warning("dm_stream chunk write failed stream_id=%s sequence=%s error=%s", stream_id, sequence, exc)
        raise

    logger.info(
        "dm_stream chunk persisted stream_id=%s sequence=%s byte_len=%s total_bytes=%s chunk_count=%s",
        stream_id, sequence, byte_len, stream.total_bytes, stream.chunk_count,
    )
    return chunk


def list_chunks(db: Session, stream_id: uuid.UUID) -> list[DMStreamChunk]:
    return list(
        db.execute(
            select(DMStreamChunk).where(DMStreamChunk.stream_id == stream_id).order_by(DMStreamChunk.sequence.asc())
        ).scalars().all()
    )


def reconstruct_text(db: Session, stream_id: uuid.UUID) -> str:
    chunks = list_chunks(db, stream_id)
    return "".join(c.text for c in chunks)


def get_stream_with_chunks(db: Session, stream_id: uuid.UUID) -> tuple[DMStream, list[DMStreamChunk], str]:
    stream = db.get(DMStream, stream_id)
    if stream is None:
        raise DMStreamNotFoundError("Stream not found")
    chunks = list_chunks(db, stream_id)
    visible_text = "".join(c.text for c in chunks)
    return stream, chunks, visible_text


def complete_stream(
    db: Session,
    stream_id: uuid.UUID,
    *,
    completion_reason: str = "completed",
) -> DMStream:
    stream = _get_stream_for_update(db, stream_id)
    if stream.status == "completed":
        # Idempotent
        return stream
    if stream.status in ("abandoned", "failed"):
        raise DMStreamStateError(f"Cannot complete stream in status {stream.status}")

    chunks = list_chunks(db, stream_id)
    final_text = "".join(c.text for c in chunks)
    stream.final_text = final_text
    stream.status = "completed"
    stream.completed_at = _now()
    stream.completion_reason = completion_reason
    # Ensure observability metrics consistent even if chunks were zero
    if not chunks:
        stream.chunk_count = 0
        stream.total_bytes = 0
    db.flush()
    logger.info(
        "dm_stream completed stream_id=%s turn_id=%s attempt_id=%s chunk_count=%s total_bytes=%s last_sequence=%s reason=%s",
        stream_id, stream.turn_id, stream.attempt_id, stream.chunk_count, stream.total_bytes, stream.last_sequence, completion_reason,
    )
    return stream


def abandon_stream(
    db: Session,
    stream_id: uuid.UUID,
    *,
    reason: str = "abandoned",
) -> DMStream:
    stream = _get_stream_for_update(db, stream_id)
    if stream.status in ("abandoned", "failed"):
        return stream
    if stream.status == "completed":
        raise DMStreamStateError("Cannot abandon a completed stream")

    stream.status = "abandoned"
    stream.abandoned_at = _now()
    stream.abandonment_reason = reason
    # Retain partial chunks for audit; final_text stays None or partial? Store partial for observability but not canonical
    # Do not set final_text as canonical; keep None to signal not completed. Optionally store partial for audit in separate field?
    db.flush()
    logger.info(
        "dm_stream abandoned stream_id=%s turn_id=%s attempt_id=%s chunk_count=%s last_sequence=%s reason=%s",
        stream_id, stream.turn_id, stream.attempt_id, stream.chunk_count, stream.last_sequence, reason,
    )
    return stream


def fail_stream(
    db: Session,
    stream_id: uuid.UUID,
    *,
    reason: str = "failed",
) -> DMStream:
    stream = _get_stream_for_update(db, stream_id)
    if stream.status in ("abandoned", "failed"):
        return stream
    if stream.status == "completed":
        raise DMStreamStateError("Cannot fail a completed stream")
    stream.status = "failed"
    stream.abandoned_at = _now()
    stream.abandonment_reason = reason
    db.flush()
    logger.info(
        "dm_stream failed stream_id=%s reason=%s chunk_count=%s last_sequence=%s",
        stream_id, reason, stream.chunk_count, stream.last_sequence,
    )
    return stream


def list_streams_for_thread(
    db: Session,
    campaign_id: uuid.UUID,
    thread_id: uuid.UUID,
    *,
    include_abandoned: bool = False,
) -> list[DMStream]:
    q = select(DMStream).where(DMStream.campaign_id == campaign_id, DMStream.thread_id == thread_id)
    if not include_abandoned:
        q = q.where(DMStream.status == "completed")
    return list(db.execute(q.order_by(DMStream.created_at.asc())).scalars().all())


def list_all_streams_for_thread(
    db: Session,
    campaign_id: uuid.UUID,
    thread_id: uuid.UUID,
) -> list[DMStream]:
    return list(
        db.execute(
            select(DMStream).where(DMStream.campaign_id == campaign_id, DMStream.thread_id == thread_id).order_by(DMStream.created_at.asc())
        ).scalars().all()
    )
