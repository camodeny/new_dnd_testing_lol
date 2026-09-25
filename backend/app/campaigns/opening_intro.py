"""Opening-introduction cursor — issue #246 reviewer follow-up.

The production opening narrates every launch PC, but a multiplayer table
also needs a durable record of each character's introduction opportunity
before the table is considered released into normal freeform play. This
module is that record — tracking only, never gating:

- At start, the ordered launch party is frozen into the
  ``campaign.started_246`` event payload (``intro_order``).
- Each launch PC is marked introduced by its own
  ``campaign.opening_intro_advanced`` event the first time a committed
  (succeeded) shared-table turn resolves one of their submissions. The
  system opener never counts (it is nobody's action).
- The cursor (next unintroduced PC, or complete) is derived from events,
  so retries, reconnects, and recovery converge without extra state.
  Advancement is idempotent per character (operation id
  ``opening-intro:{campaign}:{character}``).

The cursor never blocks play: turns coordinate and commit identically
whether the intro is in progress or complete, so no table can strand
waiting on a PC who never acts. Consumers (future UI nudges) read
:func:`get_opening_intro_state`.
"""

from __future__ import annotations

import logging

from sqlalchemy import select as _select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Per-PC introduction marker. Payload: {character_id, character_name,
#: order_index, source_turn_id}.
OPENING_INTRO_ADVANCED_EVENT = "campaign.opening_intro_advanced"


def _start_event(db: Session, campaign_id):
    from app.campaigns.events import latest_domain_event
    from app.campaigns.campaign_start import CAMPAIGN_STARTED_EVENT

    return latest_domain_event(db, campaign_id, CAMPAIGN_STARTED_EVENT)


def _advanced_map(db: Session, campaign_id) -> dict[str, dict]:
    from models.campaigns import CampaignDomainEvent

    events = db.execute(
        _select(CampaignDomainEvent).where(
            CampaignDomainEvent.campaign_id == campaign_id,
            CampaignDomainEvent.event_type == OPENING_INTRO_ADVANCED_EVENT,
        )
    ).scalars().all()
    out: dict[str, dict] = {}
    for ev in events:
        cid = str((ev.payload or {}).get("character_id") or "")
        if cid and cid not in out:
            out[cid] = ev.payload or {}
    return out


def get_opening_intro_state(db: Session, campaign_id) -> dict | None:
    """Derive the opening-introduction cursor, or None when untracked.

    Untracked when the campaign never went through the #246 start (legacy
    actives) or its start predates intro tracking (no ``intro_order``).
    """
    start = _start_event(db, campaign_id)
    if start is None:
        return None
    order = list((start.payload or {}).get("intro_order") or [])
    if not order:
        return None
    advanced = _advanced_map(db, campaign_id)
    introduced = [c for c in order if str(c.get("character_id")) in advanced]
    remaining = [c for c in order if str(c.get("character_id")) not in advanced]
    return {
        "order": order,
        "introduced": introduced,
        "focused": remaining[0] if remaining else None,
        "complete": not remaining,
    }


def maybe_advance_opening_intro(db: Session, campaign_id) -> dict | None:
    """Mark newly-introduced launch PCs from committed shared-table history.

    A launch PC counts as introduced once a succeeded turn on the shared
    campaign thread resolves one of their (non-system) submissions. Emits
    one domain event per newly introduced PC, each on its own revision.
    Returns the refreshed cursor. No-op when untracked or complete.
    Raises on storage failure — callers that must not break a turn commit
    catch and defer (derived state self-heals on the next commit).
    """
    from app.campaigns.events import commit_campaign_mutation
    from models.campaigns import Campaign
    from models.dm import DmTurn
    from models.threads import CampaignThread, PlayerSubmission

    state = get_opening_intro_state(db, campaign_id)
    if state is None or state["complete"]:
        return state

    shared = db.execute(
        _select(CampaignThread).where(
            CampaignThread.campaign_id == campaign_id,
            CampaignThread.thread_type == "campaign",
        )
    ).scalars().first()
    if shared is None:
        return state
    thread_id_str = str(shared.id)

    succeeded = db.execute(
        _select(DmTurn).where(
            DmTurn.campaign_id == campaign_id,
            DmTurn.thread_id == thread_id_str,
            DmTurn.status == "succeeded",
        )
    ).scalars().all()
    covered_sub_ids: set[str] = set()
    for turn in succeeded:
        for sid in (turn.submission_ids or []):
            covered_sub_ids.add(str(sid))
    if not covered_sub_ids:
        return state
    import uuid as _uuid

    try:
        sub_uuids = [_uuid.UUID(s) for s in covered_sub_ids]
    except ValueError:
        return state
    subs = db.execute(
        _select(PlayerSubmission).where(PlayerSubmission.id.in_(sub_uuids))
    ).scalars().all()
    # Credit by table membership, not raw character tags: a launch PC is
    # introduced once its owner's member row has a resolved non-system
    # submission covered by a succeeded shared-table turn. Character-less
    # table talk from the same player still counts as their moment.
    from models.campaigns import CampaignMember as _CampaignMember

    members = db.execute(
        _select(_CampaignMember).where(_CampaignMember.campaign_id == campaign_id)
    ).scalars().all()
    user_by_char = {
        str(m.selected_character_id): str(m.user_id)
        for m in members
        if m.selected_character_id is not None
    }
    covered_users = {
        str(s.user_id)
        for s in subs
        if s.user_id is not None and not s.source
    }
    covered_chars = {
        cid for cid, uid in user_by_char.items() if uid in covered_users
    }

    advanced = _advanced_map(db, campaign_id)
    fresh = False
    for index, entry in enumerate(state["order"]):
        cid = str(entry.get("character_id"))
        if cid in advanced or cid not in covered_chars:
            continue
        campaign = db.get(Campaign, campaign_id)
        expected = int(campaign.revision) if campaign is not None else 0
        commit_campaign_mutation(
            db, campaign_id, expected,
            event_type=OPENING_INTRO_ADVANCED_EVENT,
            payload={
                "character_id": cid,
                "character_name": entry.get("character_name"),
                "order_index": index,
            },
            operation_id=f"opening-intro:{campaign_id}:{cid}",
            actor_id=None,
            targets={"campaign_id": str(campaign_id)},
            visibility="public",
            provenance={"source": "campaign-start-246", "issue": 246},
            commit=True,
        )
        logger.info(
            "opening_intro advanced campaign_id=%s character_id=%s order_index=%s",
            campaign_id, cid, index,
        )
        fresh = True
    if fresh:
        return get_opening_intro_state(db, campaign_id)
    return state
