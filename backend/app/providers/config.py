"""Provider configuration and retry policy knobs.

Environment-driven settings (attempt counts, backoff windows, retryable status
codes) live here so both adapters (error classification) and transport (retry
loops) share a single source of truth without importing each other.
"""
import os

RETRIABLE_STATUS_CODES = {404, 408, 409, 425, 429}


def _enabled(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


def default_max_attempts():
    return max(1, int(os.environ.get('LLM_MAX_ATTEMPTS', os.environ.get('OPENROUTER_MAX_ATTEMPTS', '4'))))


def retry_base_delay_seconds():
    return max(
        0.0,
        float(os.environ.get('LLM_RETRY_BASE_DELAY_SECONDS', os.environ.get('OPENROUTER_RETRY_BASE_DELAY_SECONDS', '1'))),
    )


def retry_max_delay_seconds():
    return max(
        retry_base_delay_seconds(),
        float(os.environ.get('LLM_RETRY_MAX_DELAY_SECONDS', os.environ.get('OPENROUTER_RETRY_MAX_DELAY_SECONDS', '8'))),
    )


def retry_delay_seconds(failed_attempt):
    return min(
        retry_base_delay_seconds() * (2 ** max(failed_attempt - 1, 0)),
        retry_max_delay_seconds(),
    )
