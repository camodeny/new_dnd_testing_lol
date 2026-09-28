"""Approved execution-role routing for BYOK credentials — issue #257.

Extends the #208 generative role policy and the #380/#381 decision runtime
with the #258 rule: every production AI run resolves role -> approved route,
and a user credential may serve a role only when its provider/model/adapter
is independently approved for that exact role AND marked BYOK-eligible.

- Generative roles (``forward_dm``, ``narration``, ``character_chat``,
  ``lore_dm_chat``): the approved model is the area pin; the approved
  providers are the primary pin plus env-configured same-model alternates.
  Cross-model substitution via BYOK is rejected unless the pair appears in
  the role's explicitly approved fallback list.
- Decision roles (one entry per decision class, e.g. ``skirmish_action``):
  approval additionally binds adapter, policy schema version, candidate
  schema version, and execution mode (shadow / primer / direct). A Jev
  credential approved for one decision class is never implicitly approved
  for another.

This module holds metadata only — never secrets. Gameplay code does not
branch on provider names; it consumes :func:`resolve_generative_route` /
:func:`resolve_decision_route`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.byok.errors import ByokError, UNSUPPORTED

EXECUTION_CLASS_GENERATIVE = "generative"
EXECUTION_CLASS_DECISION = "decision"

# Credential provider names accepted for user-managed records. ``typesafe``
# is the vendor name for the ``jev`` decision adapter (single canonical
# adapter name ``jev``; the alias is normalized on write).
SUPPORTED_CREDENTIAL_PROVIDERS = frozenset({"openai", "openrouter", "meta", "jev"})
_PROVIDER_ALIASES = {"typesafe": "jev"}

# Generative execution roles and their provider areas (single source; the
# model pin stays in ``app.providers.areas.AREA_CONFIG``).
GENERATIVE_ROLE_AREA = {
    "forward_dm": "dm",
    "narration": "narrator",
    "character_chat": "character_chat",
    "lore_dm_chat": "lore_dm_chat",
}

# Decision execution modes (single canonical vocabulary).
MODE_SHADOW = "shadow"
MODE_PRIMER = "primer"
MODE_DIRECT = "direct_execute"
DECISION_MODES = frozenset({MODE_SHADOW, MODE_PRIMER, MODE_DIRECT})


def normalize_provider(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(normalized, normalized)


def assert_supported_provider(provider: str) -> str:
    from app.byok.errors import MALFORMED

    normalized = normalize_provider(provider)
    if not normalized:
        raise ByokError("provider is required", kind=MALFORMED)
    if normalized not in SUPPORTED_CREDENTIAL_PROVIDERS:
        raise ByokError(
            f"provider {provider!r} is not supported for BYOK "
            f"(supported: {', '.join(sorted(SUPPORTED_CREDENTIAL_PROVIDERS))})",
            kind=UNSUPPORTED,
        )
    return normalized


@dataclass(frozen=True)
class GenerativeRoute:
    role: str
    execution_class: str = EXECUTION_CLASS_GENERATIVE
    provider: str = ""
    model: str = ""


@dataclass(frozen=True)
class DecisionRoute:
    role: str
    execution_class: str = EXECUTION_CLASS_DECISION
    provider: str = ""
    adapter: str = ""
    model: str = ""
    mode: str = ""
    policy_version: int = 0
    candidate_schema_version: int = 0


@dataclass(frozen=True)
class DecisionRoleApproval:
    """Approved decision-role configuration (metadata only, never secrets)."""

    decision_class: str
    adapter: str
    model: str
    policy_version: int
    candidate_schema_version: int
    allowed_modes: tuple[str, ...] = (MODE_SHADOW, MODE_PRIMER)
    byok_eligible: bool = True
    evaluation_ref: str = ""


def _seeded_decision_approvals() -> dict[str, DecisionRoleApproval]:
    """Approvals seeded from the calibrated #381 policy registry."""
    from app.decisions.frames import CANDIDATE_SCHEMA_VERSION
    from app.decisions.policy import POLICY_SCHEMA_VERSION, POLICY_REGISTRY

    from app.decisions.config import default_model as _decision_model

    approvals: dict[str, DecisionRoleApproval] = {}
    for decision_class in POLICY_REGISTRY:
        approvals[decision_class] = DecisionRoleApproval(
            decision_class=decision_class,
            adapter="jev",
            model=_decision_model(),
            policy_version=POLICY_SCHEMA_VERSION,
            candidate_schema_version=CANDIDATE_SCHEMA_VERSION,
            allowed_modes=(MODE_SHADOW, MODE_PRIMER),
            byok_eligible=True,
            evaluation_ref=f"policy-v{POLICY_SCHEMA_VERSION}/candidates-v{CANDIDATE_SCHEMA_VERSION}",
        )
    return approvals


# Canonical decision-role approval table. ``register_decision_approval`` lets
# the evaluation workflow (#269) promote routes (e.g. direct execution)
# without gameplay code branching.
_DECISION_APPROVALS: dict[str, DecisionRoleApproval] = _seeded_decision_approvals()


def register_decision_approval(approval: DecisionRoleApproval) -> DecisionRoleApproval:
    """Register (or replace) the approval for one decision class."""
    from app.byok.errors import MALFORMED

    if not approval.decision_class or not approval.decision_class.strip():
        raise ByokError("decision approval is missing a decision class", kind=MALFORMED)
    if normalize_provider(approval.adapter) != "jev":
        raise ByokError(
            f"unknown decision adapter {approval.adapter!r} for class {approval.decision_class!r} "
            "(only the jev decision adapter is BYOK-executable)",
            kind=UNSUPPORTED,
        )
    unknown_modes = set(approval.allowed_modes) - DECISION_MODES
    if unknown_modes:
        raise ByokError(
            f"unknown decision modes {sorted(unknown_modes)} for class {approval.decision_class!r}",
            kind=MALFORMED,
        )
    _DECISION_APPROVALS[approval.decision_class] = approval
    return approval


def get_decision_approval(decision_class: str) -> DecisionRoleApproval:
    try:
        return _DECISION_APPROVALS[decision_class]
    except KeyError as exc:
        raise ByokError(
            f"decision class {decision_class!r} has no approved execution route; "
            "BYOK cannot serve unapproved decision roles",
            kind=UNSUPPORTED,
        ) from exc


def _byok_providers_from_env(role: str, primary: str) -> tuple[str, ...]:
    raw = os.getenv(f"{role.upper()}_BYOK_PROVIDERS", "") or ""
    extra = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    ordered = [normalize_provider(primary)]
    for provider in extra:
        # Operator allowlist entries are still normalized and restricted to
        # credential-supported providers: a misconfigured env value can
        # never approve an arbitrary provider for a role.
        candidate = normalize_provider(provider)
        if candidate in SUPPORTED_CREDENTIAL_PROVIDERS and candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)


def resolve_generative_route(role: str, *, provider: str, model: str) -> GenerativeRoute:
    """Approve a BYOK provider/model pair for a generative role or reject.

    The model must equal the role's pinned model (same-model BYOK through
    an approved provider); explicitly approved different-model fallbacks
    from the #208 policy are honored. Anything else is an unapproved
    substitution and raises.
    """
    from app.providers import policy as role_policy

    if role not in GENERATIVE_ROLE_AREA:
        raise ByokError(
            f"unknown generative execution role {role!r} "
            f"(known: {', '.join(sorted(GENERATIVE_ROLE_AREA))})",
            kind=UNSUPPORTED,
        )
    candidate_provider = normalize_provider(provider)
    candidate_model = model.strip() if isinstance(model, str) else ""
    if not candidate_model:
        raise ByokError("model is required for generative BYOK routing", kind="malformed")
    policy = role_policy.get_role_policy(role)
    eligible = _byok_providers_from_env(role, policy.primary_provider)
    same_model_ok = candidate_model == policy.primary_model and candidate_provider in eligible
    fallback_ok = (candidate_provider, candidate_model) in policy.allowed_fallback_models
    if not (same_model_ok or fallback_ok):
        raise ByokError(
            f"provider/model {candidate_provider}/{candidate_model} is not approved "
            f"for generative role {role!r} (approved model: {policy.primary_model!r} "
            f"via {', '.join(eligible)}); BYOK never bypasses role approvals",
            kind=UNSUPPORTED,
        )
    return GenerativeRoute(role=role, provider=candidate_provider, model=candidate_model)


def resolve_decision_route(
    decision_class: str, *, provider: str, model: str, mode: str
) -> DecisionRoute:
    """Approve a BYOK credential for a decision role/mode or reject.

    Requires a #258/#269-style approval for the exact decision class binding
    adapter, model, policy/candidate schema versions, and mode. A credential
    for an approved adapter in one class is rejected in any other class
    without its own approval — possession of a Jev key is never approval.
    """
    approval = get_decision_approval(decision_class)
    candidate_provider = normalize_provider(provider)
    candidate_model = model.strip() if isinstance(model, str) else ""
    if mode not in DECISION_MODES:
        raise ByokError(
            f"unknown decision mode {mode!r} (expected one of {sorted(DECISION_MODES)})",
            kind="malformed",
        )
    if not approval.byok_eligible:
        raise ByokError(
            f"decision class {decision_class!r} is not eligible for BYOK execution",
            kind=UNSUPPORTED,
        )
    if candidate_provider != approval.adapter or candidate_model != approval.model:
        raise ByokError(
            f"provider/model {candidate_provider}/{candidate_model} is not approved "
            f"for decision class {decision_class!r} "
            f"(approved: {approval.adapter}/{approval.model}); "
            "cross-model substitution requires a recorded approval",
            kind=UNSUPPORTED,
        )
    if mode not in approval.allowed_modes:
        raise ByokError(
            f"decision class {decision_class!r} is not approved for mode {mode!r} "
            f"(approved: {', '.join(approval.allowed_modes)})",
            kind=UNSUPPORTED,
        )
    from app.decisions.frames import CANDIDATE_SCHEMA_VERSION
    from app.decisions.policy import POLICY_SCHEMA_VERSION

    if (
        approval.policy_version != POLICY_SCHEMA_VERSION
        or approval.candidate_schema_version != CANDIDATE_SCHEMA_VERSION
    ):
        raise ByokError(
            f"decision class {decision_class!r} approval binds policy-v{approval.policy_version}/"
            f"candidates-v{approval.candidate_schema_version} but runtime is "
            f"policy-v{POLICY_SCHEMA_VERSION}/candidates-v{CANDIDATE_SCHEMA_VERSION}",
            kind=UNSUPPORTED,
        )
    return DecisionRoute(
        role=decision_class,
        provider=candidate_provider,
        adapter=approval.adapter,
        model=candidate_model,
        mode=mode,
        policy_version=approval.policy_version,
        candidate_schema_version=approval.candidate_schema_version,
    )


def byok_route_available(role: str, *, provider: str, model: str) -> bool:
    """Non-raising eligibility probe for capacity/observability paths."""
    try:
        resolve_generative_route(role, provider=provider, model=model)
    except ByokError:
        return False
    return True
