"""Issue #264 — continue a campaign into a new adventure.

End to end over HTTP: the AI DM completes the current adventure, the
campaign owner continues the campaign (``POST .../adventures``), and the
persistent world carries over untouched — world entities, characters and
their sheets, clocks, and the current scene's fictional time. Continuing
never reseeds the world.

Opening the next adventure stays an owner host action over HTTP; the AI DM
has no ``start_adventure`` effect.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from models.campaigns import Adventure, Campaign, CampaignMember
from models.characters import Character, Dnd5eCharacterSheet
from models.profiles import Profile
from models.world import CampaignClock, CampaignCurrentScene, WorldEntity
from tests.test_adventure_completion_260 import _dm_complete, api  # noqa: F401 — fixture


def _seed_persistent_world(factory, cid: uuid.UUID, owner_id) -> None:
    with factory() as db:
        # The live table is open: an adventure is played on an active campaign.
        db.get(Campaign, cid).status = "active"
        char = Character(id=uuid.uuid4(), owner_id=owner_id, name="Brannoc")
        db.add(char)
        db.flush()
        db.add(Dnd5eCharacterSheet(
            character_id=char.id, owner_id=owner_id, character_name="Brannoc",
            char_class="Fighter", level=3, experience_points=900,
        ))
        member = db.get(CampaignMember, (cid, owner_id))
        member.selected_character_id = char.id
        tower = WorldEntity(
            id=uuid.uuid4(), campaign_id=cid, entity_type="location",
            name="Ashen Tower", summary="A ruined watchtower above the vale.",
        )
        db.add(tower)
        db.add(WorldEntity(
            id=uuid.uuid4(), campaign_id=cid, entity_type="npc",
            name="Mother Vell", summary="Keeper of the vale shrine.",
        ))
        db.flush()
        db.add(CampaignClock(
            id=uuid.uuid4(), campaign_id=cid, name="The lich stirs",
            status="ticking", progress=2, threshold=6,
            advancement_criteria={"mode": "deterministic", "event_types": ["combat.ended"]},
            provenance={"source": "test"},
        ))
        db.add(CampaignCurrentScene(
            campaign_id=cid, location_entity_id=tower.id, location_name="Ashen Tower",
            fictional_time="Dusk, 14th of Harvestmoon", fictional_time_details={"day": 14},
            present_actors=[], revision=3,
        ))
        db.commit()


def _world_snapshot(factory, cid: uuid.UUID) -> dict:
    with factory() as db:
        entities = db.execute(
            select(WorldEntity).where(WorldEntity.campaign_id == cid).order_by(WorldEntity.name)
        ).scalars().all()
        clocks = db.execute(
            select(CampaignClock).where(CampaignClock.campaign_id == cid).order_by(CampaignClock.name)
        ).scalars().all()
        members = db.execute(
            select(CampaignMember).where(CampaignMember.campaign_id == cid)
        ).scalars().all()
        char_ids = sorted(m.selected_character_id for m in members if m.selected_character_id)
        characters = [db.get(Character, c).to_dict() for c in char_ids]
        sheets = [
            s.to_dict() for s in db.execute(
                select(Dnd5eCharacterSheet)
                .where(Dnd5eCharacterSheet.character_id.in_(char_ids))
                .order_by(Dnd5eCharacterSheet.character_id)
            ).scalars().all()
        ]
        scene = db.get(CampaignCurrentScene, cid)
        return {
            "entities": [e.to_dict() for e in entities],
            "clocks": [c.to_dict() for c in clocks],
            "members": sorted((str(m.user_id), str(m.selected_character_id)) for m in members),
            "characters": characters,
            "sheets": sheets,
            "scene": scene.to_dict() if scene else None,
        }


def test_continue_after_completion_keeps_world_and_opens_new_adventure(api):  # noqa: F811
    client, factory, actor = api
    camp = client.post("/api/campaigns", json={"name": "Vale campaign"}).json()["campaign"]
    cid = uuid.UUID(camp["id"])
    first = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "The Sunken Chapel"},
        headers={"Idempotency-Key": "continue-264-first"},
    )
    assert first.status_code == 200, first.text
    _seed_persistent_world(factory, cid, actor["id"])

    _dm_complete(factory, cid, "victory", "continue-264-done",
                 public_summary="The chapel is cleansed.")
    listed = client.get(f"/api/campaigns/{cid}/adventures").json()
    assert listed["current_adventure_id"] is None
    assert listed["campaign_status"] == "active"
    before = _world_snapshot(factory, cid)
    assert len(before["entities"]) == 2
    assert before["scene"]["fictional_time"] == "Dusk, 14th of Harvestmoon"

    continued = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Ashes of the Vale"},
        headers={"Idempotency-Key": "continue-264-next"},
    )
    assert continued.status_code == 200, continued.text
    nxt = continued.json()["adventure"]
    assert nxt["status"] == "active"
    assert nxt["title"] == "Ashes of the Vale"

    # A retried Continue (lost acknowledgement) replays, never duplicates.
    replay = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Ashes of the Vale"},
        headers={"Idempotency-Key": "continue-264-next"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["adventure"]["id"] == nxt["id"]
    # A fresh Continue while the new adventure is open is refused.
    again = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Yet another arc"},
        headers={"Idempotency-Key": "continue-264-dup"},
    )
    assert again.status_code == 409

    after = _world_snapshot(factory, cid)
    assert after == before  # no reseed, no character/clock/time reset
    listed = client.get(f"/api/campaigns/{cid}/adventures").json()
    assert listed["current_adventure_id"] == nxt["id"]
    assert [a["status"] for a in listed["adventures"]] == ["completed", "active"]
    with factory() as db:
        assert db.get(Campaign, cid).status == "active"
        assert len(db.execute(
            select(Adventure).where(Adventure.campaign_id == cid)
        ).scalars().all()) == 2


def test_only_the_owner_can_continue(api):  # noqa: F811
    client, factory, actor = api
    camp = client.post("/api/campaigns", json={"name": "Owner-only"}).json()["campaign"]
    cid = uuid.UUID(camp["id"])
    client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Arc one"},
        headers={"Idempotency-Key": "continue-264-owner-first"},
    )
    _dm_complete(factory, cid, "retreat", "continue-264-owner-done")
    member_id = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=member_id, email="member@example.com"))
        db.add(CampaignMember(campaign_id=cid, user_id=member_id, role="player"))
        db.commit()
    actor["id"] = member_id
    refused = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Member arc"},
        headers={"Idempotency-Key": "continue-264-member"},
    )
    assert refused.status_code == 403
    assert client.get(f"/api/campaigns/{cid}/adventures").json()["current_adventure_id"] is None
