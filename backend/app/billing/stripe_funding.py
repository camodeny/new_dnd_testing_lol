"""Stripe add-funds, confirmation, and idempotent re-credit/refund accounting — issue #256.

Deterministic, code-owned payment authority (never delegated to a model;
this module performs no model calls and takes no narrative input):

- Stripe is the production payment/checkout provider for campaign fund
  additions. Checkout sessions are created server-side with the stable
  Stripe idempotency key ``funding-op:{operation_id}``, so retrying the
  same logical funding operation can never create duplicate charges.
- Only the minimum server-controlled metadata needed for reconciliation
  (``campaign_id`` + ``funding_operation_id``) is attached to Stripe
  objects. Client-supplied campaign/amount/credit values are never
  trusted: every confirmation re-validates the operation row, campaign
  attribution, and amount against authoritative Stripe state.
- Verified Stripe webhook state is authoritative for payment confirmation.
  Browser redirect/success-page state alone can never credit capacity:
  the status endpoint only credits after reconciling against Stripe.
- Webhook signature verification (HMAC-SHA256, Stripe scheme) runs before
  any processing; the ``stripe_event_id`` unique constraint makes duplicate
  or reordered delivery a no-op replay instead of a double-credit.
- Confirmed funds credit exactly one auditable ``added_funds`` #253 ledger
  entry (key ``stripe_funds:{operation_id}``); the #254 pause/resume gate
  reads the ledger, so resume is automatic from the same state.
- Failed/abandoned counted work is re-credited exactly once via an
  explicit ``recredit`` entry linked to the original AI run/usage entry.
  Recovery (non-billable) runs were never counted, so they need no
  re-credit and are rejected here. Stripe money-returned events are
  mirrored idempotently as signed accounting corrections.
- Funding changes capacity only: this module has no model-selection,
  narrative, or rules input or output, and imports no provider/DM code.
- Secrets (Stripe API key, webhook signing secret) stay server-side via
  env; raw card/payment credentials are never seen or stored — Stripe
  owns card-data collection/processing.

All writers ``flush`` but never commit; the caller owns commit/rollback.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing import ledger as _ledger
from app.observability.tracing import structured_log
from models.funding import (
    FUNDING_STATUS_CANCELED,
    FUNDING_STATUS_CONFIRMED,
    FUNDING_STATUS_FAILED,
    FUNDING_STATUS_PENDING,
    FUNDING_TERMINAL_STATUSES,
    WEBHOOK_APPLIED_IGNORED,
    WEBHOOK_APPLIED_PROCESSED,
    CampaignFundingOperation,
    StripeWebhookEvent,
)
from models.reliability import AIRun
from models.usage import (
    ENTRY_TYPE_ADMIN_ADJUSTMENT,
    ENTRY_TYPE_AI_SPEND,
    ENTRY_TYPE_RECREDIT,
    ENTRY_TYPE_REFUND,
)

logger = logging.getLogger(__name__)

# Single canonical currency for campaign add-funds (pre-alpha: no multi-currency).
FUNDING_CURRENCY = "usd"

# AI-run states that count as failed/abandoned counted work eligible for a
# one-time re-credit. Anything else (succeeded/running/unknown-fresh) needs
# an explicit owner-attested failure reason recorded in the audit entry.
RECREDIT_FAILED_RUN_STATUSES = frozenset({
    "failed", "failed_visible", "abandoned", "canceled", "cancelled", "timed_out", "expired",
})
RECREDIT_ATTESTED_REASONS = frozenset({"failed", "abandoned"})


class FundingError(RuntimeError):
    """Base class for deterministic funding failures."""


class FundingNotConfiguredError(FundingError):
    """Stripe credentials are missing server-side; checkout unavailable."""


class FundingValidationError(FundingError):
    """Client-supplied funding values failed server-side validation."""


class WebhookVerificationError(FundingError):
    """Stripe webhook signature verification failed; event untrusted."""


class StripeApiError(FundingError):
    """Stripe API call failed (transient or provider-side)."""


# ── Server-side configuration (env, never client input) ──────────────────────


def stripe_secret_key() -> str | None:
    value = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    return value or None


def stripe_webhook_secret() -> str | None:
    value = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    return value or None


def min_funding_cents() -> int:
    try:
        return max(1, int(os.getenv("STRIPE_MIN_FUNDING_CENTS", "100")))
    except (TypeError, ValueError):
        return 100


def max_funding_cents() -> int:
    try:
        return max(1, int(os.getenv("STRIPE_MAX_FUNDING_CENTS", "50000")))
    except (TypeError, ValueError):
        return 50000


def webhook_tolerance_seconds() -> int:
    try:
        return max(0, int(os.getenv("STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300")))
    except (TypeError, ValueError):
        return 300


# ── Stripe client abstraction (injectable; no card data ever handled) ─────────


class StripeClient(Protocol):
    """Minimal Stripe surface for campaign add-funds (test-mode injectable)."""

    def create_checkout_session(
        self, *, amount_cents: int, currency: str, idempotency_key: str,
        metadata: dict[str, str], success_url: str, cancel_url: str,
    ) -> dict[str, Any]:
        """Create a Checkout Session; returns ``{'id', 'url', 'payment_intent?'}``."""
        ...

    def retrieve_session(self, session_id: str) -> dict[str, Any]:
        """Authoritative Checkout Session state (payment_status, amounts, metadata)."""
        ...

    def retrieve_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        """Authoritative PaymentIntent state (status, amount_received, metadata)."""
        ...


class HttpStripeClient:
    """Live Stripe REST client over Checkout Sessions (server-side only)."""

    _API = "https://api.stripe.com/v1"

    def __init__(self, secret_key: str):
        self._secret_key = secret_key

    def _request(self, method: str, path: str, *, data: dict | None = None,
                 idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = httpx.request(
                method, f"{self._API}{path}",
                auth=(self._secret_key, ""), data=data, headers=headers, timeout=20.0,
            )
        except httpx.HTTPError as exc:
            raise StripeApiError(f"stripe transport failure: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise StripeApiError(f"stripe returned non-JSON (status {response.status_code})") from exc
        if response.status_code >= 400:
            message = payload.get("error", {}).get("message", "unknown stripe error") \
                if isinstance(payload, dict) else "unknown stripe error"
            raise StripeApiError(f"stripe rejected request (status {response.status_code}): {message}")
        return payload if isinstance(payload, dict) else {}

    def create_checkout_session(self, *, amount_cents, currency, idempotency_key,
                                metadata, success_url, cancel_url) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][product_data][name]": "Campaign funds",
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][quantity]": "1",
            "metadata[campaign_id]": metadata.get("campaign_id", ""),
            "metadata[funding_operation_id]": metadata.get("funding_operation_id", ""),
        }
        session = self._request("POST", "/checkout/sessions", data=data,
                                idempotency_key=idempotency_key)
        if not session.get("id") or not session.get("url"):
            raise StripeApiError("stripe checkout session missing id/url")
        return session

    def retrieve_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/checkout/sessions/{session_id}")

    def retrieve_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/payment_intents/{payment_intent_id}")


_client_override: StripeClient | None = None


def configure_stripe_client(client: StripeClient | None) -> None:
    """Inject a Stripe client (tests); ``None`` restores env-driven default."""
    global _client_override
    _client_override = client


def get_stripe_client() -> StripeClient:
    if _client_override is not None:
        return _client_override
    secret = stripe_secret_key()
    if not secret:
        raise FundingNotConfiguredError(
            "stripe is not configured (STRIPE_SECRET_KEY missing server-side)"
        )
    return HttpStripeClient(secret)


# ── Webhook signature verification (Stripe HMAC-SHA256 scheme) ───────────────


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, secret: str) -> None:
    """Verify a Stripe webhook signature before any processing.

    Raises :class:`WebhookVerificationError` when the header is missing,
    malformed, stale (outside tolerance), or the HMAC does not match.
    """
    if not signature_header:
        raise WebhookVerificationError("missing Stripe-Signature header")
    timestamp: str | None = None
    signatures: list[str] = []
    for part in signature_header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            timestamp = value.strip()
        elif name == "v1":
            signatures.append(value.strip())
    if not timestamp or not signatures:
        raise WebhookVerificationError("malformed Stripe-Signature header")
    try:
        event_time = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("malformed Stripe-Signature timestamp") from exc
    if webhook_tolerance_seconds() > 0 and abs(time.time() - event_time) > webhook_tolerance_seconds():
        raise WebhookVerificationError("stripe webhook timestamp outside tolerance")
    signed = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures if sig):
        raise WebhookVerificationError("stripe webhook signature mismatch")


# ── Observability (never payment secrets) ─────────────────────────────────────


def _funding_log(level: int, event: str, operation: CampaignFundingOperation | None = None,
                 **fields: Any) -> None:
    safe: dict[str, Any] = {}
    if operation is not None:
        safe.update(
            campaign_id=str(operation.campaign_id),
            funding_operation_id=str(operation.id),
            amount_cents=operation.amount_cents,
            funding_status=operation.status,
        )
    safe.update(fields)
    structured_log(logger, level, event, **safe)


# ── Funding-operation lifecycle ───────────────────────────────────────────────


def _validate_amount(amount_cents: Any) -> int:
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError) as exc:
        raise FundingValidationError(f"amount_cents must be an integer, got {amount_cents!r}") from exc
    if amount < min_funding_cents() or amount > max_funding_cents():
        raise FundingValidationError(
            f"amount_cents {amount} outside allowed range "
            f"[{min_funding_cents()}, {max_funding_cents()}]"
        )
    return amount


def _payload_matches(existing: CampaignFundingOperation, *, amount_cents: int,
                     contributor_user_id=None) -> bool:
    existing_contrib = str(existing.contributor_user_id) if existing.contributor_user_id else None
    wanted_contrib = str(contributor_user_id) if contributor_user_id else None
    return existing.amount_cents == amount_cents and existing_contrib == wanted_contrib


def get_funding_operation(db: Session, operation_id: uuid.UUID) -> CampaignFundingOperation | None:
    return db.get(CampaignFundingOperation, operation_id)


def _get_by_idempotency(db: Session, campaign_id, idempotency_key: str) -> CampaignFundingOperation | None:
    return db.scalar(
        select(CampaignFundingOperation).where(
            CampaignFundingOperation.campaign_id == campaign_id,
            CampaignFundingOperation.idempotency_key == idempotency_key,
        )
    )


def stripe_idempotency_key(operation_id: uuid.UUID) -> str:
    """Stable Stripe idempotency key for one logical funding operation."""
    return f"funding-op:{operation_id}"


def ledger_idempotency_key(operation_id: uuid.UUID) -> str:
    """Exactly-one ledger key for a confirmed funding operation."""
    return f"stripe_funds:{operation_id}"


def create_funding_operation(
    db: Session, *, campaign_id, amount_cents: int, contributor_user_id=None,
    idempotency_key: str, success_url: str, cancel_url: str,
    stripe_client: StripeClient | None = None,
) -> tuple[CampaignFundingOperation, str | None]:
    """Create (or idempotently replay) one logical funding operation.

    Retrying with the same ``(campaign_id, idempotency_key)`` returns the
    existing operation — including its checkout URL — without creating a
    duplicate Stripe session. When a previous attempt left a pending
    operation without a Stripe session (transient Stripe failure), the retry
    completes Stripe creation under the same stable Stripe idempotency key,
    so no duplicate charge can result. Flush-only; the caller commits.
    """
    if not idempotency_key or not str(idempotency_key).strip():
        raise FundingValidationError("idempotency_key is required")
    key = str(idempotency_key).strip()
    if len(key) > 255:
        raise FundingValidationError("idempotency_key must be 255 characters or fewer")
    amount = _validate_amount(amount_cents)

    existing = _get_by_idempotency(db, campaign_id, key)
    if existing is not None:
        if not _payload_matches(existing, amount_cents=amount, contributor_user_id=contributor_user_id):
            raise FundingValidationError(
                f"idempotency_key {idempotency_key!r} already used with different funding payload"
            )
        if existing.status == FUNDING_STATUS_PENDING and not existing.stripe_checkout_session_id:
            _attach_stripe_session(db, existing, success_url=success_url, cancel_url=cancel_url,
                                   stripe_client=stripe_client)
        _funding_log(logging.INFO, "funding.checkout_replayed", existing)
        return existing, existing.checkout_url

    operation = CampaignFundingOperation(
        campaign_id=campaign_id,
        amount_cents=amount,
        currency=FUNDING_CURRENCY,
        status=FUNDING_STATUS_PENDING,
        idempotency_key=key,
        contributor_user_id=contributor_user_id,
    )
    try:
        with db.begin_nested():
            db.add(operation)
            db.flush()
    except IntegrityError as exc:
        # Concurrent creation with the same (campaign_id, idempotency_key):
        # the savepoint keeps the session usable; return the winner instead
        # of minting a second operation (which would carry a different
        # Stripe idempotency key and could double-charge).
        winner = _get_by_idempotency(db, campaign_id, key)
        if winner is None:
            raise
        if not _payload_matches(winner, amount_cents=amount, contributor_user_id=contributor_user_id):
            raise FundingValidationError(
                f"idempotency_key {key!r} already used with different funding payload"
            ) from exc
        if winner.status == FUNDING_STATUS_PENDING and not winner.stripe_checkout_session_id:
            _attach_stripe_session(db, winner, success_url=success_url, cancel_url=cancel_url,
                                   stripe_client=stripe_client)
        _funding_log(logging.INFO, "funding.checkout_replayed", winner)
        return winner, winner.checkout_url
    _attach_stripe_session(db, operation, success_url=success_url, cancel_url=cancel_url,
                           stripe_client=stripe_client)
    _funding_log(logging.INFO, "funding.checkout_started", operation)
    return operation, operation.checkout_url


def _attach_stripe_session(db: Session, operation: CampaignFundingOperation, *,
                           success_url: str, cancel_url: str,
                           stripe_client: StripeClient | None = None) -> None:
    """Create the Stripe Checkout Session for a pending operation (flush, no commit)."""
    client = stripe_client or get_stripe_client()
    try:
        session = client.create_checkout_session(
            amount_cents=operation.amount_cents,
            currency=operation.currency,
            idempotency_key=stripe_idempotency_key(operation.id),
            metadata={
                "campaign_id": str(operation.campaign_id),
                "funding_operation_id": str(operation.id),
            },
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except FundingNotConfiguredError:
        raise
    except FundingError:
        raise
    except Exception as exc:
        _funding_log(logging.WARNING, "funding.stripe_error", operation, error=str(exc)[:300])
        raise StripeApiError(f"stripe checkout creation failed: {exc}") from exc
    operation.stripe_checkout_session_id = (
        str(session["id"]) if isinstance(session.get("id"), str) and session.get("id") else None
    )
    operation.checkout_url = (
        str(session["url"]) if isinstance(session.get("url"), str) and session.get("url") else None
    )
    payment_intent = session.get("payment_intent")
    if isinstance(payment_intent, str) and payment_intent:
        operation.stripe_payment_intent_id = payment_intent
    db.flush()


def confirm_funding_operation(
    db: Session, operation: CampaignFundingOperation, *, stripe_event_id: str,
    payment_intent_id: str | None = None,
) -> Any:
    """Credit exactly one ``added_funds`` ledger entry for a paid operation.

    Idempotent: an already-confirmed operation returns its existing ledger
    entry instead of double-crediting. Late signals for terminal
    failed/canceled operations are ignored (fail closed, logged) rather than
    credited. Flush-only; the caller commits.
    """
    from app.billing.resolution_guarantee import evaluate_new_work

    key = ledger_idempotency_key(operation.id)
    if operation.status == FUNDING_STATUS_CONFIRMED:
        from models.usage import CampaignUsageEntry

        replayed = db.scalar(
            select(CampaignUsageEntry).where(
                CampaignUsageEntry.campaign_id == operation.campaign_id,
                CampaignUsageEntry.idempotency_key == key,
            )
        )
        _funding_log(logging.INFO, "funding.confirm_duplicate", operation,
                     stripe_event_id=stripe_event_id)
        return replayed
    if operation.status in (FUNDING_STATUS_FAILED, FUNDING_STATUS_CANCELED):
        _funding_log(logging.WARNING, "funding.late_event_ignored", operation,
                     stripe_event_id=stripe_event_id)
        return None
    if payment_intent_id:
        operation.stripe_payment_intent_id = payment_intent_id
    entry = _ledger.record_entry(
        db,
        campaign_id=operation.campaign_id,
        entry_type="added_funds",
        amount_cents=operation.amount_cents,
        idempotency_key=key,
        contributor_user_id=operation.contributor_user_id,
        note="Stripe-confirmed campaign funds",
        # Aggregate-safe correlation only: the internal funding-operation id.
        # No Stripe identifiers or payment details enter the ledger (#253).
        entry_metadata={"funding_operation_id": str(operation.id)},
    )
    db.flush()
    operation.status = FUNDING_STATUS_CONFIRMED
    operation.stripe_confirm_event_id = stripe_event_id
    operation.confirmed_at = datetime.now(timezone.utc)
    operation.ledger_entry_id = entry.id
    db.flush()
    # Immediately re-evaluate the #254 gate from the same authoritative
    # state so paused play resumes with no recovery flow; resume latency is
    # the observable funding-to-play delay.
    try:
        decision = evaluate_new_work(db, operation.campaign_id)
        resumed = bool(decision.get("allowed"))
    except Exception as exc:  # policy failure must not roll back a valid payment
        logger.warning("funding resume evaluation failed funding_operation_id=%s error=%s",
                       operation.id, exc)
        resumed = False
    created_at = operation.created_at
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    latency = (datetime.now(timezone.utc) - created_at).total_seconds() if created_at else None
    _funding_log(logging.INFO, "funding.confirmed", operation,
                 stripe_event_id=stripe_event_id, resumed=resumed,
                 resume_latency_s=round(latency, 1) if latency is not None else None)
    return entry


def mark_funding_terminal(db: Session, operation: CampaignFundingOperation, *,
                          status: str, stripe_event_id: str, reason: str = "") -> CampaignFundingOperation:
    """Record a Stripe failure/cancel outcome. Capacity and gameplay untouched.

    Idempotent: terminal operations stay as they are (a confirmed operation
    is never un-confirmed by a late failure signal).
    """
    if status not in (FUNDING_STATUS_FAILED, FUNDING_STATUS_CANCELED):
        raise FundingValidationError(f"invalid terminal funding status: {status!r}")
    if operation.status in FUNDING_TERMINAL_STATUSES:
        _funding_log(logging.INFO, "funding.terminal_duplicate", operation,
                     stripe_event_id=stripe_event_id)
        return operation
    operation.status = status
    db.flush()
    _funding_log(logging.INFO, "funding.payment_failed" if status == FUNDING_STATUS_FAILED
                 else "funding.payment_canceled", operation,
                 stripe_event_id=stripe_event_id, reason=reason[:200])
    return operation


def reconcile_funding_operation(
    db: Session, operation: CampaignFundingOperation, *,
    stripe_client: StripeClient | None = None,
) -> CampaignFundingOperation:
    """Reconcile ambiguous local state against authoritative Stripe state.

    Pending operations are resolved by retrieving the Checkout Session (and,
    when needed, its PaymentIntent) from Stripe: paid → confirm and credit
    exactly once; expired/failed → terminal with capacity unchanged; anything
    else → stays pending (outage/delay never guesses success). Flush-only.
    """
    if operation.status in FUNDING_TERMINAL_STATUSES:
        return operation
    if not operation.stripe_checkout_session_id:
        _funding_log(logging.INFO, "funding.reconciliation_pending", operation,
                     detail="no_stripe_session")
        return operation
    client = stripe_client or get_stripe_client()
    try:
        session = client.retrieve_session(operation.stripe_checkout_session_id)
    except FundingNotConfiguredError:
        raise
    except Exception as exc:
        _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                     error=str(exc)[:300])
        raise StripeApiError(f"stripe reconciliation failed: {exc}") from exc
    # Never trust echoed metadata blindly: the session must belong to this
    # operation's campaign and amount before any credit.
    metadata = session.get("metadata") or {}
    if str(metadata.get("funding_operation_id", "")) != str(operation.id):
        _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                     detail="session_metadata_mismatch")
        raise FundingValidationError("stripe session does not reconcile to this funding operation")
    _check_campaign_matches(operation, session)
    if not _check_amount_matches(operation, session):
        _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                     detail="amount_mismatch")
        raise FundingValidationError("stripe session amount does not match funding operation")
    payment_status = str(session.get("payment_status") or "")
    session_status = str(session.get("status") or "")
    payment_intent = session.get("payment_intent")
    payment_intent_id = payment_intent if isinstance(payment_intent, str) else None
    if payment_status == "paid":
        confirm_funding_operation(db, operation, stripe_event_id="reconcile",
                                  payment_intent_id=payment_intent_id)
        _funding_log(logging.INFO, "funding.reconciliation_confirmed", operation)
        return operation
    if session_status == "expired":
        mark_funding_terminal(db, operation, status=FUNDING_STATUS_CANCELED,
                              stripe_event_id="reconcile", reason="session_expired")
        return operation
    if payment_intent_id:
        try:
            intent = client.retrieve_payment_intent(payment_intent_id)
        except Exception as exc:
            _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                         error=str(exc)[:300])
            raise StripeApiError(f"stripe reconciliation failed: {exc}") from exc
        intent_status = str(intent.get("status") or "")
        if intent_status == "succeeded":
            _check_campaign_matches(operation, intent)
            if not _check_amount_matches(operation, intent):
                _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                             detail="amount_mismatch")
                raise FundingValidationError(
                    "stripe payment intent amount does not match funding operation"
                )
            confirm_funding_operation(db, operation, stripe_event_id="reconcile",
                                      payment_intent_id=payment_intent_id)
            _funding_log(logging.INFO, "funding.reconciliation_confirmed", operation)
        elif intent_status in ("canceled",):
            mark_funding_terminal(db, operation, status=FUNDING_STATUS_CANCELED,
                                  stripe_event_id="reconcile", reason="payment_intent_canceled")
        # Anything else (requires_payment_method/action, processing, …) is
        # still awaiting the payer: stay pending rather than guessing
        # failure. Explicit failure arrives authoritatively via webhook
        # (payment_intent.payment_failed / async_payment_failed).
    _funding_log(logging.INFO, "funding.reconciliation_pending", operation,
                 detail="stripe_state_unresolved")
    return operation


# ── Webhook handling (verified Stripe state is authoritative) ─────────────────


def _minimal_receipt(event_type: str, obj: dict[str, Any]) -> dict[str, Any]:
    """Persist only reconciliation-safe identifiers, never payment secrets."""
    receipt: dict[str, Any] = {"event_type": event_type}
    for key in ("id", "object", "status", "payment_status", "amount_total",
                "amount_received", "currency", "payment_intent"):
        value = obj.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            receipt[key] = value
    metadata = obj.get("metadata")
    if isinstance(metadata, dict):
        receipt["metadata"] = {
            key: metadata[key] for key in ("campaign_id", "funding_operation_id")
            if isinstance(metadata.get(key), str)
        }
    return receipt


def _resolve_operation(db: Session, obj: dict[str, Any],
                       payment_intent_id: str | None = None) -> CampaignFundingOperation | None:
    metadata = obj.get("metadata") or {}
    operation_id = metadata.get("funding_operation_id")
    if isinstance(operation_id, str) and operation_id:
        try:
            operation = db.get(CampaignFundingOperation, uuid.UUID(operation_id))
        except (ValueError, AttributeError):
            operation = None
        if operation is not None:
            return operation
    lookup = payment_intent_id or (obj.get("payment_intent") if isinstance(obj.get("payment_intent"), str) else None)
    if lookup:
        return db.scalar(
            select(CampaignFundingOperation).where(
                CampaignFundingOperation.stripe_payment_intent_id == lookup
            )
        )
    session_id = obj.get("id") if obj.get("object") == "checkout.session" else None
    if session_id:
        return db.scalar(
            select(CampaignFundingOperation).where(
                CampaignFundingOperation.stripe_checkout_session_id == session_id
            )
        )
    return None


def _check_amount_matches(operation: CampaignFundingOperation, obj: dict[str, Any]) -> bool:
    """The Stripe-side amount must equal the authorized operation amount.

    Strict integer comparison: Stripe amounts are integer cents. Non-integer
    values (floats, strings, bools) never match — truncating or coercing
    them could hide a tampered/shifted amount. Objects carrying no amount
    field at all pass vacuously (nothing contradicts the operation).
    """
    for key in ("amount_total", "amount_received", "amount"):
        value = obj.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if value != operation.amount_cents:
            return False
    currency = obj.get("currency")
    if isinstance(currency, str) and currency and currency.lower() != operation.currency.lower():
        return False
    return True


def _check_campaign_matches(operation: CampaignFundingOperation, obj: dict[str, Any]) -> None:
    """Reject a verified-Stripe object whose campaign attribution disagrees.

    The metadata was set server-side at creation; a mismatch means the
    Stripe object does not belong to this operation (confused-deputy /
    dashboard-edited metadata). Fail closed rather than crediting.
    """
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        return
    campaign_id = metadata.get("campaign_id")
    if isinstance(campaign_id, str) and campaign_id and campaign_id != str(operation.campaign_id):
        raise FundingValidationError(
            "stripe object campaign does not match funding operation"
        )


def handle_stripe_event(db: Session, event: dict[str, Any], *,
                        stripe_client: StripeClient | None = None) -> dict[str, Any]:
    """Apply one verified Stripe event idempotently (flush, no commit).

    Duplicate ``stripe_event_id`` deliveries return ``duplicate`` without
    reprocessing; reordered events resolve through the operation's current
    status plus reconciliation, so no ordering can double-credit.
    """
    event_id = event.get("id")
    event_type = event.get("type")
    if not isinstance(event_id, str) or not event_id or not isinstance(event_type, str):
        raise FundingValidationError("stripe event missing id/type")
    obj = event.get("data", {}).get("object") if isinstance(event.get("data"), dict) else None
    if not isinstance(obj, dict):
        obj = {}

    # Duplicate-delivery guard: the unique constraint makes concurrent
    # redelivery collide; the savepoint keeps the session usable.
    row = StripeWebhookEvent(
        stripe_event_id=event_id, event_type=event_type, applied=WEBHOOK_APPLIED_PROCESSED,
        receipt=_minimal_receipt(event_type, obj),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # Confirm this really is a redelivered event_id before answering
        # duplicate: any other integrity failure must surface, never masquerade.
        winner = db.scalar(
            select(StripeWebhookEvent).where(StripeWebhookEvent.stripe_event_id == event_id)
        )
        if winner is None:
            raise
        _funding_log(logging.INFO, "funding.webhook_duplicate",
                     campaign_id=event.get("campaign_id"), stripe_event_id=event_id,
                     event_type=event_type)
        return {"status": "duplicate", "event_id": event_id}
    row.applied = WEBHOOK_APPLIED_PROCESSED

    outcome = _dispatch_event(db, event_id=event_id, event_type=event_type, obj=obj,
                              stripe_client=stripe_client)
    row.campaign_id = outcome.get("campaign_id")
    operation = outcome.get("operation")
    if isinstance(operation, CampaignFundingOperation):
        row.funding_operation_id = operation.id
        row.campaign_id = operation.campaign_id
    row.applied = outcome.get("applied", WEBHOOK_APPLIED_PROCESSED)
    db.flush()
    result: dict[str, Any] = {"status": outcome.get("status", "processed"), "event_id": event_id}
    if isinstance(operation, CampaignFundingOperation):
        result["funding_operation_id"] = str(operation.id)
        result["funding_status"] = operation.status
    return result


def _dispatch_event(db: Session, *, event_id: str, event_type: str, obj: dict[str, Any],
                    stripe_client: StripeClient | None) -> dict[str, Any]:
    if event_type == "checkout.session.completed":
        operation = _resolve_operation(db, obj)
        if operation is None:
            logger.warning("funding webhook references unknown operation event_id=%s", event_id)
            return {"status": "ignored", "applied": WEBHOOK_APPLIED_IGNORED, "campaign_id": None}
        _check_campaign_matches(operation, obj)
        payment_status = str(obj.get("payment_status") or "")
        payment_intent = obj.get("payment_intent")
        payment_intent_id = payment_intent if isinstance(payment_intent, str) else None
        if payment_status == "paid":
            if not _check_amount_matches(operation, obj):
                _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                             stripe_event_id=event_id, detail="amount_mismatch")
                raise FundingValidationError("stripe payment amount does not match funding operation")
            confirm_funding_operation(db, operation, stripe_event_id=event_id,
                                      payment_intent_id=payment_intent_id)
            return {"status": "confirmed", "operation": operation}
        _funding_log(logging.INFO, "funding.webhook_pending_payment", operation,
                     stripe_event_id=event_id, payment_status=payment_status or "unknown")
        return {"status": "pending", "operation": operation}
    if event_type == "payment_intent.succeeded":
        payment_intent_id = obj.get("id") if isinstance(obj.get("id"), str) else None
        operation = _resolve_operation(db, obj, payment_intent_id=payment_intent_id)
        if operation is None:
            logger.warning("funding webhook references unknown payment event_id=%s", event_id)
            return {"status": "ignored", "applied": WEBHOOK_APPLIED_IGNORED, "campaign_id": None}
        _check_campaign_matches(operation, obj)
        if not _check_amount_matches(operation, obj):
            _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                         stripe_event_id=event_id, detail="amount_mismatch")
            raise FundingValidationError("stripe payment amount does not match funding operation")
        confirm_funding_operation(db, operation, stripe_event_id=event_id,
                                  payment_intent_id=payment_intent_id)
        return {"status": "confirmed", "operation": operation}
    if event_type in ("checkout.session.expired", "checkout.session.async_payment_failed",
                      "payment_intent.payment_failed", "payment_intent.canceled"):
        operation = _resolve_operation(
            db, obj,
            payment_intent_id=obj.get("id") if obj.get("object") == "payment_intent"
            and isinstance(obj.get("id"), str) else None,
        )
        if operation is None:
            return {"status": "ignored", "applied": WEBHOOK_APPLIED_IGNORED, "campaign_id": None}
        terminal = (FUNDING_STATUS_CANCELED if event_type in
                    ("checkout.session.expired", "payment_intent.canceled")
                    else FUNDING_STATUS_FAILED)
        mark_funding_terminal(db, operation, status=terminal, stripe_event_id=event_id,
                              reason=event_type)
        return {"status": terminal, "operation": operation}
    if event_type in ("charge.refunded", "refund.created", "refund.updated"):
        return _mirror_stripe_refund(db, event_id=event_id, event_type=event_type, obj=obj,
                                     stripe_client=stripe_client)
    logger.info("funding webhook ignored event_type=%s event_id=%s", event_type, event_id)
    return {"status": "ignored", "applied": WEBHOOK_APPLIED_IGNORED, "campaign_id": None}


def _cents(value: Any) -> int | None:
    """Strict integer cents: non-integer amounts are unusable, never truncated."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _lower(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def _refund_final(item: dict[str, Any]) -> bool:
    """Whether a refund object actually moved money.

    Only ``succeeded`` refunds (or objects carrying no status at all, as in
    minimal ``charge.refunded`` nests) mirror into accounting. Pending,
    failed, or canceled refunds move nothing; their final state arrives via
    a later ``refund.updated`` / ``charge.refunded`` delivery.
    """
    status = item.get("status")
    return not isinstance(status, str) or status == "succeeded"


def _refund_items(obj: dict[str, Any]) -> tuple[list[tuple[str, int, str | None, bool]], str | None]:
    """Every identifiable refund in a refund-ish object + the payment intent id.

    Returns ``([(refund_id, amount_cents, currency, moves_money), ...],
    payment_intent_id)``. A ``charge.refunded`` charge re-emits its whole
    ``refunds.data`` list on every delivery, so every item — not just the
    first — is returned; each is mirrored idempotently by refund id
    downstream. Items without a usable id or positive integer amount are
    dropped (never guessed). ``moves_money`` is false for pending/failed/
    canceled refunds, which are ignored until their final state arrives.
    """
    items: list[tuple[str, int, str | None, bool]] = []
    payment_intent = obj.get("payment_intent")
    payment_intent_id = payment_intent if isinstance(payment_intent, str) else None
    if obj.get("object") == "refund":
        refund_id = obj.get("id") if isinstance(obj.get("id"), str) else None
        amount = _cents(obj.get("amount"))
        if refund_id and amount is not None and amount > 0:
            items.append((refund_id, amount, _lower(obj.get("currency")), _refund_final(obj)))
        return items, payment_intent_id
    # charge.refunded: refunds live under obj["refunds"]["data"] — Stripe
    # re-sends the full list (e.g. two partial refunds), so process all.
    refunds = obj.get("refunds")
    data = refunds.get("data") if isinstance(refunds, dict) else None
    if isinstance(data, list):
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            refund_id = item.get("id") if isinstance(item.get("id"), str) else None
            amount = _cents(item.get("amount"))
            if not refund_id or amount is None or amount <= 0 or refund_id in seen:
                continue
            seen.add(refund_id)
            items.append((refund_id, amount,
                          _lower(item.get("currency")) or _lower(obj.get("currency")),
                          _refund_final(item)))
    return items, payment_intent_id


def _mirrored_refund_total_cents(db: Session, operation: CampaignFundingOperation) -> int:
    """Cumulative already-mirrored refund cents for one funding operation."""
    from models.usage import CampaignUsageEntry

    candidates = db.scalars(
        select(CampaignUsageEntry).where(
            CampaignUsageEntry.campaign_id == operation.campaign_id,
            CampaignUsageEntry.entry_type == ENTRY_TYPE_ADMIN_ADJUSTMENT,
            CampaignUsageEntry.amount_cents < 0,
        )
    ).all()
    total = 0
    for entry in candidates:
        metadata = entry.entry_metadata or {}
        if (str(metadata.get("funding_operation_id") or "") == str(operation.id)
                and metadata.get("stripe_refund_id")):
            total += abs(int(entry.amount_cents))
    return total


def _mirror_stripe_refund(db: Session, *, event_id: str, event_type: str, obj: dict[str, Any],
                          stripe_client: StripeClient | None) -> dict[str, Any]:
    """Mirror Stripe money-returned state into campaign accounting, idempotently.

    A Stripe refund means funds left the campaign pool, so each refund is
    mirrored as a signed negative correction (never a positive credit, and
    never by editing history). Every refund id gets its own ledger key
    ``stripe_refund:{refund_id}``: a ``charge.refunded`` delivery re-emits
    the charge's whole refund list, so already-mirrored ids replay as
    duplicates while newly-appearing partial refunds are mirrored — no
    ordering or batching can double-mirror or skip one. The cumulative
    mirrored total can never exceed the confirmed funding amount
    (fail closed on excess). If the funding operation is still pending when
    the refund arrives (reordered delivery), state is reconciled against
    Stripe first so refunds apply to the confirmed credit, never to nothing.
    """
    items, payment_intent_id = _refund_items(obj)
    operation = _resolve_operation(db, obj, payment_intent_id=payment_intent_id)
    if operation is None:
        logger.warning("funding refund references unknown operation event_id=%s", event_id)
        return {"status": "ignored", "applied": WEBHOOK_APPLIED_IGNORED, "campaign_id": None}
    _check_campaign_matches(operation, obj)
    if operation.status == FUNDING_STATUS_PENDING:
        # Reordered refund-before-confirm: reconcile first so the refund
        # mirrors against the confirmed credit. Reconciliation failures
        # propagate (500 → Stripe retries) rather than mirroring a negative
        # correction with no confirmed credit behind it.
        reconcile_funding_operation(db, operation, stripe_client=stripe_client)
    if operation.status != FUNDING_STATUS_CONFIRMED:
        # No confirmed credit exists to reverse (failed/canceled, or Stripe
        # state still unresolved): never post a negative correction against
        # nothing. Terminal-failure refunds are Stripe-side only.
        _funding_log(logging.INFO, "funding.refund_without_credit", operation,
                     stripe_event_id=event_id, detail=operation.status)
        return {"status": "ignored", "applied": WEBHOOK_APPLIED_IGNORED,
                "campaign_id": operation.campaign_id, "operation": operation}
    if not items:
        _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                     stripe_event_id=event_id, detail="refund_without_identifier")
        raise FundingValidationError("stripe refund event carries no refund identifier")
    from models.usage import CampaignUsageEntry

    mirrored_total = _mirrored_refund_total_cents(db, operation)
    mirrored_new = 0
    replayed = 0
    for refund_id, refund_amount, refund_currency, moves_money in items:
        if not moves_money:
            # Pending/failed/canceled: moves no money yet. Its final state
            # arrives via a later refund.updated / charge.refunded delivery.
            _funding_log(logging.INFO, "funding.refund_not_final", operation,
                         stripe_event_id=event_id, stripe_refund_id=refund_id)
            continue
        if refund_currency and refund_currency != operation.currency.lower():
            _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                         stripe_event_id=event_id, detail="refund_currency_mismatch")
            raise FundingValidationError("stripe refund currency does not match funding operation")
        key = f"stripe_refund:{refund_id}"
        if db.scalar(
            select(CampaignUsageEntry).where(
                CampaignUsageEntry.campaign_id == operation.campaign_id,
                CampaignUsageEntry.idempotency_key == key,
            )
        ) is not None:
            _funding_log(logging.INFO, "funding.refund_duplicate", operation,
                         stripe_event_id=event_id, stripe_refund_id=refund_id)
            replayed += 1
            continue
        if mirrored_total + refund_amount > operation.amount_cents:
            _funding_log(logging.WARNING, "funding.reconciliation_failed", operation,
                         stripe_event_id=event_id, detail="refund_exceeds_funding")
            raise FundingValidationError(
                "stripe refunds cumulatively exceed the funded amount: refusing to mirror"
            )
        _ledger.record_entry(
            db,
            campaign_id=operation.campaign_id,
            entry_type=ENTRY_TYPE_ADMIN_ADJUSTMENT,
            amount_cents=-refund_amount,
            idempotency_key=key,
            contributor_user_id=operation.contributor_user_id,
            note="Stripe refund mirrored: funds returned to payer",
            entry_metadata={
                "funding_operation_id": str(operation.id),
                "stripe_refund_id": refund_id,
                "stripe_event_id": event_id,
            },
        )
        db.flush()
        mirrored_total += refund_amount
        mirrored_new += 1
        _funding_log(logging.INFO, "funding.refund_mirrored", operation,
                     stripe_event_id=event_id, stripe_refund_id=refund_id,
                     refund_amount_cents=refund_amount)
    if mirrored_new:
        return {"status": "refunded", "operation": operation}
    if replayed:
        return {"status": "duplicate", "operation": operation}
    return {"status": "ignored", "applied": WEBHOOK_APPLIED_IGNORED,
            "campaign_id": operation.campaign_id, "operation": operation}


# ── One-time re-credit for failed/abandoned counted work ──────────────────────


def _existing_recredit(db: Session, campaign_id, ai_run_id: uuid.UUID):
    """Find any recredit/refund entry already linked to this AI run."""
    from models.usage import CampaignUsageEntry

    candidates = db.scalars(
        select(CampaignUsageEntry).where(
            CampaignUsageEntry.campaign_id == campaign_id,
            CampaignUsageEntry.entry_type.in_([ENTRY_TYPE_RECREDIT, ENTRY_TYPE_REFUND]),
        )
    ).all()
    wanted = str(ai_run_id)
    for entry in candidates:
        metadata = entry.entry_metadata or {}
        if str(metadata.get("recredit_for_ai_run_id") or "") == wanted:
            return entry
    return None


def recredit_failed_run(
    db: Session, *, campaign_id, ai_run_id: uuid.UUID, actor_user_id=None,
    failure_reason: str | None = None, idempotency_key: str | None = None,
    note: str | None = None,
) -> Any:
    """Re-credit exactly once for failed/abandoned work that was counted.

    Links the compensating ``recredit`` entry to the original AI run and its
    ``ai_spend`` ledger entry. Recovery/non-billable runs were never counted
    (ledger excludes them), so they need no re-credit and are rejected.
    A second re-credit for the same run raises
    :class:`LedgerConflictError`. Flush-only; the caller commits.
    """
    from models.usage import CampaignUsageEntry

    run = db.get(AIRun, ai_run_id)
    if run is None:
        raise FundingValidationError(f"AI run {ai_run_id} not found")
    trace_campaign = None
    if run.trace_id:
        from models.reliability import OperationTrace

        trace = db.get(OperationTrace, run.trace_id)
        trace_campaign = trace.campaign_id if trace else None
    if trace_campaign is None or str(trace_campaign) != str(campaign_id):
        raise _ledger.LedgerConflictError(
            f"run {ai_run_id} is not attributable to campaign {campaign_id}: refusing to re-credit"
        )
    spend = db.scalar(select(CampaignUsageEntry).where(CampaignUsageEntry.ai_run_id == ai_run_id))
    if spend is None or str(spend.campaign_id) != str(campaign_id):
        # Nothing was ever counted for this run — recovery/non-billable work
        # is free by default and needs no charge or re-credit.
        raise FundingValidationError(
            f"run {ai_run_id} has no counted spend entry: nothing to re-credit"
        )
    if spend.entry_type != ENTRY_TYPE_AI_SPEND:
        raise FundingValidationError(f"run {ai_run_id} has no ai_spend entry: nothing to re-credit")
    # Serialize concurrent re-credits for the same run on the spend row:
    # the second transaction blocks here until the first commits, then sees
    # the committed recredit in the duplicate scan below instead of
    # double-crediting under a different idempotency key. (SQLite ignores
    # FOR UPDATE; Postgres serializes.)
    db.execute(
        select(CampaignUsageEntry.id)
        .where(CampaignUsageEntry.id == spend.id)
        .with_for_update()
    )
    status = str(run.status or "")
    reason = (failure_reason or "").strip().lower()
    if status not in RECREDIT_FAILED_RUN_STATUSES:
        if reason not in RECREDIT_ATTESTED_REASONS:
            raise FundingValidationError(
                f"run {ai_run_id} status={status!r} is not failed/abandoned: "
                "re-credit requires a failed/abandoned run or an explicit "
                "failure attestation ('failed'/'abandoned')"
            )
    else:
        reason = reason or status
    duplicate = _existing_recredit(db, campaign_id, ai_run_id)
    if duplicate is not None:
        raise _ledger.LedgerConflictError(
            f"run {ai_run_id} was already re-credited (entry {duplicate.id}): refusing a second re-credit"
        )
    amount = abs(int(spend.amount_cents))
    if amount <= 0:
        raise FundingValidationError(f"run {ai_run_id} counted no spend: nothing to re-credit")
    key = (idempotency_key or f"recredit:{ai_run_id}").strip()
    if not key:
        raise FundingValidationError("idempotency_key is required")
    if len(key) > 255:
        raise FundingValidationError("idempotency_key must be 255 characters or fewer")
    entry = _ledger.record_entry(
        db,
        campaign_id=campaign_id,
        entry_type=ENTRY_TYPE_RECREDIT,
        amount_cents=amount,
        idempotency_key=key,
        contributor_user_id=actor_user_id,
        note=note or f"Re-credit for {reason} AI run",
        entry_metadata={
            "recredit_for_ai_run_id": str(ai_run_id),
            "recredit_for_entry_id": str(spend.id),
            "ai_run_status": status,
            "failure_reason": reason,
        },
    )
    db.flush()
    _funding_log(logging.INFO, "funding.recredit", campaign_id=str(campaign_id),
                 ai_run_id=str(ai_run_id), recredit_entry_id=str(entry.id),
                 amount_cents=amount, failure_reason=reason)
    return entry
