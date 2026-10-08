"""Loot boxes — issue #463.

The AI DM awards loot as sealed boxes: it generates a pool of items of
varying rarity, and each recipient character gets a box that draws from it.
The character's player opens the box; code draws the contents (weighted by
rarity), adds a coin purse sized to the character's level, and writes both
to the sheet exactly once. Boxes come only from play — never sold.

Code owns every number: which rarities a character's level allows, how
many items a box gives and their odds (the campaign's ``loot_mode``), the
draw itself, coin amounts, and the sheet write. The DM owns the fiction:
the box's name and what is in the pool.

Post-combat loot (``loot_availability`` hook, #239) completes when the DM
awards a box for that encounter or declines; if it does neither within
:data:`LOOT_HOOK_TURN_LIMIT` table turns, code closes the hook as declined.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.observability.tracing import structured_log
from models.campaigns import Campaign, CampaignMember
from models.characters import Character, Dnd5eCharacterSheet
from models.loot import LootBox

logger = logging.getLogger(__name__)

LOOT_BOX_OPENED_EVENT = "loot_box.opened"

RARITIES = ("common", "uncommon", "rare", "very_rare", "legendary")
#: Lowest character level whose boxes may hold each rarity (2024 DMG tiers).
RARITY_MIN_LEVEL = {"common": 1, "uncommon": 1, "rare": 5, "very_rare": 11, "legendary": 17}
#: Base odds of drawing an item of each rarity.
RARITY_WEIGHT = {"common": 60, "uncommon": 25, "rare": 10, "very_rare": 4, "legendary": 1}
ITEM_KINDS = ("weapon", "armor", "potion", "scroll", "wand", "ring", "wondrous", "gear", "trinket", "gem", "art")
POOL_MIN, POOL_MAX = 6, 20

#: Per ``loot_mode``: items drawn per box, multiplier on the odds of
#: anything above common, and the coin purse multiplier.
LOOT_MODE_RULES: dict[str, dict[str, float]] = {
    "frequent_gamble": {"draws": 2, "rarity_boost": 1.0, "coins": 1.0},
    "rare_treasure": {"draws": 2, "rarity_boost": 2.5, "coins": 1.0},
    "generous": {"draws": 4, "rarity_boost": 1.0, "coins": 1.5},
    "scarce": {"draws": 1, "rarity_boost": 1.0, "coins": 0.5},
}
#: Coin purse by character level: (number of d6, gold per pip).
_PURSE = ((4, 2), (10, 4), (16, 30), (20, 100))

#: DM table turns after an encounter ends before code declines its loot.
LOOT_HOOK_TURN_LIMIT = 3


class LootError(ValueError):
    """A refused award or open; ``message`` is DM- or player-facing."""


def max_rarity(level: int) -> str:
    allowed = [r for r in RARITIES if int(level) >= RARITY_MIN_LEVEL[r]]
    return allowed[-1]


def _purse_dice(level: int) -> tuple[int, int]:
    return _PURSE[0] if level <= 4 else _PURSE[1] if level <= 10 else _PURSE[2] if level <= 16 else _PURSE[3]


def _sheet(db: Session, character_id: Any) -> Dnd5eCharacterSheet | None:
    from app.characters.service import latest_sheet

    return latest_sheet(db, character_id)


def _roster_character(db: Session, campaign: Campaign, character_id: Any) -> Character:
    try:
        cid = uuid.UUID(str(character_id))
    except ValueError as exc:
        raise LootError(f"character_id {character_id!r} must be a UUID") from exc
    seated = db.execute(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign.id, CampaignMember.selected_character_id == cid,
        )
    ).scalars().first()
    character = db.get(Character, cid)
    if seated is None or character is None:
        raise LootError(f"character {cid} is not seated at this table; loot boxes go to player characters")
    return character


def check_award(db: Session, campaign: Campaign, args: dict[str, Any]) -> list[str]:
    """Problems with an ``award_loot_box`` (empty when legal); DM-facing feedback."""
    problems: list[str] = []
    rarities = [str(item.get("rarity")) for item in args.get("items") or []]
    for character_id in args.get("character_ids") or []:
        try:
            character = _roster_character(db, campaign, character_id)
        except LootError as exc:
            problems.append(str(exc))
            continue
        sheet = _sheet(db, character.id)
        level = int(sheet.level or 1) if sheet is not None else 1
        cap = max_rarity(level)
        too_rare = sorted({r for r in rarities if RARITIES.index(r) > RARITIES.index(cap)}, key=RARITIES.index)
        if too_rare:
            problems.append(
                f"{character.name} is level {level}: their boxes hold items up to {cap.replace('_', ' ')}, "
                f"not {', '.join(r.replace('_', ' ') for r in too_rare)}"
            )
    encounter_id = args.get("encounter_id")
    if encounter_id and pending_hook(db, campaign.id, encounter_id) is None:
        problems.append(f"encounter {encounter_id} has no loot waiting to be awarded")
    return problems


def pending_hook(db: Session, campaign_id: Any, encounter_id: Any):
    from models.combat import EncounterEndFollowup

    try:
        eid = uuid.UUID(str(encounter_id))
    except ValueError:
        return None
    return db.execute(
        select(EncounterEndFollowup).where(
            EncounterEndFollowup.campaign_id == campaign_id,
            EncounterEndFollowup.encounter_id == eid,
            EncounterEndFollowup.hook_type == "loot_availability",
            EncounterEndFollowup.status == "pending",
        )
    ).scalars().first()


def _complete_hook(db: Session, campaign_id: Any, encounter_id: Any, result: dict[str, Any]) -> None:
    hook = pending_hook(db, campaign_id, encounter_id)
    if hook is not None:
        hook.status = "complete"
        hook.result = result
        hook.attempts = int(hook.attempts or 0) + 1


def award_loot_boxes_inline(
    db: Session, campaign: Campaign, turn: Any, args: dict[str, Any], operation_key: str,
) -> list[LootBox]:
    """One sealed box per recipient, inside the DM turn commit; replays are no-ops."""
    problems = check_award(db, campaign, args)
    if problems:
        raise LootError("; ".join(problems))
    rules = LOOT_MODE_RULES.get(str(campaign.loot_mode), LOOT_MODE_RULES["frequent_gamble"])
    pool = [
        {
            "item_id": f"i{index}",
            "name": str(item["name"]).strip(),
            "rarity": str(item["rarity"]),
            "kind": str(item.get("kind") or "gear"),
            "description": str(item.get("description") or "").strip(),
            "quantity": int(item.get("quantity") or 1),
        }
        for index, item in enumerate(args["items"])
    ]
    encounter_id = uuid.UUID(str(args["encounter_id"])) if args.get("encounter_id") else None
    boxes = []
    for raw_id in args["character_ids"]:
        character_id = uuid.UUID(str(raw_id))
        award_key = f"{operation_key}:{character_id}"[:200]
        existing = db.execute(
            select(LootBox).where(LootBox.campaign_id == campaign.id, LootBox.award_key == award_key)
        ).scalars().first()
        if existing is not None:
            boxes.append(existing)
            continue
        sheet = _sheet(db, character_id)
        box = LootBox(
            campaign_id=campaign.id,
            character_id=character_id,
            thread_id=str(turn.thread_id),
            audience=str(getattr(turn, "audience", None) or "campaign"),
            encounter_id=encounter_id,
            source_turn_id=turn.id,
            award_key=award_key,
            title=str(args["title"]).strip(),
            draws=min(int(rules["draws"]), len(pool)),
            loot_mode=str(campaign.loot_mode),
            character_level=int(sheet.level or 1) if sheet is not None else 1,
            pool=pool,
        )
        db.add(box)
        boxes.append(box)
    if encounter_id is not None:
        _complete_hook(db, campaign.id, encounter_id, {"awarded": True, "turn_id": str(turn.id)})
    db.flush()
    structured_log(
        logger, logging.INFO, "loot_box_awarded",
        campaign_id=str(campaign.id), boxes=len(boxes), pool_size=len(pool),
        rarities={r: sum(1 for i in pool if i["rarity"] == r) for r in RARITIES},
    )
    return boxes


def decline_loot_inline(db: Session, campaign: Campaign, turn: Any, args: dict[str, Any]) -> None:
    if pending_hook(db, campaign.id, args.get("encounter_id")) is None:
        raise LootError(f"encounter {args.get('encounter_id')} has no loot waiting to be awarded")
    _complete_hook(db, campaign.id, args["encounter_id"], {
        "awarded": False, "reason": str(args.get("reason") or "")[:400], "turn_id": str(turn.id),
    })


def settle_loot_hooks(db: Session, campaign_id: Any, thread_id: str) -> int:
    """Decline post-combat loot the DM left pending for too many table turns.

    Runs in every DM turn commit in the thread (the one that ended the
    encounter included); each pass counts on the hook's ``attempts``.
    """
    from models.combat import Encounter, EncounterEndFollowup

    hooks = db.execute(
        select(EncounterEndFollowup)
        .join(Encounter, Encounter.id == EncounterEndFollowup.encounter_id)
        .where(
            EncounterEndFollowup.campaign_id == campaign_id,
            EncounterEndFollowup.hook_type == "loot_availability",
            EncounterEndFollowup.status == "pending",
            Encounter.thread_id == str(thread_id),
        )
    ).scalars().all()
    closed = 0
    for hook in hooks:
        hook.attempts = int(hook.attempts or 0) + 1
        if hook.attempts > LOOT_HOOK_TURN_LIMIT:
            hook.status = "complete"
            hook.result = {"awarded": False, "reason": "no_award"}
            closed += 1
    return closed


# ── Opening ────────────────────────────────────────────────────────────────


def _draw(pool: list[dict[str, Any]], draws: int, boost: float, rng: Any) -> list[dict[str, Any]]:
    """Weighted draw without replacement; rarer items are less likely."""
    remaining = list(pool)
    drawn = []
    for _ in range(min(draws, len(remaining))):
        weights = [RARITY_WEIGHT[i["rarity"]] * (1 if i["rarity"] == "common" else boost) for i in remaining]
        pick = rng.random() * sum(weights)
        for index, weight in enumerate(weights):
            pick -= weight
            if pick < 0 or index == len(weights) - 1:
                drawn.append(remaining.pop(index))
                break
    return drawn


def _add_to_equipment(sheet: Dnd5eCharacterSheet, items: list[dict[str, Any]], box_id: str) -> None:
    equipment = [dict(e) for e in (sheet.equipment or []) if isinstance(e, dict)]
    for item in items:
        match = next(
            (e for e in equipment if e.get("name") == item["name"] and e.get("rarity") == item["rarity"]), None,
        )
        if match is not None:
            match["quantity"] = int(match.get("quantity") or 1) + int(item["quantity"])
            continue
        equipment.append({
            "name": item["name"], "quantity": int(item["quantity"]), "rarity": item["rarity"],
            "kind": item["kind"], "description": item["description"], "source": f"loot_box:{box_id}",
        })
    sheet.equipment = equipment


def open_loot_box(
    db: Session, campaign: Campaign, box_id: Any, *, actor_id: uuid.UUID, rng: Any | None = None,
) -> tuple[LootBox, Any]:
    """Open a sealed box for its character's player: draw, pay coins, write the sheet.

    Flush-only (the caller's idempotent command commits). Returns the box
    and the ``loot_box.opened`` domain event.
    """
    from app.campaigns.events import commit_campaign_mutation
    from app.campaigns.service import require_playable_campaign

    rng = rng or secrets.SystemRandom()
    box = db.execute(
        select(LootBox).where(LootBox.id == uuid.UUID(str(box_id)), LootBox.campaign_id == campaign.id)
        .with_for_update()
    ).scalars().first()
    if box is None:
        raise LootError("loot box not found")
    character = db.get(Character, box.character_id)
    if character is None or str(character.owner_id) != str(actor_id):
        raise PermissionError("only this character's player can open their loot box")
    if box.status == "opened":
        raise LootError("this loot box is already open")
    require_playable_campaign(campaign)
    sheet = _sheet(db, box.character_id)
    if sheet is None:
        raise LootError("this character has no sheet to put the loot on")
    rules = LOOT_MODE_RULES.get(box.loot_mode, LOOT_MODE_RULES["frequent_gamble"])
    items = _draw(list(box.pool), int(box.draws), float(rules["rarity_boost"]), rng)
    dice, per_pip = _purse_dice(int(box.character_level))
    gold = int(sum(rng.randint(1, 6) for _ in range(dice)) * per_pip * float(rules["coins"]))
    contents = {"items": items, "gp": gold}

    def _mutate(_locked):
        _add_to_equipment(sheet, items, str(box.id))
        sheet.gp = int(sheet.gp or 0) + gold
        box.status = "opened"
        box.contents = contents
        box.opened_at = utcnow()
        box.opened_by = actor_id

    _, event = commit_campaign_mutation(
        db, campaign.id, int(campaign.revision or 0),
        event_type=LOOT_BOX_OPENED_EVENT,
        payload={
            "loot_box_id": str(box.id),
            "thread_id": box.thread_id,
            "character_id": str(box.character_id),
            "character_name": character.name,
            "title": box.title,
            "items": [{"name": i["name"], "rarity": i["rarity"], "quantity": i["quantity"]} for i in items],
            "gp": gold,
        },
        operation_id=f"loot_box:{box.id}:opened",
        actor_id=actor_id,
        visibility="private" if box.audience == "private" else "public",
        provenance={"source": "loot_box", "thread_id": box.thread_id},
        mutate=_mutate,
        commit=False,
    )
    db.flush()
    structured_log(
        logger, logging.INFO, "loot_box_opened",
        loot_box_id=str(box.id), character_id=str(box.character_id), items=len(items), gp=gold,
        rarities=[i["rarity"] for i in items],
    )
    return box, event


# ── Projections ────────────────────────────────────────────────────────────


def box_view(box: LootBox) -> dict[str, Any]:
    """The owner's view: a sealed box shows its odds, an opened one its contents."""
    view: dict[str, Any] = {
        "id": str(box.id),
        "title": box.title,
        "status": box.status,
        "draws": int(box.draws),
        "pool_size": len(box.pool or []),
        "pool_rarities": {r: n for r in RARITIES if (n := sum(1 for i in box.pool or [] if i.get("rarity") == r))},
        "created_at": box.created_at.isoformat() if box.created_at else None,
    }
    if box.status == "opened" and box.contents:
        view["contents"] = box.contents
        view["opened_at"] = box.opened_at.isoformat() if box.opened_at else None
    return view


def character_loot_boxes(db: Session, character_id: Any) -> list[dict[str, Any]]:
    rows = db.execute(
        select(LootBox).where(LootBox.character_id == character_id).order_by(LootBox.created_at.desc()).limit(20)
    ).scalars().all()
    return [box_view(b) for b in rows]


def loot_context(db: Session, campaign: Campaign, thread_id: str) -> dict[str, Any]:
    """DM-only: loot mode, each seated PC's rarity ceiling, and post-combat loot waiting."""
    from models.combat import Encounter, EncounterEndFollowup

    party = []
    members = db.execute(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign.id, CampaignMember.selected_character_id.is_not(None),
        )
    ).scalars().all()
    for member in members:
        character = db.get(Character, member.selected_character_id)
        if character is None:
            continue
        sheet = _sheet(db, character.id)
        level = int(sheet.level or 1) if sheet is not None else 1
        party.append({"character_id": str(character.id), "name": character.name, "level": level,
                      "max_rarity": max_rarity(level)})
    pending = db.execute(
        select(EncounterEndFollowup.encounter_id)
        .join(Encounter, Encounter.id == EncounterEndFollowup.encounter_id)
        .where(
            EncounterEndFollowup.campaign_id == campaign.id,
            EncounterEndFollowup.hook_type == "loot_availability",
            EncounterEndFollowup.status == "pending",
            Encounter.thread_id == str(thread_id),
        )
    ).scalars().all()
    rules = LOOT_MODE_RULES.get(str(campaign.loot_mode), LOOT_MODE_RULES["frequent_gamble"])
    return {
        "loot_mode": str(campaign.loot_mode),
        "items_per_box": int(rules["draws"]),
        "party": party,
        "encounters_awaiting_loot": [str(e) for e in pending],
    }
