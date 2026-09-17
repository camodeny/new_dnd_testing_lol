"""Deterministic failure taxonomy for bounded decision calls."""

from __future__ import annotations

from typing import Any


class DecisionError(Exception):
    """Normalized decision-runtime failure.

    ``kind`` is one of 'config', 'timeout', 'connection', 'http',
    'malformed', 'unsupported_feature'. ``retryable`` distinguishes
    transient transport failures from permanent ones. Classification comes
    from runtime metadata only — never from model output.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        kind: str = "http",
        original: Any = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.kind = kind
        self.original = original
