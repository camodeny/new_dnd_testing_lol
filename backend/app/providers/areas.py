"""Per-call-area provider/model pins.

Provider + model are set in code per call area — no env overrides for
either. Only API keys come from the environment
(``OPENAI_API_KEY``, ``META_API_KEY``, ``OPENROUTER_API_KEY``).

  dm             — forward-DM adjudication
  narrator       — streaming turn narration (prose expander)
  character_chat — character-creator assistant
  lore_dm_chat   — lobby lore-DM setup assistant (private lore back-and-forth)
"""

AREA_CONFIG = {
    "dm": ("meta", "muse-spark-1.3-contributor"),
    "narrator": ("openai", "gpt-6-luna"),
    "character_chat": ("meta", "muse-spark-1.3-contributor"),
    "lore_dm_chat": ("meta", "muse-spark-1.3-contributor"),
}

AREAS = tuple(AREA_CONFIG)


def resolve_area(area):
    """Resolve (adapter, model, provider_name) for a call area.

    Raises RuntimeError with a deployment-actionable message when the
    area is unknown or the provider's API key is absent
    (adapter.require_config is the fail-clear gate).
    """
    from app.providers.registry import provider_registry

    try:
        provider_name, model = AREA_CONFIG[area]
    except KeyError:
        raise RuntimeError(
            f"Unknown provider area {area!r} (known: {', '.join(AREAS)})"
        ) from None
    adapter = provider_registry.get(provider_name)
    adapter.require_config(model)
    return adapter, model, adapter.name


def resolve_role(role):
    """Resolve (adapter, model, provider_name) for an execution role.

    Thin mapping over :func:`resolve_area` — roles (``forward_dm``,
    ``narration``) are the #208 policy surface; areas remain the pin
    table. Failover/fallback candidates resolve through
    ``app.providers.policy`` (same model via alternate providers, or only
    explicitly approved different models).
    """
    from app.providers import policy as _policy

    area = _policy.ROLE_AREA.get(role, role)
    return resolve_area(area)


def resolve_failover_adapters(role):
    """Resolve same-model / explicitly-approved failover adapters for a role.

    Returns a list of (adapter, model, provider_name); unapproved model
    substitution is never included. Adapters whose credentials are absent
    are skipped (fail-clear per provider, not fatal for the chain).
    """
    from app.providers import policy as _policy
    from app.providers.registry import provider_registry

    path = _policy.execution_path(role)[1:]  # skip primary
    out = []
    for provider_name, model in path:
        try:
            adapter = provider_registry.get(provider_name)
            adapter.require_config(model)
        except Exception:
            continue
        out.append((adapter, model, adapter.name))
    return out
