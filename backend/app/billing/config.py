"""Billing pricing/entitlement configuration — issue #253.

Single canonical source for monetary capacity parameters. Values come from
the environment so checkout/plan changes never touch storytelling, DM, or
rules logic. This module must stay import-free of gameplay code.
"""

from __future__ import annotations

import json
import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float | None) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Default funded allocation granted to a new campaign (cents). Checkout and
# plan entitlements override this per campaign via ledger entries; the value
# here is only the fallback default.
DEFAULT_FUNDED_CENTS = _int_env("BILLING_DEFAULT_FUNDED_CENTS", 0)

# Display rounding: percentage shown to players (0-100, one decimal).
PERCENT_DECIMALS = 1

# Canonical AI-run pricing (USD per 1M tokens), issue #253.
#
# The capacity ledger must reflect actual monetary cost, never message/token
# counts — but providers report token usage, so one deterministic,
# env-configurable translation lives here (outside storytelling/DM logic).
# ``None`` means unpriced: cost resolution returns ``None`` and the ledger
# surfaces the run as ambiguous rather than guessing zero.
DEFAULT_INPUT_PER_MTOK_USD = _float_env("BILLING_DEFAULT_INPUT_PER_MTOK_USD", None)
DEFAULT_OUTPUT_PER_MTOK_USD = _float_env("BILLING_DEFAULT_OUTPUT_PER_MTOK_USD", None)


def _model_price_overrides() -> dict:
    raw = os.getenv("BILLING_MODEL_PRICES_JSON", "")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _price_pair(provider: str | None, model: str | None) -> tuple[float | None, float | None]:
    overrides = _model_price_overrides()
    keys = [
        f"{provider}/{model}" if provider and model else None,
        model or None,
        provider or None,
    ]
    for key in keys:
        if key and key in overrides:
            pair = overrides[key]
            try:
                return float(pair[0]), float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
    return DEFAULT_INPUT_PER_MTOK_USD, DEFAULT_OUTPUT_PER_MTOK_USD


def _tokens_from_usage(usage: dict | None) -> tuple[int | None, int | None]:
    """Normalize provider-native usage dicts to (input, output) tokens."""
    if not isinstance(usage, dict):
        return None, None
    def _pick(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
        return None
    return (
        _pick("prompt_tokens", "input_tokens"),
        _pick("completion_tokens", "output_tokens"),
    )


def tokens_from_usage(usage: dict | None) -> tuple[int | None, int | None]:
    """Public accessor for normalized (input, output) token counts."""
    return _tokens_from_usage(usage)


def cost_usd_for(provider: str | None, model: str | None, usage: dict | None) -> float | None:
    """Canonical USD cost for one provider call, or ``None`` when unknown.

    Returns ``None`` (ambiguous, never zero-guessed) when token counts or
    pricing are unavailable. A fully-priced zero-token call costs ``0.0``.
    """
    input_tokens, output_tokens = _tokens_from_usage(usage)
    if input_tokens is None and output_tokens is None:
        return None
    price_in, price_out = _price_pair(provider, model)
    if price_in is None or price_out is None:
        return None
    return (input_tokens or 0) * price_in / 1_000_000 + (output_tokens or 0) * price_out / 1_000_000
