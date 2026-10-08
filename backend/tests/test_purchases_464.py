"""Issue #464 — a PC buys an item; code checks the price and moves coin and item."""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.dm.mechanics import loot_issues
from app.loot.inventory import InventoryError, pay, wallet
from models.campaigns import Campaign
from models.characters import Dnd5eCharacterSheet
from tests.support.combat import apply_dm_effect
from tests.test_loot_boxes_463 import _contract, _turn, table  # noqa: F401 — shared fixture


def _sheet(**coins):
    return SimpleNamespace(**{c: 0 for c in ("cp", "sp", "ep", "gp", "pp")} | coins)


@pytest.mark.parametrize("coins,price,left", [
    ({"gp": 10}, 250, {"gp": 7, "sp": 5}),                    # break a gold piece, change in silver
    ({"cp": 30, "gp": 2}, 25, {"cp": 5, "gp": 2}),            # small coins first
    ({"pp": 1}, 1, {"gp": 9, "sp": 9, "cp": 9}),               # change from platinum
    ({"ep": 3, "sp": 2}, 120, {"ep": 1}),                      # silver, then electrum; nothing broken
    ({"gp": 5}, 500, {}),
])
def test_paying_makes_correct_change(coins, price, left):
    sheet = _sheet(**coins)
    pay(sheet, price)
    assert {c: n for c, n in wallet(sheet).items() if n} == left


def test_cannot_pay_more_than_the_purse():
    with pytest.raises(InventoryError, match="not enough coin"):
        pay(_sheet(gp=1), 101)


def _buy(name="Rope of Climbing", gp=20, sp=0, character=None, t=None):
    return {"id": f"buy_{uuid.uuid4().hex[:6]}", "effect_type": "purchase", "arguments": {
        "character_id": str(character), "item": {"name": name, "rarity": "uncommon", "kind": "wondrous"},
        "price": {"gp": gp, "sp": sp}}}


def test_purchase_pays_and_adds_the_item(table):  # noqa: F811
    t, s = table, table["s"]
    hero_sheet = s.execute(select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == t["hero"])).scalars().one()
    hero_sheet.gp = 30
    s.commit()
    turn, attempt = _turn(t)
    apply_dm_effect(s, t["camp_id"], turn.id, attempt.id, "purchase", _buy(character=t["hero"], gp=20, sp=5)["arguments"])
    s.expire_all()
    hero_sheet = s.execute(select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == t["hero"])).scalars().one()
    assert (hero_sheet.gp, hero_sheet.sp) == (9, 5)
    assert [(e["name"], e["source"]) for e in hero_sheet.equipment] == [("Rope of Climbing", "purchase")]


def test_unaffordable_purchases_are_refused_before_anything_moves(table):  # noqa: F811
    t, s = table, table["s"]
    campaign = s.get(Campaign, t["camp_id"])  # Hero has 5 gp
    (issue,) = loot_issues(s, campaign, _contract([_buy(character=t["hero"], gp=20)]))
    assert issue.message == "Hero cannot afford Rope of Climbing: it costs 20 gp and they have 5 gp"
    # Two buys that each fit but not together.
    issues = loot_issues(s, campaign, _contract([_buy("Torch", gp=3, character=t["hero"]), _buy("Lamp", gp=3, character=t["hero"])]))
    assert [i.message for i in issues] == ["Hero cannot afford Lamp: it costs 3 gp and they have 5 gp left after their other purchases"]
    turn, attempt = _turn(t)
    with pytest.raises(ValueError, match="not enough coin"):
        apply_dm_effect(s, t["camp_id"], turn.id, attempt.id, "purchase", _buy(character=t["hero"], gp=20)["arguments"])
