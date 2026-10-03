"""Lobby invitations — domain helpers for issue #242.

Transport-agnostic: no FastAPI imports. Routes own authentication and the
campaign authorization preamble; the invite commands below own validation,
transactions, email delivery, and observability, and reject with
:class:`CampaignCommandError`.

Email delivery is env-driven (single canonical integration, stdlib only):

- ``RESEND_API_KEY`` set → send via Resend (production path).
- Unset → safe local/dev fallback: no network mail, the invite stays usable
  via link/code and sending is retryable.
- ``INVITE_EMAIL_FROM``: sender address (fallback ``no-reply@example.com``).
- ``INVITE_BASE_URL`` / ``PUBLIC_APP_URL``: origin used to build the
  shareable invite URL (``{origin}/invite/{CODE}``).

A delivery failure never invalidates the invite — the link/code stays usable
and sending is retryable; the failure is recorded on the invite row
(``last_delivery_status``/``last_delivery_error``) for observability.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.campaigns.service import CampaignCommandError, generate_invite_code, is_campaign_member, member_count
from models.campaigns import Campaign, CampaignInvite, CampaignMember
from models.profiles import Profile

logger = logging.getLogger(__name__)

INVITE_STATUSES = frozenset({"active", "revoked"})
DELIVERY_STATUSES = frozenset({"sent", "failed", "skipped"})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_code(raw) -> str:
    return str(raw or "").strip().upper()


def code_fingerprint(code: str) -> str:
    """One-way short fingerprint for log correlation — issue #242.

    Invite codes are bearer credentials, so they never appear verbatim in
    logs; use this truncated SHA-256 hex where an unknown-code lookup (or a
    lifecycle transition) needs correlation without the secret.
    """
    import hashlib

    return hashlib.sha256(normalize_code(code).encode()).hexdigest()[:12]


def normalize_email(raw) -> str | None:
    if raw is None:
        return None
    email = str(raw).strip().lower()
    if not email:
        return None
    if len(email) > 320 or not _EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    return email


def validate_recipient_label(raw) -> str | None:
    if raw is None:
        return None
    label = str(raw).strip()
    if len(label) > 128:
        raise ValueError("Recipient label must be 128 characters or fewer")
    return label or None


def parse_expiry(raw) -> datetime | None:
    """Accept ``expires_at`` (ISO-8601) or ``expires_in_hours`` (number)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        hours = float(raw)
    elif isinstance(raw, str) and raw.strip() != "":
        text = raw.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                hours = float(text)
            except ValueError:
                raise ValueError("expires_at must be ISO-8601 or expires_in_hours must be a number")
            else:
                return _expiry_from_hours(hours)
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
    else:
        raise ValueError("expires_at must be ISO-8601 or expires_in_hours must be a number")
    return _expiry_from_hours(float(raw))


def _expiry_from_hours(hours: float) -> datetime:
    if hours <= 0 or hours > 24 * 365:
        raise ValueError("expires_in_hours must be between 0 and 8760")
    return utcnow() + timedelta(hours=hours)


def invite_is_expired(invite, *, now: datetime | None = None) -> bool:
    expires_at = getattr(invite, "expires_at", None)
    if expires_at is None:
        return False
    current = now or utcnow()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= current


def invite_usability(invite, *, now: datetime | None = None) -> tuple[bool, str | None]:
    """(usable, reason). Reason is a stable machine string for observability."""
    if invite is None:
        return False, "not_found"
    if str(getattr(invite, "status", "active") or "active").lower() != "active":
        return False, "revoked"
    if invite_is_expired(invite, now=now):
        return False, "expired"
    return True, None


def mask_email(email: str | None) -> str | None:
    """Privacy-preserving email hint for non-owner lobby views.

    ``jane@example.com`` -> ``j***@example.com``. Returns None when empty.
    """
    if not email:
        return None
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = local[:1] if local else "*"
    return f"{head}***@{domain}"


def public_invite_dict(invite, campaign, *, member_count: int) -> dict:
    """Minimal safe metadata for pre-membership lookup — issue #242 security.

    Never includes owner id, emails, recipient labels, delivery history, or
    campaign internals (brief/boundaries/seed). Enough for a recipient to
    recognize the table and decide to sign up.
    """
    required = int(getattr(campaign, "required_players", 1) or 1)
    usable, reason = invite_usability(invite)
    return {
        "code": invite.code,
        "campaign_id": str(campaign.id),
        "campaign_name": campaign.name,
        "campaign_status": campaign.status,
        "required_players": required,
        "member_count": int(member_count),
        "seats_remaining": max(0, required - int(member_count)),
        "usable": usable,
        "unusable_reason": reason,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
    }


def owner_invite_dict(invite) -> dict:
    usable, reason = invite_usability(invite)
    return {
        "id": str(invite.id),
        "campaign_id": str(invite.campaign_id),
        "code": invite.code,
        "invite_url_path": f"/invite/{invite.code}",
        "status": invite.status,
        # Canonical usability so owner UIs filter expiry deterministically
        # regardless of which endpoint populated the row.
        "usable": usable,
        "unusable_reason": reason,
        "intended_email": invite.intended_email,
        "recipient_label": invite.recipient_label,
        "created_by": str(invite.created_by) if invite.created_by else None,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "revoked_at": invite.revoked_at.isoformat() if invite.revoked_at else None,
        "accepted_count": int(invite.accepted_count or 0),
        "last_delivery_status": invite.last_delivery_status,
        "last_delivery_error": invite.last_delivery_error,
        "last_delivery_at": invite.last_delivery_at.isoformat() if invite.last_delivery_at else None,
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
    }


def lobby_invite_projection(invite, *, viewer_is_owner: bool) -> dict:
    """Outstanding-invite entry for the lobby projection.

    Owners see the full record (code + link). Other members see only
    non-sensitive metadata (recipient label, masked email hint, status,
    expiry): the raw code is a bearer credential accepted by
    ``POST /api/invites/accept``, so it never leaves the owner view.
    ``id`` is safe to expose (accept-by-id does not exist) and gives
    clients a stable row key.
    """
    base = {
        "id": str(invite.id),
        "status": invite.status,
        "usable": invite_usability(invite)[0],
        "recipient_label": invite.recipient_label,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "accepted_count": int(invite.accepted_count or 0),
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
    }
    if viewer_is_owner:
        base["code"] = invite.code
        base["invite_url_path"] = f"/invite/{invite.code}"
        base["intended_email"] = invite.intended_email
        base["last_delivery_status"] = invite.last_delivery_status
        base["last_delivery_error"] = invite.last_delivery_error
    else:
        base["intended_email_hint"] = mask_email(invite.intended_email)
    return base


# ── Invite URL / email ──────────────────────────────────────────────────────


def invite_base_url() -> str:
    return (
        os.environ.get("INVITE_BASE_URL")
        or os.environ.get("PUBLIC_APP_URL")
        or "http://localhost:3000"
    ).rstrip("/")


def invite_url(code: str) -> str:
    return f"{invite_base_url()}/invite/{normalize_code(code)}"


def render_invite_email(*, campaign_name: str, code: str, inviter_label: str | None = None) -> tuple[str, str]:
    link = invite_url(code)
    inviter = f" from {inviter_label}" if inviter_label else ""
    subject = f"You're invited to {campaign_name}"
    body = (
        f"You've been invited{inviter} to join the campaign \"{campaign_name}\".\n\n"
        f"Join here: {link}\n\n"
        f"Or enter this invite code: {normalize_code(code)}\n\n"
        "If you don't have an account yet, sign up first — your invite will "
        "be waiting and you'll land straight in the campaign lobby.\n"
    )
    return subject, body


def send_invite_email(*, to_email: str, campaign_name: str, code: str,
                      inviter_label: str | None = None) -> tuple[bool, str | None]:
    """Send one invite email via Resend (stdlib-only HTTP).

    Returns (sent, error). Never raises for delivery failures — callers
    record the outcome and keep the link/code usable. Without
    ``RESEND_API_KEY`` this is a safe local/dev no-send fallback.
    """
    subject, body = render_invite_email(
        campaign_name=campaign_name, code=code, inviter_label=inviter_label
    )
    from_addr = os.environ.get("INVITE_EMAIL_FROM") or "no-reply@example.com"
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        logger.info(
            "invite email skipped provider=resend to_hash=%s campaign=%s code_hash=%s",
            _addr_hash(to_email), campaign_name, code_fingerprint(code),
        )
        return False, "email delivery is not configured (set RESEND_API_KEY)"
    try:
        _send_via_resend(
            to_email=to_email, subject=subject, body=body,
            from_addr=from_addr, api_key=api_key,
        )
    except Exception as exc:  # noqa: BLE001 — delivery failure is data, not a crash
        logger.warning(
            "invite email delivery failed provider=resend to_hash=%s code_hash=%s error=%s",
            _addr_hash(to_email), code_fingerprint(code), type(exc).__name__,
        )
        return False, str(exc) or type(exc).__name__
    logger.info(
        "invite email delivered provider=resend to_hash=%s code_hash=%s",
        _addr_hash(to_email), code_fingerprint(code),
    )
    return True, None


def _addr_hash(email: str) -> str:
    import hashlib

    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


def _post_json(url: str, *, headers: dict, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    # NOTE: api.resend.com sits behind Cloudflare, which 403s (error 1010)
    # requests carrying urllib's default `Python-urllib/*` User-Agent. Send
    # an explicit product UA so delivery works from any runtime.
    headers = {"User-Agent": "dnd-invites/1.0 (+transactional)", **headers}
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    timeout = float(os.environ.get("INVITE_EMAIL_TIMEOUT_SECONDS") or "10")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status or 200), response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace") if hasattr(exc, "read") else str(exc)
        raise RuntimeError(f"email API HTTP {exc.code}: {detail[:500]}") from exc


def _send_via_resend(*, to_email: str, subject: str, body: str, from_addr: str,
                     api_key: str) -> None:
    if not api_key.strip():
        raise RuntimeError("RESEND_API_KEY is not configured")
    status, _ = _post_json(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload={"from": from_addr, "to": [to_email], "subject": subject, "text": body},
    )
    if status >= 300:
        raise RuntimeError(f"resend HTTP {status}")


# ── Invite commands ─────────────────────────────────────────────────────────


def owner_invites(db: Session, campaign_id: uuid.UUID) -> list[dict]:
    rows = db.execute(
        select(CampaignInvite).where(CampaignInvite.campaign_id == campaign_id).order_by(CampaignInvite.created_at.asc())
    ).scalars().all()
    return [owner_invite_dict(inv) for inv in rows]


def _require_lobby_membership_open(campaign: Campaign) -> None:
    if campaign.status != "lobby":
        raise CampaignCommandError("Campaign membership is locked after the lobby", status_code=409)


def create_invite(db: Session, campaign: Campaign, *, actor_id: uuid.UUID, payload: dict | None) -> dict:
    """Mint one invite on the locked campaign row (lobby-only).

    Creation does not bump the fictional revision. ``campaign`` must be
    loaded ``FOR UPDATE`` so a concurrent start cannot slip between the
    lobby check and the insert.
    """
    if campaign.status != "lobby":
        logger.warning(
            "campaign invite creation rejected campaign_id=%s actor_id=%s status=%s",
            campaign.id, actor_id, campaign.status,
        )
    _require_lobby_membership_open(campaign)
    body = payload if isinstance(payload, dict) else {}
    try:
        intended_email = normalize_email(body.get("intended_email"))
        recipient_label = validate_recipient_label(body.get("recipient_label"))
        raw_expiry = body.get("expires_at")
        if raw_expiry is None:
            raw_expiry = body.get("expires_in_hours")
        expires_at = parse_expiry(raw_expiry)
    except ValueError as exc:
        raise CampaignCommandError(str(exc), status_code=400) from exc
    for _ in range(5):
        code = generate_invite_code()
        if db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first():
            continue
        inv = CampaignInvite(
            id=uuid.uuid4(),
            campaign_id=campaign.id,
            code=code,
            created_by=actor_id,
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
            campaign.id, actor_id, code_fingerprint(code), bool(intended_email),
        )
        result = owner_invite_dict(inv)
        result["invite_url"] = invite_url(code)
        result["invite_url_path"] = f"/invite/{code}"
        return result
    raise CampaignCommandError("Failed to generate invite", status_code=500)


def revoke_invite(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    code: str,
    expected_revision: int,
    operation_id: str,
) -> dict:
    """Revoke by code: a status flip (row preserved); re-revoking is a no-op.

    Bearer codes never enter domain-event history (member-visible): events
    correlate by invite id instead.
    """
    from app.campaigns.events import commit_campaign_mutation

    revoked: list[CampaignInvite] = []

    def _mutate(locked: Campaign):
        _require_lobby_membership_open(locked)
        invite = db.execute(
            select(CampaignInvite).where(CampaignInvite.campaign_id == campaign_id, CampaignInvite.code == code)
        ).scalars().first()
        if invite is None:
            raise CampaignCommandError("Campaign invite not found", status_code=404)
        if invite.status != "revoked":
            invite.status = "revoked"
            invite.revoked_at = utcnow()
        revoked.append(invite)

    def _invite_id() -> str | None:
        return str(revoked[0].id) if revoked else None

    campaign_after, event = commit_campaign_mutation(
        db,
        campaign_id,
        expected_revision,
        event_type="campaign.invite_revoked",
        operation_id=operation_id,
        actor_id=actor_id,
        targets_builder=lambda: {"invite_id": _invite_id()},
        payload_builder=lambda: {"invite_id": _invite_id(), "invite_status": "revoked"},
        mutate=_mutate,
        commit=False,
    )
    logger.info(
        "campaign invite revoked campaign_id=%s actor_id=%s code_hash=%s revision=%s",
        campaign_id, actor_id, code_fingerprint(code), campaign_after.revision,
    )
    invite_body = owner_invite_dict(revoked[0])
    invite_body["invite_url"] = invite_url(code)
    return {"ok": True, "campaign": campaign_after.to_dict(), "invite": invite_body, "event": event.to_dict()}


def _campaign_invite(db: Session, campaign_id: uuid.UUID, code: str) -> CampaignInvite:
    clean = normalize_code(code)
    if not clean:
        raise CampaignCommandError("Invite code required", status_code=400)
    inv = db.execute(
        select(CampaignInvite).where(CampaignInvite.campaign_id == campaign_id, CampaignInvite.code == clean)
    ).scalars().first()
    if inv is None:
        # A code pasted against the wrong campaign surfaces "not found for
        # this campaign", not a leak.
        scoped = db.execute(select(CampaignInvite).where(CampaignInvite.code == clean)).scalars().first()
        if scoped is not None and scoped.campaign_id != campaign_id:
            raise CampaignCommandError("Invite not found for this campaign", status_code=404)
        raise CampaignCommandError("Invite not found", status_code=404)
    return inv


def email_invite(db: Session, campaign: Campaign, *, actor_id: uuid.UUID, code: str, payload: dict) -> dict:
    """Email one usable invite; delivery failure never invalidates it.

    ``campaign`` must be loaded ``FOR UPDATE``: the lock is held only for
    validation + persisting the delivery intent; the provider call happens
    after commit so a slow provider cannot hold the campaign row. A stale
    email is harmless because acceptance revalidates the invite.
    """
    _require_lobby_membership_open(campaign)
    invite = _campaign_invite(db, campaign.id, code)
    raw_email = payload.get("to_email") or payload.get("email") or invite.intended_email
    try:
        to_email = normalize_email(raw_email)
    except ValueError as exc:
        raise CampaignCommandError(str(exc), status_code=400) from exc
    if not to_email:
        raise CampaignCommandError("to_email is required", status_code=400)
    usable, unusable_reason = invite_usability(invite)
    if not usable:
        # Never send a link acceptance will reject. Delivery state untouched.
        raise CampaignCommandError(f"Invite is {unusable_reason}", status_code=410)
    inviter = db.get(Profile, actor_id)
    campaign_name = campaign.name
    invite_id = invite.id
    invite_code = invite.code
    inviter_label = inviter.username if inviter else None
    invite.intended_email = to_email
    db.commit()
    sent, error = send_invite_email(
        to_email=to_email,
        campaign_name=campaign_name,
        code=invite_code,
        inviter_label=inviter_label,
    )
    # Short follow-up transaction: persist only the delivery outcome.
    invite = db.get(CampaignInvite, invite_id)
    if invite is None:  # pragma: no cover — row deleted mid-send
        raise CampaignCommandError("Invite not found", status_code=404)
    invite.intended_email = to_email
    invite.last_delivery_status = "sent" if sent else "failed"
    invite.last_delivery_error = error
    invite.last_delivery_at = utcnow()
    db.commit()
    db.refresh(invite)
    if sent:
        logger.info(
            "campaign invite email sent campaign_id=%s actor_id=%s code_hash=%s",
            campaign.id, actor_id, code_fingerprint(invite.code),
        )
    else:
        logger.warning(
            "campaign invite email failed campaign_id=%s actor_id=%s code_hash=%s error=%s",
            campaign.id, actor_id, code_fingerprint(invite.code), error,
        )
    invite_body = owner_invite_dict(invite)
    invite_body["invite_url"] = invite_url(invite.code)
    # Always 200 while the invite itself is valid: clients branch on
    # ``delivery.sent`` / ``ok``; the link/code fallback stays usable and
    # the send is retryable.
    return {"ok": sent, "invite": invite_body, "delivery": {"sent": sent, "error": error}}


def lookup_invite(db: Session, code: str) -> dict:
    """Pre-membership lookup: minimal safe metadata only (#242 security).

    Never exposes owner id, emails, or campaign internals; revoked/expired
    invites surface 410 with a stable reason.
    """
    clean = normalize_code(code)
    if not clean:
        raise CampaignCommandError("Code required", status_code=400)
    inv = db.execute(select(CampaignInvite).where(CampaignInvite.code == clean)).scalars().first()
    if not inv:
        logger.info("invite lookup miss code_hash=%s", code_fingerprint(clean))
        raise CampaignCommandError("Invite not found", status_code=404)
    camp = db.get(Campaign, inv.campaign_id)
    if not camp or camp.is_deleted:
        raise CampaignCommandError("Campaign not found", status_code=404)
    usable, reason = invite_usability(inv)
    members = member_count(db, camp.id)
    logger.info(
        "invite lookup code_hash=%s campaign_id=%s usable=%s reason=%s",
        code_fingerprint(clean), camp.id, usable, reason or "-",
    )
    if not usable:
        raise CampaignCommandError(f"Invite is {reason}", status_code=410)
    return public_invite_dict(inv, camp, member_count=members)


def _accept(db: Session, campaign: Campaign, invite: CampaignInvite, user_id: uuid.UUID) -> dict:
    """Idempotent membership creation shared by join and code-based accept.

    Lost acks are safe: existing members return ok without a duplicate row.
    Enforces revocation, expiry, lobby-only lock, and required-player
    capacity in domain order.
    """
    usable, reason = invite_usability(invite)
    if not usable:
        logger.warning(
            "campaign invite accept rejected campaign_id=%s actor_id=%s code_hash=%s reason=%s",
            campaign.id, user_id, code_fingerprint(invite.code), reason,
        )
        raise CampaignCommandError(f"Invite is {reason}", status_code=410)
    if is_campaign_member(db, campaign.id, user_id):
        logger.info(
            "campaign invite duplicate accept campaign_id=%s actor_id=%s code_hash=%s",
            campaign.id, user_id, code_fingerprint(invite.code),
        )
        return {"ok": True, "campaign": campaign.to_dict(), "idempotent": True, "duplicate": True}
    if campaign.status != "lobby":
        db.rollback()
        logger.warning(
            "campaign join rejected by membership lock campaign_id=%s actor_id=%s status=%s",
            campaign.id, user_id, campaign.status,
        )
    _require_lobby_membership_open(campaign)
    members = member_count(db, campaign.id)
    required = int(campaign.required_players or 1)
    if members >= required:
        db.rollback()
        logger.warning(
            "campaign join rejected full campaign_id=%s actor_id=%s members=%s required=%s",
            campaign.id, user_id, members, required,
        )
        raise CampaignCommandError("Campaign is full", status_code=409)
    db.add(CampaignMember(campaign_id=campaign.id, user_id=user_id, role="player"))
    invite.accepted_count = int(invite.accepted_count or 0) + 1
    db.commit()
    logger.info(
        "campaign invite accepted campaign_id=%s actor_id=%s code_hash=%s members=%s",
        campaign.id, user_id, code_fingerprint(invite.code), members + 1,
    )
    return {"ok": True, "campaign": campaign.to_dict(), "idempotent": False, "duplicate": False}


def join_campaign(db: Session, campaign: Campaign, *, user_id: uuid.UUID, raw_code) -> dict:
    """Campaign-scoped join; ``campaign`` must be loaded ``FOR UPDATE``."""
    code = str(raw_code or "").strip().upper()
    if not code:
        raise CampaignCommandError("Invite code required", status_code=400)
    inv = db.execute(
        select(CampaignInvite).where(CampaignInvite.campaign_id == campaign.id, CampaignInvite.code == code)
    ).scalars().first()
    if inv is None:
        raise CampaignCommandError("Invalid invite code", status_code=403)
    return _accept(db, campaign, inv, user_id)


def accept_invite_code(db: Session, *, user_id: uuid.UUID, raw_code) -> dict:
    """Code-based acceptance for the shareable ``/invite/:code`` flow."""
    code = str(raw_code or "").strip().upper()
    if not code:
        raise CampaignCommandError("Invite code required", status_code=400)
    inv = db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first()
    if not inv:
        logger.info("invite accept miss code_hash=%s", code_fingerprint(code))
        raise CampaignCommandError("Invite not found", status_code=404)
    camp = db.execute(
        select(Campaign).where(Campaign.id == inv.campaign_id).with_for_update()
    ).scalars().first()
    if not camp or camp.is_deleted:
        raise CampaignCommandError("Campaign not found", status_code=404)
    # Revalidate after acquiring the campaign serialization lock: revocation
    # commits through the same lock, so a revoke that landed between the code
    # lookup above and this lock must be observed.
    db.refresh(inv)
    result = _accept(db, camp, inv, user_id)
    return {**result, "campaign_id": str(camp.id), "code": inv.code}
