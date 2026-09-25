"""Production campaign start — issue #246.

Owner-authorized transition from seeded ``starting`` into the active live
table. Commits the staged #245 world seed exactly once (seed event is the
convergence key upstream), ensures the durable shared gameplay thread,
enqueues the opening DM turn through the production submission/turn
pipeline, and flips ``starting -> active`` atomically.

Replaces the temporary #355 solo bootstrap (already deleted in #245):
there is exactly one production start path for solo and multiplayer.

Transaction boundary: every mutation here is flush-only (commit=False).
The caller — ``execute_http_idempotent()`` — owns the single atomic commit
of the idempotency record + start state. Never commit from inside this
module and never run the DM executor here; opening execution stays
autonomous (cron sweep / inline hook). Failures leave an owed observable
opening turn rather than a second start opportunity.

Privacy: the opening submission is OOC system text naming only public PC
names from the seeded scene. Private lore is never copied into the
opening — secrets may influence later events without being disclosed.
"""

from __future__ import annotations

import logging

from sqlalchemy import select as _select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Domain event emitted exactly once per started campaign.
CAMPAIGN_STARTED_EVENT = "campaign.started_246"

#: Campaign statuses the start job accepts. ``starting`` is the normal path
#: (seeded via #245). ``active`` without a start event converges legacy
#: campaigns that reached active via the raw lifecycle endpoint.
STARTABLE_STATUSES = frozenset({"starting", "active"})

#: Deterministic opening instruction. Keyed for idempotent reuse: retries
#: converge on the opening submission with this source marker, never on
#: merely "a turn exists", so unrelated history cannot suppress the
#: required opening. The marker also keeps projections from rendering this
#: system instruction as player speech.
OPENING_SOURCE = "campaign-start-246"
OPENING_OOC_TEXT = (
    "[Campaign start #246] Begin the adventure: describe the opening scene, "
    "introduce each party member by name, and ask what the party does."
)


class CampaignStartError(ValueError):
    """Start eligibility/validation failure — mapped to 409 at the boundary."""


def _provenance() -> dict:
    return {"source": "campaign-start-246", "issue": 246}


def _find_opening_turn(db: Session, campaign_id, thread_id_str: str):
    """Return (turn, attempt) for the #246 opening submission, if any."""
    from app.dm.turns import get_attempt, list_turns
    from models.threads import PlayerSubmission

    opening = db.execute(
        _select(PlayerSubmission).where(
            PlayerSubmission.campaign_id == campaign_id,
            PlayerSubmission.thread_id == thread_id_str,
            PlayerSubmission.source == OPENING_SOURCE,
        ).order_by(PlayerSubmission.sequence.asc()).limit(1)
    ).scalars().first()
    if opening is None:
        return None, None, None
    for candidate in list_turns(db, campaign_id, thread_id=thread_id_str, limit=20):
        if str(opening.id) in [str(s) for s in (candidate.submission_ids or [])]:
            attempt = (
                get_attempt(db, candidate.current_attempt_id)
                if candidate.current_attempt_id
                else None
            )
            return candidate, attempt, opening
    return None, None, opening


def _ensure_opening_turn(db: Session, campaign, *, thread_id_str: str, owner_id, operation_id: str):
    """Create the opening submission + DM turn once; reuse #246 provenance only."""
    from app.dm.turns import StreamBoundaryError, TurnConflictError, coordinate_turn
    from app.runtime.submissions import accept_submission
    from models.campaigns import CampaignMember

    turn, attempt, opening = _find_opening_turn(db, campaign.id, thread_id_str)
    if turn is not None:
        logger.info(
            "campaign_start opening_turn_reused campaign_id=%s thread_id=%s turn_id=%s",
            campaign.id, thread_id_str, turn.id,
        )
        return turn, attempt, True

    if opening is not None:
        # Opening submission exists but its turn is gone: coordinate fresh.
        try:
            coord = coordinate_turn(db, campaign.id, thread_id_str, audience="campaign", commit=False)
        except (TurnConflictError, StreamBoundaryError) as exc:
            raise CampaignStartError(
                f"Campaign opening is blocked by live turn state: {exc}"
            ) from exc
        if coord is None:
            raise CampaignStartError("Opening turn coordination produced no turn")
        return coord[0], coord[1], False

    # Owner acts as the table opener. Prefer the owner's own selected PC so
    # the submission carries valid character ownership; fall back to
    # character-less system submission when the owner has no PC row.
    owner_member = db.get(CampaignMember, {"campaign_id": campaign.id, "user_id": owner_id})
    character_id = None
    if owner_member is not None and getattr(owner_member, "selected_character_id", None) is not None:
        character_id = owner_member.selected_character_id

    submission = accept_submission(
        db,
        campaign_id=campaign.id,
        user_id=owner_id,
        character_id=character_id,
        raw_content=OPENING_OOC_TEXT,
        segments=[{"type": "ooc", "text": OPENING_OOC_TEXT}],
        thread_id=thread_id_str,
        audience="campaign",
        source=OPENING_SOURCE,
    )
    db.flush()
    try:
        coord = coordinate_turn(db, campaign.id, thread_id_str, audience="campaign", commit=False)
    except (TurnConflictError, StreamBoundaryError) as exc:
        raise CampaignStartError(
            f"Campaign opening is blocked by live turn state: {exc}"
        ) from exc
    if coord is None:
        raise CampaignStartError("Opening turn coordination produced no turn")
    turn, attempt = coord
    logger.info(
        "campaign_start opening_turn campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s submission_id=%s",
        campaign.id, thread_id_str, turn.id, attempt.id, submission.id,
    )
    db.flush()
    return turn, attempt, False


def _started_snapshot(db: Session, campaign, *, replayed: bool) -> dict:
    from app.campaigns.service import compute_start_eligibility
    from app.runtime.threads import get_or_create_campaign_thread
    from models.campaigns import CampaignMember

    members = db.execute(
        _select(CampaignMember).where(CampaignMember.campaign_id == campaign.id)
    ).scalars().all()
    eligibility = compute_start_eligibility(campaign, list(members), db)
    thread = get_or_create_campaign_thread(db, campaign.id, created_by=campaign.owner_id)
    thread_id_str = str(thread.id)
    turn, attempt, _ = _find_opening_turn(db, campaign.id, thread_id_str)
    body: dict = {
        "campaign": campaign.to_dict(),
        "thread_id": thread_id_str,
        "thread": thread.to_dict(),
        "replayed": replayed,
        "eligibility": eligibility,
    }
    if turn is not None:
        body["dm_turn"] = turn.to_dict()
    if attempt is not None:
        from models.campaigns import Campaign as _Campaign

        _ = _Campaign
        body["dm_attempt"] = attempt.to_dict()
    return body


def run_campaign_start(
    db: Session,
    campaign_id,
    *,
    actor_id,
    operation_id: str,
) -> dict:
    """Open the live table for a seeded campaign (flush-only).

    Owner-only. Requires the #245 seed event and a fully ready launch party.
    Moves ``starting -> active`` (or converges an already-active campaign
    missing its start event) and stages the opening DM turn through the
    production pipeline. Re-running after the start event converges
    idempotently. Raises ``CampaignStartError`` on misuse — the campaign
    stays startable and the client retries with corrected inputs.
    """
    from app.campaigns.events import commit_campaign_mutation, has_domain_event
    from app.campaigns.service import (
        CampaignArchivedError, compute_start_eligibility, require_playable_campaign,
    )
    from app.campaigns.world_seed import WORLD_SEEDED_EVENT
    from app.runtime.threads import get_or_create_campaign_thread
    from models.campaigns import Campaign, CampaignMember

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignStartError("Campaign not found")
    if campaign.owner_id != actor_id:
        raise CampaignStartError("Only the owner can start the campaign")
    try:
        require_playable_campaign(campaign)
    except CampaignArchivedError as exc:
        raise CampaignStartError("Archived campaigns cannot be started") from exc

    # Serialize concurrent different-key starts on the campaign row.
    db.execute(_select(Campaign).where(Campaign.id == campaign.id).with_for_update())
    db.refresh(campaign)

    if str(campaign.status or "").lower() not in STARTABLE_STATUSES:
        raise CampaignStartError(
            f"Campaign status {campaign.status} cannot be started "
            "(seed the world first, then open the live table)"
        )
    if not has_domain_event(db, campaign.id, WORLD_SEEDED_EVENT):
        raise CampaignStartError("Campaign world must be seeded before opening the live table")
    if has_domain_event(db, campaign.id, CAMPAIGN_STARTED_EVENT):
        db.refresh(campaign)
        return _started_snapshot(db, campaign, replayed=True)

    members = db.execute(
        _select(CampaignMember).where(CampaignMember.campaign_id == campaign.id)
    ).scalars().all()
    members = sorted(members, key=lambda m: (str(m.user_id), str(m.selected_character_id)))
    eligibility = compute_start_eligibility(campaign, members, db)
    if not eligibility.get("eligible"):
        raise CampaignStartError(
            f"Campaign start not eligible: {'; '.join(eligibility.get('blockers') or [])}"
        )

    # Shared gameplay thread — durable, reused across retries/reconnects.
    thread = get_or_create_campaign_thread(db, campaign.id, created_by=actor_id)
    db.flush()
    thread_id_str = str(thread.id)

    from_status = str(campaign.status)
    holder: dict = {"thread_id": thread_id_str}

    def _mutate(locked):
        # Status flip only. The opening turn is staged AFTER the revision
        # bump below: coordinate_turn captures campaign.revision as the
        # attempt's source_revision, so coordinating inside _mutate would
        # stage a turn that is already stale once this event commits and
        # execution would refuse it (MissingAuthoritativeContextError).
        if str(locked.status or "").lower() == "starting":
            locked.status = "active"
        elif str(locked.status or "").lower() != "active":
            raise CampaignStartError(f"Campaign status {locked.status} cannot be started")

    expected = int(campaign.revision)
    campaign_after, _event = commit_campaign_mutation(
        db, campaign.id, expected, event_type=CAMPAIGN_STARTED_EVENT,
        payload={
            "from": from_status,
            "to": "active",
            "thread_id": thread_id_str,
        },
        operation_id=f"{operation_id}:started",
        actor_id=actor_id,
        targets={"campaign_id": str(campaign.id)},
        visibility="public",
        provenance=_provenance(),
        mutate=_mutate,
        commit=False,
    )
    db.refresh(campaign_after)
    # Same outer idempotent transaction: a turn-staging failure rolls back
    # the activation above, so a retried start can never duplicate the seed,
    # the activation, or the opening narration.
    turn, attempt, reused = _ensure_opening_turn(
        db, campaign_after, thread_id_str=thread_id_str,
        owner_id=actor_id, operation_id=operation_id,
    )
    holder.update({
        "turn_id": str(turn.id) if turn is not None else None,
        "attempt_id": str(attempt.id) if attempt is not None else None,
        "turn_reused": reused,
        "turn": turn.to_dict() if turn is not None else None,
        "attempt": attempt.to_dict() if attempt is not None else None,
    })
    logger.info(
        "campaign_start opened campaign_id=%s %s->%s thread_id=%s turn_id=%s revision=%s",
        campaign.id, from_status, campaign_after.status,
        holder.get("thread_id"), holder.get("turn_id"), campaign_after.revision,
    )
    snapshot = _started_snapshot(db, campaign_after, replayed=False)
    # Prefer the in-transaction staged turn payload (snapshot re-reads the
    # same rows; this keeps the response stable even under lazy expiry).
    if holder.get("turn") is not None:
        snapshot["dm_turn"] = holder["turn"]
    if holder.get("attempt") is not None:
        snapshot["dm_attempt"] = holder["attempt"]
    return snapshot
