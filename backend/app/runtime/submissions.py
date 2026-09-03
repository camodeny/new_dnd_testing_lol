"""Durable player-submission application service — issue #194."""

from __future__ import annotations

import logging
import re
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.campaigns import Campaign
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
) -> PlayerSubmission:
    started = time.monotonic()
    # The campaign lock serializes sequence allocation without treating acceptance
    # as a fictional mutation or advancing Campaign.revision.
    campaign = db.execute(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    ).scalars().first()
    if campaign is None:
        raise SubmissionValidationError("Campaign not found")

    if character_id is not None:
        character = db.get(Character, character_id)
        if character is None or character.owner_id != user_id:
            raise SubmissionValidationError("character_id must identify one of your characters")

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
        resolution_status="accepted",
    )
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
