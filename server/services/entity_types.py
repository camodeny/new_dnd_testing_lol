"""Shared canonical entity types for campaign knowledge graphs."""


WORLD_ENTITY_TYPES = (
    "character",
    "npc",
    "location",
    "faction",
    "group",
    "item",
    "vehicle",
    "event",
    "threat",
    "concept",
    "other",
)

WORLD_ENTITY_TYPE_HINT = " | ".join(WORLD_ENTITY_TYPES)

WORLD_ENTITY_TYPE_ALIASES = {
    "pc": "character",
    "player character": "character",
    "player_character": "character",
    "person": "npc",
    "organization": "faction",
    "organisation": "faction",
    "family": "group",
    "npc group": "group",
    "npc_group": "group",
    "party": "group",
    "object": "item",
    "item device": "item",
    "item/device": "item",
    "item object": "item",
    "item/object": "item",
    "mechanism": "item",
    "ship": "vehicle",
    "vessel": "vehicle",
    "entity": "other",
    "unknown": "other",
}


def normalize_world_entity_type(value):
    normalized = " ".join(str(value or "").strip().lower().split())
    normalized = WORLD_ENTITY_TYPE_ALIASES.get(normalized, normalized)
    return normalized if normalized in WORLD_ENTITY_TYPES else "other"
