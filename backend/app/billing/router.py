"""Stripe add-funds transport — APIRouter. Depends on the funding service for domain logic."""
import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.billing import stripe_funding as funding
from app.billing.ledger import LedgerConflictError
from app.campaigns.auth import authorized_campaign
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from database import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


def _allowed_return_hosts(request: Request) -> set[str]:
    """Hosts a Stripe return URL may point at: same-origin + configured app + loopback."""
    import os as _os
    from urllib.parse import urlparse as _urlparse

    hosts: set[str] = {"localhost", "127.0.0.1", "::1"}
    try:
        current = request.url.hostname if request.url else None
    except Exception:
        current = None
    if current:
        hosts.add(current.lower())
    for env_name in ("APP_PUBLIC_URL", "FRONTEND_URL", "APP_BASE_URL"):
        raw = (_os.getenv(env_name) or "").strip()
        if not raw:
            continue
        try:
            host = _urlparse(raw).hostname
        except Exception:
            continue
        if host:
            hosts.add(host.lower())
    return hosts


def _validate_return_url(
    raw: str | None, campaign_id: uuid_lib.UUID, field: str,
    request: Request | None = None,
) -> str:
    """Validate a Stripe redirect URL: same live-table return, no open redirect.

    Accepts absolute paths or absolute https URLs (http only for loopback
    dev). The host must be same-origin (or a configured app host): the path
    prefix check alone is not enough, since ``https://evil.test/campaigns/…``
    would otherwise pass. The path must stay under this campaign's page.
    """
    from urllib.parse import urlparse

    value = (raw or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    parsed = urlparse(value)
    if parsed.scheme:
        if parsed.scheme not in ("https", "http"):
            raise HTTPException(status_code=400, detail=f"{field} must use https")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise HTTPException(status_code=400, detail=f"{field} must use https")
        host = (parsed.hostname or "").lower()
        if not host:
            raise HTTPException(status_code=400, detail=f"{field} is not a valid URL")
        allowed = _allowed_return_hosts(request) if request is not None else {"localhost", "127.0.0.1", "::1"}
        if host not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"{field} must return to the application origin",
            )
    path = parsed.path or "/"
    expected_prefix = f"/campaigns/{campaign_id}"
    if not (path == expected_prefix or path.startswith(expected_prefix + "/") or path.startswith(expected_prefix + "?")):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must return to the same campaign live table",
        )
    return value


def _public_operation(operation) -> dict:
    return operation.to_public_dict()


@router.post("/api/campaigns/{campaign_id}/funding/checkout")
def start_funding_checkout(campaign_id: str, payload: dict, request: Request,
                           db: Session = Depends(get_db)):
    """Start a Stripe add-funds checkout — issue #256.

    Any campaign member may add funds (recorded as their contribution);
    amounts and campaign attribution are validated server-side. Retrying
    with the same idempotency key returns the same operation and checkout
    URL without creating duplicate Stripe charges. Funding changes capacity
    only — never model quality or game outcomes.
    """
    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    idempotency_key = require_idempotency_key(
        request, str(payload.get("operation_id") or "").strip() or None
    )
    if "amount_cents" not in payload:
        raise HTTPException(status_code=400, detail="amount_cents is required")
    try:
        amount = int(payload["amount_cents"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="amount_cents must be an integer") from exc
    currency = str(payload.get("currency") or funding.FUNDING_CURRENCY).strip().lower()
    if currency != funding.FUNDING_CURRENCY:
        raise HTTPException(
            status_code=400, detail=f"currency must be {funding.FUNDING_CURRENCY}"
        )
    success_url = _validate_return_url(payload.get("success_url"), campaign.id, "success_url", request)
    cancel_url = _validate_return_url(payload.get("cancel_url"), campaign.id, "cancel_url", request)
    try:
        operation, checkout_url = funding.create_funding_operation(
            db,
            campaign_id=campaign.id,
            amount_cents=amount,
            contributor_user_id=profile.id,
            idempotency_key=idempotency_key,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except funding.FundingValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except funding.FundingNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except funding.StripeApiError as exc:
        logger.warning("funding checkout stripe error campaign_id=%s error=%s", campaign.id, exc)
        raise HTTPException(status_code=502, detail="Payment provider unavailable; retry shortly") from exc
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("funding checkout commit failed campaign_id=%s error=%s", campaign.id, exc)
        raise HTTPException(status_code=500, detail="Funding operation could not be saved") from exc
    db.refresh(operation)
    if not checkout_url:
        raise HTTPException(status_code=502, detail="Payment provider unavailable; retry shortly")
    return {"funding_operation": _public_operation(operation), "checkout_url": checkout_url}


@router.get("/api/campaigns/{campaign_id}/funding/operations/{operation_id}")
def get_funding_operation(campaign_id: str, operation_id: str, request: Request,
                          db: Session = Depends(get_db)):
    """Funding-operation status with authoritative reconciliation — issue #256.

    Ambiguous pending state is reconciled against Stripe before responding,
    so a browser that closed before confirmation recovers the valid payment
    without ever crediting from redirect state alone. Returns the member-safe
    projection plus aggregate capacity (no Stripe identifiers, no payment
    details).
    """
    from app.billing.ledger import public_capacity

    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    try:
        operation_uuid = uuid_lib.UUID(str(operation_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Funding operation not found") from exc
    operation = funding.get_funding_operation(db, operation_uuid)
    if operation is None or str(operation.campaign_id) != str(campaign.id):
        raise HTTPException(status_code=404, detail="Funding operation not found")
    if operation.status == funding.FUNDING_STATUS_PENDING:
        try:
            funding.reconcile_funding_operation(db, operation)
            db.commit()
        except funding.FundingNotConfiguredError as exc:
            db.rollback()
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (funding.StripeApiError, funding.FundingValidationError) as exc:
            # Transient provider failure or unresolvable state: the operation
            # stays pending rather than guessing. A Stripe outage never
            # mutates capacity or gameplay.
            db.rollback()
            logger.warning("funding reconcile failed operation_id=%s error=%s", operation.id, exc)
            operation = funding.get_funding_operation(db, operation_uuid)
        else:
            db.refresh(operation)
    return {
        "funding_operation": _public_operation(operation),
        "capacity": public_capacity(db, campaign.id),
    }


@router.post("/api/billing/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe webhook receiver — verified Stripe state is authoritative.

    The ``Stripe-Signature`` header is verified before any processing;
    unverified events are rejected without touching accounting. Duplicate
    deliveries are answered without reprocessing. Transient DB failures
    return 500 so Stripe retries safely.
    """
    secret = funding.stripe_webhook_secret()
    if not secret:
        logger.warning("funding webhook received without server configuration")
        raise HTTPException(status_code=503, detail="Stripe webhook not configured")
    raw_body = await request.body()
    try:
        funding.verify_webhook_signature(
            raw_body, request.headers.get("stripe-signature"), secret
        )
    except funding.WebhookVerificationError as exc:
        logger.warning("funding webhook verification failed error=%s", exc)
        funding._funding_log(logging.WARNING, "funding.webhook_verification_failed",
                             error=str(exc)[:200])
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from exc
    try:
        import json as _json

        event = _json.loads(raw_body.decode() or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    try:
        outcome = funding.handle_stripe_event(db, event)
    except funding.FundingValidationError as exc:
        db.rollback()
        logger.warning("funding webhook rejected event error=%s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Transient DB/runtime failure: roll back and return 500 so Stripe
        # redelivers; redelivery is safe via event-id dedupe + ledger keys.
        db.rollback()
        logger.warning("funding webhook processing failed error=%s", exc)
        raise HTTPException(status_code=500, detail="Webhook processing failed; retry") from exc
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("funding webhook commit failed error=%s", exc)
        raise HTTPException(status_code=500, detail="Webhook processing failed; retry") from exc
    return {"received": True, **outcome}


@router.post("/api/campaigns/{campaign_id}/recredits")
def create_recredit(campaign_id: str, payload: dict, request: Request, response: Response,
                    db: Session = Depends(get_db)):
    """Explicit one-time re-credit for failed/abandoned counted work — issue #256.

    Owner-only accounting correction. Links the compensating entry to the
    original AI run and its spend entry; a second re-credit for the same run
    is rejected. Recovery (non-billable) runs need no re-credit and are
    rejected. Same-key retries replay the stored result.
    """
    from app.billing.ledger import get_capacity_summary

    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the campaign owner can issue re-credits")
    idempotency_key = require_idempotency_key(
        request, str(payload.get("operation_id") or "").strip() or None
    )
    raw_run = payload.get("ai_run_id")
    if not raw_run:
        raise HTTPException(status_code=400, detail="ai_run_id is required")
    try:
        run_uuid = uuid_lib.UUID(str(raw_run))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid ai_run_id") from exc
    failure_reason = payload.get("failure_reason")
    note = payload.get("note")

    def _execute():
        try:
            entry = funding.recredit_failed_run(
                db,
                campaign_id=campaign.id,
                ai_run_id=run_uuid,
                actor_user_id=profile.id,
                failure_reason=str(failure_reason) if failure_reason is not None else None,
                idempotency_key=idempotency_key,
                note=str(note) if note is not None else None,
            )
        except funding.FundingValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LedgerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.flush()
        return {
            "entry": entry.to_dict(),
            "capacity": get_capacity_summary(db, campaign.id),
        }

    return execute_http_idempotent(
        db,
        response,
        actor_id=profile.id,
        idempotency_key=idempotency_key,
        command_type="campaign.recredit",
        scope_type="campaign",
        scope_id=campaign.id,
        payload=payload,
        execute=_execute,
    )


@router.get("/api/campaigns/{campaign_id}/funding/operations")
def list_funding_operations(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    """Owner-visible funding history (member-safe projection, no payment details)."""
    from sqlalchemy import select as _select

    from models.funding import CampaignFundingOperation as _Operation

    profile = resolve_profile(request, db)
    campaign = authorized_campaign(db, campaign_id, profile.id)
    # Member-safe projection for every row: status + amounts only, so any
    # campaign member may list. No Stripe identifiers or payment details.
    rows = db.scalars(
        _select(_Operation)
        .where(_Operation.campaign_id == campaign.id)
        .order_by(_Operation.created_at.desc())
        .limit(50)
    ).all()
    return {"funding_operations": [_public_operation(row) for row in rows]}
