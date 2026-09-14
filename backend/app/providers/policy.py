"""Role-aware provider/model execution policy — issue #208.

Single canonical policy surface for AI execution roles (``forward_dm``,
``narration``). Extends the #354 pinned areas without introducing a
parallel provider/runtime stack: primary pins stay in
``app.providers.areas.AREA_CONFIG``; this module adds the hardening
around them:

- same-model alternate-provider failover (allowed),
- different-model fallback only when explicitly approved for the role
  (default: none approved — unapproved substitution is impossible),
- bounded automatic retry/failover budgets,
- unified retryable-vs-terminal classification,
- non-billable recovery accounting hooks.

Provider-specific branching stays inside provider/execution adapters;
game logic consumes only this policy surface.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FORWARD_DM_ROLE = "forward_dm"
NARRATION_ROLE = "narration"
ROLES = (FORWARD_DM_ROLE, NARRATION_ROLE)

# Area name (providers/areas.py) per execution role. Role names differ
# from area names (``dm`` vs ``forward_dm``) for readability at call sites.
ROLE_AREA = {
    FORWARD_DM_ROLE: "dm",
    NARRATION_ROLE: "narrator",
}

# Generic player-visible failure — never includes provider/model/status
# details. Infrastructure specifics stay in logs + attempt.last_error.
GENERIC_RETRYABLE_MESSAGE = "The storyteller faltered. You can retry this turn."


@dataclass(frozen=True)
class RolePolicy:
    role: str
    primary_provider: str
    primary_model: str
    # Same approved model through alternate approved providers, in order.
    failover_providers: tuple[str, ...] = ()
    # Different-model fallback, allowed ONLY when explicitly approved for
    # the role. Empty by default: unapproved substitution is impossible.
    allowed_fallback_models: tuple[tuple[str, str], ...] = ()
    max_attempts: int = 3


def _failover_from_env(role: str) -> tuple[str, ...]:
    prefix = role.upper()
    raw = os.getenv(f"{prefix}_FAILOVER_PROVIDERS", "") or ""
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def _fallback_models_from_env(role: str) -> tuple[tuple[str, str], ...]:
    prefix = role.upper()
    raw = os.getenv(f"{prefix}_FALLBACK_MODELS", "") or ""
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or "/" not in item:
            continue
        provider, model = item.split("/", 1)
        provider, model = provider.strip().lower(), model.strip()
        if provider and model:
            out.append((provider, model))
    return tuple(out)


def get_role_policy(role: str) -> RolePolicy:
    """Resolve the execution policy for a role (single canonical source)."""
    from app.providers.areas import AREA_CONFIG

    area = ROLE_AREA.get(role, role)
    try:
        primary_provider, primary_model = AREA_CONFIG[area]
    except KeyError:
        raise RuntimeError(f"Unknown execution role {role!r}") from None
    return RolePolicy(
        role=role,
        primary_provider=primary_provider,
        primary_model=primary_model,
        failover_providers=_failover_from_env(role),
        allowed_fallback_models=_fallback_models_from_env(role),
        max_attempts=max(1, int(os.getenv(f"{role.upper()}_MAX_ATTEMPTS", "3") or 3)),
    )


def is_model_approved(role: str, provider: str, model: str) -> bool:
    """True only for the primary pin, same-model failover, or an explicitly
    approved fallback. Anything else is an unapproved substitution."""
    policy = get_role_policy(role)
    p = (provider or "").strip().lower()
    m = (model or "").strip()
    if p == policy.primary_provider and m == policy.primary_model:
        return True
    if m == policy.primary_model and p in policy.failover_providers:
        return True
    return (p, m) in policy.allowed_fallback_models


def execution_path(role: str) -> list[tuple[str, str]]:
    """Ordered (provider, model) attempts: primary, same-model failover,
    then explicitly approved different-model fallbacks. Bounded by policy."""
    policy = get_role_policy(role)
    path = [(policy.primary_provider, policy.primary_model)]
    for provider in policy.failover_providers:
        candidate = (provider, policy.primary_model)
        if candidate not in path:
            path.append(candidate)
    for candidate in policy.allowed_fallback_models:
        if candidate not in path:
            path.append(candidate)
    return path[: policy.max_attempts]


def classify_execution_failure(exc: BaseException) -> tuple[str, str]:
    """Unified retryable-vs-terminal classification with a reason string.

    Returns ("retriable" | "terminal", reason). Single taxonomy plumbed
    end-to-end; provider/transport/schema/tool failures map here.
    """
    from app.providers.contracts import ProviderError

    if isinstance(exc, ProviderError):
        if exc.kind in ("malformed", "unsupported_feature", "config"):
            # Malformed structured output: retriable via bounded
            # regeneration at the validation layer, but terminal as a raw
            # transport outcome (no point hammering the same bytes).
            if exc.kind == "malformed":
                return "retriable", f"malformed_structured_output:{exc.kind}"
            return "terminal", f"provider_{exc.kind}"
        if exc.retryable:
            return "retriable", f"provider_{exc.kind}:{exc.status_code or 'transient'}"
        return "terminal", f"provider_{exc.kind}:{exc.status_code or 'permanent'}"
    msg = str(exc)
    if "_API_KEY is not set" in msg or "_MODEL is not set" in msg:
        return "retriable", "missing_provider_config"
    try:
        from app.worker.executor import TERMINAL, RETRIABLE, classify_error

        cls = classify_error(exc)
        if cls == TERMINAL:
            return "terminal", f"worker_terminal:{type(exc).__name__}"
        return "retriable", f"worker_retriable:{type(exc).__name__}"
    except Exception:
        return "terminal", "classification_failed"


# ── Recovery observability (process-local counters) ─────────────────────────

_recovery_metrics: dict = {
    "failover_attempts": 0,
    "recovery_runs": 0,
    "recovery_billable": 0,
    "partial_resumes": 0,
    "partial_continuations": 0,
    "exhausted_failures": 0,
    "failover_reasons": [],
    "ttft_added_ms_samples": [],
}


def record_failover_attempt(reason: str, provider: str, model: str) -> None:
    _recovery_metrics["failover_attempts"] += 1
    _recovery_metrics["failover_reasons"].append(
        {"reason": reason[:200], "provider": provider, "model": model}
    )


def record_recovery_run(*, billable: bool = False, ttft_added_ms: float = 0.0) -> None:
    _recovery_metrics["recovery_runs"] += 1
    if billable:
        _recovery_metrics["recovery_billable"] += 1
    if ttft_added_ms:
        _recovery_metrics["ttft_added_ms_samples"].append(float(ttft_added_ms))


def record_partial_resume(method: str) -> None:
    if method == "direct_resume":
        _recovery_metrics["partial_resumes"] += 1
    else:
        _recovery_metrics["partial_continuations"] += 1


def record_exhausted() -> None:
    _recovery_metrics["exhausted_failures"] += 1


def get_recovery_metrics() -> dict:
    out = {
        k: (list(v) if isinstance(v, list) else v)
        for k, v in _recovery_metrics.items()
    }
    samples = out["ttft_added_ms_samples"] or [0]
    out["ttft_added_ms_p50"] = sorted(samples)[len(samples) // 2]
    return out


def reset_recovery_metrics() -> None:
    for k, v in _recovery_metrics.items():
        _recovery_metrics[k] = [] if isinstance(v, list) else 0
