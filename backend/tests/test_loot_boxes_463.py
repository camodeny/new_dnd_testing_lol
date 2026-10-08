"""Issue #463 — the AI DM awards loot boxes; players open them; code owns the draw."""
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.mechanics import loot_issues  # noqa: E402
from app.loot.service import (  # noqa: E402
    LOOT_BOX_OPENED_EVENT,
    LootError,
    box_view,
    loot_context,
    open_loot_box,
    settle_loot_hooks,
)
from database import Base  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import EncounterEndFollowup  # noqa: E402
from models.loot import LootBox  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402
from models.world import WorldEntity  # noqa: E402
from tests.support.combat import apply_dm_effect, dm_end_encounter, dm_start_encounter  # noqa: E402


class FixedRandom:
    """Draws always take the first remaining item; every die shows ``face``."""

    def __init__(self, face=3):
        self.face = face

    def random(self):
        return 0.0

    def randint(self, low, high):
        return max(low, min(high, self.face))


POOL = [
    {"name": "Potion of Healing", "rarity": "common", "kind": "potion", "description": "Restores 2d4+2 hit points."},
    {"name": "Silver Locket", "rarity": "common", "kind": "trinket"},
    {"name": "Rope of Climbing", "rarity": "uncommon", "kind": "wondrous"},
    {"name": "Bag of Marbles", "rarity": "common", "kind": "gear", "quantity": 2},
    {"name": "Garnet", "rarity": "common", "kind": "gem"},
    {"name": "Bracers of Archery", "rarity": "uncommon", "kind": "wondrous"},
]


@pytest.fixture
def table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'loot.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner, player, camp_id, thread_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hero, scout = uuid.uuid4(), uuid.uuid4()
    with factory() as s:
        s.add_all([
            Profile(id=owner, email="owner@example.com"),
            Profile(id=player, email="player@example.com"),
            Campaign(id=camp_id, owner_id=owner, name="Table", revision=0, loot_mode="frequent_gamble"),
            CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner),
            CampaignMember(campaign_id=camp_id, user_id=owner, role="owner", selected_character_id=hero),
            CampaignMember(campaign_id=camp_id, user_id=player, role="player", selected_character_id=scout),
        ])
        s.flush()
        s.add_all([
            Character(id=hero, owner_id=owner, name="Hero", system="dnd5e"),
            Character(id=scout, owner_id=player, name="Scout", system="dnd5e"),
            Dnd5eCharacterSheet(character_id=hero, owner_id=owner, character_name="Hero", level=3, gp=5),
            Dnd5eCharacterSheet(character_id=scout, owner_id=player, character_name="Scout", level=6),
        ])
        s.commit()
        yield {"s": s, "owner": owner, "player": player, "camp_id": camp_id, "thread_id": thread_id,
               "hero": hero, "scout": scout}


def _turn(t, text="We search the camp."):
    from app.dm.turns import coordinate_turn
    from app.submissions.service import accept_submission

    s = t["s"]
    accept_submission(s, campaign_id=t["camp_id"], user_id=t["owner"], character_id=t["hero"],
                      raw_content=text, segments=[{"type": "ic", "text": text}], thread_id=str(t["thread_id"]))
    s.commit()
    turn, attempt = coordinate_turn(s, t["camp_id"], str(t["thread_id"]), commit=False)
    s.commit()
    return turn, attempt


def _award(t, character_ids, *, items=POOL, encounter_id=None, effect_id="loot_1", turn=None):
    turn, attempt = turn or _turn(t)
    args = {"character_ids": [str(c) for c in character_ids], "title": "The bandit chief's strongbox", "items": items}
    if encounter_id:
        args["encounter_id"] = str(encounter_id)
    apply_dm_effect(t["s"], t["camp_id"], turn.id, attempt.id, "award_loot_box", args, effect_id=effect_id)
    return t["s"].execute(select(LootBox).order_by(LootBox.created_at)).scalars().all()


def _contract(effects):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "loot",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": "Coins glint in the dust.", "claim_kind": "observation", "origin": "dm_adjudication"}]}],
        "staged_effects": effects,
    })


def _ended_encounter(t):
    s = t["s"]
    turn, attempt = _turn(t, "Steel rings out.")
    goblin = WorldEntity(campaign_id=t["camp_id"], entity_type="npc", name="Goblin", visibility="campaign",
                         details={"initiative_modifier": 2})
    s.add(goblin)
    s.commit()
    encounter = dm_start_encounter(s, t["camp_id"], turn.id, attempt.id, [{"npc_entity_id": str(goblin.id)}], npc_d20=10)
    dm_end_encounter(s, t["camp_id"], turn.id, attempt.id, encounter.id)
    return encounter


def _hook(t, encounter):
    t["s"].expire_all()
    return t["s"].execute(select(EncounterEndFollowup).where(
        EncounterEndFollowup.encounter_id == encounter.id,
        EncounterEndFollowup.hook_type == "loot_availability")).scalars().one()


def test_award_seals_one_box_per_character_and_hides_the_pool(table):
    t = table
    hero_box, scout_box = _award(t, [t["hero"], t["scout"]])
    assert (hero_box.character_id, scout_box.character_id) == (t["hero"], t["scout"])
    assert hero_box.status == "sealed" and hero_box.draws == 2 and len(hero_box.pool) == 6
    view = box_view(hero_box)
    assert view["pool_rarities"] == {"common": 4, "uncommon": 2}
    assert "contents" not in view and "Potion of Healing" not in str(view)


def test_replayed_award_never_duplicates_boxes(table):
    t = table
    turn = _turn(t)
    _award(t, [t["hero"]], turn=turn)
    assert len(_award(t, [t["hero"]], turn=turn)) == 1


def test_open_draws_items_and_coins_onto_the_sheet_once(table):
    t, s = table, table["s"]
    (box,) = _award(t, [t["hero"]])
    campaign = s.get(Campaign, t["camp_id"])
    box, event = open_loot_box(s, campaign, box.id, actor_id=t["owner"], rng=FixedRandom(face=3))
    s.commit()
    assert [i["name"] for i in box.contents["items"]] == ["Potion of Healing", "Silver Locket"]
    assert box.contents["gp"] == 4 * 3 * 2  # level 3: 4d6 x 2 gp, every die a 3
    sheet = s.execute(select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == t["hero"])).scalars().one()
    assert sheet.gp == 5 + 24
    assert [(e["name"], e["rarity"]) for e in sheet.equipment] == [("Potion of Healing", "common"), ("Silver Locket", "common")]
    assert event.event_type == LOOT_BOX_OPENED_EVENT and event.payload["gp"] == 24
    with pytest.raises(LootError, match="already open"):
        open_loot_box(s, campaign, box.id, actor_id=t["owner"])
    assert s.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.event_type == LOOT_BOX_OPENED_EVENT)).scalars().all() == [event]


def test_only_the_characters_player_opens_their_box(table):
    t, s = table, table["s"]
    (box,) = _award(t, [t["hero"]])
    with pytest.raises(PermissionError):
        open_loot_box(s, s.get(Campaign, t["camp_id"]), box.id, actor_id=t["player"])


def test_rarer_items_need_higher_levels(table):
    t, s = table, table["s"]
    rare_pool = POOL[:-1] + [{"name": "Flame Tongue", "rarity": "rare", "kind": "weapon"}]
    award = {"id": "loot_1", "effect_type": "award_loot_box", "arguments": {
        "character_ids": [str(t["hero"]), str(t["scout"])], "title": "Hoard", "items": rare_pool}}
    issues = loot_issues(s, s.get(Campaign, t["camp_id"]), _contract([award]))
    # Hero (level 3) tops out at uncommon; Scout (level 6) may find a rare item.
    assert [i.message for i in issues] == ["Hero is level 3: their boxes hold items up to uncommon, not rare"]


def test_loot_mode_sets_items_per_box(table):
    t, s = table, table["s"]
    s.get(Campaign, t["camp_id"]).loot_mode = "generous"
    s.commit()
    (box,) = _award(t, [t["hero"]])
    assert box.draws == 4


def test_encounter_loot_completes_by_award_or_decline(table):
    t, s = table, table["s"]
    encounter = _ended_encounter(t)
    assert _hook(t, encounter).status == "pending"
    assert loot_context(s, s.get(Campaign, t["camp_id"]), str(t["thread_id"]))["encounters_awaiting_loot"] == [str(encounter.id)]
    _award(t, [t["hero"]], encounter_id=encounter.id)
    assert _hook(t, encounter).status == "complete" and _hook(t, encounter).result["awarded"] is True

    second = _ended_encounter(t)
    turn, attempt = _turn(t)
    apply_dm_effect(s, t["camp_id"], turn.id, attempt.id, "decline_loot",
                    {"encounter_id": str(second.id), "reason": "The goblins carried nothing."})
    assert _hook(t, second).result == {"awarded": False, "reason": "The goblins carried nothing.", "turn_id": str(turn.id)}


def test_unawarded_loot_closes_after_three_table_turns(table):
    t, s = table, table["s"]
    encounter = _ended_encounter(t)
    # The commit that ended the encounter, then three more table turns.
    for expected in ("pending", "pending", "pending", "complete"):
        settle_loot_hooks(s, t["camp_id"], str(t["thread_id"]))
        s.commit()
        assert _hook(t, encounter).status == expected
    assert _hook(t, encounter).result == {"awarded": False, "reason": "no_award"}
