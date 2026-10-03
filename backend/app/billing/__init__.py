"""Billing domain — entitlement + usage accounting (issue #253).

Pricing/entitlement knobs live in ``app.billing.config`` (env-driven, never
in storytelling logic). Campaign capacity accounting lives in
``app.billing.ledger`` (deterministic, append-only, code-owned).
"""
