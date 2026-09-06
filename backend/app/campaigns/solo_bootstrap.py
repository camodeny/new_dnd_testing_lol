"""Minimal solo campaign bootstrap into the production live-table runtime — issue #355.

Pre-alpha scaffold for dogfooding. Deleted/replaced once #245/#246 provide the
full production start path; do NOT preserve compatibility with this bootstrap.

What it does (owner-authorized, solo only):
- Requires exactly one joined member with one valid selected/ready PC (#241).
- Reuses authoritative campaign lifecycle (#240): lobby -> starting -> active.
- Ensures the durable shared gameplay thread exists (no duplicate threads).
- Seeds the minimum authoritative starting scene required by the DM context
  assembler: selected PC, campaign settings, a small deterministic starting
  scene/location/premise. Explicitly NOT the full #245 canonical world seed.
- Enqueues the opening DM turn through the same production
  submission/turn pipeline (#354): a real PlayerSubmission + coordinate_turn.
  Execution itself stays autonomous (cron sweep / inline hook); failures leave
  one owed observable opening turn rather than a second campaign start.
- Idempotent under repeated clicks (state-guarded, not just key-guarded):
  existing thread / scene / lifecycle state / opening turn are reused.
- Marks bootstrap origin via event provenance + scene operation_id so the
  scaffold can be removed cleanly when #245/#246 land.

Transaction boundary: every mutation here is flush-only (commit=False).
The caller — ``execute_http_idempotent()`` — owns the single atomic commit
of the idempotency record + all bootstrap state. Never commit from inside
this module and never run the DM executor here; the router triggers
best-effort execution after the outer commit succeeds.

Does NOT build: parallel /test-dm endpoint, second frontend surface,
client-only transcript, world graph generation, multiplayer start.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Marker for temporary scaffold state — grep for this when deleting for #245/#246.
SOLO_BOOTSTRAP_TAG = "solo-bootstrap-355"
OPENING_OOC_TEXT = (
    "[Solo bootstrap #355 pre-alpha] Begin the adventure: "
    "describe the opening scene and ask what I do."
)
BOOTSTRAP_LOCATION_NAME = "Emberhold Tavern"
BOOTSTRAP_FICTIONAL_TIME = "Evening of the first day"
BOOTSTRAP_PREMISE = (
    "Pre-alpha solo bootstrap (#355): the hero rests at the Emberhold Tavern "
    "as strange lights move beyond the fog. This is a minimal deterministic "
    "starting premise, not the canonical world seed (#245)."
)


class SoloBootstrapError(ValueError):
    """Eligibility/validation failure — mapped to 409/422 at the boundary."""


def _solo_eligibility_blockers(campaign, members: list, db: Session) -> list[str]:
    """Solo-specific checks on top of the authoritative lobby eligibility."""
    from app.campaigns.service import compute_start_eligibility

    blockers: list[str] = []
    required = int(getattr(campaign, "required_players", 1) or 1)
    if required != 1:
        blockers.append(f"Solo bootstrap requires required_players=1 (have {required})")
    if len(members) != 1:
        blockers.append(f"Solo bootstrap requires exactly one member (have {len(members)})")
    eligibility = compute_start_eligibility(campaign, list(members), db)
    blockers.extend(eligibility.get("blockers") or [])
    return blockers


def _ensure_lifecycle_active(db: Session, campaign, *, actor_id, operation_id: str):
    """Advance lobby -> starting -> active idempotently; no-op when active."""
    from app.campaigns.events import commit_campaign_mutation

    provenance = {
        "source": SOLO_BOOTSTRAP_TAG,
        "issue": 355,
        "temporary": True,
        "replaced_by": "#245/#246",
    }
    # Refresh to current revision before each step.
    db.refresh(campaign)
    if campaign.status == "active":
        return campaign
    steps = []
    if campaign.status == "lobby":
        steps.append("starting")
    if campaign.status == "starting" or (steps and steps[-1] == "starting"):
        steps.append("active")
    if campaign.status not in ("lobby", "starting"):
        raise SoloBootstrapError(f"Campaign status {campaign.status} cannot solo-bootstrap")
    for target in steps:
        expected = int(campaign.revision)
        from_status = campaign.status

        def _mutate(locked, _target=target):
            locked.status = _target

        campaign, _ = commit_campaign_mutation(
            db,
            campaign.id,
            expected,
            event_type=f"campaign.lifecycle.{target}",
            operation_id=f"{operation_id}:lifecycle:{target}",
            actor_id=actor_id,
            payload={
                "from": from_status,
                "to": target,
                SOLO_BOOTSTRAP_TAG: True,
            },
            provenance=provenance,
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "solo_bootstrap lifecycle campaign_id=%s %s->%s revision=%s",
            campaign.id, from_status, target, campaign.revision,
        )
    db.refresh(campaign)
    return campaign


def _ensure_bootstrap_scene(db: Session, campaign, *, pc_name: str, actor_id, operation_id: str):
    """Create the minimal deterministic starting scene once; reuse afterwards."""
    from app.campaigns.events import commit_campaign_mutation
    from app.world.service import apply_scene_update_inline, create_entity_inline
    from models.world import CampaignCurrentScene

    existing = db.get(CampaignCurrentScene, campaign.id)
    if existing is not None:
        return existing, False

    entity_key = f"{SOLO_BOOTSTRAP_TAG}:location:{campaign.id}"
    holder: dict = {}

    def _mutate(locked):
        prior = int(locked.revision) if locked.revision is not None else 0
        entity, _ = create_entity_inline(
            db,
            locked,
            entity_type="location",
            name=BOOTSTRAP_LOCATION_NAME,
            summary="Pre-alpha solo bootstrap starting location (not canonical world seed).",
            status="active",
            visibility="campaign",
            details={"bootstrap": SOLO_BOOTSTRAP_TAG, "temporary": True},
            operation_id=f"{operation_id}:location",
            idempotency_key=entity_key,
        )
        holder["location_entity_id"] = entity.id
        scene = apply_scene_update_inline(
            db,
            locked,
            new_revision=prior + 1,
            location_entity_id=entity.id,
            location_name=BOOTSTRAP_LOCATION_NAME,
            fictional_time=BOOTSTRAP_FICTIONAL_TIME,
            present_actors=[{"name": pc_name, "kind": "pc", "role": "protagonist"}],
            environment={
                "premise": BOOTSTRAP_PREMISE,
                "bootstrap": SOLO_BOOTSTRAP_TAG,
                "temporary": True,
                "replaced_by": "#245/#246",
            },
            visibility="campaign",
            operation_id=f"{operation_id}:scene",
        )
        holder["scene"] = scene.to_dict()

    expected = int(campaign.revision)
    campaign_after, _ = commit_campaign_mutation(
        db,
        campaign.id,
        expected,
        event_type="world.scene_bootstrapped_solo_355",
        payload_builder=lambda: {
            "scene": holder.get("scene"),
            "location_entity_id": str(holder.get("location_entity_id")),
            SOLO_BOOTSTRAP_TAG: True,
        },
        operation_id=f"{operation_id}:scene",
        actor_id=actor_id,
        targets={"campaign_id": str(campaign.id)},
        visibility="public",
        provenance={
            "source": SOLO_BOOTSTRAP_TAG,
            "issue": 355,
            "temporary": True,
            "replaced_by": "#245/#246",
        },
        mutate=_mutate,
        commit=False,
    )
    db.refresh(campaign_after)
    scene = db.get(CampaignCurrentScene, campaign.id)
    logger.info(
        "solo_bootstrap scene campaign_id=%s location=%s revision=%s",
        campaign.id, BOOTSTRAP_LOCATION_NAME, campaign_after.revision,
    )
    return scene, True


def _ensure_opening_turn(db: Session, campaign, *, thread_id_str: str, owner_id, character_id, operation_id: str):
    """Create the opening submission + DM turn once; reuse only bootstrap provenance.

    Reuse is keyed to the bootstrap opening submission (deterministic
    ``OPENING_OOC_TEXT``), never to merely "a turn exists": unrelated lobby
    history must not suppress the required opening. If live turn state makes
    a new turn impossible, fail explicitly instead of substituting history.
    """
    from sqlalchemy import select as _select

    from app.dm.turns import (
        StreamBoundaryError,
        TurnConflictError,
        coordinate_turn,
        get_attempt,
        list_turns,
    )
    from app.runtime.submissions import accept_submission
    from models.threads import PlayerSubmission

    def _turn_for_submission(submission_id) -> tuple | None:
        for candidate in list_turns(db, campaign.id, thread_id=thread_id_str, limit=20):
            if str(submission_id) in [str(s) for s in (candidate.submission_ids or [])]:
                attempt = get_attempt(db, candidate.current_attempt_id) if candidate.current_attempt_id else None
                return candidate, attempt
        return None

    opening = db.execute(
        _select(PlayerSubmission).where(
            PlayerSubmission.campaign_id == campaign.id,
            PlayerSubmission.thread_id == thread_id_str,
            PlayerSubmission.raw_content == OPENING_OOC_TEXT,
        ).order_by(PlayerSubmission.sequence.asc()).limit(1)
    ).scalars().first()
    if opening is not None:
        found = _turn_for_submission(opening.id)
        if found is not None:
            turn, attempt = found
            logger.info(
                "solo_bootstrap opening_turn_reused campaign_id=%s thread_id=%s turn_id=%s",
                campaign.id, thread_id_str, turn.id,
            )
            return turn, attempt, True
        # Opening submission exists but its turn is gone (e.g. abandoned):
        # coordinate a fresh turn around it.
        try:
            coord = coordinate_turn(db, campaign.id, thread_id_str, audience="campaign", commit=False)
        except (TurnConflictError, StreamBoundaryError) as exc:
            raise SoloBootstrapError(
                f"Solo bootstrap opening is blocked by live turn state: {exc}"
            ) from exc
        if coord is None:
            raise SoloBootstrapError("Opening turn coordination produced no turn")
        return coord[0], coord[1], False

    submission = accept_submission(
        db,
        campaign_id=campaign.id,
        user_id=owner_id,
        character_id=character_id,
        raw_content=OPENING_OOC_TEXT,
        segments=[{"type": "ooc", "text": OPENING_OOC_TEXT}],
        thread_id=thread_id_str,
        audience="campaign",
    )
    db.flush()
    try:
        coord = coordinate_turn(db, campaign.id, thread_id_str, audience="campaign", commit=False)
    except (TurnConflictError, StreamBoundaryError) as exc:
        raise SoloBootstrapError(
            f"Solo bootstrap opening is blocked by live turn state: {exc}"
        ) from exc
    if coord is None:
        raise SoloBootstrapError("Opening turn coordination produced no turn")
    turn, attempt = coord
    logger.info(
        "solo_bootstrap opening_turn campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s submission_id=%s",
        campaign.id, thread_id_str, turn.id, attempt.id, submission.id,
    )
    # No DM execution here: this runs inside the outer idempotent
    # transaction (flush-only). The router triggers best-effort execution
    # after the atomic commit; provider failures leave the pending turn as
    # the owed, observable opening turn.
    db.flush()
    return turn, attempt, False


def run_solo_bootstrap(
    db: Session,
    campaign_id,
    *,
    actor_id,
    operation_id: str,
):
    """Owner-authorized solo bootstrap. Returns response dict. Raises on misuse.

    Flush-only: the caller owns the atomic commit. Concurrent different-key
    starts serialize on the campaign row below, so a revision conflict inside
    here is unexpected — it aborts the whole idempotent command (no partial
    state, no stranded record) and the client retries the same key, which
    then converges via the reuse paths. Never roll back or retry in here:
    the outer transaction owns the idempotency record.
    """
    from sqlalchemy import select as _select

    from app.campaigns.service import compute_start_eligibility
    from app.runtime.threads import get_or_create_campaign_thread
    from models.campaigns import Campaign, CampaignMember
    from models.characters import Character

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise SoloBootstrapError("Campaign not found")
    if campaign.owner_id != actor_id:
        raise SoloBootstrapError("Only the owner can start the solo bootstrap")
    # Serialize concurrent different-key starts on the campaign row: the
    # winner commits first and the loser re-reads canonical state after it.
    # (No-op where the dialect ignores row locks; the revision guard plus the
    # run-level retry above still converge.)
    db.execute(
        _select(Campaign).where(Campaign.id == campaign.id).with_for_update()
    )
    db.refresh(campaign)
    members = db.execute(
        _select(CampaignMember).where(CampaignMember.campaign_id == campaign.id)
    ).scalars().all()
    members = list(members)

    logger.info(
        "solo_bootstrap start campaign_id=%s actor_id=%s op=%s status=%s member_count=%s",
        campaign.id, actor_id, operation_id, campaign.status, len(members),
    )
    blockers = _solo_eligibility_blockers(campaign, members, db)
    if blockers:
        logger.warning(
            "solo_bootstrap ineligible campaign_id=%s actor_id=%s blockers=%s",
            campaign.id, actor_id, blockers,
        )
        raise SoloBootstrapError(f"Solo bootstrap not eligible: {'; '.join(blockers)}")

    member = members[0]
    char = db.get(Character, member.selected_character_id)
    pc_name = (char.name or "Hero").strip() or "Hero"

    # Shared gameplay thread — durable, reused across retries/reconnects.
    thread = get_or_create_campaign_thread(db, campaign.id, created_by=actor_id)
    db.flush()
    thread_id_str = str(thread.id)
    logger.info(
        "solo_bootstrap thread campaign_id=%s thread_id=%s op=%s",
        campaign.id, thread_id_str, operation_id,
    )

    # Lifecycle before scene/turn so source_revision reflects the active table.
    campaign = _ensure_lifecycle_active(db, campaign, actor_id=actor_id, operation_id=operation_id)

    scene, scene_reused = _ensure_bootstrap_scene(
        db, campaign, pc_name=pc_name, actor_id=actor_id, operation_id=operation_id
    )
    # Scene commit bumped revision — refresh before turn assembly.
    db.refresh(campaign)

    turn, attempt, turn_reused = _ensure_opening_turn(
        db,
        campaign,
        thread_id_str=thread_id_str,
        owner_id=actor_id,
        character_id=member.selected_character_id,
        operation_id=operation_id,
    )

    eligibility = compute_start_eligibility(campaign, members, db)
    db.refresh(campaign)
    return {
        "campaign": campaign.to_dict(),
        "thread_id": thread_id_str,
        "thread": thread.to_dict(),
        "scene": scene.to_dict() if scene else None,
        "dm_turn": turn.to_dict() if turn else None,
        "dm_attempt": attempt.to_dict() if attempt else None,
        "replayed": bool(scene_reused and turn_reused and campaign.status == "active"),
        "solo_bootstrap": True,
        "temporary": True,
        "replaced_by": "#245/#246",
        "eligibility": eligibility,
    }
