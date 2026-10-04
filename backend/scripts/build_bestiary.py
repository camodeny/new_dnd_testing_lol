"""Build the committed SRD 5.2.1 bestiary (issue #478).

Deterministically converts the pinned ``Cantilux/dnd-srd-json`` monster
resources (the same derivative the rules corpus trusts) into structured stat
blocks at ``app/rules/data/srd521_bestiary.json``. Runtime code only reads
that file; nothing here runs in production.

Usage:
  python -m scripts.build_bestiary                  # fetch the pinned commit
  python -m scripts.build_bestiary --source-dir DIR # read DIR/<id>.json

Parse failures are reported and fail the build: a stat block is never
guessed. Attacks whose damage is not a dice expression (flat ``Hit: 1``)
are dropped from ``attacks`` and listed under ``skipped_attacks``.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.request
from typing import Any

SOURCE_REPO = "Cantilux/dnd-srd-json"
SOURCE_COMMIT = "df536fe94c92cff49cc531f7283f6995b881fefa"
RAW_BASE = f"https://raw.githubusercontent.com/{SOURCE_REPO}/{SOURCE_COMMIT}/data"
OUTPUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "rules", "data", "srd521_bestiary.json")

DAMAGE_TYPES = {
    "acid", "bludgeoning", "cold", "fire", "force", "lightning", "necrotic",
    "piercing", "poison", "psychic", "radiant", "slashing", "thunder",
}
_FIELD_STOP = r"(?= (?:Skills|Gear|Resistances|Vulnerabilities|Immunities|Senses|Languages|CR) |$)"
_ATTACK_RE = re.compile(
    r"(?P<name>[A-Z][^.]{0,80}?)\. (?P<kind>Melee or Ranged|Melee|Ranged) Attack Roll: "
    r"(?P<bonus>[+\-−]\d+)[^.]*?(?:\.[^.]*?)*?Hit: (?P<hit>[^.]*?) (?P<type>[A-Z][a-z]+) damage"
)
_DICE_HIT_RE = re.compile(r"^\d+ \((?P<dice>\d+d\d+(?: [+\-−] \d+)?)\)$")
# Source-data gaps fixed from the official SRD 5.2.1 PDF (sha256 8974902d…,
# the artifact pinned in app/rules_corpus/official_manifest.json). Each entry
# cites the page it was checked against; nothing here is inferred.
ABILITY_OVERRIDES: dict[str, dict[str, int]] = {
    # Derivative table drops the STR score; PDF p.342: "Str 1 −5 −5".
    "will-o-wisp": {"str": 1},
}
_SECTION_SPLIT = re.compile(r"(?:^|\s)(?:Legendary Actions|Bonus Actions|Reactions|Actions|Traits)\s")
_RIDER_RE = re.compile(r"^,? plus \d+ \((?P<dice>\d+d\d+(?: [+\-−] \d+)?)\) (?P<type>[A-Z][a-z]+) damage")


class BestiaryParseError(ValueError):
    pass


def _int(raw: str) -> int:
    return int(raw.replace("−", "-").replace("+", "").replace(",", "").strip())


def _field(content: str, label: str) -> str | None:
    match = re.search(rf"(?:^| ){label} (.+?){_FIELD_STOP}", content)
    return match.group(1).strip() if match else None


def _damage_interactions(raw: str | None) -> tuple[list[str], list[str]]:
    """``"Poison; Exhaustion, Poisoned"`` -> (damage types, conditions)."""
    if not raw:
        return [], []
    damage: list[str] = []
    conditions: list[str] = []
    for part in raw.split(";"):
        for item in part.split(","):
            word = item.strip().lower()
            if not word:
                continue
            (damage if word in DAMAGE_TYPES else conditions).append(word)
    return damage, conditions


def _attacks(content: str) -> tuple[list[dict[str, Any]], list[str]]:
    attacks: list[dict[str, Any]] = []
    skipped: list[str] = []
    for match in _ATTACK_RE.finditer(content):
        # The lazy name can start mid-header ("… CR 1/8 (XP 25) Actions Scimitar"):
        # keep only what follows the last section heading.
        name = _SECTION_SPLIT.split(match.group("name").strip())[-1].strip()
        damage_type = match.group("type").lower()
        dice = _DICE_HIT_RE.match(match.group("hit").strip())
        if dice is None or damage_type not in DAMAGE_TYPES:
            skipped.append(name)
            continue
        riders = []
        tail = content[match.end():]
        rider = _RIDER_RE.match(tail)
        # Unconditional riders only ("plus 5 (2d4) Fire damage."); a rider
        # gated on advantage/form/etc. ("… if the attack roll had Advantage")
        # is not a flat property of the attack.
        if rider and rider.group("type").lower() in DAMAGE_TYPES and not tail[rider.end():].startswith(" if"):
            riders.append({
                "damage": rider.group("dice").replace("−", "-").replace(" ", ""),
                "damage_type": rider.group("type").lower(),
            })
        attacks.append({
            "name": name,
            "extra_damage": riders,
            "kind": {"Melee": "melee", "Ranged": "ranged"}.get(match.group("kind"), "melee_or_ranged"),
            "attack_bonus": _int(match.group("bonus")),
            "damage": dice.group("dice").replace("−", "-").replace(" ", ""),
            "damage_type": damage_type,
        })
    return attacks, skipped


def _abilities(tables: list[dict[str, Any]]) -> dict[str, int]:
    """Scores from the ability table, read as a token stream.

    A few source tables have shifted cells (``"DEX", "DEX", "10 +0"``), so
    each label takes the next unsigned integer token rather than a fixed
    column; modifiers/saves are signed and never mistaken for a score.
    """
    tokens: list[str] = []
    for table in tables or []:
        for row in table.get("rows") or []:
            for cell in row:
                tokens.extend(str(cell).split())
    scores: dict[str, int] = {}
    labels = ("STR", "DEX", "CON", "INT", "WIS", "CHA")
    for i, token in enumerate(tokens):
        label = token.upper()
        if label not in labels or label.lower() in scores:
            continue
        for nxt in tokens[i + 1:i + 3]:
            if nxt.isdigit():
                scores[label.lower()] = int(nxt)
                break
    return scores


def parse_monster(raw: dict[str, Any]) -> dict[str, Any]:
    """One Cantilux monster resource -> stat-block record. Raises on gaps."""
    mid = raw.get("id")
    content = str(raw.get("content") or "")
    # Most entries read "1/8 (XP 25; …)"; a few wyrmlings read "3 (700 XP; …)".
    challenge = re.match(
        r"^(?P<cr>\d+(?:/\d+)?) \((?:XP (?P<xp>[\d,]+)|(?P<xp_after>[\d,]+) XP)",
        str(raw.get("challenge") or ""),
    )
    initiative = re.search(r"Initiative ([+\-−]\d+)", content)
    if challenge is None or initiative is None:
        raise BestiaryParseError(f"{mid}: unparseable challenge/initiative")
    try:
        armor_class = int(raw["armor_class"])
        hit_points = int(raw["hit_points"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BestiaryParseError(f"{mid}: missing AC/HP") from exc
    abilities = {**_abilities(raw.get("tables") or []), **ABILITY_OVERRIDES.get(mid, {})}
    if len(abilities) != 6:
        raise BestiaryParseError(f"{mid}: expected 6 ability scores, got {sorted(abilities)}")
    resist, _ = _damage_interactions(_field(content, "Resistances"))
    vulnerable, _ = _damage_interactions(_field(content, "Vulnerabilities"))
    immune, condition_immune = _damage_interactions(_field(content, "Immunities"))
    attacks, skipped = _attacks(content)
    return {
        "id": mid,
        "name": raw["name"],
        "category": raw.get("category"),
        "type_line": raw.get("type_line"),
        "challenge_rating": challenge.group("cr"),
        "xp": _int(challenge.group("xp") or challenge.group("xp_after")),
        "armor_class": armor_class,
        "hit_points": hit_points,
        "hit_points_roll": str(raw.get("hit_points_roll") or "").replace(" ", "") or None,
        "speed": raw.get("speed"),
        "initiative_modifier": _int(initiative.group(1)),
        "abilities": abilities,
        "attacks": attacks,
        "skipped_attacks": skipped,
        "resistances": resist,
        "vulnerabilities": vulnerable,
        "immunities": immune,
        "condition_immunities": condition_immune,
        "source_section_id": (raw.get("source") or {}).get("sectionId"),
    }


def _fetch(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{RAW_BASE}/{path}", timeout=60) as resp:
        return json.loads(resp.read())


def load_sources(source_dir: str | None) -> list[dict[str, Any]]:
    if source_dir:
        names = sorted(n for n in os.listdir(source_dir) if n.endswith(".json"))
        return [json.load(open(os.path.join(source_dir, n))) for n in names]
    ids = [item["id"] for item in _fetch("collections/monsters.json")["items"]]
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        return list(pool.map(lambda i: _fetch(f"resources/monsters/{i}.json"), ids))


def build(source_dir: str | None = None) -> dict[str, Any]:
    monsters: list[dict[str, Any]] = []
    errors: list[str] = []
    for raw in load_sources(source_dir):
        try:
            monsters.append(parse_monster(raw))
        except BestiaryParseError as exc:
            errors.append(str(exc))
    if errors:
        raise BestiaryParseError("bestiary build failed:\n" + "\n".join(errors))
    monsters.sort(key=lambda m: m["id"])
    return {
        "corpus": "srd",
        "corpus_version": "5.2.1",
        "source": {"repo": SOURCE_REPO, "commit": SOURCE_COMMIT},
        "license": "CC-BY-4.0",
        "attribution": (
            "This work includes material from the System Reference Document 5.2.1 (\"SRD 5.2.1\") "
            "by Wizards of the Coast LLC, available at https://www.dndbeyond.com/srd. "
            "The SRD 5.2.1 is licensed under the Creative Commons Attribution 4.0 International License."
        ),
        "monsters": monsters,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir")
    args = parser.parse_args(argv)
    data = build(args.source_dir)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    print(f"wrote {len(data['monsters'])} stat blocks to {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
