"""Billing pricing/entitlement configuration — issue #253.

Single canonical source for monetary capacity parameters. Values come from
the environment so checkout/plan changes never touch storytelling, DM, or
rules logic. This module must stay import-free of gameplay code.
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Default funded allocation granted to a new campaign (cents). Checkout and
# plan entitlements override this per campaign via ledger entries; the value
# here is only the fallback default.
DEFAULT_FUNDED_CENTS = _int_env("BILLING_DEFAULT_FUNDED_CENTS", 0)

# Display rounding: percentage shown to players (0-100, one decimal).
PERCENT_DECIMALS = 1
