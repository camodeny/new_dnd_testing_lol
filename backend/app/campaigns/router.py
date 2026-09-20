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
    CampaignArchivedError,
    character_launch_validity,
    compute_start_eligibility,
    generate_invite_code,
    is_campaign_member,
    is_launch_locked,
    normalize_loot_mode,
    normalize_required_players,
    parse_campaign_id,
    random_brief,
    require_playable_campaign,
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
import app.adventures.service  # noqa: F401 — registers the adventure.closing worker
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
    # Shared pre-start OOC lobby chat thread — issue #243. Eager so lobby
    # chat GET stays retrieval-only for fresh campaigns; older campaigns
    # lazily converge on first lobby chat access.
    db.add(
        CampaignThread(
            id=uuid_lib.uuid4(),
            campaign_id=camp.id,
            thread_type="lobby",
            title="Lobby",
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
    # Shared pre-start OOC lobby chat thread — issue #243 (see create_campaign).
    db.add(
        _CampaignThread(
            id=uuid_lib.uuid4(),
            campaign_id=camp.id,
            thread_type="lobby",
            title="Lobby",
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
            # rejected while a DM attempt is running or streaming, or its turn
            # is streaming. Prepared and pending work simply defers and resumes
            # after restore, so it never blocks dormancy. Stuck running claims
            # do not block forever: recover_stuck_attempts resets expired
            # claims to prepared (only after proving the executor is dead via
            # the advisory execution lock), which unblocks a later archive.
            # Dormancy itself never infers executor death from timestamps.
            from models.dm import DmTurn as _DmTurn, DmTurnAttempt as _DmAttempt

            _streaming_turn = db.execute(
                select(_DmTurn.id).where(
                    _DmTurn.campaign_id == locked.id,
                    _DmTurn.status == "streaming",
                ).limit(1)
            ).scalars().first()
            _inflight_attempt = db.execute(
                select(_DmAttempt.id).where(
                    _DmAttempt.campaign_id == locked.id,
                    _DmAttempt.status.in_(("running", "streaming")),
                ).limit(1)
            ).scalars().first()
            if _inflight_attempt is not None or _streaming_turn is not None:
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
        # Archive dormancy (issue #265): even event-only mutations bump the
        # revision and append to the event stream, so the guard runs
        # unconditionally on the locked row before any lobby-setting checks.
        require_playable_campaign(campaign)
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
        try:
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
                mutate=_mutate,
                commit=False,
            )
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    from app.campaigns.invites import invite_usability, lobby_invite_projection

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
    viewer_is_owner = camp.owner_id == profile.id
    invite_rows = db.execute(
        select(CampaignInvite)
        .where(CampaignInvite.campaign_id == cid)
        .order_by(CampaignInvite.created_at.asc())
    ).scalars().all()
    # Shared OOC lobby chat thread id (issue #243) — read-only discovery, no
    # creation here so the lobby projection stays side-effect-free. The full
    # chat snapshot lives behind GET .../lobby/chat.
    from app.runtime.threads import get_lobby_thread as _get_lobby_thread

    _lobby_thread = _get_lobby_thread(db, cid)
    return {
        "campaign": camp.to_dict(),
        "members": [_member_lobby_projection(db, camp, m) for m in member_list],
        "eligibility": eligibility,
        "launch_locked": is_launch_locked(camp.status),
        "lobby_thread_id": str(_lobby_thread.id) if _lobby_thread else None,
        # Joined vs outstanding invited state — issue #242. Outstanding =
        # usable active invites. Revoked/expired history stays owner-only;
        # members receive only currently usable rows (still without bearer
        # codes or raw emails — see lobby_invite_projection).
        "invites": [
            lobby_invite_projection(inv, viewer_is_owner=viewer_is_owner)
            for inv in invite_rows
            if viewer_is_owner or invite_usability(inv)[0]
        ],
        "outstanding_invites": sum(
            1 for inv in invite_rows
            if invite_usability(inv)[0]
        ),
    }


@router.get("/api/campaigns/{campaign_id}/lobby/chat")
def get_lobby_chat(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    """Lobby OOC snapshot — issue #243.

    Durable read projection for refresh/reconnect: the lobby thread plus its
    ordered OOC history and the private Realtime channel to subscribe to.
    Members only; removed/non-member users get 403. Ensures the lobby thread
    (mirroring the runtime GET-submissions precedent) so pre-existing
    campaigns converge without a dedicated migration backfill.
    """
    from app.campaigns.auth import authorized_campaign
    from app.campaigns.lobby_chat import list_lobby_messages
    from app.realtime.channels import live_table_channel
    from app.runtime.threads import (
        ThreadAuthorizationError,
        ThreadNotFoundError,
        assert_can_read_thread,
        get_or_create_lobby_thread,
    )

    profile = resolve_profile(request, db)
    camp = authorized_campaign(db, campaign_id, profile.id)
    thread = get_or_create_lobby_thread(db, camp.id, created_by=profile.id)
    db.commit()
    try:
        assert_can_read_thread(db, camp.id, thread.id, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        logger.info(
            "lobby_chat read denied campaign_id=%s user_id=%s",
            camp.id, profile.id,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    messages = list_lobby_messages(db, camp.id, thread)
    logger.info(
        "lobby_chat snapshot campaign_id=%s user_id=%s thread_id=%s message_count=%s",
        camp.id, profile.id, thread.id, len(messages),
    )
    return {
        "thread": thread.to_dict(),
        "messages": messages,
        "channel": live_table_channel(camp.id, thread.id),
        "campaign_status": camp.status,
    }


@router.post("/api/campaigns/{campaign_id}/lobby/chat", status_code=201)
def post_lobby_chat(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Post one OOC lobby message — issue #243.

    Member-only, idempotent (``Idempotency-Key`` header or ``operation_id``),
    writable only while pre-start (``lobby``/``starting``). The message is
    forced OOC and stored via the shared submission infrastructure, then
    projected best-effort to Realtime. This endpoint deliberately never
    coordinates a forward DM turn, bumps the campaign revision, appends
    domain events, or touches clocks/world state.
    """
    from app.campaigns.auth import authorized_campaign
    from app.campaigns.lobby_chat import (
        LobbyChatStatusError,
        LobbyChatValidationError,
        post_lobby_message,
    )
    from app.runtime.threads import (
        ThreadAuthorizationError,
        assert_can_write_thread,
        get_or_create_lobby_thread,
    )

    profile = resolve_profile(request, db)
    camp = authorized_campaign(db, campaign_id, profile.id)
    try:
        from app.campaigns.lobby_chat import require_lobby_chat_writable

        require_lobby_chat_writable(camp)
    except LobbyChatStatusError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    thread = get_or_create_lobby_thread(db, camp.id, created_by=profile.id)
    db.commit()
    try:
        assert_can_write_thread(db, camp.id, thread.id, profile.id)
    except ThreadAuthorizationError as exc:
        logger.info(
            "lobby_chat write denied campaign_id=%s user_id=%s",
            camp.id, profile.id,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        from app.campaigns.lobby_chat import validate_lobby_chat_payload

        content = validate_lobby_chat_payload(payload)
    except LobbyChatValidationError as exc:
        logger.info("lobby_chat rejected campaign_id=%s reason=validation", camp.id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    idempotency_key = require_idempotency_key(
        request, str(payload.get("operation_id") or "").strip() or None
    )

    def _execute():
        # Issue #243 lifecycle race: re-check lobby-writable status and thread
        # membership on the locked campaign row. A concurrent starting->active
        # transition or member removal that commits after the transport-level
        # checks above must still refuse the write (mirrors the #265
        # lifecycle-race pattern; accept_submission's campaign lock alone only
        # re-checks archive, not lobby status/membership).
        from app.campaigns.lobby_chat import require_lobby_chat_writable
        from app.runtime.threads import (
            ThreadNotFoundError as _ThreadNotFoundError,
        )

        locked = db.execute(
            select(Campaign)
            .where(Campaign.id == camp.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalars().first()
        if locked is None:
            raise HTTPException(status_code=404, detail="Campaign not found")
        try:
            require_lobby_chat_writable(locked)
        except LobbyChatStatusError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            assert_can_write_thread(db, camp.id, thread.id, profile.id)
        except _ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        except ThreadAuthorizationError as exc:
            logger.info(
                "lobby_chat write denied under lock campaign_id=%s user_id=%s",
                camp.id, profile.id,
            )
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        submission, stored_segments = post_lobby_message(
            db, campaign=locked, user_id=profile.id, content=content,
        )
        return {
            "thread": get_or_create_lobby_thread(db, camp.id, created_by=profile.id).to_dict(),
            "message": submission.to_dict(stored_segments),
            "campaign_status": locked.status,
        }

    result = execute_http_idempotent(
        db,
        response,
        actor_id=profile.id,
        idempotency_key=idempotency_key,
        command_type="lobby_chat.post",
        scope_type="campaign_lobby",
        scope_id=f"{camp.id}:{thread.id}",
        payload=payload,
        execute=_execute,
    )
    # Live-table Realtime projection — best-effort after authoritative commit.
    # Never rolls back DB on publish failure (#198 failure/recovery); the
    # snapshot GET above is the durable recovery path.
    try:
        from uuid import UUID as _UUID

        from models.threads import PlayerSubmission as _PlayerSubmission
        from models.threads import PlayerSubmissionSegment as _PlayerSubmissionSegment

        msg = result.get("message") if isinstance(result, dict) else None
        if msg and msg.get("id"):
            db_sub = db.get(_PlayerSubmission, _UUID(str(msg["id"])))
            if db_sub is not None:
                segs = (
                    db.query(_PlayerSubmissionSegment)
                    .filter_by(submission_id=db_sub.id)
                    .order_by(_PlayerSubmissionSegment.position)
                    .all()
                )
                from app.realtime.service import publish_submission_created

                publish_submission_created(db, db_sub, segments=segs)
    except Exception as exc:
        logger.warning(
            "lobby_chat realtime publish guard failed campaign_id=%s thread_id=%s error=%s",
            camp.id, thread.id, exc,
        )
    return result


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
def list_campaign_invites(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    """Owner view of all invite records — issue #242.

    Single canonical list (full records incl. intended email + delivery
    state). Members see outstanding invites via the lobby projection, which
    masks emails; this endpoint stays owner-only.
    """
    from app.campaigns.invites import owner_invite_dict

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can view invites")
    rows = db.execute(
        select(CampaignInvite).where(CampaignInvite.campaign_id == cid).order_by(CampaignInvite.created_at.asc())
    ).scalars().all()
    return {"invites": [owner_invite_dict(inv) for inv in rows]}


def _invite_or_404(db: Session, cid, code: str) -> CampaignInvite:
    from app.campaigns.invites import normalize_code

    clean = normalize_code(code)
    if not clean:
        raise HTTPException(status_code=400, detail="Invite code required")
    inv = db.execute(
        select(CampaignInvite).where(
            CampaignInvite.campaign_id == cid, CampaignInvite.code == clean
        )
    ).scalars().first()
    if inv is None:
        # Fall back to global code match so a code pasted against the wrong
        # campaign surfaces "not found for this campaign", not a leak.
        scoped = db.execute(
            select(CampaignInvite).where(CampaignInvite.code == clean)
        ).scalars().first()
        if scoped is not None and scoped.campaign_id != cid:
            raise HTTPException(status_code=404, detail="Invite not found for this campaign")
        raise HTTPException(status_code=404, detail="Invite not found")
    return inv


@router.post("/api/campaigns/{campaign_id}/invites")
def create_campaign_invite(campaign_id: str, request: Request, db: Session = Depends(get_db), payload: dict | None = None):
    """Owner creates one invite — issue #242.

    Accepts optional ``intended_email`` / ``recipient_label`` /
    ``expires_at`` | ``expires_in_hours``. Each call mints a distinct code
    (multiple outstanding invites per campaign). Lobby-only; creation does
    not bump the fictional revision (see #240 — the existing lifecycle tests
    create invites before transitioning on revision 0).
    """
    import uuid as _uuid

    from app.campaigns.invites import (
        code_fingerprint,
        invite_url,
        normalize_email,
        owner_invite_dict,
        parse_expiry,
        validate_recipient_label,
    )

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    # Serialize with lifecycle transitions (#242 review): the campaign row
    # is the same lock the acceptance paths take, so a concurrent start
    # cannot slip between the lobby-only check and the insert below.
    # (SQLite ignores FOR UPDATE in tests; Postgres serializes writers.)
    camp = db.execute(
        select(Campaign).where(Campaign.id == cid).with_for_update()
    ).scalars().first()
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
    try:
        intended_email = normalize_email(payload.get("intended_email") if isinstance(payload, dict) else None)
        recipient_label = validate_recipient_label(payload.get("recipient_label") if isinstance(payload, dict) else None)
        raw_expiry = (payload.get("expires_at") if isinstance(payload, dict) else None)
        if raw_expiry is None and isinstance(payload, dict):
            raw_expiry = payload.get("expires_in_hours")
        expires_at = parse_expiry(raw_expiry)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for _ in range(5):
        code = generate_invite_code()
        if not db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first():
            inv = CampaignInvite(
                id=_uuid.uuid4(),
                campaign_id=cid,
                code=code,
                created_by=profile.id,
                intended_email=intended_email,
                recipient_label=recipient_label,
                status="active",
                expires_at=expires_at,
            )
            db.add(inv)
            db.commit()
            db.refresh(inv)
            logger.info(
                "campaign invite created campaign_id=%s actor_id=%s code_hash=%s has_email=%s",
                cid, profile.id, code_fingerprint(code), bool(intended_email),
            )
            body = owner_invite_dict(inv)
            body["invite_url"] = invite_url(code)
            body["invite_url_path"] = f"/invite/{code}"
            return body
    raise HTTPException(status_code=500, detail="Failed to generate invite")


@router.delete("/api/campaigns/{campaign_id}/invites/{code}")
def revoke_campaign_invite(
    campaign_id: str,
    code: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Owner revokes one invite by code — issue #242.

    Status flip (row preserved for observability); revoked codes can never
    create new membership. Revisioned + idempotent like other lobby
    mutations. Retrying a revoke for an already-revoked invite is a no-op
    success so lost acks are safe.
    """
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone

    from app.campaigns.invites import code_fingerprint, invite_url, normalize_code, owner_invite_dict

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
    clean = normalize_code(code)

    def _mutate(locked: Campaign):
        if locked.status != "lobby":
            raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
        invite = db.execute(
            select(CampaignInvite).where(
                CampaignInvite.campaign_id == cid, CampaignInvite.code == clean
            )
        ).scalars().first()
        if invite is None:
            raise HTTPException(status_code=404, detail="Campaign invite not found")
        if invite.status == "revoked":
            _mutate.result = invite
            return
        invite.status = "revoked"
        invite.revoked_at = _datetime.now(_timezone.utc)
        _mutate.result = invite

    _mutate.result = None  # type: ignore[attr-defined]

    def _execute():
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.invite_revoked",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            # Bearer codes never enter domain-event history (member-visible):
            # correlate by invite id instead.
            targets_builder=lambda: {
                "invite_id": str(_mutate.result.id) if _mutate.result is not None else None,  # type: ignore[attr-defined]
            },
            payload_builder=lambda: {
                "invite_id": str(_mutate.result.id) if _mutate.result is not None else None,  # type: ignore[attr-defined]
                "invite_status": "revoked",
            },
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "campaign invite revoked campaign_id=%s actor_id=%s code_hash=%s revision=%s",
            cid, profile.id, code_fingerprint(clean), campaign_after.revision,
        )
        body = owner_invite_dict(_mutate.result)  # type: ignore[attr-defined]
        body["invite_url"] = invite_url(clean)
        return {"ok": True, "campaign": campaign_after.to_dict(), "invite": body, "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.invite.revoke",
            scope_type="campaign_invite",
            scope_id=f"{cid}:{code_fingerprint(clean)}",
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.post("/api/campaigns/{campaign_id}/invites/{code}/email")
def send_campaign_invite_email(
    campaign_id: str,
    code: str,
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
):
    """Owner sends one invite by email — issue #242.

    Delivery failure (or an unconfigured provider) never invalidates the
    invite: the response always carries the link/code fallback and the send
    is retryable. The outcome is recorded on the invite row.
    """
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone

    from app.campaigns.invites import (
        code_fingerprint,
        invite_url,
        invite_usability,
        normalize_email,
        owner_invite_dict,
        send_invite_email,
    )

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    # Serialize validation with lifecycle transitions and revocation (#242
    # review): the campaign row is the same lock create/revoke/accept take.
    # The lock is held only for validation + persisting the delivery intent;
    # the Resend network call happens after commit so a slow provider cannot
    # hold the campaign row. A stale email is harmless because acceptance
    # revalidates the invite; delivery outcome is persisted afterwards.
    camp = db.execute(
        select(Campaign).where(Campaign.id == cid).with_for_update()
    ).scalars().first()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can send invites")
    if camp.status != "lobby":
        raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
    invite = _invite_or_404(db, cid, code)
    raw_email = payload.get("to_email") or payload.get("email") or invite.intended_email
    try:
        to_email = normalize_email(raw_email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not to_email:
        raise HTTPException(status_code=400, detail="to_email is required")
    usable, unusable_reason = invite_usability(invite)
    if not usable:
        # Never send a link acceptance will reject — the owner can mint a
        # fresh invite instead. Delivery state is untouched (nothing sent).
        raise HTTPException(status_code=410, detail=f"Invite is {unusable_reason}")
    inviter = db.get(Profile, profile.id)
    campaign_name = camp.name
    invite_id = invite.id
    invite_code = invite.code
    inviter_label = inviter.username if inviter else None
    invite.intended_email = to_email
    db.commit()
    # Network I/O outside the campaign lock/transaction (see above).
    sent, error = send_invite_email(
        to_email=to_email,
        campaign_name=campaign_name,
        code=invite_code,
        inviter_label=inviter_label,
    )
    # Short follow-up transaction: persist only the delivery outcome.
    invite = db.get(CampaignInvite, invite_id)
    if invite is None:  # pragma: no cover — row deleted mid-send
        raise HTTPException(status_code=404, detail="Invite not found")
    invite.intended_email = to_email
    invite.last_delivery_status = "sent" if sent else "failed"
    invite.last_delivery_error = error
    invite.last_delivery_at = _datetime.now(_timezone.utc)
    db.commit()
    db.refresh(invite)
    if sent:
        logger.info(
            "campaign invite email sent campaign_id=%s actor_id=%s code_hash=%s",
            cid, profile.id, code_fingerprint(invite.code),
        )
    else:
        logger.warning(
            "campaign invite email failed campaign_id=%s actor_id=%s code_hash=%s error=%s",
            cid, profile.id, code_fingerprint(invite.code), error,
        )
    body = owner_invite_dict(invite)
    body["invite_url"] = invite_url(invite.code)
    # Always 200 while the invite itself is valid: delivery failure (or an
    # unconfigured provider) never invalidates the link/code, the fallback
    # below stays usable, and the send is retryable. Clients branch on
    # ``delivery.sent`` / ``ok``, not the status code.
    return {"ok": sent, "invite": body, "delivery": {"sent": sent, "error": error}}


@router.get("/api/campaigns/{campaign_id}/party")
def get_party_roster(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    """Active roster + preserved fallen-PC canon + pending introductions — issue #266."""
    from app.campaigns.replacements import party_roster

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
    return {"party": party_roster(db, camp)}


@router.post("/api/campaigns/{campaign_id}/pc-deaths")
def declare_pc_death(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Declare a party PC dead/retired — terminal canon state, never deletion (#266)."""
    from app.campaigns.replacements import PcLifecycleError, declare_pc_death as _declare

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can declare PC death")
    raw_char = payload.get("character_id")
    if not raw_char:
        raise HTTPException(status_code=400, detail="character_id is required")
    try:
        char_id = uuid_lib.UUID(str(raw_char))
    except ValueError:
        raise HTTPException(status_code=404, detail="Character not found")
    target_status = str(payload.get("status") or "dead").strip().lower()
    if target_status not in ("dead", "retired"):
        raise HTTPException(status_code=400, detail="status must be dead or retired")
    cause = payload.get("cause")
    is_tpk = bool(payload.get("is_tpk", False))
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _execute():
        def _mutate(locked: Campaign):
            # Archive dormancy (issue #265): frozen canon, checked on the
            # locked row inside the serialized mutation.
            require_playable_campaign(locked)
            try:
                row = _declare(
                    db, locked, char_id,
                    status=target_status,
                    cause=str(cause) if cause is not None else None,
                    is_tpk=is_tpk,
                    actor_id=profile.id,
                )
            except PcLifecycleError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            _mutate.result = row

        _mutate.result = None  # type: ignore[attr-defined]
        try:
            campaign_after, event = commit_campaign_mutation(
                db,
                cid,
                expected_revision,
                event_type=f"campaign.pc_{target_status}",
                operation_id=operation_id or idempotency_key,
                actor_id=profile.id,
                targets={"character_id": str(char_id)},
                payload_builder=lambda: {
                    "character_id": str(char_id),
                    "status": _mutate.result.status,  # type: ignore[attr-defined]
                    "cause": _mutate.result.cause,  # type: ignore[attr-defined]
                    "is_tpk": bool(_mutate.result.is_tpk),  # type: ignore[attr-defined]
                },
                mutate=_mutate,
                commit=False,
            )
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        logger.info(
            "pc death declared campaign_id=%s actor_id=%s character_id=%s status=%s revision=%s",
            cid, profile.id, char_id, _mutate.result.status, campaign_after.revision,  # type: ignore[attr-defined]
        )
        return {
            "ok": True,
            "campaign": campaign_after.to_dict(),
            "lifecycle": _mutate.result.to_dict(),  # type: ignore[attr-defined]
            "event": event.to_dict(),
        }

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="campaign.pc.death",
            scope_type="campaign", scope_id=cid,
            payload={**payload, "character_id": str(char_id)},
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.post("/api/campaigns/{campaign_id}/pc-replacements")
def activate_pc_replacement(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Activate a replacement PC for the caller's fallen PC — issue #266."""
    from app.campaigns.replacements import PcLifecycleError, activate_replacement as _activate

    from models.characters import Character

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
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
    new_char = db.get(Character, char_id)
    if new_char is None:
        raise HTTPException(status_code=404, detail="Character not found")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _execute():
        def _mutate(locked: Campaign):
            # Archive dormancy (issue #265): frozen canon, checked on the
            # locked row inside the serialized mutation.
            require_playable_campaign(locked)
            try:
                fresh_member = db.get(CampaignMember, {"campaign_id": cid, "user_id": profile.id})
                if fresh_member is None:
                    raise HTTPException(status_code=403, detail="Not a member of this campaign")
                fresh_char = db.get(Character, char_id)
                if fresh_char is None:
                    raise HTTPException(status_code=404, detail="Character not found")
                _mutate.result = _activate(db, locked, fresh_member, fresh_char, actor_id=profile.id)  # type: ignore[attr-defined]
            except PcLifecycleError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        _mutate.result = None  # type: ignore[attr-defined]
        try:
            campaign_after, event = commit_campaign_mutation(
                db,
                cid,
                expected_revision,
                event_type="campaign.pc_replaced",
                operation_id=operation_id or idempotency_key,
                actor_id=profile.id,
                targets_builder=lambda: {
                    "user_id": str(profile.id),
                    "dead_character_id": _mutate.result["dead_character_id"],  # type: ignore[attr-defined]
                    "new_character_id": _mutate.result["new_character_id"],  # type: ignore[attr-defined]
                },
                payload_builder=lambda: dict(_mutate.result),  # type: ignore[attr-defined]
                mutate=_mutate,
                commit=False,
            )
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        logger.info(
            "pc replacement activated campaign_id=%s actor_id=%s dead=%s new=%s revision=%s",
            cid, profile.id,
            _mutate.result["dead_character_id"], _mutate.result["new_character_id"],  # type: ignore[attr-defined]
            campaign_after.revision,
        )
        return {
            "ok": True,
            "campaign": campaign_after.to_dict(),
            "replacement": _mutate.result,  # type: ignore[attr-defined]
            "event": event.to_dict(),
        }

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="campaign.pc.replacement",
            scope_type="campaign_member", scope_id=f"{cid}:{profile.id}",
            payload={**payload, "character_id": str(char_id)},
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.post("/api/campaigns/{campaign_id}/pc-replacements/{character_id}/introduce")
def introduce_pc_replacement(
    campaign_id: str,
    character_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Mark a replacement PC narratively introduced — normal-play hook (#266)."""
    from app.campaigns.replacements import PcLifecycleError, mark_replacement_introduced as _introduce

    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
        char_id = uuid_lib.UUID(str(character_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign or character id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can mark introductions")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _execute():
        def _mutate(locked: Campaign):
            # Archive dormancy (issue #265): frozen canon, checked on the
            # locked row inside the serialized mutation.
            require_playable_campaign(locked)
            try:
                _mutate.result = _introduce(db, locked, char_id, actor_id=profile.id)  # type: ignore[attr-defined]
            except PcLifecycleError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        _mutate.result = None  # type: ignore[attr-defined]
        try:
            campaign_after, event = commit_campaign_mutation(
                db,
                cid,
                expected_revision,
                event_type="campaign.pc_introduced",
                operation_id=operation_id or idempotency_key,
                actor_id=profile.id,
                targets={"character_id": str(char_id)},
                payload_builder=lambda: {
                    "character_id": str(char_id),
                    "replacement_of_character_id": (
                        str(_mutate.result.replacement_of_character_id)  # type: ignore[attr-defined]
                        if _mutate.result.replacement_of_character_id else None  # type: ignore[attr-defined]
                    ),
                },
                mutate=_mutate,
                commit=False,
            )
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        logger.info(
            "pc replacement introduced campaign_id=%s actor_id=%s character_id=%s revision=%s",
            cid, profile.id, char_id, campaign_after.revision,
        )
        return {
            "ok": True,
            "campaign": campaign_after.to_dict(),
            "lifecycle": _mutate.result.to_dict(),  # type: ignore[attr-defined]
            "event": event.to_dict(),
        }

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="campaign.pc.introduction",
            scope_type="campaign", scope_id=cid,
            payload={**payload, "character_id": str(char_id)},
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.get("/api/invites/lookup")
def lookup_invite(code: str, request: Request, db: Session = Depends(get_db)):
    """Pre-membership invite lookup — issue #242 security.

    Authenticated (real Supabase JWT) but pre-membership: returns only the
    minimal safe metadata needed to recognize the table (name, seats,
    usability). Never exposes owner id, emails, or campaign internals.
    Revoked/expired invites surface 410 with a stable reason so clients can
    explain without leaking anything else.
    """
    from app.campaigns.invites import (
        code_fingerprint,
        invite_usability,
        normalize_code,
        public_invite_dict,
    )

    resolve_profile(request, db)
    clean = normalize_code(code)
    if not clean:
        raise HTTPException(status_code=400, detail="Code required")
    inv = db.execute(select(CampaignInvite).where(CampaignInvite.code == clean)).scalars().first()
    if not inv:
        logger.info("invite lookup miss code_hash=%s", code_fingerprint(clean))
        raise HTTPException(status_code=404, detail="Invite not found")
    camp = db.get(Campaign, inv.campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    usable, reason = invite_usability(inv)
    member_count = db.scalar(
        select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == camp.id)
    ) or 0
    logger.info(
        "invite lookup code_hash=%s campaign_id=%s usable=%s reason=%s",
        code_fingerprint(clean), camp.id, usable, reason or "-",
    )
    if not usable:
        raise HTTPException(status_code=410, detail=f"Invite is {reason}")
    return public_invite_dict(inv, camp, member_count=int(member_count))


def _accept_invite(db: Session, camp: Campaign, invite: CampaignInvite, profile) -> dict:
    """Idempotent membership creation — issue #242.

    Shared by campaign-scoped join and code-based accept. Lost acks are
    safe: existing members (and same-invite retries) return ok without
    writing a duplicate row. Enforces revocation, expiry, lobby-only lock,
    and required-player capacity in domain order.
    """
    from app.campaigns.invites import code_fingerprint, invite_usability

    usable, reason = invite_usability(invite)
    if not usable:
        logger.warning(
            "campaign invite accept rejected campaign_id=%s actor_id=%s code_hash=%s reason=%s",
            camp.id, profile.id, code_fingerprint(invite.code), reason,
        )
        raise HTTPException(status_code=410, detail=f"Invite is {reason}")
    if is_campaign_member(db, camp.id, profile.id):
        logger.info(
            "campaign invite duplicate accept campaign_id=%s actor_id=%s code_hash=%s",
            camp.id, profile.id, code_fingerprint(invite.code),
        )
        return {"ok": True, "campaign": camp.to_dict(), "idempotent": True, "duplicate": True}
    if camp.status != "lobby":
        db.rollback()
        logger.warning(
            "campaign join rejected by membership lock campaign_id=%s actor_id=%s status=%s",
            camp.id, profile.id, camp.status,
        )
        raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
    member_count = db.scalar(
        select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == camp.id)
    ) or 0
    required = int(camp.required_players or 1)
    if int(member_count) >= required:
        db.rollback()
        logger.warning(
            "campaign join rejected full campaign_id=%s actor_id=%s members=%s required=%s",
            camp.id, profile.id, member_count, required,
        )
        raise HTTPException(status_code=409, detail="Campaign is full")
    member = CampaignMember(campaign_id=camp.id, user_id=profile.id, role="player")
    db.add(member)
    invite.accepted_count = int(invite.accepted_count or 0) + 1
    db.commit()
    logger.info(
        "campaign invite accepted campaign_id=%s actor_id=%s code_hash=%s members=%s",
        camp.id, profile.id, code_fingerprint(invite.code), int(member_count) + 1,
    )
    return {"ok": True, "campaign": camp.to_dict(), "idempotent": False, "duplicate": False}


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
    inv = db.execute(
        select(CampaignInvite).where(
            CampaignInvite.campaign_id == cid, CampaignInvite.code == code
        )
    ).scalars().first()
    if inv is None:
        raise HTTPException(status_code=403, detail="Invalid invite code")
    return _accept_invite(db, camp, inv, profile)


@router.post("/api/invites/accept")
def accept_invite_by_code(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Code-based acceptance — shareable ``/invite/:code`` flow, issue #242.

    Resolves the campaign from the code so a recipient who signed up through
    the invite URL (context preserved client-side across auth) lands in the
    right lobby with one call. Same idempotency + lock + capacity semantics
    as campaign-scoped join.
    """
    profile = resolve_profile(request, db)
    code = str((payload or {}).get("code") or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Invite code required")
    inv = db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first()
    if not inv:
        from app.campaigns.invites import code_fingerprint as _accept_fp

        logger.info("invite accept miss code_hash=%s", _accept_fp(code))
        raise HTTPException(status_code=404, detail="Invite not found")
    camp = db.execute(
        select(Campaign).where(Campaign.id == inv.campaign_id).with_for_update()
    ).scalars().first()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    # Revalidate the invite after acquiring the campaign serialization lock
    # (#242 review): revocation commits through the same campaign lock, so a
    # revoke that landed between the code lookup above and this lock must be
    # observed — otherwise a stale active snapshot could mint membership.
    # (READ COMMITTED re-read; no invite-row-first lock order introduced.)
    db.refresh(inv)
    result = _accept_invite(db, camp, inv, profile)
    return {**result, "campaign_id": str(camp.id), "code": inv.code}


# ── Adventures (issue #260) ─────────────────────────────────────────────────

_ADVENTURE_OUTCOME_ERROR = (
    "outcome must be one of victory, failure, retreat, capture, death, tpk, villain_victory"
)


def _adventure_campaign_or_404(db: Session, campaign_id: str) -> Campaign:
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    campaign = db.get(Campaign, cid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _require_adventure_reader(db: Session, campaign: Campaign, profile) -> None:
    if campaign.owner_id != profile.id and not is_campaign_member(db, campaign.id, profile.id):
        raise HTTPException(status_code=403, detail="Not a campaign member")


def _require_adventure_writer(db: Session, campaign: Campaign, profile) -> None:
    # Adventure completion is a DM decision surfaced through the campaign
    # owner; players cannot declare arcs complete.
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the campaign owner can manage adventures")


@router.get("/api/campaigns/{campaign_id}/adventures")
def list_adventures_endpoint(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    from app.adventures.service import get_current_adventure, list_adventures

    profile = resolve_profile(request, db)
    campaign = _adventure_campaign_or_404(db, campaign_id)
    _require_adventure_reader(db, campaign, profile)
    adventures = list_adventures(db, campaign.id)
    current = get_current_adventure(db, campaign.id)
    # Only the player-visible summary leaves the table for members; the
    # DM's reason, metadata, provenance ids, and closing bookkeeping stay
    # owner-visible (issue #260 security).
    is_owner = campaign.owner_id == profile.id
    serialize = (lambda a: a.to_dict()) if is_owner else (lambda a: a.to_public_dict())
    return {
        "campaign_id": str(campaign.id),
        "campaign_status": campaign.status,
        "current_adventure_id": str(current.id) if current else None,
        "adventures": [serialize(a) for a in adventures],
    }


@router.post("/api/campaigns/{campaign_id}/adventures")
def start_adventure_endpoint(
    campaign_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)
):
    from app.adventures.service import AdventureAlreadyActiveError, start_adventure

    profile = resolve_profile(request, db)
    campaign = _adventure_campaign_or_404(db, campaign_id)
    _require_adventure_writer(db, campaign, profile)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata must be an object")
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)
    # Source-range boundary for derived summaries (issue #263): an explicit
    # start_sequence stays inclusive; the default is derived inside
    # start_adventure from the LOCKED campaign revision (next event after the
    # current cursor), never from the pre-lock read above.
    raw_start = payload.get("start_sequence")
    start_sequence = None
    if raw_start is not None:
        try:
            start_sequence = int(raw_start)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="start_sequence must be an integer")
        if start_sequence < 0:
            raise HTTPException(status_code=400, detail="start_sequence must be non-negative")

    def _execute():
        try:
            adventure = start_adventure(
                db, campaign.id, title, adventure_metadata=metadata,
                start_sequence=start_sequence, commit=False,
            )
        except AdventureAlreadyActiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Committed atomically with the idempotency record by execute_http_idempotent.
        return {"adventure": adventure.to_dict(), "campaign_status": campaign.status}

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="adventure.start", scope_type="campaign", scope_id=campaign.id,
            payload=payload, execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/campaigns/{campaign_id}/adventures/current/complete")
def complete_adventure_endpoint(
    campaign_id: str, payload: dict, request: Request, response: Response, db: Session = Depends(get_db)
):
    from app.adventures.service import (
        AdventureAlreadyCompletedError,
        AdventureNotFoundError,
        complete_adventure,
    )

    profile = resolve_profile(request, db)
    campaign = _adventure_campaign_or_404(db, campaign_id)
    _require_adventure_writer(db, campaign, profile)
    expected_revision = _expected_revision(payload)
    outcome = str(payload.get("outcome") or "").strip().lower()
    if not outcome:
        raise HTTPException(status_code=400, detail=_ADVENTURE_OUTCOME_ERROR)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)
    raw_aid = str(payload.get("adventure_id") or "").strip() or None
    adventure_id = None
    if raw_aid:
        try:
            adventure_id = uuid_lib.UUID(raw_aid)
        except ValueError:
            raise HTTPException(status_code=400, detail="adventure_id must be a UUID")

    def _execute():
        try:
            adventure, event = complete_adventure(
                db, campaign.id,
                outcome=outcome,
                reason=payload.get("reason"),
                public_summary=payload.get("public_summary"),
                adventure_id=adventure_id,
                operation_id=operation_id or idempotency_key,
                actor_id=profile.id,
                expected_revision=expected_revision,
                commit=False,
            )
        except AdventureNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AdventureAlreadyCompletedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            # Includes invalid outcome + revision conflicts surfaced as ValueError.
            from app.campaigns.events import RevisionConflictError as _RCE

            if isinstance(exc, _RCE):
                raise HTTPException(
                    status_code=409, detail=str(exc),
                    headers={"X-Current-Revision": str(exc.actual_revision)},
                ) from exc
            if isinstance(exc, CampaignArchivedError):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Shared #263 finalization: bind the authoritative end cursor and
        # derive the summary on this path too (best-effort; the response
        # shape is unchanged).
        from app.adventures.service import finalize_adventure_derived as _finalize

        current = db.get(Campaign, campaign.id)
        _finalize(
            db, adventure,
            event_sequence=event.sequence if event is not None else None,
            revision=current.revision if current is not None else None,
            actor_id=profile.id,
        )
        db.flush()
        return {
            "adventure": adventure.to_dict(),
            "event": event.to_dict() if event is not None and hasattr(event, "to_dict") else None,
            "campaign_status": current.status if current else campaign.status,
        }

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="adventure.complete", scope_type="campaign", scope_id=campaign.id,
            payload=payload, execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
