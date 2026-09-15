"""Adventure lifecycle + derived summary/recap service — issue #263.

Canonical rules enforced here:

- Completion is authoritative (``commit_campaign_mutation`` /
  ``adventure.completed``); the campaign stays continuable.
- Summary/recap rows are derived (``is_derived`` always true) and never
  override event/fact/world authority.
- Generation failure never invalidates the completed adventure.
- Recap text is visibility-filtered; leak validation fails closed.
- Repair/retcon marks artifacts stale; regeneration bumps version.
"""

from __future__ import annotations

import logging
import re
import uuid as uuid_lib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.adventures import (
    ADVENTURE_OUTCOMES,
    HIDDEN_VISIBILITIES,
    Adventure,
    AdventureSummary,
)

logger = logging.getLogger(__name__)

GENERATOR_PROVIDER = "template"
GENERATOR_MODEL = "adventure-recap-v1"


class AdventureError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_outcome(outcome: str | None) -> str:
    value = str(outcome or "").strip().lower()
    if value not in ADVENTURE_OUTCOMES:
        raise AdventureError(
            f"Outcome must be one of: {', '.join(sorted(ADVENTURE_OUTCOMES))}"
        )
    return value


def validate_title(title) -> str:
    value = str(title or "").strip() or "Untitled Adventure"
    if len(value) > 256:
        raise AdventureError("Adventure title must be 256 characters or fewer")
    return value


def create_adventure(
    db: Session,
    campaign_id: uuid_lib.UUID,
    *,
    title: str = "Untitled Adventure",
    start_sequence: int = 0,
    idempotency_key: str | None = None,
    extra: dict | None = None,
    commit: bool = True,
) -> Adventure:
    """Open a new adventure arc in a campaign (non-fictional setup row)."""
    title = validate_title(title)
    if idempotency_key:
        existing = db.execute(
            select(Adventure).where(
                Adventure.campaign_id == campaign_id,
                Adventure.idempotency_key == idempotency_key,
            )
        ).scalars().first()
        if existing is not None:
            return existing
    adv = Adventure(
        campaign_id=campaign_id,
        title=title,
        status="open",
        start_sequence=int(start_sequence or 0),
        idempotency_key=idempotency_key,
        extra=extra or {},
    )
    db.add(adv)
    db.flush()
    if commit:
        db.commit()
        db.refresh(adv)
    logger.info(
        "adventure opened adventure_id=%s campaign_id=%s title=%s",
        adv.id, campaign_id, title,
    )
    return adv


def complete_adventure(
    db: Session,
    campaign_id: uuid_lib.UUID,
    adventure_id: uuid_lib.UUID,
    *,
    outcome: str,
    outcome_reason: str | None = None,
    expected_revision: int,
    operation_id: str | None = None,
    actor_id: uuid_lib.UUID | None = None,
    source_turn_id: uuid_lib.UUID | None = None,
    source_attempt_id: uuid_lib.UUID | None = None,
    commit: bool = True,
) -> tuple[Adventure, object]:
    """DM-declared adventure completion — authoritative fictional mutation.

    Idempotent on ``operation_id``: a retried completion reuses the existing
    adventure row / domain event instead of duplicating. Downstream
    summary/recap generation is best-effort derived work queued after commit
    and never blocks completion.
    """
    from app.campaigns.events import commit_campaign_mutation

    outcome = validate_outcome(outcome)
    if outcome_reason is not None and len(str(outcome_reason)) > 4000:
        raise AdventureError("Outcome reason must be 4000 characters or fewer")

    adv = db.get(Adventure, adventure_id)
    if adv is None or adv.campaign_id != campaign_id:
        raise AdventureError("Adventure not found")
    if adv.status == "completed":
        # Idempotent replay: return current state + no new event.
        existing_event = None
        if operation_id:
            from models.campaigns import CampaignDomainEvent

            existing_event = db.execute(
                select(CampaignDomainEvent).where(
                    CampaignDomainEvent.campaign_id == campaign_id,
                    CampaignDomainEvent.operation_id == operation_id,
                )
            ).scalars().first()
        return adv, existing_event

    def _mutate(campaign) -> None:
        locked = db.get(Adventure, adventure_id)
        if locked is None:
            raise AdventureError("Adventure not found")
        if locked.status == "completed":
            return
        locked.status = "completed"
        locked.outcome = outcome
        locked.outcome_reason = (
            str(outcome_reason).strip() or None if outcome_reason else None
        )
        locked.source_turn_id = source_turn_id
        locked.source_attempt_id = source_attempt_id
        locked.completed_at = _utcnow()

    def _payload():
        fresh = db.get(Adventure, adventure_id)
        return {
            "adventure_id": str(adventure_id),
            "title": fresh.title if fresh else None,
            "outcome": outcome,
            "outcome_reason": str(outcome_reason).strip() if outcome_reason else None,
            "source_turn_id": str(source_turn_id) if source_turn_id else None,
            "source_attempt_id": str(source_attempt_id) if source_attempt_id else None,
        }

    campaign, event = commit_campaign_mutation(
        db,
        campaign_id,
        expected_revision,
        event_type="adventure.completed",
        operation_id=operation_id,
        actor_id=actor_id,
        targets={"adventure_id": str(adventure_id)},
        visibility="public",
        provenance={"adventure_id": str(adventure_id), "outcome": outcome},
        mutate=_mutate,
        payload_builder=_payload,
        commit=False,
    )
    # Bind authoritative source provenance onto the adventure row.
    fresh = db.get(Adventure, adventure_id)
    fresh.source_event_id = event.id
    fresh.end_sequence = event.sequence
    fresh.end_revision = campaign.revision
    # Ensure a pending derived-summary placeholder exists (retryable work).
    _ensure_summary_placeholder(db, fresh)
    # Kick off best-effort generation in-transaction (savepoint-guarded so a
    # generator crash can never roll back the authoritative completion).
    try:
        with db.begin_nested():
            generate_summary(
                db, fresh, actor_id=actor_id, commit=False,
                source_event_to_override=event.sequence,
                source_revision_override=campaign.revision,
            )
    except Exception as exc:  # noqa: BLE001 — derived work must not break completion
        logger.warning(
            "adventure summary auto-generate deferred adventure_id=%s error=%s",
            adventure_id, exc,
        )
    if commit:
        db.commit()
        db.refresh(fresh)
        db.refresh(campaign)
        db.refresh(event)
    else:
        db.flush()
    logger.info(
        "adventure completed adventure_id=%s campaign_id=%s outcome=%s revision=%s",
        adventure_id, campaign_id, outcome, campaign.revision,
    )
    return fresh, event


# ── Derived summary/recap generation ──────────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9]{4,}")


def _event_text(ev) -> str:
    payload = ev.payload or {}
    for key in ("summary", "text", "narration", "description", "content"):
        val = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(val, str) and val.strip():
            return val.strip()
    return f"{ev.event_type} (seq {ev.sequence})"


def _is_hidden(ev) -> bool:
    return str(getattr(ev, "visibility", "public") or "public").strip().lower() in HIDDEN_VISIBILITIES


def _event_visible_to(ev, viewer_id: uuid_lib.UUID | None) -> bool:
    if not _is_hidden(ev):
        return True
    # Actor-visible private events stay visible to their actor only.
    return viewer_id is not None and ev.actor_id == viewer_id


def _private_tokens(events: list) -> set[str]:
    tokens: set[str] = set()
    for ev in events:
        if _is_hidden(ev):
            tokens.update(_WORD_RE.findall(_event_text(ev).lower()))
    return tokens


def _validate_no_leak(recap_text: str, source_events: list) -> list[str]:
    """Fail closed: recap must not contain tokens unique to hidden sources."""
    public_tokens: set[str] = set()
    private_tokens: set[str] = set()
    for ev in source_events:
        toks = set(_WORD_RE.findall(_event_text(ev).lower()))
        if _is_hidden(ev):
            private_tokens.update(toks)
        else:
            public_tokens.update(toks)
    leaked = sorted(t for t in _WORD_RE.findall(recap_text.lower()) if t in private_tokens - public_tokens)
    return leaked


def build_historical_text(adventure: Adventure, events: list) -> str:
    lines = [
        f"Adventure '{adventure.title}' concluded with outcome: {adventure.outcome or 'unknown'}.",
    ]
    if adventure.outcome_reason:
        lines.append(f"Outcome: {adventure.outcome_reason.strip()}")
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
    lines = [f"Recap: {adventure.title} — {adventure.outcome or 'concluded'}."]
    memorable = visible_events[:12]
    if not memorable:
        lines.append("The party's deeds on this adventure are yet to be sung — no public events were recorded.")
    else:
        lines.append("Memorable moments:")
        for ev in memorable:
            lines.append(f"- {_event_text(ev)}")
    if adventure.outcome_reason:
        lines.append(f"Consequence: {adventure.outcome_reason.strip()}")
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
    actor_id: uuid_lib.UUID | None = None,
    commit: bool = True,
    force_fail: bool = False,
    source_event_to_override: int | None = None,
    source_revision_override: int | None = None,
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
        leaked = _validate_no_leak(recap, events)
        if leaked:
            row.leak_failures = int(row.leak_failures or 0) + 1
            row.validation_failures = int(row.validation_failures or 0) + 1
            raise RuntimeError(f"recap leak validation failed: {', '.join(leaked[:8])}")
        was_rebuild = row.status in ("stale", "failed")
        row.historical_text = historical
        row.recap_text = recap
        row.status = "current"
        row.source_event_from = int(adventure.start_sequence or 0)
        row.source_event_to = (
            source_event_to_override
            if source_event_to_override is not None
            else adventure.end_sequence
        )
        row.source_revision = (
            source_revision_override
            if source_revision_override is not None
            else adventure.end_revision
        )
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
    adventure_id: uuid_lib.UUID,
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
    viewer_id: uuid_lib.UUID | None = None,
) -> dict:
    """Player-facing recap projection with per-viewer visibility filtering.

    The recap text is ALWAYS freshly built for the requesting viewer from
    currently visible source events — cached text from another actor's
    generation is never served cross-viewer. If the stored row is stale or
    failed, the live projection carries a warning instead of being silently
    presented as current.
    """
    events = _source_events(db, adventure)
    visible = [ev for ev in events if _event_visible_to(ev, viewer_id)]
    text = build_recap_text(adventure, visible)
    # Defense in depth: the freshly built text derives solely from visible
    # sources, so this must always pass; a failure means a builder bug.
    leaked = _validate_no_leak(text, events)
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
        "adventure": adventure.to_dict(),
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
