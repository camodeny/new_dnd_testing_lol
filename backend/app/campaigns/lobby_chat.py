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

from app.runtime.submissions import MAX_CONTENT_LENGTH, accept_submission, list_submissions
from app.runtime.threads import get_lobby_thread, get_or_create_lobby_thread
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


class LobbyChatValidationError(ValueError):
    """Malformed lobby chat payload (router maps to HTTP 422)."""


class LobbyChatStatusError(ValueError):
    """Lobby chat write outside the pre-start window (router maps to HTTP 409)."""


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


__all__ = [
    "LOBBY_CHAT_AUDIENCE",
    "LOBBY_CHAT_HISTORY_LIMIT",
    "LOBBY_CHAT_WRITABLE_STATUSES",
    "LobbyChatStatusError",
    "LobbyChatValidationError",
    "get_lobby_thread",
    "list_lobby_messages",
    "post_lobby_message",
    "require_lobby_chat_writable",
    "validate_lobby_chat_payload",
]
