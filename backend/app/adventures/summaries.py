"""Derived adventure summaries and player recaps — issue #263.

Canonical rules enforced here:

- Summary/recap rows are derived (``is_derived`` always true) and never
  override event/fact/world authority.
- Generation failure never invalidates the completed adventure.
- Recap text is visibility-filtered; leak validation fails closed.
- Repair/retcon marks artifacts stale; regeneration bumps version.
- The recap served to members is always freshly projected per viewer from
  currently visible source events — cached text from another actor's
  generation is never served cross-viewer.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.events import ACTOR_READABLE_EVENT_VISIBILITIES
from models.campaigns import Adventure, AdventureSummary

logger = logging.getLogger(__name__)

GENERATOR_PROVIDER = "template"
GENERATOR_MODEL = "adventure-recap-v1"


class AdventureError(Exception):
    pass


def _is_hidden(ev) -> bool:
    """Fail-closed mirror of the canonical event-feed visibility rule.

    Only exactly ``"public"`` is globally visible; every other value
    (including unknown strings, empty, or missing) is hidden unless the
    viewer is its actor and the visibility is actor-readable (see
    ``_event_visible_to`` and ``list_campaign_events``).
    """
    return getattr(ev, "visibility", None) != "public"


_WORD_RE = re.compile(r"[a-z0-9]{4,}")


def _event_text(ev) -> str:
    payload = ev.payload or {}
    for key in ("summary", "text", "narration", "description", "content"):
        val = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(val, str) and val.strip():
            return val.strip()
    return f"{ev.event_type} (seq {ev.sequence})"


def _event_visible_to(ev, viewer_id: uuid.UUID | None) -> bool:
    if not _is_hidden(ev):
        return True
    # Same actor rule as the event feed: DM-only events attributed to a
    # human (the AI's events carry the owner as actor) stay hidden.
    return (
        viewer_id is not None
        and ev.actor_id == viewer_id
        and ev.visibility in ACTOR_READABLE_EVENT_VISIBILITIES
    )


def _trusted_adventure_tokens(adventure: Adventure) -> set[str]:
    """Tokens from explicitly member-visible adventure fields.

    Title, outcome, and public_summary are all part of the member-safe
    projection (``to_public_dict``) and are deliberately published to
    players, so the leak validator must never treat them as hidden evidence.
    The DM-private reason and metadata are intentionally excluded.
    """
    toks: set[str] = set()
    for part in (adventure.title, adventure.outcome, adventure.public_summary):
        if part:
            toks.update(_WORD_RE.findall(str(part).lower()))
    return toks


def _validate_no_leak(
    recap_text: str,
    source_events: list,
    *,
    viewer_id: uuid.UUID | None = None,
    trusted_tokens: set[str] | frozenset = frozenset(),
) -> list[str]:
    """Fail closed: recap must not contain tokens unique to sources hidden
    FROM THIS VIEWER.

    Tokens from events the viewer may see (public, or actor-visible to them)
    plus explicitly trusted member-visible adventure fields are always
    permitted; only tokens exclusive to hidden-from-viewer sources are
    forbidden. With ``viewer_id=None`` (the stored public baseline) every
    hidden event counts as forbidden.
    """
    allowed: set[str] = set(trusted_tokens)
    forbidden: set[str] = set()
    for ev in source_events:
        toks = set(_WORD_RE.findall(_event_text(ev).lower()))
        if _event_visible_to(ev, viewer_id):
            allowed.update(toks)
        else:
            forbidden.update(toks)
    leaked = sorted(t for t in _WORD_RE.findall(recap_text.lower()) if t in forbidden - allowed)
    return leaked


def build_historical_text(adventure: Adventure, events: list) -> str:
    """DM-internal durable summary; may compress hidden sources (no human reader)."""
    lines = [
        f"Adventure '{adventure.title}' concluded with outcome: {adventure.outcome or 'unknown'}.",
    ]
    consequence = (adventure.public_summary or "").strip() or (adventure.reason or "").strip()
    if consequence:
        lines.append(f"Outcome: {consequence}")
    if not events:
        lines.append("No recorded domain events in the adventure range.")
    else:
        lines.append(f"Source: {len(events)} domain event(s), sequences {events[0].sequence}–{events[-1].sequence}.")
        for ev in events[:25]:
            scope = "hidden" if _is_hidden(ev) else "public"
            lines.append(f"- [seq {ev.sequence}][{scope}] {ev.event_type}: {_event_text(ev)}")
        if len(events) > 25:
            lines.append(f"- …and {len(events) - 25} more event(s).")
    lines.append("Derived artifact: events/facts/world state outrank this prose on conflict.")
    return "\n".join(lines)


def build_recap_text(adventure: Adventure, visible_events: list) -> str:
    """Player-facing recap built ONLY from viewer-visible events plus the
    member-visible public summary. The DM-private reason never appears here."""
    lines = [f"Recap: {adventure.title} — {adventure.outcome or 'concluded'}."]
    memorable = visible_events[:12]
    if not memorable:
        lines.append("The party's deeds on this adventure are yet to be sung — no public events were recorded.")
    else:
        lines.append("Memorable moments:")
        for ev in memorable:
            lines.append(f"- {_event_text(ev)}")
    if (adventure.public_summary or "").strip():
        lines.append(f"Consequence: {adventure.public_summary.strip()}")
    lines.append("What comes next remains unwritten.")
    return "\n".join(lines)


def _ensure_summary_placeholder(db: Session, adventure: Adventure) -> AdventureSummary:
    existing = db.execute(
        select(AdventureSummary).where(AdventureSummary.adventure_id == adventure.id)
    ).scalars().first()
    if existing is not None:
        return existing
    row = AdventureSummary(
        adventure_id=adventure.id,
        campaign_id=adventure.campaign_id,
        version=1,
        source_event_from=int(adventure.start_sequence or 0),
        source_event_to=adventure.end_sequence,
        source_revision=adventure.end_revision,
        is_derived=True,
        status="pending",
        provider=GENERATOR_PROVIDER,
        model=GENERATOR_MODEL,
    )
    db.add(row)
    db.flush()
    return row


def _source_events(db: Session, adventure: Adventure) -> list:
    from models.campaigns import CampaignDomainEvent

    query = select(CampaignDomainEvent).where(
        CampaignDomainEvent.campaign_id == adventure.campaign_id,
    )
    start = int(adventure.start_sequence or 0)
    query = query.where(CampaignDomainEvent.sequence >= start)
    if adventure.end_sequence is not None:
        query = query.where(CampaignDomainEvent.sequence <= int(adventure.end_sequence))
    return list(db.execute(query.order_by(CampaignDomainEvent.sequence.asc())).scalars().all())


def generate_summary(
    db: Session,
    adventure: Adventure,
    *,
    actor_id: uuid.UUID | None = None,
    commit: bool = True,
    force_fail: bool = False,
) -> AdventureSummary:
    """Generate (or regenerate) the derived summary + player recap.

    Retryable derived work: on failure the row goes to ``failed`` with the
    error recorded, attempts incremented, and any prior text retained but
    flagged stale-equivalent (never presented as current). The adventure's
    completed status is untouched.
    """
    row = _ensure_summary_placeholder(db, adventure)
    row.attempts = int(row.attempts or 0) + 1
    row.error = None
    try:
        if force_fail:
            raise RuntimeError("summary generator unavailable (injected failure)")
        events = _source_events(db, adventure)
        historical = build_historical_text(adventure, events)
        # Stored recap is the PUBLIC baseline only: it must never embed one
        # viewer's actor-visible private content, because cached text could
        # otherwise cross viewers. Per-viewer private projection happens at
        # read time in project_recap().
        visible = [ev for ev in events if _event_visible_to(ev, None)]
        recap = build_recap_text(adventure, visible)
        leaked = _validate_no_leak(
            recap, events, trusted_tokens=_trusted_adventure_tokens(adventure)
        )
        if leaked:
            row.leak_failures = int(row.leak_failures or 0) + 1
            row.validation_failures = int(row.validation_failures or 0) + 1
            raise RuntimeError(f"recap leak validation failed: {', '.join(leaked[:8])}")
        was_rebuild = row.status in ("stale", "failed")
        row.historical_text = historical
        row.recap_text = recap
        row.status = "current"
        row.source_event_from = int(adventure.start_sequence or 0)
        row.source_event_to = adventure.end_sequence
        row.source_revision = adventure.end_revision
        row.summary_metadata = {
            "event_count": len(events),
            "visible_event_count": len(visible),
            "hidden_event_count": len(events) - len(visible),
            "is_derived": True,
        }
        if was_rebuild:
            row.rebuild_count = int(row.rebuild_count or 0) + 1
            row.version = int(row.version or 1) + 1
        logger.info(
            "adventure summary generated adventure_id=%s attempt=%s events=%s status=current",
            adventure.id, row.attempts, len(events),
        )
    except Exception as exc:
        row.status = "failed"
        row.error = str(exc)[:2000]
        logger.warning(
            "adventure summary generation failed adventure_id=%s attempt=%s error=%s",
            adventure.id, row.attempts, exc,
        )
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    return row


def mark_stale(
    db: Session,
    adventure_id: uuid.UUID,
    *,
    reason: str = "repair/retcon",
    commit: bool = True,
) -> AdventureSummary:
    """Mark the derived artifact stale after a repair/retcon (issue #263).

    Prior text is retained but ``status=stale`` so it is never silently
    presented as current; callers then invoke ``generate_summary`` to rebuild.
    """
    row = db.execute(
        select(AdventureSummary).where(AdventureSummary.adventure_id == adventure_id)
    ).scalars().first()
    if row is None:
        raise AdventureError("Adventure summary not found")
    row.status = "stale"
    row.stale_count = int(row.stale_count or 0) + 1
    meta = dict(row.summary_metadata or {})
    meta["stale_reason"] = str(reason)[:500]
    row.summary_metadata = meta
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    logger.info("adventure summary marked stale adventure_id=%s reason=%s", adventure_id, reason)
    return row


def project_recap(
    db: Session,
    adventure: Adventure,
    row: AdventureSummary,
    *,
    viewer_id: uuid.UUID | None = None,
) -> dict:
    """Player-facing recap projection with per-viewer visibility filtering.

    The recap text is ALWAYS freshly built for the requesting viewer from
    currently visible source events — cached text from another actor's
    generation is never served cross-viewer. If the stored row is stale or
    failed, the live projection carries a warning instead of being silently
    presented as current. The adventure is projected through its
    member-safe ``to_public_dict`` so DM-private fields never reach members.
    """
    events = _source_events(db, adventure)
    visible = [ev for ev in events if _event_visible_to(ev, viewer_id)]
    text = build_recap_text(adventure, visible)
    # Defense in depth: the freshly built text derives solely from
    # viewer-visible sources, so viewer-relative validation must always pass;
    # a failure means a builder bug.
    leaked = _validate_no_leak(
        text, events,
        viewer_id=viewer_id,
        trusted_tokens=_trusted_adventure_tokens(adventure),
    )
    if leaked:
        row.leak_failures = int(row.leak_failures or 0) + 1
        logger.warning(
            "recap projection leak adventure_id=%s viewer=%s tokens=%s",
            adventure.id, viewer_id, leaked[:8],
        )
        public_only = [ev for ev in events if not _is_hidden(ev)]
        text = build_recap_text(adventure, public_only)
    warning = (
        "Recap is being rebuilt; showing a live projection of visible events."
        if row.status != "current"
        else None
    )
    row.views = int(row.views or 0) + 1
    db.flush()
    return {
        "adventure": adventure.to_public_dict(),
        "recap_text": text,
        "status": row.status,
        "version": row.version,
        "is_derived": True,
        "authority": "derived: events/facts/world outrank this prose on conflict",
        "stale_warning": warning,
        "source_event_from": row.source_event_from,
        "source_event_to": row.source_event_to,
        "source_revision": row.source_revision,
    }


def finalize_adventure_derived(
    db: Session,
    adventure: Adventure,
    *,
    event_sequence: int | None = None,
    revision: int | None = None,
    actor_id: uuid.UUID | None = None,
) -> AdventureSummary | None:
    """Shared #263 post-completion finalization for EVERY completion path.

    Binds the authoritative end cursor (completion event sequence + campaign
    revision) and creates/generates the derived AdventureSummary. Called by
    the HTTP completion endpoint and the staged DM effect so no supported
    path finishes an adventure without its derived artifacts.

    Best-effort by design: derived-work failures are recorded on the summary
    row (or logged if even the placeholder cannot persist) and never
    invalidate the committed completion. Flush-only — safe inside an
    uncommitted transaction; the caller owns the commit.
    """
    from sqlalchemy import func as _func

    from models.campaigns import Campaign as _Campaign
    from models.campaigns import CampaignDomainEvent as _DomainEvent

    try:
        _from_source_event = False
        if event_sequence is None:
            # Prefer the adventure's own authoritative completion event
            # (deterministic for legacy/repair rows); fall back to the latest
            # visible sequence only when no completion event is linked.
            if adventure.source_event_id is not None:
                _src = db.execute(
                    select(_DomainEvent.sequence).where(
                        _DomainEvent.id == adventure.source_event_id
                    )
                ).scalar()
                if _src is not None:
                    event_sequence = int(_src)
                    _from_source_event = True
            if event_sequence is None:
                event_sequence = db.execute(
                    select(_func.max(_DomainEvent.sequence)).where(
                        _DomainEvent.campaign_id == adventure.campaign_id
                    )
                ).scalar()
        if revision is None:
            if _from_source_event and event_sequence is not None:
                # Domain-event sequence == resulting campaign revision, so a
                # source-event-derived end pins both bounds exactly even when
                # later events exist (legacy repair case).
                revision = int(event_sequence)
            else:
                camp = db.get(_Campaign, adventure.campaign_id)
                revision = camp.revision if camp is not None else None
        if event_sequence is not None:
            adventure.end_sequence = int(event_sequence)
        if revision is not None:
            adventure.end_revision = int(revision)
        with db.begin_nested():
            return generate_summary(db, adventure, actor_id=actor_id, commit=False)
    except Exception as exc:  # noqa: BLE001 — derived work must not break completion
        logger.warning(
            "adventure derived finalization deferred adventure_id=%s error=%s",
            adventure.id, exc,
        )
    try:
        return _ensure_summary_placeholder(db, adventure)
    except Exception as exc:  # noqa: BLE001 — placeholder itself is best-effort here
        logger.warning(
            "adventure summary placeholder deferred adventure_id=%s error=%s",
            adventure.id, exc,
        )
        return None
