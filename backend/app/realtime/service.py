"""Audience-safe Supabase Realtime projections — issue #198.

Responsibilities:
- Define server-side projection events (submissions, DM chunks/status, encounters, maps)
  with stable event/revision identifiers for dedupe/reconciliation.
- Publish only audience-authorized payloads to private channels.
- Ensure realtime delivery failure never rolls back authoritative DB state.

Design notes:
- DB writes (submissions, dm chunks) commit authoritatively first; publish is
  best-effort AFTER commit, wrapped in try/except so failures are observable
  but never mutate rollback.
- Payloads are private-channel-safe: we never broadcast hidden canonical state
  to a shared channel. Each thread has its own channel (see channels.py).
- Tests run on SQLite without a real Supabase endpoint — publisher degrades to
  a no-op (or in-memory recorder when monkeypatched) and never raises.
- Optional Supabase Broadcast path: if SUPABASE_URL + SERVICE_ROLE_KEY are
  configured we POST to Realtime broadcast; otherwise we log and skip.

Stable identifiers:
- submission: event_id = f"submission:{submission.id}"  (also sequence)
- dm chunk:   event_id = f"dm-chunk:{stream_id}:{sequence}"
- dm status:  event_id = f"dm-status:{stream_id}:{status}:{completed_at}"
All payloads include channel, revision/sequence, and timestamp for ordering.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.realtime.channels import live_table_channel
from app.threads.service import list_threads_for_user
from models.campaigns import Campaign
from models.dm import DMStream
from models.dm import DMStreamChunk
from models.threads import PlayerSubmission
from models.threads import PlayerSubmissionSegment

logger = logging.getLogger(__name__)

# ── projection event builders ───────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_submission_event(
    submission: PlayerSubmission,
    segments: list[PlayerSubmissionSegment] | None,
    campaign_revision: int | None = None,
) -> dict[str, Any]:
    """Build realtime payload for a new player submission."""
    seg_dicts = None
    if segments is not None:
        seg_dicts = [s.to_dict() for s in segments]
    # event_id is stable for dedupe; sequence is thread-scoped ordering
    return {
        "type": "submission.created",
        "event_id": f"submission:{submission.id}",
        "id": str(submission.id),
        "campaign_id": str(submission.campaign_id),
        "thread_id": str(submission.thread_id),
        "sequence": int(submission.sequence),
        "revision": int(campaign_revision) if campaign_revision is not None else None,
        "user_id": str(submission.user_id),
        "audience": submission.audience,
        "raw_content": submission.raw_content,
        "source": submission.source,
        "segments": seg_dicts,
        "accepted_at": submission.accepted_at.isoformat() if submission.accepted_at else None,
        "timestamp": _utcnow_iso(),
        "dedupe_key": str(submission.id),
    }


def build_dm_chunk_event(stream: DMStream, chunk: DMStreamChunk) -> dict[str, Any]:
    return {
        "type": "dm.chunk",
        "event_id": f"dm-chunk:{stream.id}:{chunk.sequence}",
        "stream_id": str(stream.id),
        "campaign_id": str(stream.campaign_id),
        "thread_id": str(stream.thread_id),
        "turn_id": stream.turn_id,
        "attempt_id": stream.attempt_id,
        "sequence": int(chunk.sequence),
        "text": chunk.text,
        "byte_length": int(chunk.byte_length),
        "timestamp": chunk.created_at.isoformat() if chunk.created_at else _utcnow_iso(),
        "dedupe_key": f"{stream.id}:{chunk.sequence}",
    }


def build_dm_status_event(stream: DMStream, *, visible_text: str | None = None) -> dict[str, Any]:
    return {
        "type": "dm.status",
        "event_id": f"dm-status:{stream.id}:{stream.status}:{stream.updated_at.isoformat() if stream.updated_at else _utcnow_iso()}",
        "stream_id": str(stream.id),
        "campaign_id": str(stream.campaign_id),
        "thread_id": str(stream.thread_id),
        "turn_id": stream.turn_id,
        "attempt_id": stream.attempt_id,
        "status": stream.status,
        "chunk_count": int(stream.chunk_count or 0),
        "total_bytes": int(stream.total_bytes or 0),
        "last_sequence": stream.last_sequence,
        "visible_text": visible_text,
        "final_text": stream.final_text,
        "completion_reason": stream.completion_reason,
        "abandonment_reason": stream.abandonment_reason,
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{stream.id}:{stream.status}",
    }


def build_projection_invalidated_event(
    campaign: Campaign,
    *,
    thread_id: uuid.UUID | str | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    """Projection invalidation for visibility expansion/contraction — issue #250.

    Emitted (post-commit, best-effort) when a ``WorldVisibilityGrant`` is
    created or revoked so affected clients reload their per-player
    projections via a visibility-safe snapshot.

    Audience-neutral by design: thread channels are authorized per thread,
    not per user, so this payload carries NO user-specific or
    secret-visibility metadata — no grantee, no target kind, no
    grant/revoke direction, no record ids, no content. Just
    type/campaign/thread/revision. Every subscriber quietly refetches its
    own filtered snapshot; a missed event still converges because the
    campaign revision advanced with the grant/revoke commit.
    """
    rev = int(revision) if revision is not None else int(campaign.revision or 0)
    tid = str(thread_id) if thread_id else None
    return {
        "type": "projection.invalidated",
        "event_id": f"projection-invalidated:{campaign.id}:{tid or 'campaign'}:{rev}",
        "campaign_id": str(campaign.id),
        "thread_id": tid,
        "revision": rev,
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{campaign.id}:projection-invalidated:{tid or 'campaign'}:{rev}",
    }


def build_encounter_started_event(encounter, *, revision: int | None = None) -> dict[str, Any]:
    """Projection for ``encounter.started`` — issue #230."""
    return {
        "type": "encounter.started",
        "event_id": f"encounter:{encounter.id}:started",
        "encounter_id": str(encounter.id),
        "campaign_id": str(encounter.campaign_id),
        "thread_id": str(encounter.thread_id),
        "status": encounter.status,
        "revision": int(revision) if revision is not None else None,
        "participant_count": int(encounter.participant_count or 0),
        "start_source": encounter.start_source,
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{encounter.id}:started",
    }


def build_encounter_ready_event(encounter, *, revision: int | None = None) -> dict[str, Any]:
    """Projection for ``encounter.initiative_ready`` — issue #230."""
    return {
        "type": "encounter.initiative_ready",
        "event_id": f"encounter:{encounter.id}:ready",
        "encounter_id": str(encounter.id),
        "campaign_id": str(encounter.campaign_id),
        "thread_id": str(encounter.thread_id),
        "status": encounter.status,
        "round": int(encounter.round or 1),
        "revision": int(revision) if revision is not None else None,
        "turn_order_ids": [str(pid) for pid in (encounter.turn_order_ids or [])],
        "active_participant_id": str(encounter.active_participant_id) if encounter.active_participant_id else None,
        "tie_resolution": encounter.tie_resolution,
        "time_to_first_turn_ms": encounter.time_to_first_turn_ms,
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{encounter.id}:ready",
    }


def build_encounter_ended_event(encounter, *, revision: int | None = None) -> dict[str, Any]:
    """Projection for ``encounter.ended`` — issue #239.

    Carries outcome/reason/rounds only — never hidden NPC HP breakdowns or
    DM-only detail (members converge via the privacy-filtered snapshot).
    """
    return {
        "type": "encounter.ended",
        "event_id": f"encounter:{encounter.id}:ended",
        "encounter_id": str(encounter.id),
        "campaign_id": str(encounter.campaign_id),
        "thread_id": str(encounter.thread_id),
        "status": encounter.status,
        "round": int(encounter.round or 1),
        "revision": int(revision) if revision is not None else None,
        "outcome": encounter.end_outcome,
        "end_duration_ms": encounter.end_duration_ms,
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{encounter.id}:ended",
    }


def build_encounter_turn_event(encounter, kind: str, *, revision: int | None = None) -> dict[str, Any]:
    """Projection for turn progression — issue #231.

    ``kind`` is one of ``started`` / ``ended`` / ``skipped``. Payloads carry
    turn order positions and round/sequence only — never resource budgets or
    hidden stat breakdowns (members converge via the snapshot projection).
    """
    return {
        "type": f"encounter.turn_{kind}",
        "event_id": f"encounter:{encounter.id}:turn:{int(encounter.turn_sequence or 0)}:{kind}",
        "encounter_id": str(encounter.id),
        "campaign_id": str(encounter.campaign_id),
        "thread_id": str(encounter.thread_id),
        "status": encounter.status,
        "round": int(encounter.round or 1),
        "revision": int(revision) if revision is not None else None,
        "turn_sequence": int(encounter.turn_sequence or 0),
        "active_participant_id": str(encounter.active_participant_id) if encounter.active_participant_id else None,
        "active_index": int(encounter.active_index or 0),
        "skipped_count": int(encounter.skipped_count or 0),
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{encounter.id}:turn:{int(encounter.turn_sequence or 0)}:{kind}",
    }


def build_encounter_map_event(encounter, *, map_revision: int | None = None, revision: int | None = None) -> dict[str, Any]:
    """Projection for ``encounter.map_updated`` — issue #232.

    Carries geometry dimensions, policy, and revision only — never DM-only
    terrain labels or hidden token positions (members converge via the
    privacy-filtered snapshot projection).
    """
    return {
        "type": "encounter.map_updated",
        "event_id": f"encounter:{encounter.id}:map:{int(map_revision or 0)}",
        "encounter_id": str(encounter.id),
        "campaign_id": str(encounter.campaign_id),
        "thread_id": str(encounter.thread_id),
        "status": encounter.status,
        "revision": int(revision) if revision is not None else None,
        "map_revision": int(map_revision or 0),
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{encounter.id}:map:{int(map_revision or 0)}",
    }


def build_encounter_moved_event(encounter, participant_id, *, to: dict | None = None, revision: int | None = None, move_id: str | None = None) -> dict[str, Any]:
    """Projection for ``encounter.moved`` — issue #232.

    Carries the moved token's destination only; budgets and hidden state stay
    in the snapshot projection. ``move_id`` (the movement-ledger row id)
    keeps every move in one turn a distinct event: without it, incremental
    moves by the same participant share an event id and the realtime
    deduper drops all but the first as duplicates.
    """
    # Per-move identity when known; the legacy turn-scoped key stays as the
    # fallback so older callers keep stable ids.
    move_suffix = str(move_id) if move_id else f"{int(encounter.turn_sequence or 0)}"
    return {
        "type": "encounter.moved",
        "event_id": f"encounter:{encounter.id}:moved:{participant_id}:{move_suffix}",
        "encounter_id": str(encounter.id),
        "campaign_id": str(encounter.campaign_id),
        "thread_id": str(encounter.thread_id),
        "status": encounter.status,
        "revision": int(revision) if revision is not None else None,
        "turn_sequence": int(encounter.turn_sequence or 0),
        "participant_id": str(participant_id),
        "to": dict(to or {}),
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{encounter.id}:moved:{participant_id}:{move_suffix}",
    }


# ── publisher abstraction ───────────────────────────────────────────────────

class RealtimePublisher:
    """Pluggable publisher — real Supabase or in-memory for tests."""

    def publish(self, channel: str, event: str, payload: dict[str, Any]) -> bool:
        raise NotImplementedError


class NoopRealtimePublisher(RealtimePublisher):
    """Default: log and count, never fail. Used in tests / when Supabase not configured."""

    def publish(self, channel: str, event: str, payload: dict[str, Any]) -> bool:
        logger.info("realtime publish channel=%s event=%s event_id=%s", channel, event, payload.get("event_id"))
        return True


class SupabaseRealtimePublisher(RealtimePublisher):
    """Best-effort Supabase Broadcast publisher.

    Uses REST broadcast if SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY available.
    Falls back to Noop on any failure — never raises to caller (caller wraps).
    """

    def publish(self, channel: str, event: str, payload: dict[str, Any]) -> bool:
        url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or ""
        # Private broadcast must use service_role — anon cannot publish to private channels
        # and would be an audience-safety bypass. Intentionally no anon fallback.
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
        if not url or not key:
            if not key and url:
                logger.warning("realtime publish skipped (SUPABASE_SERVICE_ROLE_KEY missing — private broadcast requires service_role) channel=%s event=%s", channel, event)
            else:
                logger.info("realtime publish skipped (no Supabase config) channel=%s event=%s", channel, event)
            return True
        # Lazy import so tests without httpx don't fail import.
        try:
            import httpx  # type: ignore

            # Supabase Realtime broadcast REST: POST /realtime/v1/api/broadcast
            endpoint = url.rstrip("/") + "/realtime/v1/api/broadcast"
            body = {"messages": [{"topic": channel, "event": event, "payload": payload}]}
            headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            # Short timeout — realtime must not block authoritative work.
            resp = httpx.post(endpoint, json=body, headers=headers, timeout=2.0)
            if resp.status_code >= 400:
                logger.warning("realtime publish http failure channel=%s event=%s status=%s body=%s", channel, event, resp.status_code, resp.text[:500])
                return False
            logger.info("realtime publish ok channel=%s event=%s", channel, event)
            return True
        except Exception as exc:
            logger.warning("realtime publish exception channel=%s event=%s error=%s", channel, event, exc)
            return False


# Global publisher — monkeypatchable in tests via set_realtime_publisher
_publisher: RealtimePublisher = SupabaseRealtimePublisher()


def get_realtime_publisher() -> RealtimePublisher:
    return _publisher


def set_realtime_publisher(publisher: RealtimePublisher | None) -> None:
    global _publisher
    _publisher = publisher or NoopRealtimePublisher()


# ── high-level publish helpers (audience-safe, failure-isolated) ───────────

def _publish_best_effort(channel: str, event: str, payload: dict[str, Any]) -> bool:
    """Publish without ever raising — failures are logged."""
    try:
        ok = _publisher.publish(channel, event, payload)
        if not ok:
            logger.warning("realtime publish returned false channel=%s event=%s event_id=%s", channel, event, payload.get("event_id"))
            return False
        return True
    except Exception as exc:
        logger.warning("realtime publish failure channel=%s event=%s event_id=%s error=%s", channel, event, payload.get("event_id"), exc)
        return False


def publish_submission_created(
    db: Session,
    submission: PlayerSubmission,
    *,
    segments: list[PlayerSubmissionSegment] | None = None,
) -> bool:
    """Publish a submission projection to its private thread channel.

    Call AFTER db.commit() — failure leaves DB intact.
    Audience safety: channel is thread-scoped; we do not broadcast private
    submissions to the shared campaign channel.
    """
    try:
        campaign = db.get(Campaign, submission.campaign_id)
        revision = int(campaign.revision) if campaign and campaign.revision is not None else None
        channel = live_table_channel(submission.campaign_id, submission.thread_id)
        payload = build_submission_event(submission, segments, campaign_revision=revision)
        # Also include channel in payload for client convenience
        payload["channel"] = channel
        payload["revision"] = revision
        return _publish_best_effort(channel, payload["type"], payload)
    except Exception as exc:
        logger.warning("publish_submission_created failed submission_id=%s error=%s", submission.id, exc)
        return False


def publish_dm_chunk_created(
    db: Session,
    stream: DMStream,
    chunk: DMStreamChunk,
) -> bool:
    try:
        channel = live_table_channel(stream.campaign_id, stream.thread_id)
        payload = build_dm_chunk_event(stream, chunk)
        payload["channel"] = channel
        return _publish_best_effort(channel, payload["type"], payload)
    except Exception as exc:
        logger.warning("publish_dm_chunk_created failed stream_id=%s seq=%s error=%s", stream.id, chunk.sequence, exc)
        return False


def publish_dm_status(
    db: Session,
    stream: DMStream,
    *,
    visible_text: str | None = None,
) -> bool:
    try:
        channel = live_table_channel(stream.campaign_id, stream.thread_id)
        payload = build_dm_status_event(stream, visible_text=visible_text)
        payload["channel"] = channel
        return _publish_best_effort(channel, payload["type"], payload)
    except Exception as exc:
        logger.warning("publish_dm_status failed stream_id=%s error=%s", stream.id, exc)
        return False


def publish_projection_invalidated(
    db: Session,
    campaign: Campaign,
    *,
    thread_id: uuid.UUID | str,
) -> bool:
    """Publish an audience-neutral projection invalidation to one thread channel.

    Call AFTER db.commit() — failure leaves authoritative state intact.
    Payload carries no user-specific or secret-visibility metadata (see
    builder). The event tells subscribers to reload via the
    visibility-safe snapshot; a missed event still converges because the
    campaign revision advanced with the grant/revoke commit.
    """
    try:
        revision = int(campaign.revision) if campaign.revision is not None else None
        channel = live_table_channel(campaign.id, thread_id)
        payload = build_projection_invalidated_event(
            campaign, thread_id=thread_id, revision=revision,
        )
        payload["channel"] = channel
        return _publish_best_effort(channel, payload["type"], payload)
    except Exception as exc:
        logger.warning("publish_projection_invalidated failed campaign_id=%s error=%s", campaign.id, exc)
        return False


def publish_projection_invalidated_for_grantee(
    db: Session,
    campaign: Campaign,
    *,
    grantee_user_id: uuid.UUID | str | None = None,
    max_threads: int = 20,
) -> int:
    """Publish invalidation to every thread the grantee can read — issue #250.

    Convenience wrapper for visibility grant/revoke commit paths: resolves
    the grantee's readable threads and publishes one audience-neutral
    invalidation per thread (bounded). The grantee id is used ONLY for
    thread resolution and is never serialized into any payload. Returns
    the count of successful publishes. Never raises. Call AFTER db.commit().
    """
    try:
        threads = list_threads_for_user(db, campaign.id, grantee_user_id)
    except Exception as exc:
        logger.warning(
            "publish_projection_invalidated thread resolution failed campaign_id=%s error=%s",
            campaign.id, exc,
        )
        return 0
    sent = 0
    for thread in list(threads or [])[: max(1, int(max_threads or 20))]:
        thread_id = getattr(thread, "id", thread)
        if publish_projection_invalidated(db, campaign, thread_id=thread_id):
            sent += 1
    return sent


def _publish_encounter_event(db: Session, encounter, payload: dict[str, Any]) -> bool:
    """Best-effort encounter projection to the source-turn thread channel.

    Call AFTER db.commit() — failure leaves authoritative state intact.
    Audience safety: payloads carry turn order + display names only; hidden
    NPC stat breakdowns and hidden combatants never enter realtime payloads
    (members converge via the privacy-filtered snapshot).
    """
    try:
        from app.combat.service import (
            hidden_participant_ids,
            redact_hidden_combatants,
            visible_turn_order,
        )

        channel = live_table_channel(encounter.campaign_id, encounter.thread_id)
        # Thread channels are shared, so hidden combatants are removed for
        # every subscriber; a grantee converges through the snapshot.
        hidden = hidden_participant_ids(db, encounter.id)
        payload = redact_hidden_combatants(
            payload, hidden,
            visible_order=visible_turn_order(encounter, hidden)["turn_order_ids"],
        )
        payload["channel"] = channel
        return _publish_best_effort(channel, payload["type"], payload)
    except Exception as exc:
        logger.warning(
            "publish_encounter failed encounter_id=%s error=%s", getattr(encounter, "id", "?"), exc
        )
        return False


def publish_encounter_started(db: Session, encounter) -> bool:
    """Publish the encounter.started projection (best-effort, post-commit)."""
    try:
        campaign = db.get(Campaign, encounter.campaign_id)
        revision = int(campaign.revision) if campaign and campaign.revision is not None else None
        return _publish_encounter_event(db, encounter, build_encounter_started_event(encounter, revision=revision))
    except Exception as exc:
        logger.warning("publish_encounter_started failed encounter_id=%s error=%s", getattr(encounter, "id", "?"), exc)
        return False


def publish_encounter_ready(db: Session, encounter) -> bool:
    """Publish the encounter.initiative_ready projection (best-effort, post-commit)."""
    try:
        campaign = db.get(Campaign, encounter.campaign_id)
        revision = int(campaign.revision) if campaign and campaign.revision is not None else None
        return _publish_encounter_event(db, encounter, build_encounter_ready_event(encounter, revision=revision))
    except Exception as exc:
        logger.warning("publish_encounter_ready failed encounter_id=%s error=%s", getattr(encounter, "id", "?"), exc)
        return False


def publish_encounter_turn(db: Session, encounter, kind: str) -> bool:
    """Publish a turn progression projection (best-effort, post-commit).
    ``kind`` is ``started`` / ``ended`` / ``skipped``. Same durability
    contract as the lifecycle publishers: the committed domain event is
    authoritative; this is latency-only with stable event ids so replays
    stay idempotent.
    """
    if kind not in ("started", "ended", "skipped"):
        return False
    try:
        campaign = db.get(Campaign, encounter.campaign_id)
        revision = int(campaign.revision) if campaign and campaign.revision is not None else None
        return _publish_encounter_event(db, encounter, build_encounter_turn_event(encounter, kind, revision=revision))
    except Exception as exc:
        logger.warning("publish_encounter_turn failed encounter_id=%s kind=%s error=%s", getattr(encounter, "id", "?"), kind, exc)
        return False


def publish_encounter_ended(db: Session, encounter) -> bool:
    """Publish the ``encounter.ended`` projection (best-effort, post-commit)."""
    try:
        campaign = db.get(Campaign, encounter.campaign_id)
        revision = int(campaign.revision) if campaign and campaign.revision is not None else None
        return _publish_encounter_event(db, encounter, build_encounter_ended_event(encounter, revision=revision))
    except Exception as exc:
        logger.warning("publish_encounter_ended failed encounter_id=%s error=%s", getattr(encounter, "id", "?"), exc)
        return False


def publish_encounter_map(db: Session, encounter) -> bool:
    """Publish the ``encounter.map_updated`` projection (best-effort, post-commit)."""
    try:
        from app.combat.maps import get_map

        encounter_map = get_map(db, encounter.id)
        campaign = db.get(Campaign, encounter.campaign_id)
        revision = int(campaign.revision) if campaign and campaign.revision is not None else None
        return _publish_encounter_event(
            db, encounter,
            build_encounter_map_event(
                encounter,
                map_revision=int(encounter_map.revision or 1) if encounter_map else 0,
                revision=revision,
            ),
        )
    except Exception as exc:
        logger.warning("publish_encounter_map failed encounter_id=%s error=%s", getattr(encounter, "id", "?"), exc)
        return False


def publish_encounter_moved(db: Session, encounter, participant_id, *, move_id: str | None = None) -> bool:
    """Publish the ``encounter.moved`` projection (best-effort, post-commit).

    A hidden combatant's move publishes nothing: no player payload carries
    that combatant, so there is nothing for clients to update and any event
    would reveal that it exists.
    """
    try:
        from app.combat.maps import get_placement
        from app.combat.service import hidden_participant_ids

        if str(participant_id) in hidden_participant_ids(db, encounter.id):
            return True
        placement = get_placement(db, encounter.id, participant_id)
        campaign = db.get(Campaign, encounter.campaign_id)
        revision = int(campaign.revision) if campaign and campaign.revision is not None else None
        payload = build_encounter_moved_event(
            encounter, participant_id,
            to={"col": int(placement.col), "row": int(placement.row)} if placement else {},
            revision=revision,
            move_id=move_id,
        )
        return _publish_encounter_event(db, encounter, payload)
    except Exception as exc:
        logger.warning("publish_encounter_moved failed encounter_id=%s error=%s", getattr(encounter, "id", "?"), exc)
        return False
