"""SRD 5.2.1 stat blocks and encounter budgets (issue #478).

Read-only access to the committed bestiary (``data/srd521_bestiary.json``,
built by ``scripts.build_bestiary`` from the pinned SRD derivative) plus the
SRD's encounter-building tables. Code owns every number here: the DM only
picks a stat block by id, and :func:`check_assignable` decides whether that
pick fits the party.

Assigned blocks become NPC ``details`` in the shape ``rules.attacks`` and
``combat.service`` already read (``hit_points``, ``armor_class``,
``attack_bonus``/``damage``, ``dexterity``, ``initiative_modifier``,
resistances), so no rules code branches on where stats came from.
"""
from __future__ import annotations

import difflib
import json
import os
import re
from functools import lru_cache
from typing import Any

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "srd521_bestiary.json")

#: SRD 5.2.1 "Gameplay Toolbox › Combat Encounters › Step 2: Determine Your
#: XP Budget": per-character XP by party level for Low / Moderate / High.
XP_BUDGET_PER_CHARACTER: dict[int, tuple[int, int, int]] = {
    1: (50, 75, 100), 2: (100, 150, 200), 3: (150, 225, 400), 4: (250, 375, 500),
    5: (500, 750, 1100), 6: (600, 1000, 1400), 7: (750, 1300, 1700), 8: (1000, 1700, 2100),
    9: (1300, 2000, 2600), 10: (1600, 2300, 3100), 11: (1900, 2900, 4100), 12: (2200, 3700, 4700),
    13: (2600, 4200, 5400), 14: (2900, 4900, 6200), 15: (3300, 5400, 7800), 16: (3800, 6100, 9800),
    17: (4500, 7200, 11700), 18: (5000, 8700, 14200), 19: (5500, 10700, 17200), 20: (6400, 13200, 22000),
}

#: Campaign difficulty -> SRD encounter difficulty column. The SRD has no
#: tier above High, so ``deadly`` shares it (deadliness comes from play).
_DIFFICULTY_COLUMN = {"easy": 0, "medium": 1, "hard": 2, "deadly": 2}

#: NPC details key holding the assigned block's provenance; its presence
#: means the NPC's stats are canon and cannot be re-assigned.
STAT_BLOCK_SECTION = "stat_block"


#: Every details key :func:`stat_block_details` writes. Combat stats are
#: DM-only: member projections drop them (``rules.state``).
STAT_BLOCK_KEYS = (
    STAT_BLOCK_SECTION, "hit_points", "armor_class", "initiative_modifier", "dexterity",
    "abilities", "attacks", "attack_name", "attack_bonus", "damage", "resistances",
    "vulnerabilities", "immunities", "condition_immunities", "multiattack",
)


#: SRD 5.2.1 creature types; every committed block has exactly one.
CREATURE_TYPES = (
    "aberration", "beast", "celestial", "construct", "dragon", "elemental", "fey",
    "fiend", "giant", "humanoid", "monstrosity", "ooze", "plant", "undead",
)


class StatBlockError(ValueError):
    """Refused stat-block pick; ``code`` is stable for feedback/telemetry."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=1)
def _bestiary() -> dict[str, Any]:
    with open(_DATA_PATH) as fh:
        data = json.load(fh)
    data["by_id"] = {m["id"]: m for m in data["monsters"]}
    return data


def get_stat_block(monster_id: str) -> dict[str, Any] | None:
    return _bestiary()["by_id"].get(str(monster_id or "").strip().lower())


def creature_type(block: dict[str, Any]) -> str:
    """The block's SRD creature type, read from its type line ("Huge Dragon (Chromatic)")."""
    head = str(block.get("type_line") or "").split(",")[0].lower()
    for kind in CREATURE_TYPES:
        if re.search(rf"\b{kind}s?\b", head):
            return kind
    raise ValueError(f"stat block {block.get('id')!r} has no recognizable creature type")


def describe(block: dict[str, Any]) -> str:
    """One line a DM can judge fit by: type, CR, defenses, and attacks."""
    attacks = ", ".join(f"{a['name']} ({a['damage_type']})" for a in block["attacks"][:3]) or "no weapon attacks"
    defenses = [f"immune {'/'.join(block['immunities'])}" if block["immunities"] else "",
                f"resists {'/'.join(block['resistances'])}" if block["resistances"] else ""]
    extra = "; ".join(d for d in defenses if d)
    return (f"{block['id']} ({creature_type(block).title()}, CR {block['challenge_rating']}, "
            f"AC {block['armor_class']}, HP {block['hit_points']}; {attacks}" + (f"; {extra}" if extra else "") + ")")


def _profile_tokens(block: dict[str, Any]) -> set[str]:
    text = " ".join([
        block["id"].replace("-", " "), block["name"], str(block.get("type_line") or ""),
        " ".join(block["immunities"]), " ".join(block["resistances"]), " ".join(block["condition_immunities"]),
        " ".join(f"{a['name']} {a['damage_type']}" for a in block["attacks"]),
    ]).lower()
    return set(re.findall(r"[a-z]+", text))


def search_blocks(query: str, *, max_xp: int, kind: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """In-budget blocks ranked by overlap between ``query`` and each block's profile.

    Lexical on purpose: the DM describes the creature's nature ("silt water
    ooze", "fire spirit", "veteran soldier") and code returns only blocks the
    party may face, so the pick stays inside the legal candidate set.
    """
    words = set(re.findall(r"[a-z]+", str(query or "").lower()))
    legal = [m for m in _bestiary()["monsters"] if m["xp"] <= max_xp and (kind is None or creature_type(m) == kind)]
    ranked = sorted(legal, key=lambda m: (-len(words & _profile_tokens(m)), -m["xp"], m["id"]))
    return ranked[:max(1, limit)]


def encounter_xp_budget(party_levels: list[int], difficulty: str) -> int:
    """Total party XP budget: per-character budget at each PC's level, summed."""
    column = _DIFFICULTY_COLUMN.get(str(difficulty or "medium"), 1)
    return sum(XP_BUDGET_PER_CHARACTER[min(max(int(level), 1), 20)][column] for level in party_levels)


def suggest_alternatives(requested: str, *, max_xp: int, limit: int = 5) -> list[str]:
    """Closest-named legal blocks (``id (CR x)``) at or under ``max_xp``."""
    legal = [m for m in _bestiary()["monsters"] if m["xp"] <= max_xp]
    names = {m["id"]: m for m in legal}
    ranked = difflib.get_close_matches(str(requested or "").lower(), list(names), n=limit, cutoff=0.0)
    return [f"{mid} (CR {names[mid]['challenge_rating']})" for mid in ranked]


def check_assignable(
    monster_id: str, *, party_levels: list[int], difficulty: str, declared_type: str | None = None,
) -> dict[str, Any]:
    """The stat block for ``monster_id`` if the party may face it, else raise.

    v1 bound: one creature's XP may not exceed the whole party's encounter
    budget at the campaign difficulty. An empty roster (no PCs yet) is
    treated as one level-1 character.
    """
    levels = party_levels or [1]
    budget = encounter_xp_budget(levels, difficulty)
    block = get_stat_block(monster_id)
    if block is None:
        raise StatBlockError(
            "unknown_stat_block",
            f"no SRD stat block {monster_id!r}; closest: {', '.join(suggest_alternatives(monster_id, max_xp=budget))}",
        )
    if declared_type is not None and creature_type(block) != declared_type:
        same_type = "; ".join(describe(m) for m in search_blocks(monster_id, max_xp=budget, kind=declared_type, limit=5))
        raise StatBlockError(
            "stat_block_type_mismatch",
            f"{block['name']} is a {creature_type(block)}, but the creature is a {declared_type}. "
            f"In-budget {declared_type} blocks: {same_type or 'none; pick another type that fits or narrate it'}",
        )
    if block["xp"] > budget:
        raise StatBlockError(
            "stat_block_over_budget",
            f"{block['name']} (CR {block['challenge_rating']}, {block['xp']} XP) exceeds this party's "
            f"{difficulty} encounter budget of {budget} XP; alternatives: "
            f"{', '.join(suggest_alternatives(monster_id, max_xp=budget))}",
        )
    return block


def stat_block_details(block: dict[str, Any]) -> dict[str, Any]:
    """NPC ``details`` keys for an assigned block (fresh, full-HP state)."""
    primary = block["attacks"][0] if block["attacks"] else None
    details: dict[str, Any] = {
        STAT_BLOCK_SECTION: {
            "source": "srd",
            "corpus_version": _bestiary()["corpus_version"],
            "monster_id": block["id"],
            "name": block["name"],
            "challenge_rating": block["challenge_rating"],
            "xp": block["xp"],
            "source_section_id": block["source_section_id"],
        },
        "hit_points": {"current": block["hit_points"], "maximum": block["hit_points"], "temporary": 0},
        "armor_class": block["armor_class"],
        "initiative_modifier": block["initiative_modifier"],
        "dexterity": block["abilities"]["dex"],
        "abilities": dict(block["abilities"]),
        "attacks": [dict(a) for a in block["attacks"]],
        # Attacks one Attack action makes (Multiattack); code enforces it (#236).
        "multiattack": int(block.get("multiattack") or 1),
        "resistances": list(block["resistances"]),
        "vulnerabilities": list(block["vulnerabilities"]),
        "immunities": list(block["immunities"]),
        "condition_immunities": list(block["condition_immunities"]),
    }
    if primary is not None:
        details.update(attack_name=primary["name"], attack_bonus=primary["attack_bonus"], damage=primary["damage"])
    return details


def bestiary_attribution() -> str:
    return _bestiary()["attribution"]
