"""Billing pricing/entitlement configuration — issue #253.

Single canonical source for monetary capacity parameters: per-model AI
pricing pinned in code. Checkout/plan changes never touch storytelling, DM,
or rules logic. This module must stay import-free of gameplay code.
"""

from __future__ import annotations

# Display rounding: percentage shown to players (0-100, one decimal).
PERCENT_DECIMALS = 1

# Canonical AI-run pricing (USD per 1M tokens), issue #259.
#
# The capacity ledger spends actual monetary cost, never message/token
# counts. Neither production provider reports cost (only token usage), so
# per-model list prices are pinned here, keyed by the exact provider/model
# pins in ``app.providers.areas.AREA_CONFIG``. Reasoning tokens arrive in the
# provider's output count and bill at the output rate. A provider/model not
# in this table is unpriced: cost resolution returns ``None`` and the ledger
# surfaces the run as ambiguous rather than guessing zero.
#
# (input, output) list prices as of 2026-10-08:
MODEL_PRICES_PER_MTOK_USD: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "gpt-6-luna"): (0.10, 0.50),
    ("meta", "muse-spark-1.3-contributor"): (0.10, 0.20),
}


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
    prices = MODEL_PRICES_PER_MTOK_USD.get((provider or "", model or ""))
    if prices is None:
        return None
    price_in, price_out = prices
    return (input_tokens or 0) * price_in / 1_000_000 + (output_tokens or 0) * price_out / 1_000_000
