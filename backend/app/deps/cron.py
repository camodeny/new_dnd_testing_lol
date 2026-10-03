"""Cron trigger auth guard shared by the Supabase pg_cron sweep endpoints.

When ``CRON_SECRET`` is set, callers must send
``Authorization: Bearer <CRON_SECRET>``. When unset, the guard fails closed
with 503 — it is only reachable without a secret under an explicit
local/test bypass (``ALLOW_INSECURE_CRON=1``).
"""
from __future__ import annotations

import logging
import os

from fastapi import HTTPException

logger = logging.getLogger(__name__)

#: Explicit local/test bypass for the fail-closed cron guard. Never set in
#: production — cron callers must present CRON_SECRET instead.
INSECURE_CRON_BYPASS_ENV = "ALLOW_INSECURE_CRON"


def _insecure_cron_bypassed() -> bool:
    return os.getenv(INSECURE_CRON_BYPASS_ENV, "").lower() in ("1", "true", "yes", "on")


def require_cron_secret(authorization: str | None) -> None:
    expected = os.getenv("CRON_SECRET")
    if expected:
        if authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="Unauthorized")
        return
    if _insecure_cron_bypassed():
        logger.warning("cron auth bypassed via ALLOW_INSECURE_CRON (local/test only)")
        return
    raise HTTPException(status_code=503, detail="CRON_SECRET is not configured")
