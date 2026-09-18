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
    get_capacity_summary,
    public_capacity,
    reconcile,
    record_ai_spend_for_run,
    record_entry,
    recovery_cost_usd,
    usd_to_cents,
)

__all__ = [
    "AccountingError",
    "AmbiguousCostError",
    "LedgerConflictError",
    "NonBillableRunError",
    "get_capacity_summary",
    "public_capacity",
    "reconcile",
    "record_ai_spend_for_run",
    "record_entry",
    "recovery_cost_usd",
    "usd_to_cents",
]
