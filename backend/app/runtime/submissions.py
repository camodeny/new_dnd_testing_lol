"""Durable player-submission application service — issue #194."""

from __future__ import annotations

import logging
import re
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.campaigns import Campaign
from models.campaigns import CampaignMember
from models.characters import Character
from models.threads import PlayerSubmission
from models.threads import PlayerSubmissionSegment

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<(ic|ooc)>([\s\S]*?)</\1>", re.IGNORECASE)
_TAG_MARKER = re.compile(r"</?(?:ic|ooc)\b", re.IGNORECASE)
MAX_CONTENT_LENGTH = 50_000
MAX_SEGMENTS = 100


class SubmissionValidationError(ValueError):
    pass


def validate_submission_payload(payload: object) -> tuple[str, list[dict[str, str]]]:
    if not isinstance(payload, dict):
        raise SubmissionValidationError("Request body must be an object")
    content = payload.get("content", payload.get("raw_content"))
    if not isinstance(content, str) or not content:
        raise SubmissionValidationError("content must be a non-empty string")
    if len(content) > MAX_CONTENT_LENGTH:
        raise SubmissionValidationError(f"content must be {MAX_CONTENT_LENGTH} characters or fewer")

    supplied = payload.get("segments")
    if supplied is not None:
        if not isinstance(supplied, list) or not supplied:
            raise SubmissionValidationError("segments must be a non-empty array")
        if len(supplied) > MAX_SEGMENTS:
            raise SubmissionValidationError(f"segments must contain at most {MAX_SEGMENTS} items")
        segments = []
        total_segment_length = 0
        for position, item in enumerate(supplied):
            if not isinstance(item, dict):
                raise SubmissionValidationError(f"segments[{position}] must be an object")
            kind = item.get("type")
            text = item.get("text")
            if kind not in ("ic", "ooc"):
                raise SubmissionValidationError(f"segments[{position}].type must be 'ic' or 'ooc'")
            if not isinstance(text, str) or not text:
                raise SubmissionValidationError(f"segments[{position}].text must be a non-empty string")
            total_segment_length += len(text)
            if total_segment_length > MAX_CONTENT_LENGTH:
                raise SubmissionValidationError(
                    f"combined segment text must be {MAX_CONTENT_LENGTH} characters or fewer"
                )
            segments.append({"type": kind, "text": text})
        return content, segments

    return content, parse_tagged_content(content)


def parse_tagged_content(content: str) -> list[dict[str, str]]:
    """Parse explicit IC/OOC tags; untagged text is intentionally OOC."""
    if not _TAG_MARKER.search(content):
        return [{"type": "ooc", "text": content}]

    segments: list[dict[str, str]] = []
    cursor = 0
    for match in _TAG.finditer(content):
        prefix = content[cursor:match.start()]
        if _TAG_MARKER.search(prefix):
            raise SubmissionValidationError(
                "Malformed IC/OOC tags; use matched <ic>...</ic> and <ooc>...</ooc> tags"
            )
        if prefix:
            segments.append({"type": "ooc", "text": prefix})
        text = match.group(2)
        if not text:
            raise SubmissionValidationError("IC/OOC tagged segments cannot be empty")
        if _TAG_MARKER.search(text):
            raise SubmissionValidationError(
                "Nested or malformed IC/OOC tags are not supported; provide ordered segments instead"
            )
        segments.append({"type": match.group(1).lower(), "text": text})
        cursor = match.end()
    suffix = content[cursor:]
    if _TAG_MARKER.search(suffix) or not segments:
        raise SubmissionValidationError(
            "Malformed IC/OOC tags; use matched <ic>...</ic> and <ooc>...</ooc> tags"
        )
    if suffix:
        segments.append({"type": "ooc", "text": suffix})
    if len(segments) > MAX_SEGMENTS:
        raise SubmissionValidationError(f"parsed content contains more than {MAX_SEGMENTS} segments")
    return segments


def accept_submission(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    user_id: uuid.UUID,
    raw_content: str,
    segments: list[dict[str, str]],
    character_id: uuid.UUID | None = None,
    thread_id: str = "main",
    audience: str = "campaign",
    source: str | None = None,
) -> PlayerSubmission:
    started = time.monotonic()
    # The campaign lock serializes sequence allocation without treating acceptance
    # as a fictional mutation or advancing Campaign.revision.
    campaign = db.execute(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    ).scalars().first()
    if campaign is None:
        raise SubmissionValidationError("Campaign not found")
    # Issue #265 — re-check on the locked row: a concurrent archive that won
    # the row lock after the transport-level check must still refuse the write.
    from app.campaigns.service import require_playable_campaign

    require_playable_campaign(campaign)

    if character_id is not None:
        character = db.get(Character, character_id)
        if character is None or character.owner_id != user_id:
            raise SubmissionValidationError("character_id must identify one of your characters")
    else:
        # Default speaker: the sender's selected launch character. A live-table
        # submission with no explicit character speaks as the sender's PC, so
        # DM context always carries the player<->character linkage (protected
        # PCs lane) even for clients holding a stale roster. Explicit claims
        # above stay the only override path.
        member = (
            db.execute(
                select(CampaignMember).where(
                    CampaignMember.campaign_id == campaign_id,
                    CampaignMember.user_id == user_id,
                )
            )
            .scalars()
            .first()
        )
        selected = member.selected_character_id if member is not None else None
        if selected is not None:
            character = db.get(Character, selected)
            if character is not None and character.owner_id == user_id:
                character_id = selected
            else:
                logger.warning(
                    "player_submission selected character unusable "
                    "campaign_id=%s user_id=%s selected_character_id=%s",
                    campaign_id,
                    user_id,
                    selected,
                )

    prior = None
    submission = None
    # Bounded sequence re-allocation: the campaign row lock serializes this
    # on Postgres, but SQLite ignores FOR UPDATE, so two concurrent
    # acceptances can read the same max(sequence). On a sequence conflict,
    # back off briefly (lets the winner commit) and recompute — never fail
    # a player's submission on a transient allocation race.
    for allocation_attempt in range(3):
        prior = db.scalar(
            select(func.max(PlayerSubmission.sequence)).where(
                PlayerSubmission.campaign_id == campaign_id,
                PlayerSubmission.thread_id == thread_id,
            )
        ) or 0
        submission = PlayerSubmission(
            campaign_id=campaign_id,
            user_id=user_id,
            character_id=character_id,
            thread_id=thread_id,
            audience=audience,
            sequence=prior + 1,
            raw_content=raw_content,
            source=source,
            resolution_status="accepted",
        )
        try:
            with db.begin_nested():
                db.add(submission)
                db.flush()
                for position, segment in enumerate(segments):
                    db.add(PlayerSubmissionSegment(
                        submission_id=submission.id,
                        position=position,
                        segment_type=segment["type"],
                        text=segment["text"],
                    ))
                db.flush()
            break
        except IntegrityError as exc:
            if "sequence" not in str(getattr(exc, "orig", exc)).lower() \
                    or allocation_attempt >= 2:
                raise
            logger.info(
                "player_submission sequence race campaign_id=%s thread_id=%s "
                "sequence=%s retry=%s",
                campaign_id, thread_id, prior + 1, allocation_attempt + 1,
            )
            time.sleep(0.05 * (allocation_attempt + 1))
    assert submission is not None
    logger.info(
        "player_submission accepted campaign_id=%s thread_id=%s submission_id=%s sequence=%s "
        "segment_count=%s segment_types=%s latency_ms=%.2f",
        campaign_id, thread_id, submission.id, submission.sequence, len(segments),
        [segment["type"] for segment in segments], (time.monotonic() - started) * 1000,
    )
    return submission


def list_submissions(db: Session, campaign_id: uuid.UUID, thread_id: str = "main", limit: int = 200):
    submissions = db.execute(
        select(PlayerSubmission).where(
            PlayerSubmission.campaign_id == campaign_id,
            PlayerSubmission.thread_id == thread_id,
        ).order_by(PlayerSubmission.sequence).limit(limit)
    ).scalars().all()
    if not submissions:
        return []
    ids = [submission.id for submission in submissions]
    segments = db.execute(
        select(PlayerSubmissionSegment).where(
            PlayerSubmissionSegment.submission_id.in_(ids)
        ).order_by(PlayerSubmissionSegment.submission_id, PlayerSubmissionSegment.position)
    ).scalars().all()
    by_submission = {submission_id: [] for submission_id in ids}
    for segment in segments:
        by_submission[segment.submission_id].append(segment)
    return [submission.to_dict(by_submission[submission.id]) for submission in submissions]
