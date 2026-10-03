"""Lobby invitations — issue #242."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.campaigns import invites
from app.deps.auth import current_profile
from app.deps.campaign import campaign_for, require_expected_revision, run_campaign_command
from app.deps.idempotency import command_keys
from database import get_db
from models.campaigns import Campaign

router = APIRouter()


@router.get("/api/campaigns/{campaign_id}/invites")
def list_campaign_invites(
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only owner can view invites")),
    db: Session = Depends(get_db),
):
    """Owner view of all invite records.

    Single canonical list (full records incl. intended email + delivery
    state). Members see outstanding invites via the lobby projection, which
    masks emails; this endpoint stays owner-only.
    """
    return {"invites": invites.owner_invites(db, campaign.id)}


@router.post("/api/campaigns/{campaign_id}/invites")
def create_campaign_invite(
    payload: dict | None = None,
    profile=Depends(current_profile),
    # Locked: serializes with lifecycle transitions and acceptance (#242).
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only owner can create invite", lock=True)),
    db: Session = Depends(get_db),
):
    """Owner creates one invite.

    Accepts optional ``intended_email`` / ``recipient_label`` /
    ``expires_at`` | ``expires_in_hours``. Each call mints a distinct code
    (multiple outstanding invites per campaign). Lobby-only.
    """
    return invites.create_invite(db, campaign, actor_id=profile.id, payload=payload)


@router.delete("/api/campaigns/{campaign_id}/invites/{code}")
def revoke_campaign_invite(
    code: str,
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only owner can revoke invite")),
    db: Session = Depends(get_db),
):
    """Owner revokes one invite by code.

    Status flip (row preserved for observability); revoked codes can never
    create new membership. Revisioned + idempotent like other lobby
    mutations; re-revoking is a no-op success so lost acks are safe.
    """
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    clean = invites.normalize_code(code)
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.invite.revoke",
        scope_type="campaign_invite", scope_id=f"{campaign.id}:{invites.code_fingerprint(clean)}",
        payload=payload,
        execute=lambda: invites.revoke_invite(
            db, campaign.id, actor_id=profile.id, code=clean,
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )


@router.post("/api/campaigns/{campaign_id}/invites/{code}/email")
def send_campaign_invite_email(
    code: str,
    payload: dict,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only owner can send invites", lock=True)),
    db: Session = Depends(get_db),
):
    """Owner sends one invite by email.

    Delivery failure (or an unconfigured provider) never invalidates the
    invite: the response always carries the link/code fallback and the send
    is retryable. The outcome is recorded on the invite row.
    """
    return invites.email_invite(db, campaign, actor_id=profile.id, code=code, payload=payload)


@router.get("/api/invites/lookup")
def lookup_invite(code: str, profile=Depends(current_profile), db: Session = Depends(get_db)):
    """Pre-membership invite lookup (authenticated, minimal safe metadata)."""
    return invites.lookup_invite(db, code)


@router.post("/api/campaigns/{campaign_id}/join")
def join_campaign(
    payload: dict,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for(None, lock=True, live_only=True)),
    db: Session = Depends(get_db),
):
    return invites.join_campaign(db, campaign, user_id=profile.id, raw_code=payload.get("code"))


@router.post("/api/invites/accept")
def accept_invite_by_code(payload: dict, profile=Depends(current_profile), db: Session = Depends(get_db)):
    """Code-based acceptance — shareable ``/invite/:code`` flow.

    Resolves the campaign from the code so a recipient who signed up through
    the invite URL lands in the right lobby with one call. Same idempotency +
    lock + capacity semantics as campaign-scoped join.
    """
    return invites.accept_invite_code(db, user_id=profile.id, raw_code=(payload or {}).get("code"))
