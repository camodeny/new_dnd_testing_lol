"""Audience-safe Supabase Realtime projections — issue #198.

Responsibilities:
- Define server-side projection events (submissions, DM chunks/thinking/status, revision)
  with stable event/revision identifiers for dedupe/reconciliation.
- Publish only audience-authorized payloads to private channels.
- Ensure realtime delivery failure never rolls back authoritative DB state.
- Provide snapshot→subscribe race reconciliation helpers.
- Observability counters (publish failures, dedupe, reconciliation, etc.).

Design notes:
- DB writes (submissions, dm chunks) commit authoritatively first; publish is
  best-effort AFTER commit, wrapped in try/except so failures are observable
  but never mutate rollback.
- Payloads are private-channel-safe: we never broadcast hidden canonical state
  to a shared channel. Each thread has its own channel (see channels.py).
- Tests run on SQLite without a real Supabase endpoint — publisher degrades to
  a no-op (or in-memory recorder when monkeypatched) and never raises.
- Optional Supabase Broadcast path: if SUPABASE_URL + SERVICE_ROLE_KEY are
  configured we POST to Realtime broadcast; otherwise we log and count the
  attempt. Outbox path (#190) may be used where appropriate (enqueue rather
  than direct POST) — caller chooses via publish_via_outbox flag.

Stable identifiers:
- submission: event_id = f"submission:{submission.id}"  (also sequence)
- dm chunk:   event_id = f"dm-chunk:{stream_id}:{sequence}"
- dm status:  event_id = f"dm-status:{stream_id}:{status}:{completed_at}"
- revision:   event_id = f"revision:{campaign_id}:{revision}"
All payloads include channel, revision/sequence, and timestamp for ordering.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.realtime.channels import live_table_channel
from models import Campaign, DMStream, DMStreamChunk, PlayerSubmission, PlayerSubmissionSegment

logger = logging.getLogger(__name__)

# ── observability counters (in-memory, process-local) ──────────────────────

_counters: Counter = Counter(
    {
        "publish_attempts": 0,
        "publish_failures": 0,
        "publish_successes": 0,
        "duplicate_deliveries": 0,
        "reconciliation_events": 0,
        "snapshot_catchups": 0,
        "subscription_count": 0,
        "reconnect_count": 0,
    }
)


def get_realtime_metrics() -> dict[str, int]:
    return dict(_counters)


def _inc(key: str, n: int = 1) -> None:
    _counters[key] += n


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


def build_dm_thinking_event(
    campaign_id: uuid.UUID | str,
    thread_id: uuid.UUID | str,
    turn_id: str,
    status: str = "thinking",
    trace_id: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "dm.thinking",
        "event_id": f"dm-thinking:{turn_id}:{status}:{_utcnow_iso()}",
        "campaign_id": str(campaign_id),
        "thread_id": str(thread_id),
        "turn_id": turn_id,
        "status": status,
        "trace_id": trace_id,
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{turn_id}:{status}",
    }


def build_revision_event(campaign: Campaign, *, thread_id: uuid.UUID | str | None = None) -> dict[str, Any]:
    return {
        "type": "revision",
        "event_id": f"revision:{campaign.id}:{campaign.revision}",
        "campaign_id": str(campaign.id),
        "thread_id": str(thread_id) if thread_id else None,
        "revision": int(campaign.revision),
        "timestamp": _utcnow_iso(),
        "dedupe_key": f"{campaign.id}:{campaign.revision}",
    }


# ── publisher abstraction ───────────────────────────────────────────────────

class RealtimePublisher:
    """Pluggable publisher — real Supabase or in-memory for tests."""

    def publish(self, channel: str, event: str, payload: dict[str, Any]) -> bool:
        raise NotImplementedError


class NoopRealtimePublisher(RealtimePublisher):
    """Default: log and count, never fail. Used in tests / when Supabase not configured."""

    def publish(self, channel: str, event: str, payload: dict[str, Any]) -> bool:
        _inc("publish_attempts")
        _inc("publish_successes")
        logger.info("realtime publish channel=%s event=%s event_id=%s", channel, event, payload.get("event_id"))
        return True


class InMemoryRealtimePublisher(RealtimePublisher):
    """Test helper: records publishes for assertions, can inject failures."""

    def __init__(self, *, fail_next: bool = False):
        self.published: list[dict[str, Any]] = []
        self.fail_next = fail_next
        self.fail_all = False

    def publish(self, channel: str, event: str, payload: dict[str, Any]) -> bool:
        _inc("publish_attempts")
        if self.fail_all or self.fail_next:
            self.fail_next = False
            _inc("publish_failures")
            logger.warning("realtime publish injected failure channel=%s event=%s", channel, event)
            raise RuntimeError("injected realtime publish failure")
        rec = {"channel": channel, "event": event, "payload": dict(payload)}
        self.published.append(rec)
        _inc("publish_successes")
        logger.info("realtime publish (memory) channel=%s event=%s event_id=%s", channel, event, payload.get("event_id"))
        return True

    def clear(self) -> None:
        self.published.clear()

    def events_for_channel(self, channel: str) -> list[dict[str, Any]]:
        return [p for p in self.published if p["channel"] == channel]


class SupabaseRealtimePublisher(RealtimePublisher):
    """Best-effort Supabase Broadcast publisher.

    Uses REST broadcast if SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY available.
    Falls back to Noop on any failure — never raises to caller (caller wraps).
    """

    def publish(self, channel: str, event: str, payload: dict[str, Any]) -> bool:
        _inc("publish_attempts")
        url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or ""
        # Private broadcast must use service_role — anon cannot publish to private channels
        # and would be an audience-safety bypass. Intentionally no anon fallback.
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
        if not url or not key:
            _inc("publish_successes")
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
                _inc("publish_failures")
                logger.warning("realtime publish http failure channel=%s event=%s status=%s body=%s", channel, event, resp.status_code, resp.text[:500])
                return False
            _inc("publish_successes")
            logger.info("realtime publish ok channel=%s event=%s", channel, event)
            return True
        except Exception as exc:
            _inc("publish_failures")
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
    """Publish without ever raising — failures are logged + counted."""
    try:
        ok = _publisher.publish(channel, event, payload)
        if not ok:
            # Publisher already counted failure (Supabase path); don't double-count.
            logger.warning("realtime publish returned false channel=%s event=%s event_id=%s", channel, event, payload.get("event_id"))
            return False
        return True
    except Exception as exc:
        # Publisher counted failure for injected cases; if not, count here.
        # We count once in helper as fallback to ensure metric increments even
        # for publishers that raise without counting (future impls).
        # To avoid double-count, check if InMemory already counted: it does,
        # so we skip extra inc when exception message is our injected marker.
        if "injected realtime publish failure" not in str(exc):
            _inc("publish_failures")
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
        _inc("publish_failures")
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
        _inc("publish_failures")
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
        _inc("publish_failures")
        logger.warning("publish_dm_status failed stream_id=%s error=%s", stream.id, exc)
        return False


def publish_revision(
    db: Session,
    campaign: Campaign,
    *,
    thread_id: uuid.UUID | str | None = None,
) -> bool:
    try:
        # If thread_id given, publish to that thread's channel; otherwise no channel publish.
        # Revision is also implied via submission/dm events, but explicit revision event
        # helps clients reconcile.
        if thread_id is None:
            # No channel-scoped revision broadcast — could be campaign-wide but we keep it thread-scoped.
            return True
        channel = live_table_channel(campaign.id, thread_id)
        payload = build_revision_event(campaign, thread_id=thread_id)
        payload["channel"] = channel
        return _publish_best_effort(channel, payload["type"], payload)
    except Exception as exc:
        _inc("publish_failures")
        logger.warning("publish_revision failed campaign_id=%s error=%s", campaign.id, exc)
        return False


# ── deduplication / out-of-order helpers (also used frontend, mirrored here for tests) ──

def dedupe_events(events: list[dict[str, Any]], *, key: str = "event_id") -> list[dict[str, Any]]:
    """Return events deduped by stable key, preserving first-seen order."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        k = str(ev.get(key) or ev.get("dedupe_key") or "")
        if not k:
            # No stable key — keep it (can't dedupe safely)
            out.append(ev)
            continue
        if k in seen:
            _inc("duplicate_deliveries")
            continue
        seen.add(k)
        out.append(ev)
    return out


def is_event_newer(a: dict[str, Any], b: dict[str, Any], *, order_key: str = "sequence") -> bool:
    """True if a is newer than b by order_key (numeric)."""
    try:
        return int(a.get(order_key, 0)) > int(b.get(order_key, 0))
    except Exception:
        return False


def reconcile_buffered_events(
    snapshot: dict[str, Any],
    buffered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter buffered realtime events to those newer than the snapshot.

    Stream-scoped reconciliation (issue #198 review):
    - Submissions: sequence > history_high_water_mark (or revision).
    - DM chunks: per-stream last_sequence — only the active stream's high water
      matters; stale chunks for an old stream or completed streams are dropped.
    - DM status/thinking: per-stream — keep only if stream_id matches the
      active stream or is a new stream not yet in snapshot; never keep a
      stale status for stream A when snapshot now has stream B active, even if
      the event is non-sequenced, to avoid regressing B's projection.
    - Unknown DM stream_ids (new streams created after snapshot) are kept —
      they are strictly newer than snapshot.
    """
    _inc("reconciliation_events")
    recon = snapshot.get("reconciliation", {}) if isinstance(snapshot, dict) else {}
    high_water = recon.get("history_high_water_mark")
    if high_water is None:
        high_water = recon.get("snapshot_sequence")
    if high_water is None:
        high_water = recon.get("snapshot_revision")
    if high_water is None:
        high_water = snapshot.get("revision", 0) if isinstance(snapshot, dict) else 0
    try:
        hw = int(high_water or 0)
    except Exception:
        hw = 0

    dm_state = snapshot.get("dm_state", {}) if isinstance(snapshot, dict) else {}
    dm_hw = dm_state.get("last_sequence")
    try:
        dm_hw_int = int(dm_hw) if dm_hw is not None else -1
    except Exception:
        dm_hw_int = -1
    active_stream_id = None
    try:
        active_stream_id = str(dm_state.get("stream_id")) if dm_state.get("stream_id") else None
        # Some snapshots expose active stream via dm_state.stream_id or id — handle both
        if not active_stream_id and dm_state.get("id"):
            active_stream_id = str(dm_state.get("id"))
    except Exception:
        active_stream_id = None
    # Completed stream ids for drop logic
    dm_messages = snapshot.get("dm_messages", []) if isinstance(snapshot, dict) else []
    completed_ids: set[str] = set()
    completed_last: dict[str, int] = {}
    if isinstance(dm_messages, list):
        for m in dm_messages:
            if isinstance(m, dict) and m.get("id"):
                sid = str(m["id"])
                completed_ids.add(sid)
                # Store last_sequence if present, else infer from chunk_count-1
                ls = m.get("last_sequence")
                if ls is not None:
                    try:
                        completed_last[sid] = int(ls)
                    except Exception:
                        pass
                elif m.get("chunk_count") is not None:
                    try:
                        completed_last[sid] = int(m["chunk_count"]) - 1
                    except Exception:
                        pass
                # Also handle stream_id alias
                if m.get("stream_id"):
                    completed_ids.add(str(m["stream_id"]))

    filtered: list[dict[str, Any]] = []
    for ev in buffered:
        t = ev.get("type")
        seq = ev.get("sequence")
        stream_id = str(ev.get("stream_id")) if ev.get("stream_id") else None

        # Non-sequenced DM status/thinking: still stream-scoped
        if t in ("dm.status", "dm.thinking") and seq is None:
            if stream_id is None:
                filtered.append(ev)
                continue
            # Keep only if stream matches active or is new (not in completed)
            if active_stream_id and stream_id == active_stream_id:
                filtered.append(ev)
            elif stream_id not in completed_ids and stream_id != active_stream_id:
                # New stream not yet in snapshot — keep (strictly newer)
                filtered.append(ev)
            else:
                # Stale status for old completed stream or old active -> drop to avoid regressing B
                continue
            continue

        if seq is None:
            # Generic non-sequenced (e.g. revision) — keep if not DM status handled above
            filtered.append(ev)
            continue
        try:
            seq_int = int(seq)
        except Exception:
            filtered.append(ev)
            continue

        if t == "dm.chunk":
            # Stream-scoped chunk reconciliation
            if stream_id is None:
                # No stream id — fallback to old global check
                if seq_int > dm_hw_int:
                    filtered.append(ev)
                continue
            if active_stream_id and stream_id == active_stream_id:
                if seq_int > dm_hw_int:
                    filtered.append(ev)
            elif stream_id in completed_ids:
                # Completed stream: check per-stream high water if known, else drop
                cl = completed_last.get(stream_id, -1)
                # For completed, any buffered chunk with seq > completed_last would be newer than snapshot?
                # But completed streams should not receive new chunks after completion, so drop duplicates.
                # Keep only if strictly newer than completed's last (would indicate missed chunk before snapshot's finalization)
                # However snapshot's dm_messages already includes final_text, so buffered chunk for completed is stale.
                # We drop unless seq > completed_last and completed_last is known and chunk not in snapshot.
                # Since snapshot's final_text already authoritative, safest is drop.
                # But to support missed chunk that snapshot missed (snapshot taken before chunk commit), we would need
                # to keep if seq > completed_last. That case is when snapshot's completed_last is stale.
                # For safety, keep if seq > cl and cl != -1, else drop.
                if cl != -1 and seq_int > cl:
                    filtered.append(ev)
                # else drop (stale)
            else:
                # Unknown stream (new stream B created after snapshot) — keep
                filtered.append(ev)
        else:
            # Submissions etc.
            if seq_int > hw:
                filtered.append(ev)
    return filtered


def apply_events_to_snapshot(
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a new snapshot dict with events applied (pure, for testing convergence).

    Dedupes and orders events, then appends new submission/dm messages if not
    already present. Used to verify that 'snapshot + missed events = same as
    fresh snapshot'.
    """
    from copy import deepcopy

    snap = deepcopy(snapshot)
    events = dedupe_events(events)
    # Sort by sequence for deterministic apply (but keep stable for non-seq)
    def _seq(ev):
        try:
            return int(ev.get("sequence", 0))
        except Exception:
            return 0

    # Apply only newer events
    reconciled = reconcile_buffered_events(snap, events)
    reconciled_sorted = sorted(reconciled, key=_seq)

    history = snap.get("history", {})
    messages = history.get("messages", []) if isinstance(history, dict) else []
    existing_ids = {m.get("id") for m in messages if isinstance(m, dict)}

    for ev in reconciled_sorted:
        if ev.get("type") == "submission.created":
            eid = ev.get("id")
            if eid and eid not in existing_ids:
                # Reconstruct minimal message shape
                messages.append(
                    {
                        "id": eid,
                        "sequence": ev.get("sequence"),
                        "thread_id": ev.get("thread_id"),
                        "raw_content": ev.get("raw_content"),
                        "segments": ev.get("segments"),
                    }
                )
                existing_ids.add(eid)
        elif ev.get("type") == "dm.chunk":
            # DM chunks contribute to dm_state.visible_text in live snapshot;
            # for convergence check we just ensure deduped set matches persisted.
            pass

    if isinstance(history, dict):
        history["messages"] = sorted(messages, key=lambda m: int(m.get("sequence", 0)))
        snap["history"] = history
    _inc("snapshot_catchups")
    return snap
