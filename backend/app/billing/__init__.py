"""Billing domain — entitlement + usage accounting (issue #253).

Pricing/entitlement knobs live in ``app.billing.config`` (env-driven, never
in storytelling logic). Campaign capacity accounting lives in
``app.billing.ledger`` (deterministic, append-only, code-owned).
"""

from app.billing.ledger import (
    AccountingError,
    AmbiguousCostError,
    LedgerConflictError,
    NonBillableRunError,
    charge_completed_run,
    get_capacity_summary,
    public_capacity,
    reconcile,
    record_ai_spend_for_run,
    record_entry,
    recovery_cost_usd,
    usd_to_cents,
)

from app.billing.stripe_funding import (
    FundingError,
    FundingNotConfiguredError,
    FundingValidationError,
    StripeApiError,
    WebhookVerificationError,
    confirm_funding_operation,
    create_funding_operation,
    get_funding_operation,
    handle_stripe_event,
    ledger_idempotency_key,
    mark_funding_terminal,
    reconcile_funding_operation,
    recredit_failed_run,
    stripe_idempotency_key,
    verify_webhook_signature,
)

__all__ = [
    "AccountingError",
    "AmbiguousCostError",
    "FundingError",
    "FundingNotConfiguredError",
    "FundingValidationError",
    "LedgerConflictError",
    "NonBillableRunError",
    "StripeApiError",
    "WebhookVerificationError",
    "charge_completed_run",
    "confirm_funding_operation",
    "create_funding_operation",
    "get_capacity_summary",
    "get_funding_operation",
    "handle_stripe_event",
    "ledger_idempotency_key",
    "mark_funding_terminal",
    "public_capacity",
    "reconcile",
    "reconcile_funding_operation",
    "record_ai_spend_for_run",
    "record_entry",
    "recredit_failed_run",
    "recovery_cost_usd",
    "stripe_idempotency_key",
    "usd_to_cents",
    "verify_webhook_signature",
]
