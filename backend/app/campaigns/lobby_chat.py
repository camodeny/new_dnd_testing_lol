"""Shared out-of-character lobby chat — issue #243.

Pre-start campaign members coordinate here. Lobby chat is durable table talk,
explicitly non-fictional:

- persisted with the shared thread/message infrastructure
  (:class:`CampaignThread` ``thread_type='lobby'`` + ``PlayerSubmission``
  rows with ``audience='lobby'``);
- every message is forced to a single OOC segment — IC content can never be
  stored through this path;
- posting never coordinates a forward DM turn, bumps ``Campaign.revision``,
  appends domain events, enqueues post-turn work, or touches clocks/world;
  the gameplay submission endpoint and :func:`coordinate_turn` refuse lobby
  threads fail-closed as defense in depth;
- reads stay available to members after start (preserve/archive); writes are
  allowed only while pre-start (``lobby``/``starting``).

The AI is the sole DM; lobby chat is player coordination, never DM narration.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.submissions.service import MAX_CONTENT_LENGTH, accept_submission, list_submissions
from app.threads.service import get_or_create_lobby_thread
from app.campaigns.service import CampaignCommandError
from models.campaigns import Campaign
from models.threads import CampaignThread, PlayerSubmission, PlayerSubmissionSegment

logger = logging.getLogger(__name__)

#: Submission audience marking lobby OOC table talk. PlayerSubmission.audience
#: has no check constraint; "lobby" keeps lobby rows trivially excludable
#: from every gameplay-thread query (DM context, seed inputs, transcripts).
LOBBY_CHAT_AUDIENCE = "lobby"

#: Campaign statuses in which lobby chat accepts new messages. ``starting``
#: is still pre-live-play (no live table yet); ``active``/``archived`` are
#: read-only so post-start play cannot leak into the lobby transcript.
LOBBY_CHAT_WRITABLE_STATUSES = frozenset({"lobby", "starting"})

#: Snapshot page size for lobby history reload (matches submission default).
LOBBY_CHAT_HISTORY_LIMIT = 200


class LobbyChatValidationError(CampaignCommandError):
    """Malformed lobby chat payload."""

    status_code = 422


class LobbyChatStatusError(CampaignCommandError):
    """Lobby chat write outside the pre-start window."""

    status_code = 409


def require_lobby_chat_writable(campaign: Campaign) -> None:
    """Reject lobby chat writes once the campaign has left pre-start."""
    if str(campaign.status or "").lower() not in LOBBY_CHAT_WRITABLE_STATUSES:
        logger.warning(
            "lobby_chat write rejected campaign_id=%s status=%s reason=not_pre_start",
            campaign.id,
            campaign.status,
        )
        raise LobbyChatStatusError(
            f"Lobby chat is read-only after the campaign leaves the lobby (status={campaign.status})"
        )


def validate_lobby_chat_payload(payload: object) -> str:
    """Extract and validate lobby chat content — always stored as OOC."""
    if not isinstance(payload, dict):
        raise LobbyChatValidationError("Request body must be an object")
    content = payload.get("content", payload.get("raw_content"))
    if not isinstance(content, str) or not content.strip():
        raise LobbyChatValidationError("content must be a non-empty string")
    if len(content) > MAX_CONTENT_LENGTH:
        raise LobbyChatValidationError(
            f"content must be {MAX_CONTENT_LENGTH} characters or fewer"
        )
    return content


def post_lobby_message(
    db: Session,
    *,
    campaign: Campaign,
    user_id: uuid.UUID,
    content: str,
) -> tuple[PlayerSubmission, list]:
    """Persist one OOC lobby message with zero fictional side effects.

    Uses :func:`accept_submission` for durable sequence allocation only.
    Provably side-effect-free on game state: no DM turn coordination, no
    ``Campaign.revision`` bump, no domain events, no post-turn enqueue, no
    clock/world writes — callers must not add any either (see router).
    """
    lobby_thread = get_or_create_lobby_thread(db, campaign.id, created_by=user_id)
    thread_id_str = str(lobby_thread.id)
    # Forced OOC: the raw text is stored verbatim as a single OOC segment, so
    # even literal "<ic>...</ic>" markup can never become an IC segment here.
    segments = [{"type": "ooc", "text": content}]
    submission = accept_submission(
        db,
        campaign_id=campaign.id,
        user_id=user_id,
        character_id=None,
        raw_content=content,
        segments=segments,
        thread_id=thread_id_str,
        audience=LOBBY_CHAT_AUDIENCE,
    )
    assert all(s["type"] == "ooc" for s in segments)
    stored_segments = (
        db.query(PlayerSubmissionSegment)
        .filter_by(submission_id=submission.id)
        .order_by(PlayerSubmissionSegment.position)
        .all()
    )
    logger.info(
        "lobby_chat accepted campaign_id=%s thread_id=%s submission_id=%s sequence=%s "
        "user_id=%s no_dm_coordination=true",
        campaign.id,
        thread_id_str,
        submission.id,
        submission.sequence,
        user_id,
    )
    return submission, stored_segments


def list_lobby_messages(
    db: Session,
    campaign_id: uuid.UUID,
    lobby_thread: CampaignThread,
    *,
    limit: int = LOBBY_CHAT_HISTORY_LIMIT,
) -> list[dict]:
    """Snapshot-reload path: ordered lobby history for refresh/reconnect."""
    return list_submissions(db, campaign_id, thread_id=str(lobby_thread.id), limit=limit)


def _thread_error(exc: Exception, *, campaign_id: uuid.UUID, user_id: uuid.UUID, action: str) -> CampaignCommandError:
    from app.threads.service import ThreadNotFoundError

    if isinstance(exc, ThreadNotFoundError):
        return CampaignCommandError("Thread not found", status_code=404)
    logger.info("lobby_chat %s denied campaign_id=%s user_id=%s", action, campaign_id, user_id)
    return CampaignCommandError(str(exc), status_code=403)


def lobby_chat_snapshot(db: Session, campaign: Campaign, user_id: uuid.UUID) -> dict:
    """Durable read projection for refresh/reconnect (members only).

    Ensures the lobby thread so pre-existing campaigns converge without a
    dedicated backfill.
    """
    from app.realtime.channels import live_table_channel
    from app.threads.service import ThreadAuthorizationError, ThreadNotFoundError, assert_can_read_thread

    thread = get_or_create_lobby_thread(db, campaign.id, created_by=user_id)
    db.commit()
    try:
        assert_can_read_thread(db, campaign.id, thread.id, user_id)
    except (ThreadNotFoundError, ThreadAuthorizationError) as exc:
        raise _thread_error(exc, campaign_id=campaign.id, user_id=user_id, action="read") from exc
    messages = list_lobby_messages(db, campaign.id, thread)
    logger.info(
        "lobby_chat snapshot campaign_id=%s user_id=%s thread_id=%s message_count=%s",
        campaign.id, user_id, thread.id, len(messages),
    )
    return {
        "thread": thread.to_dict(),
        "messages": messages,
        "channel": live_table_channel(campaign.id, thread.id),
        "campaign_status": campaign.status,
    }


def writable_lobby_thread(db: Session, campaign: Campaign, user_id: uuid.UUID, payload: object) -> tuple[CampaignThread, str]:
    """Pre-start, write-authorized lobby thread plus the validated content."""
    from app.threads.service import ThreadAuthorizationError, assert_can_write_thread

    require_lobby_chat_writable(campaign)
    thread = get_or_create_lobby_thread(db, campaign.id, created_by=user_id)
    db.commit()
    try:
        assert_can_write_thread(db, campaign.id, thread.id, user_id)
    except ThreadAuthorizationError as exc:
        raise _thread_error(exc, campaign_id=campaign.id, user_id=user_id, action="write") from exc
    try:
        content = validate_lobby_chat_payload(payload)
    except LobbyChatValidationError:
        logger.info("lobby_chat rejected campaign_id=%s reason=validation", campaign.id)
        raise
    return thread, content


def post_lobby_chat(
    db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, *, user_id: uuid.UUID, content: str,
) -> dict:
    """Post under the campaign lock (flush-only; caller commits).

    Re-checks lobby-writable status and thread membership on the locked row:
    a concurrent starting->active transition or member removal committing
    after the transport-level checks must still refuse the write
    (accept_submission's own lock only re-checks archive).
    """
    from sqlalchemy import select

    from app.threads.service import ThreadAuthorizationError, ThreadNotFoundError, assert_can_write_thread

    locked = db.execute(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if locked is None:
        raise CampaignCommandError("Campaign not found", status_code=404)
    require_lobby_chat_writable(locked)
    try:
        assert_can_write_thread(db, campaign_id, thread_id, user_id)
    except (ThreadNotFoundError, ThreadAuthorizationError) as exc:
        raise _thread_error(exc, campaign_id=campaign_id, user_id=user_id, action="write under lock") from exc
    submission, stored_segments = post_lobby_message(db, campaign=locked, user_id=user_id, content=content)
    return {
        "thread": get_or_create_lobby_thread(db, campaign_id, created_by=user_id).to_dict(),
        "message": submission.to_dict(stored_segments),
        "campaign_status": locked.status,
    }


def publish_lobby_message(db: Session, result: dict, *, campaign_id: uuid.UUID, thread_id: uuid.UUID) -> None:
    """Best-effort Realtime projection after the authoritative commit.

    Never rolls back on publish failure; the snapshot GET is the durable
    recovery path.
    """
    try:
        from app.realtime.service import publish_submission_created

        msg = result.get("message") if isinstance(result, dict) else None
        if msg and msg.get("id"):
            db_sub = db.get(PlayerSubmission, uuid.UUID(str(msg["id"])))
            if db_sub is not None:
                segs = (
                    db.query(PlayerSubmissionSegment)
                    .filter_by(submission_id=db_sub.id)
                    .order_by(PlayerSubmissionSegment.position)
                    .all()
                )
                publish_submission_created(db, db_sub, segments=segs)
    except Exception as exc:
        logger.warning(
            "lobby_chat realtime publish guard failed campaign_id=%s thread_id=%s error=%s",
            campaign_id, thread_id, exc,
        )
