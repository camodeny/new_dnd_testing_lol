"""Per-call-area provider/model pins.

Provider + model are set in code per call area — no env overrides for
either. Only API keys come from the environment
(``OPENAI_API_KEY``, ``META_API_KEY``, ``OPENROUTER_API_KEY``).

  dm             — forward-DM adjudication
  narrator       — streaming turn narration (prose expander)
  character_chat — character-creator assistant
  lore_dm_chat   — lobby lore-DM setup assistant (private lore back-and-forth)
  world_seed     — brief-grounded starting setting for a new campaign
"""

AREA_CONFIG = {
    "dm": ("openai", "gpt-6-luna"),
    "narrator": ("openai", "gpt-6-luna"),
    "character_chat": ("meta", "muse-spark-1.3-contributor"),
    "lore_dm_chat": ("meta", "muse-spark-1.3-contributor"),
    "world_seed": ("openai", "gpt-6-luna"),
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

