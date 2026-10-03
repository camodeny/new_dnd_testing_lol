"""Campaign-scoped FastAPI dependencies and command helpers.

``campaign_for`` replaces the per-route parse -> load -> 404 -> 403 preamble;
``run_campaign_command`` wraps an idempotent revision-guarded command and
maps its canonical conflicts to HTTP 409.
"""

from collections.abc import Callable
import uuid

from fastapi import Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.events import RevisionConflictError
from app.campaigns.service import CampaignArchivedError, parse_campaign_id
from app.deps.auth import current_profile
from app.deps.idempotency import execute_http_idempotent
from app.visibility.access import is_campaign_participant
from database import get_db
from models.campaigns import Campaign, CampaignMember

#: ``campaign_for`` roles: ``owner`` (campaign owner), ``participant`` (owner
#: or member), ``member`` (has a membership row), ``None`` (any caller).
CAMPAIGN_ROLES = frozenset({"owner", "participant", "member", None})


def parse_campaign_id_or_404(campaign_id: str, detail: str = "Invalid campaign id") -> uuid.UUID:
    try:
        return parse_campaign_id(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=detail) from exc


def parse_uuid_or_404(raw: str, detail: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=detail) from exc


def campaign_for(
    role: str | None = "participant",
    *,
    forbidden: str = "Not a member of this campaign",
    invalid: str = "Invalid campaign id",
    lock: bool = False,
    live_only: bool = False,
) -> Callable[..., Campaign]:
    """Dependency loading ``{campaign_id}`` and authorizing the caller.

    404 ``invalid`` for a malformed id, 404 "Campaign not found" for a missing
    (or, with ``live_only``, soft-deleted) campaign, 403 ``forbidden`` when the
    caller lacks ``role``. ``lock`` loads the row ``FOR UPDATE``.
    """
    if role not in CAMPAIGN_ROLES:
        raise ValueError(f"unknown campaign role {role!r}")

    def dependency(
        campaign_id: str,
        profile=Depends(current_profile),
        db: Session = Depends(get_db),
    ) -> Campaign:
        cid = parse_campaign_id_or_404(campaign_id, invalid)
        if lock:
            campaign = db.execute(
                select(Campaign).where(Campaign.id == cid).with_for_update()
                .execution_options(populate_existing=True)
            ).scalars().first()
        else:
            campaign = db.get(Campaign, cid)
        if campaign is None or (live_only and campaign.is_deleted):
            raise HTTPException(status_code=404, detail="Campaign not found")
        if role == "owner":
            allowed = campaign.owner_id == profile.id
        elif role == "participant":
            allowed = is_campaign_participant(db, campaign, profile.id)
        elif role == "member":
            allowed = db.get(CampaignMember, {"campaign_id": cid, "user_id": profile.id}) is not None
        else:
            allowed = True
        if not allowed:
            raise HTTPException(status_code=403, detail=forbidden)
        return campaign

    return dependency


def require_owner(campaign: Campaign, user_id: uuid.UUID) -> None:
    """Owner-only gate applied after participant authorization."""
    if campaign.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Only the campaign owner can perform this DM lifecycle action")


def require_expected_revision(payload: dict) -> int:
    if "expected_revision" not in payload:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        revision = int(payload["expected_revision"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    if revision < 0:
        raise HTTPException(status_code=400, detail="expected_revision must be a non-negative integer")
    return revision


def run_campaign_command(
    db: Session,
    response: Response,
    *,
    actor_id: uuid.UUID,
    idempotency_key: str,
    command_type: str,
    scope_type: str,
    scope_id: str | uuid.UUID,
    payload: object,
    execute: Callable[[], dict | list],
) -> dict | list:
    """Idempotent campaign command with canonical 409 mapping.

    Revision conflicts surface the current revision via ``X-Current-Revision``;
    archived-campaign rejections (a ``ValueError``) map to 409 before the
    idempotency layer would treat them as 400 validation errors.
    """

    def _execute():
        try:
            return execute()
        except CampaignArchivedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            command_type=command_type,
            scope_type=scope_type,
            scope_id=scope_id,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc
