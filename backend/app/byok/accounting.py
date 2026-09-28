"""BYOK execution + accounting helpers — issue #257.

- :class:`ByokExecution` carries the resolved credential for one runtime
  call (IDs for tracing, plaintext only in memory for transport).
- :func:`mark_byok_run` records the zero-amount ``byok_marker`` ledger line
  so BYOK generative/decision runs are counted in AI/usage ledgers without
  consuming platform-funded provider cost. Credential IDs only.
- :func:`is_auth_failure` classifies invalid/expired credential failures so
  the owning runtime fails through its normal recovery/escalation path
  without exposing secret details.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ByokExecution:
    """Resolved BYOK context for one AI execution (server-side only)."""

    credential_id: uuid.UUID
    campaign_id: uuid.UUID
    secret: str = field(repr=False)
    provider: str


def is_auth_failure(exc: BaseException) -> bool:
    """Whether a failure looks like an invalid/expired provider credential.

    Pure metadata classification (status codes + provider taxonomy); the
    secret itself is never inspected. Auth failures are runtime recovery
    events, never player-visible secret details.

    Deliberately narrow: a bare 403 (quota, geo-block, rate limit) or a
    message containing only ``forbidden``/``expired`` (e.g. trial or quota
    expiry) must NOT kill a good key — only 401, 403 with auth-specific
    markers, missing-key config errors, or explicit invalid-key text flag
    the credential ``invalid`` (which requires rotation to re-arm).
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    auth_markers = (
        "invalid api key",
        "invalid_api_key",
        "incorrect api key",
        "wrong api key",
        "invalid key",
        "bad credentials",
        "invalid credentials",
        "unauthorized",
        "authentication",
        "key expired",
        "api key expired",
    )
    status_code = getattr(exc, "status_code", None)
    if status_code == 401:
        return True
    if status_code == 403:
        return any(marker in text for marker in auth_markers)
    kind = getattr(exc, "kind", "") or ""
    if kind == "config" and "API_KEY" in str(exc):
        return True
    return any(marker in text for marker in auth_markers)


def mark_byok_run(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    ai_run_id=None,
    credential_id: uuid.UUID,
    execution_class: str,
    role: str,
    provider: str,
    model: str,
) -> None:
    """Append the zero-amount BYOK marker for one succeeded BYOK run.

    Idempotent per AI run (``byok:{run}`` key). Fail-soft callers swallow
    errors: accounting must never rewrite gameplay.
    """
    from app.billing.ledger import record_entry

    key = f"byok:{ai_run_id}" if ai_run_id is not None else f"byok:{credential_id}:{role}:{uuid.uuid4().hex[:8]}"
    record_entry(
        db,
        campaign_id=campaign_id,
        entry_type="byok_marker",
        amount_cents=0,
        idempotency_key=key,
        note=f"BYOK {execution_class}/{role} run (non-platform spend)",
        entry_metadata={
            "credential_id": str(credential_id),
            "execution_class": execution_class,
            "role": role,
            "provider": provider,
            "model": model,
            "ai_run_id": str(ai_run_id) if ai_run_id is not None else None,
        },
    )


def mark_byok_run_fail_soft(
    session_factory,
    *,
    campaign_id,
    ai_run_id=None,
    credential_id,
    execution_class: str,
    role: str,
    provider: str,
    model: str,
) -> None:
    """Independent-transaction BYOK marker write (never touches gameplay)."""
    if session_factory is None or campaign_id is None:
        return
    try:
        with session_factory() as db:
            try:
                mark_byok_run(
                    db,
                    campaign_id=campaign_id,
                    ai_run_id=ai_run_id,
                    credential_id=credential_id,
                    execution_class=execution_class,
                    role=role,
                    provider=provider,
                    model=model,
                )
                db.commit()
            except Exception:
                db.rollback()
    except Exception:
        return
