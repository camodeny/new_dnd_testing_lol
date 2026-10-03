"""Campaign CRUD, settings, lifecycle, world seed, start, and the event feed."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.campaigns import lifecycle
from app.campaigns.events import RevisionConflictError, list_campaign_events
from app.campaigns.service import (
    CampaignCommandError,
    campaign_settings_changes,
    create_campaign,
    random_brief,
    update_campaign_settings,
    validate_campaign_name,
    validate_seed,
    validated_setup,
    visible_campaigns,
)
from app.deps.auth import current_profile
from app.deps.campaign import (
    campaign_for,
    parse_campaign_id_or_404,
    require_expected_revision,
    run_campaign_command,
)
from app.deps.idempotency import command_keys, execute_http_idempotent
from database import get_db
from models.campaigns import Campaign

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/campaigns")
def list_campaigns(include_archived: bool = False, profile=Depends(current_profile), db: Session = Depends(get_db)):
    """Active campaign surfaces — issue #265.

    Archived campaigns are dormant and hidden by default; authorized
    review/history access passes ``?include_archived=true``. Detail,
    snapshot, events, and member reads stay available to members regardless.
    """
    campaigns = visible_campaigns(db, profile.id, include_archived=include_archived)
    return {"campaigns": [c.to_dict() for c in campaigns]}


@router.post("/api/campaigns")
def create_campaign_endpoint(payload: dict, profile=Depends(current_profile), db: Session = Depends(get_db)):
    try:
        name = validate_campaign_name(payload.get("name") or "")
        random_seed = validate_seed(payload.get("random_seed"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    campaign = create_campaign(
        db, profile.id,
        name=name,
        description=payload.get("description"),
        random_seed=random_seed,
        setup=validated_setup(payload, creation=True),
    )
    return {"campaign": campaign.to_dict()}


@router.post("/api/campaigns/random-brief")
def random_campaign_brief(payload: dict, profile=Depends(current_profile)):
    seed = payload.get("random_seed") or None
    return random_brief(seed if isinstance(seed, str) else None)


@router.post("/api/campaigns/quick-create")
def quick_create_campaign(payload: dict, profile=Depends(current_profile), db: Session = Depends(get_db)):
    brief = random_brief()
    campaign = create_campaign(
        db, profile.id,
        name=brief["name"],
        description=brief["description"],
        random_seed=brief["random_seed"],
        setup=validated_setup(payload, creation=True),
    )
    return {"campaign": campaign.to_dict(), "brief": brief}


@router.get("/api/campaigns/{campaign_id}")
def get_campaign(
    campaign: Campaign = Depends(campaign_for("participant", live_only=True)),
    db: Session = Depends(get_db),
):
    return {"campaign": campaign.to_dict(), "restore_from": lifecycle.restore_target(db, campaign)}


@router.delete("/api/campaigns/{campaign_id}")
def delete_campaign(
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only the owner can delete this campaign")),
    db: Session = Depends(get_db),
):
    campaign.is_deleted = True
    db.commit()
    return {"ok": True}


@router.put("/api/campaigns/{campaign_id}")
def update_campaign(
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only owner can update")),
    db: Session = Depends(get_db),
):
    changes = campaign_settings_changes(payload)
    expected_revision = require_expected_revision(payload)
    if not changes:
        raise HTTPException(status_code=400, detail="At least one campaign setting is required")
    operation_id, idempotency_key = command_keys(request, payload)
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.settings_updated", scope_type="campaign", scope_id=campaign.id,
        payload=payload,
        execute=lambda: update_campaign_settings(
            db, campaign.id, actor_id=profile.id, expected_revision=expected_revision,
            operation_id=operation_id or idempotency_key, changes=changes,
        ),
    )


@router.post("/api/campaigns/{campaign_id}/lifecycle")
def transition_campaign_lifecycle(
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only owner can change campaign lifecycle")),
    db: Session = Depends(get_db),
):
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    target = str(payload.get("status") or "").strip().lower()
    source_status = campaign.status
    try:
        result = run_campaign_command(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="campaign.lifecycle.transition", scope_type="campaign", scope_id=campaign.id,
            payload=payload,
            execute=lambda: lifecycle.transition_lifecycle(
                db, campaign.id, actor_id=profile.id, target_raw=payload.get("status"),
                expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
            ),
        )
    except (HTTPException, CampaignCommandError) as exc:
        logger.warning(
            "campaign lifecycle failed campaign_id=%s actor_id=%s from=%s to=%s status=%s",
            campaign.id, profile.id, source_status, target, exc.status_code,
        )
        raise
    lifecycle.log_lifecycle_outcome(
        db, campaign.id, actor_id=profile.id, source_status=source_status, target=target, result=result,
    )
    return result


def _staged_command_error(exc: Exception, conflict_detail: str) -> HTTPException:
    if isinstance(exc, RevisionConflictError):
        # Concurrent loser: the whole idempotent command (including its
        # record) rolled back atomically, so retrying the same key converges.
        return HTTPException(
            status_code=409,
            detail=conflict_detail,
            headers={"X-Current-Revision": str(exc.actual_revision)},
        )
    msg = str(exc)
    if msg == "Campaign not found":
        return HTTPException(status_code=404, detail=msg)
    if msg.startswith("Only the owner"):
        return HTTPException(status_code=403, detail=msg)
    return HTTPException(status_code=409, detail=msg)


@router.post("/api/campaigns/{campaign_id}/world-seed")
def world_seed_campaign(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    db: Session = Depends(get_db),
):
    """Production world-seed generation — issue #245.

    Owner-only. Requires a fully ready launch party (any size 1..6).
    Generates, validates, and stages durable seed canon (location, NPCs,
    faction, party characters, relations, facts, knowledge, starting scene,
    one pressure clock) and moves lobby -> starting atomically. Idempotent
    under repeated calls: an existing seed event converges without new
    writes. Failures leave the campaign pre-start for corrected retry.
    """
    from app.campaigns.world_seed import WorldSeedError, run_world_seed

    cid = parse_campaign_id_or_404(campaign_id)
    operation_id, idempotency_key = command_keys(request, payload)

    def _execute():
        try:
            return run_world_seed(db, cid, actor_id=profile.id, operation_id=operation_id or idempotency_key)
        except (WorldSeedError, RevisionConflictError) as exc:
            raise _staged_command_error(exc, "Concurrent world seed conflicted; retry the request") from exc

    return execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.world_seed", scope_type="campaign", scope_id=cid,
        payload=payload, execute=_execute,
    )


@router.post("/api/campaigns/{campaign_id}/campaign-start")
def campaign_start(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    profile=Depends(current_profile),
    db: Session = Depends(get_db),
):
    """Production campaign start — issue #246.

    Owner-only. Requires the #245 world seed and a fully ready launch party.
    Ensures the shared live-table thread, stages the opening DM turn through
    the production submission/turn pipeline, and moves starting -> active
    atomically. Idempotent: an existing start event converges without new
    writes. Failures leave the campaign startable with an owed opening turn.
    """
    from app.campaigns.campaign_start import CampaignStartError, run_campaign_start
    from app.dm.recovery import execute_committed_attempt

    cid = parse_campaign_id_or_404(campaign_id)
    operation_id, idempotency_key = command_keys(request, payload)

    def _execute():
        try:
            return run_campaign_start(db, cid, actor_id=profile.id, operation_id=operation_id or idempotency_key)
        except (CampaignStartError, RevisionConflictError) as exc:
            raise _staged_command_error(exc, "Concurrent campaign start conflicted; retry the request") from exc

    result = execute_http_idempotent(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.start_246", scope_type="campaign", scope_id=cid,
        payload=payload, execute=_execute,
    )
    # Execute the staged opening turn post-response: a blocking inline run
    # would hold the request for the full pipeline and proxies kill slow
    # requests. Best-effort — an unfinished attempt stays ``prepared`` for
    # ``/api/cron/dm-execute`` to reconcile.
    attempt_data = result.get("dm_attempt") if isinstance(result, dict) else None
    if attempt_data and attempt_data.get("id"):
        background_tasks.add_task(execute_committed_attempt, str(attempt_data["id"]))
    return result


@router.get("/api/campaigns/{campaign_id}/events")
def list_campaign_domain_events(
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("participant")),
    db: Session = Depends(get_db),
):
    events = list_campaign_events(db, campaign.id, viewer_id=profile.id, limit=200)
    return {"events": [event.to_dict() for event in events], "revision": campaign.revision}
