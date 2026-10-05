"""Player-lane live-table projection (``snapshot["table"]``).

What the campaign table shows a player: their character, the party, where
they are, what they know, the rolls they owe, and the fight they're in.
Everything is filtered server-side for the viewer **as a player at the
table**, which is stricter than the general member projections:

- The campaign owner is a player like any other (the AI is the only DM):
  no human receives ``dm_only`` world records, hidden encounter tokens and
  terrain, or DM-only NPC fields, so the table cannot spoil what the AI DM
  holds back.
- DM storytelling machinery is never projected at all: pressure clocks,
  NPC goals/dispositions, roll DCs, summaries, and epistemic bookkeeping.
- ``private`` world records appear only through an explicit active grant.
- PC conditions marked DM-private never leave the server; other players'
  conditions are limited to party-visible ones, and other players' health
  is a descriptor rather than exact numbers.
- Roll modifiers for the viewer's pending rolls are derived here from the
  authoritative sheet, so clients never compute rules arithmetic inputs.

Each section fails closed independently: a broken section becomes
``{"error": "projection_failed"}`` and never falls back to unfiltered data.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.campaigns import Campaign

logger = logging.getLogger(__name__)

PROJECTION_FAILED = {"error": "projection_failed"}

# Condition visibilities each audience may receive.
_SELF_HIDDEN = frozenset({"dm_private", "dm_only"})
_PARTY_VISIBLE = frozenset({"public", "party_known", "campaign"})

_FACT_LIMIT = 40
_PEOPLE_LIMIT = 30
_PERSON_FACT_LIMIT = 4


def _player_may_receive(
    db: Session, campaign: Campaign, kind: str, target_id: Any, viewer: uuid.UUID,
) -> bool:
    from app.visibility.access import may_user_receive

    return bool(may_user_receive(db, campaign, kind, target_id, viewer).get("allowed"))


# ── Characters ──────────────────────────────────────────────────────────────


def _conditions(sheet: Any, *, self_view: bool) -> list[str]:
    out: list[str] = []
    for item in getattr(sheet, "conditions", None) or []:
        if not isinstance(item, dict):
            continue
        visibility = str(item.get("visibility") or "public")
        if self_view and visibility in _SELF_HIDDEN:
            continue
        if not self_view and visibility not in _PARTY_VISIBLE:
            continue
        name = item.get("condition_name") or item.get("name") or item.get("condition")
        if name:
            out.append(str(name))
    return out


def _class_label(sheet: Any) -> str | None:
    classes = getattr(sheet, "classes", None)
    if isinstance(classes, list) and classes:
        names = [
            str(c.get("class_name") or c.get("name"))
            for c in classes
            if isinstance(c, dict) and (c.get("class_name") or c.get("name"))
        ]
        if names:
            return " / ".join(names)
    return getattr(sheet, "char_class", None)


def health_label(current: int, maximum: int) -> str:
    """Coarse health a party member can see at a glance (no exact numbers)."""
    if maximum <= 0:
        return "unknown"
    if current <= 0:
        return "down"
    ratio = current / maximum
    if ratio >= 1:
        return "unhurt"
    if ratio > 0.5:
        return "hurt"
    return "badly hurt"


def _skill_label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def roll_modifier(mechanics: Any, roll_kind: str, ability_or_skill: str) -> dict[str, Any] | None:
    """Authoritative d20 modifier for a requested roll, or None if unresolvable.

    Attack/other rolls depend on the chosen weapon or effect, so they are
    never resolved here; players enter those totals themselves.
    """
    from app.rules.mechanics import _norm_ability, _norm_skill_name

    kind = str(roll_kind or "").lower()
    raw = str(ability_or_skill or "")
    # Tolerate "Wisdom (Insight)" style requests: prefer the skill.
    parts = [p.strip(" )") for p in raw.replace("(", "|").split("|") if p.strip(" )")]
    if kind == "initiative":
        init = (mechanics.combat or {}).get("initiative") or {}
        if "modifier" in init:
            return {"modifier": int(init["modifier"]), "label": "Initiative"}
        return None
    if kind == "check":
        for part in reversed(parts):
            skill = _norm_skill_name(part)
            if skill and skill in mechanics.skills:
                return {"modifier": int(mechanics.skills[skill].effective), "label": _skill_label(skill)}
    if kind in {"check", "ability"}:
        for part in parts:
            ability = _norm_ability(part)
            if ability and ability in mechanics.abilities:
                return {"modifier": int(mechanics.abilities[ability].modifier), "label": ability.capitalize()}
    if kind == "save":
        for part in parts:
            ability = _norm_ability(part)
            if ability and ability in mechanics.saves:
                return {"modifier": int(mechanics.saves[ability].effective), "label": f"{ability.capitalize()} save"}
    return None


def _own_character(db: Session, campaign: Campaign, viewer: uuid.UUID, sheet: Any) -> dict[str, Any]:
    from app.rules.mechanics import get_character_mechanics_for_sheet
    from models.dm import PlayerRollRequest

    mechanics = get_character_mechanics_for_sheet(sheet)
    combat = mechanics.combat or {}
    skills = sorted(
        (
            {"name": _skill_label(key), "modifier": int(detail.effective), "proficient": bool(detail.proficient)}
            for key, detail in mechanics.skills.items()
        ),
        key=lambda s: (-s["modifier"], s["name"]),
    )
    pending = db.execute(
        select(PlayerRollRequest).where(
            PlayerRollRequest.campaign_id == campaign.id,
            PlayerRollRequest.requested_user_id == viewer,
            PlayerRollRequest.status == "pending",
        )
    ).scalars().all()
    roll_modifiers = {}
    for req in pending:
        resolved = roll_modifier(mechanics, req.roll_kind, req.ability_or_skill)
        if resolved is not None:
            roll_modifiers[str(req.id)] = resolved
    return {
        "hp": {
            "current": int(sheet.hit_points_current or 0),
            "max": int(sheet.hit_points_max or 0),
            "temp": int(sheet.hit_points_temp or 0),
        },
        "armor_class": int(combat.get("armor_class", {}).get("effective", sheet.armor_class)),
        "speed": int(combat.get("speed", {}).get("value", sheet.speed)),
        "resources": [
            {"name": r.name, "current": r.current, "max": r.maximum, "recharge": r.recharge}
            for r in mechanics.resources
        ],
        "attacks": [
            {"name": a.name, "to_hit": a.attack_bonus_effective if a.attack_bonus_effective is not None else a.attack_bonus,
             "damage": a.damage, "damage_type": a.damage_type}
            for a in mechanics.attacks
        ],
        "skills": skills,
        "roll_modifiers": roll_modifiers,
    }


def _party_section(db: Session, campaign: Campaign, viewer: uuid.UUID) -> dict[str, Any]:
    from app.campaigns.replacements import party_roster
    from app.characters.service import latest_sheet

    roster = party_roster(db, campaign)
    members: list[dict[str, Any]] = []
    me: dict[str, Any] | None = None
    for entry in roster.get("active", []):
        is_self = entry.get("user_id") == str(viewer)
        base = {
            "character_id": entry["character_id"],
            "user_id": entry["user_id"],
            "name": entry["name"],
            "race": entry.get("race"),
            "level": entry.get("level"),
            "class_label": entry.get("char_class"),
            "is_self": is_self,
            "health": None,
            "conditions": [],
        }
        try:
            sheet = latest_sheet(db, uuid.UUID(entry["character_id"]))
            if sheet is None:
                members.append(base)
                continue
            row = {
                **base,
                "class_label": _class_label(sheet),
                "health": health_label(int(sheet.hit_points_current or 0), int(sheet.hit_points_max or 0)),
                "conditions": _conditions(sheet, self_view=is_self),
            }
            members.append(row)
            if is_self:
                me = {**row, **_own_character(db, campaign, viewer, sheet)}
        except Exception as exc:
            logger.warning(
                "table character projection failed campaign_id=%s error=%s",
                campaign.id, exc,
            )
            members.append({**base, **PROJECTION_FAILED})
            if is_self:
                me = {**base, **PROJECTION_FAILED}
    return {"character": me, "party": members}


# ── Scene + journal ─────────────────────────────────────────────────────────


def _scene(db: Session, campaign: Campaign) -> dict[str, Any] | None:
    from app.visibility.policy import RESTRICTED_VISIBILITIES
    from app.world.service import get_current_scene

    scene = get_current_scene(db, campaign.id)
    if scene is None or scene.visibility in RESTRICTED_VISIBILITIES:
        return None
    return {"location_name": scene.location_name, "fictional_time": scene.fictional_time}


def _shops_here(db: Session, campaign: Campaign, viewer: uuid.UUID) -> list[dict[str, Any]]:
    """Shops the current scene references that this player may see.

    The hook for the contextual Shop tab. Buying and selling are not built
    yet (#464); this only says a shop is here, never stock or prices.
    """
    from app.visibility.policy import RESTRICTED_VISIBILITIES
    from app.world.service import get_current_scene
    from models.world import WorldEntity

    scene = get_current_scene(db, campaign.id)
    if scene is None or scene.visibility in RESTRICTED_VISIBILITIES:
        return []
    shops: list[dict[str, Any]] = []
    for actor in scene.present_actors or []:
        raw_id = actor.get("entity_id") if isinstance(actor, dict) else None
        if not raw_id:
            continue
        try:
            entity = db.get(WorldEntity, uuid.UUID(str(raw_id)))
        except ValueError:
            continue
        if (
            entity is None or entity.campaign_id != campaign.id or entity.entity_type != "shop"
            or not _player_may_receive(db, campaign, "entity", entity.id, viewer)
        ):
            continue
        shops.append({"entity_id": str(entity.id), "name": entity.name, "summary": entity.summary})
    return shops


def _visible_facts(
    db: Session, campaign: Campaign, viewer: uuid.UUID, *,
    entity_id: uuid.UUID | None = None, limit: int,
) -> list[dict[str, Any]]:
    from models.world import WorldFact, WorldFactEntityRef

    # dm_only rows are excluded before LIMIT so they cannot crowd out
    # visible rows; private rows still need a per-row grant check.
    q = select(WorldFact).where(
        WorldFact.campaign_id == campaign.id,
        WorldFact.status == "active",
        WorldFact.visibility != "dm_only",
    )
    if entity_id is not None:
        q = q.join(
            WorldFactEntityRef,
            (WorldFactEntityRef.fact_id == WorldFact.id)
            & (WorldFactEntityRef.entity_id == entity_id),
        )
    rows = db.execute(q.order_by(WorldFact.created_at.desc()).limit(limit * 2)).scalars().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _player_may_receive(db, campaign, "fact", row.id, viewer):
            continue
        out.append({
            "id": str(row.id),
            "content": row.content,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
        if len(out) >= limit:
            break
    return out


def _journal(db: Session, campaign: Campaign, viewer: uuid.UUID) -> dict[str, Any]:
    from app.visibility.policy import MEMBER_VISIBILITIES
    from app.world.npcs import get_npc_state
    from models.world import WorldEntity

    npcs = db.execute(
        select(WorldEntity).where(
            WorldEntity.campaign_id == campaign.id,
            WorldEntity.entity_type == "npc",
            WorldEntity.status == "active",
            WorldEntity.visibility != "dm_only",
        ).order_by(WorldEntity.created_at.desc()).limit(_PEOPLE_LIMIT * 2)
    ).scalars().all()
    people: list[dict[str, Any]] = []
    for npc in npcs:
        if not _player_may_receive(db, campaign, "entity", npc.id, viewer):
            continue
        # NPC state fields carry no default disclosure: only a field the DM
        # explicitly marked member-visible reaches players.
        state = get_npc_state(db, campaign.id, npc.id)
        role_visible = state is not None and (state.field_visibility or {}).get("role") in MEMBER_VISIBILITIES
        people.append({
            "entity_id": str(npc.id),
            "name": npc.name,
            "role": state.role if role_visible else None,
            "summary": npc.summary,
            "facts": _visible_facts(db, campaign, viewer, entity_id=npc.id, limit=_PERSON_FACT_LIMIT),
        })
        if len(people) >= _PEOPLE_LIMIT:
            break
    return {
        "people": people,
        "facts": _visible_facts(db, campaign, viewer, limit=_FACT_LIMIT),
    }


# ── Encounter ───────────────────────────────────────────────────────────────


def _encounter(db: Session, campaign: Campaign, viewer: uuid.UUID) -> dict[str, Any] | None:
    """Active encounter as a player sees it — never the owner's DM lane."""
    from app.combat.maps import MapError, reachable_for
    from app.combat.service import can_view_encounter, encounter_view, get_active_encounter

    encounter = get_active_encounter(db, campaign.id)
    if encounter is None or not can_view_encounter(db, encounter, viewer):
        return None
    view = encounter_view(db, encounter, viewer)
    participants = view.get("participants") or []
    view["my_pending_initiative"] = [
        p["id"] for p in participants
        if p.get("controller_user_id") == str(viewer) and p.get("initiative_status") == "pending"
    ]
    view["reachable"] = None
    active_id = view.get("active_participant_id")
    active = next((p for p in participants if p["id"] == active_id), None)
    if view["map"] and active and active.get("controller_user_id") == str(viewer):
        try:
            view["reachable"] = reachable_for(
                db, encounter.id, uuid.UUID(active_id), viewer_id=viewer,
            )
        except MapError:
            view["reachable"] = None
    return view


# ── Entry point ─────────────────────────────────────────────────────────────


def _section(name: str, campaign: Campaign, build, fallback):
    try:
        return build()
    except Exception as exc:
        logger.warning(
            "table %s projection failed campaign_id=%s error=%s", name, campaign.id, exc,
        )
        return fallback


def build_table_for_viewer(
    db: Session, campaign: Campaign, viewer_id: uuid.UUID,
) -> dict[str, Any]:
    """Build the player-lane table projection for one viewer. Never raises."""
    from app.visibility.access import is_campaign_participant

    viewer = viewer_id if isinstance(viewer_id, uuid.UUID) else uuid.UUID(str(viewer_id))
    if not is_campaign_participant(db, campaign, viewer):
        return {"character": None, "party": [], "scene": None,
                "journal": {"people": [], "facts": []}, "encounter": None, "shops": []}

    party = _section("party", campaign, lambda: _party_section(db, campaign, viewer),
                     {"character": PROJECTION_FAILED, "party": PROJECTION_FAILED})
    return {
        "character": party["character"],
        "party": party["party"],
        "scene": _section("scene", campaign, lambda: _scene(db, campaign), PROJECTION_FAILED),
        "journal": _section("journal", campaign, lambda: _journal(db, campaign, viewer),
                            PROJECTION_FAILED),
        "encounter": _section("encounter", campaign, lambda: _encounter(db, campaign, viewer),
                              PROJECTION_FAILED),
        "shops": _section("shops", campaign, lambda: _shops_here(db, campaign, viewer), []),
    }
