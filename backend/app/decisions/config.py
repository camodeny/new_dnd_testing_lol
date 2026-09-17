"""Environment-driven knobs for the bounded decision runtime.

Single canonical names (pre-alpha, no legacy aliases). ``TYPESAFE_API_KEY``
matches the official TypeSafe console/SDK convention.
"""

from __future__ import annotations

import math
import os

from app.decisions.errors import DecisionError

DEFAULT_MODEL = "jev-latest"
DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
MAX_DECISION_ATTEMPTS = 10

RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 529}


def api_key() -> str:
    return os.environ.get("TYPESAFE_API_KEY", "")


def default_model() -> str:
    return os.environ.get("JEV_MODEL", DEFAULT_MODEL)


def base_url() -> str:
    return os.environ.get("TYPESAFE_BASE_URL", DEFAULT_BASE_URL)


def default_timeout_seconds() -> float:
    raw = os.environ.get("DECISION_TIMEOUT_SECONDS", "10")
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise DecisionError(
            f"DECISION_TIMEOUT_SECONDS is not a number: {raw!r}",
            kind="config",
        ) from error
    if not math.isfinite(value) or value <= 0:
        raise DecisionError(
            f"DECISION_TIMEOUT_SECONDS must be a positive number: {raw!r}",
            kind="config",
        )
    return value


def default_max_attempts() -> int:
    raw = os.environ.get("DECISION_MAX_ATTEMPTS", "3")
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise DecisionError(
            f"DECISION_MAX_ATTEMPTS is not an integer: {raw!r}",
            kind="config",
        ) from error
    if not 1 <= value <= MAX_DECISION_ATTEMPTS:
        raise DecisionError(
            f"DECISION_MAX_ATTEMPTS must be 1-{MAX_DECISION_ATTEMPTS}: {raw!r}",
            kind="config",
        )
    return value


def _delay_value(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise DecisionError(
            f"{name} is not a number: {raw!r}",
            kind="config",
        ) from error
    if not math.isfinite(value) or value < 0:
        raise DecisionError(
            f"{name} must be a finite, non-negative number: {raw!r}",
            kind="config",
        )
    return value


def retry_base_delay_seconds() -> float:
    return _delay_value(
        "DECISION_RETRY_BASE_DELAY_SECONDS",
        os.environ.get("DECISION_RETRY_BASE_DELAY_SECONDS", "0.2"),
    )


def retry_max_delay_seconds() -> float:
    return max(
        retry_base_delay_seconds(),
        _delay_value(
            "DECISION_RETRY_MAX_DELAY_SECONDS",
            os.environ.get("DECISION_RETRY_MAX_DELAY_SECONDS", "2"),
        ),
    )


def retry_delay_seconds(failed_attempt: int) -> float:
    return min(
        retry_base_delay_seconds() * (2 ** max(failed_attempt - 1, 0)),
        retry_max_delay_seconds(),
    )
