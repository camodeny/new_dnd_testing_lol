"""Lobby invitations — domain helpers for issue #242.

Transport-agnostic: no FastAPI imports. The router owns auth, status codes,
and transactions; everything here is pure validation, projection, email
delivery, and observability helpers.

Email delivery is env-driven (single canonical integration, stdlib only):

- ``INVITE_EMAIL_PROVIDER``: ``log`` (default) | ``smtp`` | ``resend`` |
  ``sendgrid``. ``log`` records the invite as deliverable-via-link and does
  not send network mail; anything else requires its credentials.
- ``INVITE_BASE_URL`` / ``PUBLIC_APP_URL``: origin used to build the
  shareable invite URL (``{origin}/invite/{CODE}``).
- SMTP: ``SMTP_HOST``, ``SMTP_PORT`` (default 587), ``SMTP_USERNAME``,
  ``SMTP_PASSWORD``, ``SMTP_FROM`` (fallback ``SMTP_USERNAME``),
  ``SMTP_USE_TLS`` (default true).
- Resend: ``RESEND_API_KEY``, ``INVITE_EMAIL_FROM``.
- SendGrid: ``SENDGRID_API_KEY``, ``INVITE_EMAIL_FROM``.

A delivery failure never invalidates the invite — the link/code stays usable
and sending is retryable; the failure is recorded on the invite row
(``last_delivery_status``/``last_delivery_error``) for observability.
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

logger = logging.getLogger(__name__)

INVITE_STATUSES = frozenset({"active", "revoked"})
DELIVERY_STATUSES = frozenset({"sent", "failed", "skipped"})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_code(raw) -> str:
    return str(raw or "").strip().upper()


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
    return {
        "id": str(invite.id),
        "campaign_id": str(invite.campaign_id),
        "code": invite.code,
        "invite_url_path": f"/invite/{invite.code}",
        "status": invite.status,
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

    Owners see the full record; other members see a masked email hint plus
    the recipient label so the party can tell who is still outstanding
    without leaking addresses.
    """
    base = {
        "code": invite.code,
        "status": invite.status,
        "usable": invite_usability(invite)[0],
        "recipient_label": invite.recipient_label,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "accepted_count": int(invite.accepted_count or 0),
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
    }
    if viewer_is_owner:
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


def email_provider() -> str:
    return str(os.environ.get("INVITE_EMAIL_PROVIDER") or "log").strip().lower()


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
    """Send one invite email via the configured provider.

    Returns (sent, error). Never raises for provider failures — callers
    record the outcome and keep the link/code usable.
    """
    provider = email_provider()
    subject, body = render_invite_email(
        campaign_name=campaign_name, code=code, inviter_label=inviter_label
    )
    from_addr = (
        os.environ.get("INVITE_EMAIL_FROM")
        or os.environ.get("SMTP_FROM")
        or os.environ.get("SMTP_USERNAME")
        or "no-reply@example.com"
    )
    try:
        if provider == "smtp":
            _send_via_smtp(to_email=to_email, subject=subject, body=body, from_addr=from_addr)
        elif provider == "resend":
            _send_via_resend(to_email=to_email, subject=subject, body=body, from_addr=from_addr)
        elif provider == "sendgrid":
            _send_via_sendgrid(to_email=to_email, subject=subject, body=body, from_addr=from_addr)
        elif provider in ("log", "disabled", ""):
            logger.info(
                "invite email skipped provider=%s to_hash=%s campaign=%s code=%s",
                provider, _addr_hash(to_email), campaign_name, normalize_code(code),
            )
            return False, f"email provider '{provider or 'log'}' does not send mail"
        else:
            return False, f"unknown email provider '{provider}'"
    except Exception as exc:  # noqa: BLE001 — delivery failure is data, not a crash
        logger.warning(
            "invite email delivery failed provider=%s to_hash=%s code=%s error=%s",
            provider, _addr_hash(to_email), normalize_code(code), type(exc).__name__,
        )
        return False, str(exc) or type(exc).__name__
    logger.info(
        "invite email delivered provider=%s to_hash=%s code=%s",
        provider, _addr_hash(to_email), normalize_code(code),
    )
    return True, None


def _addr_hash(email: str) -> str:
    import hashlib

    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


def _send_via_smtp(*, to_email: str, subject: str, body: str, from_addr: str) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError("SMTP_HOST is not configured")
    port = int(os.environ.get("SMTP_PORT") or "587")
    username = os.environ.get("SMTP_USERNAME", "").strip() or None
    password = os.environ.get("SMTP_PASSWORD", "") or None
    use_tls = str(os.environ.get("SMTP_USE_TLS") or "true").strip().lower() in {"1", "true", "yes", "on"}
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_email
    message.set_content(body)
    timeout = float(os.environ.get("SMTP_TIMEOUT_SECONDS") or "10")
    if use_tls:
        with smtplib.SMTP(host, port, timeout=timeout) as client:
            client.starttls()
            if username:
                client.login(username, password or "")
            client.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as client:
            if username:
                client.login(username, password or "")
            client.send_message(message)


def _post_json(url: str, *, headers: dict, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    timeout = float(os.environ.get("INVITE_EMAIL_TIMEOUT_SECONDS") or "10")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status or 200), response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace") if hasattr(exc, "read") else str(exc)
        raise RuntimeError(f"email API HTTP {exc.code}: {detail[:500]}") from exc


def _send_via_resend(*, to_email: str, subject: str, body: str, from_addr: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    status, _ = _post_json(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload={"from": from_addr, "to": [to_email], "subject": subject, "text": body},
    )
    if status >= 300:
        raise RuntimeError(f"resend HTTP {status}")


def _send_via_sendgrid(*, to_email: str, subject: str, body: str, from_addr: str) -> None:
    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("SENDGRID_API_KEY is not configured")
    status, _ = _post_json(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload={
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": from_addr},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
    )
    if status >= 300:
        raise RuntimeError(f"sendgrid HTTP {status}")
