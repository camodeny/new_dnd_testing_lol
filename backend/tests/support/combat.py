"""Drive combat through the AI DM's staged-effect path in tests.

The AI is the only DM: encounters start, end, and change terrain only via
DM ``staged_effects`` applied inside a turn. These helpers apply one typed
effect exactly as the turn commit does (``apply_staged_effects``) and commit,
so fixtures exercise the real production path instead of a human API.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from typing import Any
from unittest import mock

from sqlalchemy.orm import Session

from app.combat.service import get_active_encounter
from app.dm.contract import normalize_contract
from app.dm.effects import apply_staged_effects
from models.campaigns import Campaign
from models.combat import Encounter
from models.dm import DmTurn, DmTurnAttempt


def apply_dm_effect(
    db: Session, campaign_id: Any, turn_id: Any, attempt_id: Any,
    effect_type: str, arguments: dict, *, effect_id: str | None = None,
) -> None:
    """Validate one staged effect through the DM contract, apply it, commit."""
    contract = normalize_contract({
        "contract_version": "dm_turn_contract_v1",
        "mode": "respond",
        "reason": f"test {effect_type}",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [{"text": "The scene shifts.", "claim_kind": "observation",
                        "origin": "dm_adjudication"}],
        }],
        "staged_effects": [{
            "id": effect_id or f"{effect_type}-{uuid.uuid4().hex[:8]}",
            "effect_type": effect_type,
            "arguments": arguments,
        }],
    })
    staged = [e.model_dump(mode="json") for e in contract.staged_effects]
    apply_staged_effects(
        db, db.get(Campaign, campaign_id), staged,
        db.get(DmTurn, turn_id), db.get(DmTurnAttempt, attempt_id),
    )
    db.commit()


def dm_start_encounter(
    db: Session, campaign_id: Any, turn_id: Any, attempt_id: Any,
    participants: list[dict], *, scene: dict | None = None, map: dict | None = None,
    npc_d20: int | None = None, effect_id: str | None = None,
) -> Encounter:
    """Start an encounter via the DM ``start_encounter`` effect.

    ``npc_d20`` pins the d20 code rolls for every NPC/monster at start, so
    tests can control initiative order deterministically.
    """
    arguments: dict[str, Any] = {"participants": participants}
    if scene is not None:
        arguments["scene"] = scene
    if map is not None:
        arguments["map"] = map
    pinned = (
        mock.patch("app.combat.service.secrets.randbelow", return_value=npc_d20 - 1)
        if npc_d20 is not None else nullcontext()
    )
    with pinned:
        apply_dm_effect(db, campaign_id, turn_id, attempt_id, "start_encounter",
                        arguments, effect_id=effect_id)
    encounter = get_active_encounter(db, campaign_id)
    assert encounter is not None, "start_encounter effect did not create an encounter"
    return encounter


def dm_end_encounter(
    db: Session, campaign_id: Any, turn_id: Any, attempt_id: Any, encounter_id: Any,
    *, outcome: str = "victory", reason: str = "The fight is over.",
    participant_outcomes: dict | None = None, effect_id: str | None = None,
) -> Encounter:
    """End an encounter via the DM ``end_encounter`` effect."""
    arguments: dict[str, Any] = {
        "encounter_id": str(encounter_id), "outcome": outcome, "reason": reason,
    }
    if participant_outcomes is not None:
        arguments["participant_outcomes"] = participant_outcomes
    apply_dm_effect(db, campaign_id, turn_id, attempt_id, "end_encounter",
                    arguments, effect_id=effect_id)
    return db.get(Encounter, uuid.UUID(str(encounter_id)))


def dm_update_terrain(
    db: Session, campaign_id: Any, turn_id: Any, attempt_id: Any, encounter_id: Any,
    *, zones: list[dict] | None = None, clear_zone_ids: list[str] | None = None,
    effect_id: str | None = None,
) -> None:
    """Change terrain via the DM ``update_map_terrain`` effect."""
    apply_dm_effect(db, campaign_id, turn_id, attempt_id, "update_map_terrain", {
        "encounter_id": str(encounter_id),
        "zones": zones or [],
        "clear_zone_ids": clear_zone_ids or [],
    }, effect_id=effect_id)


def dm_place_tokens(
    db: Session, campaign_id: Any, turn_id: Any, attempt_id: Any, encounter_id: Any,
    placements: list[dict], *, effect_id: str | None = None,
) -> None:
    """Move tokens via the DM ``update_map_placement`` effect.

    ``placements`` items are ``{"participant_id", "col", "row"}``.
    """
    apply_dm_effect(db, campaign_id, turn_id, attempt_id, "update_map_placement", {
        "encounter_id": str(encounter_id), "placements": placements,
    }, effect_id=effect_id)
