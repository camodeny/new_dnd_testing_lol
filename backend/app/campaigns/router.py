"""Campaigns transport — APIRouter. Depends on service layer for domain logic."""
import logging
import uuid as uuid_lib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.campaigns.events import (
    RevisionConflictError,
    commit_campaign_mutation,
    has_domain_event,
    latest_domain_event,
    list_campaign_events,
)
from app.campaigns.service import (
    character_launch_validity,
    compute_start_eligibility,
    generate_invite_code,
    is_campaign_member,
    is_launch_locked,
    normalize_loot_mode,
    normalize_required_players,
    parse_campaign_id,
    random_brief,
    validate_campaign_name,
    validate_content_boundaries,
    validate_difficulty,
    validate_lifecycle_transition,
    validate_optional_text,
    validate_seed,
)
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from database import get_db
from models.campaigns import Campaign
from models.campaigns import CampaignInvite
from models.campaigns import CampaignMember
from models.profiles import Profile
from models.threads import CampaignThread
from models.threads import CampaignThreadMember

router = APIRouter()
logger = logging.getLogger(__name__)


def _expected_revision(payload: dict) -> int:
    if "expected_revision" not in payload:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        revision = int(payload["expected_revision"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    if revision < 0:
        raise HTTPException(status_code=400, detail="expected_revision must be a non-negative integer")
    return revision


def _validated_setup(payload: dict, *, creation: bool = False) -> dict:
    changes = {}
    try:
        if creation or "required_players" in payload:
            raw = payload.get("required_players")
            changes["required_players"] = normalize_required_players(raw)
        if creation or "loot_mode" in payload:
            raw = payload.get("loot_mode")
            changes["loot_mode"] = normalize_loot_mode(raw)
        if creation or "difficulty" in payload:
            changes["difficulty"] = validate_difficulty(payload.get("difficulty"))
        if "theme" in payload:
            changes["theme"] = validate_optional_text(payload.get("theme"), field="Theme", max_length=128)
        if "brief" in payload:
            changes["brief"] = validate_optional_text(payload.get("brief"), field="Brief", max_length=4000)
        if creation or "content_boundaries" in payload:
            changes["content_boundaries"] = validate_content_boundaries(payload.get("content_boundaries"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return changes


@router.get("/api/campaigns")
def list_campaigns(request: Request, db: Session = Depends(get_db), include_archived: bool = False):
    """Active campaign surfaces — issue #265.

    Archived campaigns are dormant and hidden by default; authorized
    review/history access passes ``?include_archived=true``. Detail,
    snapshot, events, and member reads stay available to members regardless.
    """
    profile = resolve_profile(request, db)
    member_rows = db.execute(select(CampaignMember.campaign_id).where(CampaignMember.user_id == profile.id)).scalars().all()
    member_ids = set(member_rows)
    rows = db.execute(select(Campaign).order_by(Campaign.updated_at.desc())).scalars().all()
    visible = [c for c in rows if c.owner_id == profile.id or c.id in member_ids]
    if not include_archived:
        visible = [c for c in visible if str(c.status or "").lower() != "archived"]
    return {"campaigns": [c.to_dict() for c in visible]}


@router.post("/api/campaigns")
def create_campaign(payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        name = validate_campaign_name(payload.get("name") or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    description = payload.get("description")
    try:
        random_seed = validate_seed(payload.get("random_seed"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    setup = _validated_setup(payload, creation=True)
    camp = Campaign(
        owner_id=profile.id,
        name=name,
        description=description,
        random_seed=random_seed,
        **setup,
    )
    db.add(camp)
    db.flush()
    member = CampaignMember(campaign_id=camp.id, user_id=profile.id, role="owner")
    db.add(member)
    db.flush()
    # Create durable shared thread on campaign creation — snapshot GET is retrieval-only (#196)
    from models.threads import CampaignThread

    db.add(
        CampaignThread(
            id=uuid_lib.uuid4(),
            campaign_id=camp.id,
            thread_type="campaign",
            title="Campaign",
            created_by=profile.id,
        )
    )
    db.commit()
    db.refresh(camp)
    return {"campaign": camp.to_dict()}


@router.post("/api/campaigns/random-brief")
def random_campaign_brief(payload: dict, request: Request, db: Session = Depends(get_db)):
    resolve_profile(request, db)
    seed = payload.get("random_seed") or None
    brief = random_brief(seed if isinstance(seed, str) else None)
    return brief


@router.post("/api/campaigns/quick-create")
def quick_create_campaign(payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    brief = random_brief()
    setup = _validated_setup(payload, creation=True)
    camp = Campaign(
        owner_id=profile.id,
        name=brief["name"],
        description=brief["description"],
        random_seed=brief["random_seed"],
        **setup,
    )
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=profile.id, role="owner"))
    db.flush()
    from models.threads import CampaignThread as _CampaignThread

    db.add(
        _CampaignThread(
            id=uuid_lib.uuid4(),
            campaign_id=camp.id,
            thread_type="campaign",
            title="Campaign",
            created_by=profile.id,
        )
    )
    db.commit()
    db.refresh(camp)
    return {"campaign": camp.to_dict(), "brief": brief}


@router.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, camp.id, profile.id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    # Issue #265 — restore target is derived server-side from the authoritative
    # archive event, never reconstructed from the truncated (limit-200) events
    # feed. Only set while archived.
    restore_from: str | None = None
    if str(camp.status or "").lower() == "archived":
        archive_event = latest_domain_event(db, cid, "campaign.lifecycle.archived")
        prior = (archive_event.payload or {}).get("from") if archive_event else None
        restore_from = prior if prior in ("lobby", "starting", "active") else None
    return {"campaign": camp.to_dict(), "restore_from": restore_from}


@router.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can delete this campaign")
    if list_campaign_events(db, cid, limit=1):
        raise HTTPException(status_code=409, detail="Campaigns with domain-event history cannot be deleted")
    db.delete(camp)
    db.commit()
    return {"ok": True}


@router.put("/api/campaigns/{campaign_id}")
def update_campaign(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can update")
    changes = _validated_setup(payload)
    if "name" in payload and payload["name"] is not None:
        try:
            changes["name"] = validate_campaign_name(str(payload["name"]))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if "description" in payload:
        changes["description"] = payload["description"]
    if "random_seed" in payload:
        try:
            changes["random_seed"] = validate_seed(payload.get("random_seed") or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    expected_revision = _expected_revision(payload)
    if not changes:
        raise HTTPException(status_code=400, detail="At least one campaign setting is required")
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _mutate(campaign: Campaign):
        if campaign.status != "lobby":
            logger.warning(
                "campaign settings lock rejection campaign_id=%s actor_id=%s status=%s",
                campaign.id, profile.id, campaign.status,
            )
            raise HTTPException(status_code=409, detail="Campaign settings are locked after the lobby")
        if "required_players" in changes:
            member_count = db.scalar(
                select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == campaign.id)
            ) or 0
            if changes["required_players"] < member_count:
                raise HTTPException(
                    status_code=409,
                    detail="Required players cannot be lower than current membership; remove members first",
                )
        for field, value in changes.items():
            setattr(campaign, field, value)

    def _execute():
        campaign, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.settings_updated",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            payload={"changes": changes},
            mutate=_mutate,
            commit=False,
        )
        return {"campaign": campaign.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.settings_updated",
            scope_type="campaign",
            scope_id=cid,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as e:
        raise HTTPException(
            status_code=409,
            detail=str(e),
            headers={"X-Current-Revision": str(e.actual_revision)},
        )


def _attempt_is_fresh(started_at) -> bool:
    """True when a running DM claim is recent enough to still be live.

    Stale running claims (crashed worker, lease expired) are recovered by the
    sweep and must not block dormancy forever; a missing timestamp fails
    closed and blocks. Timezone-naive stored values are assumed UTC.
    """
    import os
    from datetime import datetime as _dt, timedelta as _td

    if started_at is None:
        return True
    try:
        lease = int(os.getenv("DM_EXECUTE_LEASE_SECONDS", "300") or 300)
    except (TypeError, ValueError):
        lease = 300
    ts = started_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= _dt.now(timezone.utc) - _td(seconds=max(60, lease))


def _archived_duration_seconds(db: Session, cid: uuid_lib.UUID) -> float | None:
    """Wall-clock seconds since the latest archive event (issue #265).

    Fictional time is frozen while archived, so this duration only feeds
    observability — it never advances fictional clocks or NPC plans.
    """
    try:
        event = latest_domain_event(db, cid, "campaign.lifecycle.archived")
        if event is None or event.created_at is None:
            return None
        archived_at = event.created_at
        if archived_at.tzinfo is None:
            archived_at = archived_at.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - archived_at).total_seconds())
    except Exception as exc:
        logger.warning("campaign archived duration unavailable campaign_id=%s error=%s", cid, exc)
        return None


def _verify_restored_projection(db: Session, cid: uuid_lib.UUID) -> str:
    """Read-only post-restore projection check — issue #265.

    Verifies the live-table snapshot inputs survived dormancy (shared thread,
    thread/member presence). Never mutates: a projection failure here is only
    logged, since the authoritative restored state is already committed and
    reconnect/snapshot recovers the read model.
    """
    try:
        shared = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == cid,
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        threads = db.scalar(
            select(func.count()).select_from(CampaignThread).where(CampaignThread.campaign_id == cid)
        ) or 0
        members = db.scalar(
            select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == cid)
        ) or 0
        if shared is None:
            return "degraded:shared_thread_missing"
        return f"ok:threads={threads}:members={members}"
    except Exception as exc:
        logger.warning("campaign restore projection verify failed campaign_id=%s error=%s", cid, exc)
        return "unknown:verify_failed"


@router.post("/api/campaigns/{campaign_id}/lifecycle")
def transition_campaign_lifecycle(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    campaign = db.get(Campaign, cid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can change campaign lifecycle")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    target_raw = payload.get("status")
    target = str(target_raw or "").strip().lower()
    idempotency_key = require_idempotency_key(request, operation_id)

    def _mutate(locked: Campaign):
        try:
            target = validate_lifecycle_transition(locked.status, target_raw)
        except ValueError as exc:
            logger.warning(
                "campaign lifecycle transition rejected campaign_id=%s actor_id=%s from=%s to=%s",
                locked.id, profile.id, locked.status, target_raw,
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if target == "starting":
            member_count = db.scalar(
                select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == locked.id)
            ) or 0
            if member_count < locked.required_players:
                raise HTTPException(
                    status_code=409,
                    detail=f"Campaign requires {locked.required_players} members before starting",
                )
            members = db.execute(
                select(CampaignMember).where(CampaignMember.campaign_id == locked.id)
            ).scalars().all()
            eligibility = compute_start_eligibility(locked, list(members), db)
            if not eligibility["eligible"]:
                logger.warning(
                    "campaign start blocked campaign_id=%s actor_id=%s blockers=%s",
                    locked.id, profile.id, eligibility["blockers"],
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Campaign is not ready to start",
                        "blockers": eligibility["blockers"],
                    },
                )
        if target == "archived":
            # Issue #265 — never strand visible output mid-stream: archive is
            # rejected while a DM attempt is irreversibly streaming. Prepared
            # and pending work simply defers and resumes after restore, so it
            # never blocks dormancy. This shares serialization with execution:
            # archive holds the campaign row lock here, and execution cannot
            # cross its first visibility boundary once archived.
            from models.dm import DmTurn as _DmTurn, DmTurnAttempt as _DmAttempt

            _streaming_turn = db.execute(
                select(_DmTurn.id).where(
                    _DmTurn.campaign_id == locked.id,
                    _DmTurn.status == "streaming",
                ).limit(1)
            ).scalars().first()
            _candidates = db.execute(
                select(_DmAttempt.status, _DmAttempt.started_at).where(
                    _DmAttempt.campaign_id == locked.id,
                    _DmAttempt.status.in_(("running", "streaming")),
                ).limit(25)
            ).all()
            _inflight = _streaming_turn is not None or any(
                status == "streaming" or _attempt_is_fresh(started_at)
                for status, started_at in _candidates
            )
            if _inflight:
                logger.warning(
                    "campaign archive rejected campaign_id=%s actor_id=%s reason=dm_streaming",
                    locked.id, profile.id,
                )
                raise HTTPException(
                    status_code=409,
                    detail="A DM turn is currently streaming; retry archive shortly",
                )
        if locked.status == "archived":
            # Issue #265 — restore returns the campaign to its pre-archive
            # status, so archiving can never bypass start eligibility.
            # _mutate runs inside the atomic revision-guarded transaction, so
            # a failed restore leaves the archived state intact.
            archive_event = latest_domain_event(db, locked.id, "campaign.lifecycle.archived")
            prior = (archive_event.payload or {}).get("from") if archive_event else None
            if prior not in ("lobby", "starting", "active"):
                logger.warning(
                    "campaign restore rejected campaign_id=%s actor_id=%s reason=unknown_pre_archive_status",
                    locked.id, profile.id,
                )
                raise HTTPException(status_code=409, detail="Campaign cannot be restored: pre-archive status unknown")
            if target != prior:
                logger.warning(
                    "campaign restore rejected campaign_id=%s actor_id=%s to=%s pre_archive=%s",
                    locked.id, profile.id, target, prior,
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"Restore returns the campaign to its pre-archive status ({prior})",
                )
            # Status-only reactivation: same campaign ID/world/canon — no
            # reseed, no duplicate clocks/NPCs/threads/characters.
        locked.status = target

    def _execute():
        current = db.get(Campaign, cid)
        requested = str(target_raw or "").strip().lower()
        if current is not None and requested == str(current.status or "").lower():
            # Issue #265 — idempotent archive/restore convergence: a duplicate
            # command under a different idempotency key lands on the already
            # correct lifecycle state without a revision bump or new event.
            # Same-key replays short-circuit in the idempotency layer above
            # before reaching here. Restores only converge when the campaign
            # was actually archived before; anything else falls through to
            # the strict transition validation (409).
            if requested == "archived" or has_domain_event(db, cid, "campaign.lifecycle.archived"):
                logger.info(
                    "campaign lifecycle duplicate campaign_id=%s actor_id=%s status=%s revision=%s",
                    cid, profile.id, current.status, current.revision,
                )
                return {"campaign": current.to_dict(), "converged": True, "duplicate": True}
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type=f"campaign.lifecycle.{requested or 'invalid'}",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            payload={"from": current.status if current else None, "to": requested},
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "campaign lifecycle transitioned campaign_id=%s actor_id=%s status=%s revision=%s",
            cid, profile.id, campaign_after.status, campaign_after.revision,
        )
        return {"campaign": campaign_after.to_dict(), "event": event.to_dict()}

    source_status = campaign.status
    try:
        result = execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.lifecycle.transition",
            scope_type="campaign",
            scope_id=cid,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as exc:
        logger.warning(
            "campaign lifecycle failed campaign_id=%s actor_id=%s from=%s to=%s reason=revision_conflict",
            cid, profile.id, source_status, target,
        )
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
    except HTTPException as exc:
        logger.warning(
            "campaign lifecycle failed campaign_id=%s actor_id=%s from=%s to=%s status=%s",
            cid, profile.id, source_status, target, exc.status_code,
        )
        raise
    after = result.get("campaign", {}) if isinstance(result, dict) else {}
    if isinstance(result, dict) and result.get("converged"):
        logger.info(
            "campaign lifecycle duplicate campaign_id=%s actor_id=%s status=%s revision=%s",
            cid, profile.id, after.get("status"), after.get("revision"),
        )
        return result
    if target == "archived":
        # Dormancy, not deletion: status-only transition, all durable
        # campaign/world/character/thread state preserved by construction
        # (_mutate flips status and nothing else).
        logger.info(
            "campaign archived campaign_id=%s actor_id=%s from=%s to=%s revision=%s",
            cid, profile.id, source_status, after.get("status"), after.get("revision"),
        )
    elif source_status == "archived":
        duration_s = _archived_duration_seconds(db, cid)
        projection = _verify_restored_projection(db, cid)
        logger.info(
            "campaign restored campaign_id=%s actor_id=%s from=archived to=%s revision=%s "
            "duration_archived_s=%s projection_restoration=%s",
            cid, profile.id, after.get("status"), after.get("revision"),
            duration_s, projection,
        )
    return result


@router.post("/api/campaigns/{campaign_id}/solo-bootstrap")
def solo_bootstrap_campaign(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Minimal solo start into the production live-table runtime — issue #355.

    Pre-alpha scaffold; deleted/replaced by #245/#246. Owner-only, exactly one
    ready member. Idempotent under repeated clicks (state-guarded).

    Transaction boundary: ``run_solo_bootstrap`` is flush-only; the single
    atomic commit happens in ``execute_http_idempotent``. DM execution is
    triggered best-effort only after that commit succeeds (never inside).
    """
    from app.campaigns.solo_bootstrap import SoloBootstrapError, run_solo_bootstrap

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _execute():
        try:
            return run_solo_bootstrap(
                db, cid, actor_id=profile.id,
                operation_id=operation_id or idempotency_key,
            )
        except SoloBootstrapError as exc:
            msg = str(exc)
            if msg == "Campaign not found":
                raise HTTPException(status_code=404, detail=msg) from exc
            if msg.startswith("Only the owner"):
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=409, detail=msg) from exc
        except RevisionConflictError as exc:
            # Concurrent-start loser: the whole idempotent command (including
            # its record) rolled back atomically, so retrying the same key
            # is a fresh command that converges via reuse.
            raise HTTPException(
                status_code=409,
                detail="Concurrent campaign start conflicted; retry the request",
                headers={"X-Current-Revision": str(exc.actual_revision)},
            ) from exc

    result = execute_http_idempotent(
        db,
        response,
        actor_id=profile.id,
        idempotency_key=idempotency_key,
        command_type="campaign.solo_bootstrap",
        scope_type="campaign",
        scope_id=cid,
        payload=payload,
        execute=_execute,
    )
    # Post-commit best-effort DM execution: the opening attempt is durable
    # now, so executing outside the idempotent transaction cannot wedge
    # retries. Skipped on idempotent replay (already handled) and when no
    # session factory exists (tests). Provider failures leave the pending
    # turn as the owed opening turn for the cron sweep.
    if response.headers.get("X-Idempotent-Replay") != "true":
        attempt_id = (result.get("dm_attempt") or {}).get("id") if isinstance(result, dict) else None
        if attempt_id:
            try:
                from database import SessionLocal as _SessionLocal

                if _SessionLocal is not None:
                    with _SessionLocal() as execution_db:
                        from app.dm.execution import execute_dm_attempt

                        execute_dm_attempt(execution_db, uuid_lib.UUID(str(attempt_id)))
                    logger.info(
                        "solo_bootstrap post_commit_execute_attempted campaign_id=%s attempt_id=%s",
                        cid, attempt_id,
                    )
            except Exception as exc:
                logger.warning(
                    "solo_bootstrap post_commit_execute_deferred campaign_id=%s attempt_id=%s error=%s",
                    cid, attempt_id, exc,
                )
    return result


@router.post("/api/campaigns/{campaign_id}/mutations")
def commit_campaign_mutation_endpoint(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can mutate this campaign")
    if "expected_revision" not in payload:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        expected = int(payload["expected_revision"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    event_type = str(payload.get("event_type") or payload.get("type") or "").strip()
    if not event_type:
        raise HTTPException(status_code=400, detail="event_type is required")
    operation_id = payload.get("operation_id")
    if operation_id is not None:
        operation_id = str(operation_id).strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)
    visibility = str(payload.get("visibility") or "public").strip() or "public"
    provenance = payload.get("provenance")
    targets = payload.get("targets")
    event_payload = payload.get("payload")
    mutate_fields = payload.get("mutate") if isinstance(payload.get("mutate"), dict) else None

    def _mutate(campaign: Campaign):
        if not mutate_fields:
            return
        if campaign.status != "lobby":
            raise HTTPException(status_code=409, detail="Campaign settings are locked after the lobby")
        if "name" in mutate_fields and mutate_fields["name"] is not None:
            try:
                campaign.name = validate_campaign_name(str(mutate_fields["name"]))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        if "description" in mutate_fields:
            campaign.description = mutate_fields["description"]

    def _execute():
        campaign, event = commit_campaign_mutation(
            db,
            cid,
            expected,
            event_type=event_type,
            payload=event_payload if isinstance(event_payload, dict) else ({"data": event_payload} if event_payload is not None else None),
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            targets=targets,
            visibility=visibility,
            provenance=provenance if isinstance(provenance, dict) else None,
            mutate=_mutate if mutate_fields else None,
            commit=False,
        )
        return {"campaign": campaign.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.mutation",
            scope_type="campaign",
            scope_id=cid,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as e:
        raise HTTPException(
            status_code=409,
            detail=str(e),
            headers={"X-Current-Revision": str(e.actual_revision), "X-Expected-Revision": str(e.expected_revision)},
        )


@router.get("/api/campaigns/{campaign_id}/events")
def list_campaign_domain_events(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, cid, profile.id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    events = list_campaign_events(db, cid, viewer_id=profile.id, limit=200)
    return {"events": [event.to_dict() for event in events], "revision": camp.revision}


def _member_lobby_projection(db: Session, camp: Campaign, m: CampaignMember) -> dict:
    from models.characters import Character, Dnd5eCharacterSheet

    prof = db.get(Profile, m.user_id)
    projection = {
        "user_id": str(m.user_id),
        "username": prof.username if prof else "adventurer",
        "email": prof.email if prof else None,
        "role": m.role,
        "selected_character_id": str(m.selected_character_id) if m.selected_character_id else None,
        "character_id": str(m.selected_character_id) if m.selected_character_id else None,
        "character_name": None,
        "is_ready": bool(m.is_ready),
        "ready_at": m.ready_at.isoformat() if getattr(m, "ready_at", None) else None,
        "character_valid": False,
        "character_progress": {"completed": 0, "total": 3, "percent": 0},
        "character_missing": [],
    }
    if m.selected_character_id:
        char = db.get(Character, m.selected_character_id)
        if char is not None:
            sheet = db.execute(
                select(Dnd5eCharacterSheet)
                .where(Dnd5eCharacterSheet.character_id == char.id)
                .order_by(Dnd5eCharacterSheet.updated_at.desc())
            ).scalars().first()
            validity = character_launch_validity(char, sheet)
            # Public projection only — never secret lore (backstory/notes/etc).
            char_class = (sheet.char_class if sheet and sheet.char_class else None)
            classes = (sheet.classes if sheet and sheet.classes else None)
            projection.update({
                "character_name": char.name,
                "character_valid": validity["is_valid"],
                "character_progress": validity["progress"],
                "character_missing": validity["missing"],
                "character_race": sheet.race if sheet else None,
                "character_class": char_class,
                "character_classes": classes,
                "character_level": sheet.level if sheet else None,
            })
        else:
            projection["character_missing"] = ["missing"]
    return projection


@router.get("/api/campaigns/{campaign_id}/members")
def list_campaign_members(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, cid, profile.id):
        raise HTTPException(status_code=403, detail="Not a member")
    members = db.execute(select(CampaignMember).where(CampaignMember.campaign_id == cid)).scalars().all()
    return {"members": [_member_lobby_projection(db, camp, m) for m in members]}


@router.get("/api/campaigns/{campaign_id}/lobby")
def get_campaign_lobby(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    """Authoritative lobby projection — issue #241."""
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, cid, profile.id):
        raise HTTPException(status_code=403, detail="Not a member")
    members = db.execute(select(CampaignMember).where(CampaignMember.campaign_id == cid)).scalars().all()
    member_list = list(members)
    eligibility = compute_start_eligibility(camp, member_list, db)
    return {
        "campaign": camp.to_dict(),
        "members": [_member_lobby_projection(db, camp, m) for m in member_list],
        "eligibility": eligibility,
        "launch_locked": is_launch_locked(camp.status),
    }


@router.put("/api/campaigns/{campaign_id}/members/me/character")
def select_own_character(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Select one owned PC — idempotent, ownership-verified, lobby-only."""
    from datetime import datetime

    from models.characters import Character

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.execute(
        select(Campaign).where(Campaign.id == cid).with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    member = db.get(CampaignMember, {"campaign_id": cid, "user_id": profile.id})
    if member is None:
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    raw_char = payload.get("character_id")
    if not raw_char:
        raise HTTPException(status_code=400, detail="character_id is required")
    try:
        char_id = uuid_lib.UUID(str(raw_char))
    except ValueError:
        raise HTTPException(status_code=404, detail="Character not found")
    char = db.get(Character, char_id)
    if char is None:
        raise HTTPException(status_code=404, detail="Character not found")
    if char.owner_id != profile.id:
        logger.warning(
            "character selection rejected campaign_id=%s actor_id=%s character_id=%s reason=not_owner",
            cid, profile.id, char_id,
        )
        raise HTTPException(status_code=403, detail="Only your own character can be selected")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    if is_launch_locked(camp.status):
        raise HTTPException(status_code=409, detail="Launch character is locked after campaign start")

    if member.selected_character_id == char.id:
        return {
            "ok": True,
            "campaign": camp.to_dict(),
            "member": _member_lobby_projection(db, camp, member),
            "idempotent": True,
        }

    def _mutate(locked: Campaign):
        if is_launch_locked(locked.status):
            logger.warning(
                "character selection rejected campaign_id=%s actor_id=%s status=%s reason=locked",
                cid, profile.id, locked.status,
            )
            raise HTTPException(status_code=409, detail="Launch character is locked after campaign start")
        current = db.get(CampaignMember, {"campaign_id": cid, "user_id": profile.id})
        if current is None:
            raise HTTPException(status_code=403, detail="Not a member of this campaign")
        # Re-verify ownership inside the mutation so a failed command can never
        # leave the member pointing at an unauthorized character.
        fresh = db.get(Character, char_id)
        if fresh is None:
            raise HTTPException(status_code=404, detail="Character not found")
        if fresh.owner_id != profile.id:
            raise HTTPException(status_code=403, detail="Only your own character can be selected")
        current.selected_character_id = fresh.id
        # Selection change requires re-ready.
        current.is_ready = False
        current.ready_at = None

    def _execute():
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.member_character_selected",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            targets={"user_id": str(profile.id), "character_id": str(char_id)},
            payload={"user_id": str(profile.id), "character_id": str(char_id)},
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "character selected campaign_id=%s actor_id=%s character_id=%s revision=%s",
            cid, profile.id, char_id, campaign_after.revision,
        )
        refreshed = db.get(CampaignMember, {"campaign_id": cid, "user_id": profile.id})
        return {
            "ok": True,
            "campaign": campaign_after.to_dict(),
            "member": _member_lobby_projection(db, campaign_after, refreshed),
            "event": event.to_dict(),
        }

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="campaign.member.character.select",
            scope_type="campaign_member", scope_id=f"{cid}:{profile.id}",
            payload={**payload, "character_id": str(char_id)},
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.put("/api/campaigns/{campaign_id}/members/me/readiness")
def set_own_readiness(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Ready/unready — reversible before start, lobby-only, validity-gated."""
    from datetime import datetime, timezone

    from models.characters import Character, Dnd5eCharacterSheet

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.execute(
        select(Campaign).where(Campaign.id == cid).with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    member = db.get(CampaignMember, {"campaign_id": cid, "user_id": profile.id})
    if member is None:
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    if "ready" not in payload:
        raise HTTPException(status_code=400, detail="ready is required")
    ready = payload["ready"]
    if not isinstance(ready, bool):
        raise HTTPException(status_code=400, detail="ready must be a boolean")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    if is_launch_locked(camp.status):
        raise HTTPException(status_code=409, detail="Readiness is locked after campaign start")

    if bool(member.is_ready) == ready:
        return {
            "ok": True,
            "campaign": camp.to_dict(),
            "member": _member_lobby_projection(db, camp, member),
            "idempotent": True,
        }

    if ready:
        char_id = member.selected_character_id
        if char_id is None:
            raise HTTPException(status_code=422, detail="Select a character before marking ready")
        char = db.get(Character, char_id)
        if char is None or char.owner_id != profile.id:
            raise HTTPException(status_code=422, detail="Selected character is missing or not owned")
        sheet = db.execute(
            select(Dnd5eCharacterSheet)
            .where(Dnd5eCharacterSheet.character_id == char.id)
            .order_by(Dnd5eCharacterSheet.updated_at.desc())
        ).scalars().first()
        validity = character_launch_validity(char, sheet)
        if not validity["is_valid"]:
            logger.warning(
                "readiness rejected campaign_id=%s actor_id=%s character_id=%s missing=%s",
                cid, profile.id, char_id, validity["missing"],
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"Character incomplete: missing {', '.join(validity['missing'])}",
                    "missing": validity["missing"],
                },
            )

    def _mutate(locked: Campaign):
        if is_launch_locked(locked.status):
            logger.warning(
                "readiness rejected campaign_id=%s actor_id=%s status=%s reason=locked",
                cid, profile.id, locked.status,
            )
            raise HTTPException(status_code=409, detail="Readiness is locked after campaign start")
        current = db.get(CampaignMember, {"campaign_id": cid, "user_id": profile.id})
        if current is None:
            raise HTTPException(status_code=403, detail="Not a member of this campaign")
        if ready:
            char_id = current.selected_character_id
            char = db.get(Character, char_id) if char_id else None
            if char is None or char.owner_id != profile.id:
                raise HTTPException(status_code=422, detail="Selected character is missing or not owned")
            sheet = db.execute(
                select(Dnd5eCharacterSheet)
                .where(Dnd5eCharacterSheet.character_id == char.id)
                .order_by(Dnd5eCharacterSheet.updated_at.desc())
            ).scalars().first()
            validity = character_launch_validity(char, sheet)
            if not validity["is_valid"]:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": f"Character incomplete: missing {', '.join(validity['missing'])}",
                        "missing": validity["missing"],
                    },
                )
            current.is_ready = True
            current.ready_at = datetime.now(timezone.utc)
        else:
            current.is_ready = False
            current.ready_at = None

    def _execute():
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.member_ready" if ready else "campaign.member_unready",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            targets={"user_id": str(profile.id)},
            payload={"user_id": str(profile.id), "ready": ready},
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "readiness transition campaign_id=%s actor_id=%s ready=%s revision=%s",
            cid, profile.id, ready, campaign_after.revision,
        )
        refreshed = db.get(CampaignMember, {"campaign_id": cid, "user_id": profile.id})
        return {
            "ok": True,
            "campaign": campaign_after.to_dict(),
            "member": _member_lobby_projection(db, campaign_after, refreshed),
            "event": event.to_dict(),
        }

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="campaign.member.readiness",
            scope_type="campaign_member", scope_id=f"{cid}:{profile.id}",
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.delete("/api/campaigns/{campaign_id}/members/{user_id}")
def remove_campaign_member(
    campaign_id: str,
    user_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
        target_id = uuid_lib.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign or user id")
    campaign = db.get(Campaign, cid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can remove campaign members")
    if target_id == campaign.owner_id:
        raise HTTPException(status_code=400, detail="Campaign owner cannot be removed")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _mutate(locked: Campaign):
        if locked.status != "lobby":
            logger.warning(
                "campaign membership removal rejected campaign_id=%s actor_id=%s target_id=%s status=%s",
                cid, profile.id, target_id, locked.status,
            )
            raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
        member = db.get(CampaignMember, {"campaign_id": cid, "user_id": target_id})
        if member is None:
            raise HTTPException(status_code=404, detail="Campaign member not found")
        db.delete(member)
        campaign_thread_ids = select(CampaignThread.id).where(CampaignThread.campaign_id == cid)
        db.execute(
            delete(CampaignThreadMember).where(
                CampaignThreadMember.user_id == target_id,
                CampaignThreadMember.thread_id.in_(campaign_thread_ids),
            )
        )

    def _execute():
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.member_removed",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            targets={"user_id": str(target_id)},
            payload={"user_id": str(target_id)},
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "campaign member removed campaign_id=%s actor_id=%s target_id=%s revision=%s",
            cid, profile.id, target_id, campaign_after.revision,
        )
        return {"ok": True, "campaign": campaign_after.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.member.remove",
            scope_type="campaign",
            scope_id=cid,
            payload={**payload, "user_id": str(target_id)},
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.get("/api/campaigns/{campaign_id}/characters")
def list_campaign_characters(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    """Public launch roster — only approved public character info, no secret lore."""
    from models.characters import Character, Dnd5eCharacterSheet

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, cid, profile.id):
        raise HTTPException(status_code=403, detail="Not a member")
    members = db.execute(
        select(CampaignMember).where(
            CampaignMember.campaign_id == cid,
            CampaignMember.selected_character_id.is_not(None),
        )
    ).scalars().all()
    roster = []
    for m in members:
        char = db.get(Character, m.selected_character_id)
        if char is None:
            continue
        sheet = db.execute(
            select(Dnd5eCharacterSheet)
            .where(Dnd5eCharacterSheet.character_id == char.id)
            .order_by(Dnd5eCharacterSheet.updated_at.desc())
        ).scalars().first()
        roster.append({
            "character_id": str(char.id),
            "user_id": str(m.user_id),
            "name": char.name,
            "race": sheet.race if sheet else None,
            "char_class": sheet.char_class if sheet else None,
            "classes": sheet.classes if sheet else None,
            "level": sheet.level if sheet else None,
            "is_ready": bool(m.is_ready),
        })
    return {"characters": roster}


@router.get("/api/campaigns/{campaign_id}/invites")
def get_campaign_invite(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can view invite")
    inv = db.get(CampaignInvite, cid)
    if not inv:
        return {"code": None}
    return {"code": inv.code}


@router.post("/api/campaigns/{campaign_id}/invites")
def create_campaign_invite(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can create invite")
    if camp.status != "lobby":
        logger.warning(
            "campaign invite creation rejected campaign_id=%s actor_id=%s status=%s",
            cid, profile.id, camp.status,
        )
        raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
    existing = db.get(CampaignInvite, cid)
    if existing:
        return {"code": existing.code}
    for _ in range(5):
        code = generate_invite_code()
        if not db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first():
            inv = CampaignInvite(campaign_id=cid, code=code)
            db.add(inv)
            db.commit()
            return {"code": code}
    raise HTTPException(status_code=500, detail="Failed to generate invite")


@router.delete("/api/campaigns/{campaign_id}/invites")
def revoke_campaign_invite(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    campaign = db.get(Campaign, cid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can revoke invite")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _mutate(locked: Campaign):
        if locked.status != "lobby":
            raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
        invite = db.get(CampaignInvite, cid)
        if invite is None:
            raise HTTPException(status_code=404, detail="Campaign invite not found")
        db.delete(invite)

    def _execute():
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.invite_revoked",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "campaign invite revoked campaign_id=%s actor_id=%s revision=%s",
            cid, profile.id, campaign_after.revision,
        )
        return {"ok": True, "campaign": campaign_after.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.invite.revoke",
            scope_type="campaign",
            scope_id=cid,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.get("/api/invites/lookup")
def lookup_invite(code: str, request: Request, db: Session = Depends(get_db)):
    resolve_profile(request, db)
    clean = code.strip().upper()
    if not clean:
        raise HTTPException(status_code=400, detail="Code required")
    inv = db.execute(select(CampaignInvite).where(CampaignInvite.code == clean)).scalars().first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")
    camp = db.get(Campaign, inv.campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"campaign_id": str(camp.id), "campaign": camp.to_dict()}


@router.post("/api/campaigns/{campaign_id}/join")
def join_campaign(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.execute(
        select(Campaign).where(Campaign.id == cid).with_for_update()
    ).scalars().first()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    code = str(payload.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Invite code required")
    inv = db.get(CampaignInvite, cid)
    if not inv or inv.code != code:
        inv2 = db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first()
        if not inv2 or inv2.campaign_id != cid:
            raise HTTPException(status_code=403, detail="Invalid invite code")
    if is_campaign_member(db, cid, profile.id):
        return {"ok": True, "campaign": camp.to_dict()}
    if camp.status != "lobby":
        db.rollback()
        logger.warning(
            "campaign join rejected by membership lock campaign_id=%s actor_id=%s status=%s",
            cid, profile.id, camp.status,
        )
        raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
    db.add(CampaignMember(campaign_id=cid, user_id=profile.id, role="player"))
    db.commit()
    logger.info("campaign member joined campaign_id=%s actor_id=%s", cid, profile.id)
    return {"ok": True, "campaign": camp.to_dict()}
