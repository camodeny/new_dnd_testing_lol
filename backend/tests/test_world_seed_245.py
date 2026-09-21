"""Issue #245 — production world-seed generation from settings + ready party."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.auth.service import TEST_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import CampaignCharacterLore, CampaignMember  # noqa: E402
from models.characters import Character  # noqa: E402
from models.characters import Dnd5eCharacterSheet  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import (  # noqa: E402
    CampaignClock,
    CampaignCurrentScene,
    WorldEntity,
    WorldFact,
    WorldKnowledge,
    WorldRelation,
)


def _ready_lobby(factory, campaign_id: str, user_ids: list) -> dict:
    from datetime import datetime, timezone

    cid = uuid.UUID(campaign_id)
    chars = {}
    with factory() as db:
        for uid in user_ids:
            char = Character(owner_id=uid, name=f"Hero {str(uid)[:8]}", system="dnd5e")
            db.add(char)
            db.flush()
            db.add(Dnd5eCharacterSheet(
                character_id=char.id, owner_id=uid, character_name=char.name,
                race="Human", char_class="Fighter", level=1,
            ))
            db.flush()
            member = db.get(CampaignMember, {"campaign_id": cid, "user_id": uid})
            assert member is not None
            member.selected_character_id = char.id
            member.is_ready = True
            member.ready_at = datetime.now(timezone.utc)
            chars[str(uid)] = char
        db.commit()
    return chars


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = TEST_USER_ID
    member_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    with factory() as db:
        db.add_all([
            Profile(id=owner_id, email="owner@example.com"),
            Profile(id=member_id, email="member@example.com"),
            Profile(id=outsider_id, email="outsider@example.com"),
        ])
        db.commit()

    actor = {"id": owner_id}

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setattr(
        "app.campaigns.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Seed Campaign", "required_players": 1, **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _seed(client: TestClient, campaign_id: str, key: str):
    return client.post(
        f"/api/campaigns/{campaign_id}/world-seed",
        json={"operation_id": key},
        headers={"Idempotency-Key": key},
    )


def test_world_seed_solo_happy_path(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client, theme="Ashen frontier")
    _ready_lobby(factory, campaign["id"], [owner_id])

    response = _seed(client, campaign["id"], "op-seed-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign"]["status"] == "starting"
    seed = body["seed"]
    assert seed["contract_version"] == 1
    assert seed["replayed"] is False
    assert seed["location"]["entity_id"]
    assert len(seed["npcs"]) == 1
    assert seed["faction"]["entity_id"]
    assert seed["clock"]["id"]
    assert seed["scene_present"] is True
    assert "solo-bootstrap-355" not in json.dumps(body)
    assert body["campaign"]["status"] == "starting"

    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        scene = db.get(CampaignCurrentScene, cid)
        assert scene is not None
        assert scene.location_name == seed["location"]["name"]
        assert (scene.environment or {}).get("seed") == "world-seed-245"
        assert "solo-bootstrap" not in json.dumps(scene.environment or {})
        entities = db.execute(
            select(WorldEntity).where(WorldEntity.campaign_id == cid)
        ).scalars().all()
        by_type = {}
        for e in entities:
            by_type.setdefault(e.entity_type, []).append(e)
        assert len(by_type["location"]) == 1
        assert len(by_type["npc"]) == 1
        assert len(by_type["faction"]) == 1
        assert len(by_type["character"]) == 1
        clocks = db.execute(
            select(CampaignClock).where(CampaignClock.campaign_id == cid)
        ).scalars().all()
        assert len(clocks) == 1
        clock = clocks[0]
        assert clock.status == "active"
        assert clock.threshold >= 1
        assert clock.advancement_criteria["kind"] == "deterministic"
        assert "dm.turn_committed" in clock.advancement_criteria["event_types"]


def test_world_seed_multiplayer_covers_party(api):
    client, factory, actor, owner_id, member_id, _ = api
    campaign = _create(client, required_players=2)
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(campaign["id"]), user_id=member_id))
        db.commit()
    _ready_lobby(factory, campaign["id"], [owner_id, member_id])

    response = _seed(client, campaign["id"], "op-seed-mp")
    assert response.status_code == 200, response.text
    seed = response.json()["seed"]
    assert len(seed["npcs"]) == 2

    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        chars = db.execute(
            select(WorldEntity).where(
                WorldEntity.campaign_id == cid, WorldEntity.entity_type == "character"
            )
        ).scalars().all()
        assert len(chars) == 2
        scene = db.get(CampaignCurrentScene, cid)
        actor_names = {(a.get("name") or "") for a in (scene.present_actors or [])}
        assert len(actor_names) == 2


def test_world_seed_private_lore_hook_no_leak(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    chars = _ready_lobby(factory, campaign["id"], [owner_id])
    char = chars[str(owner_id)]
    secret = "My secret pact with the Hollow Lanterns: midnight-oath-xyz"
    with factory() as db:
        db.add(CampaignCharacterLore(
            campaign_id=uuid.UUID(campaign["id"]), character_id=char.id,
            user_id=owner_id, content=secret, visibility="private", version=3,
        ))
        db.commit()

    response = _seed(client, campaign["id"], "op-seed-lore")
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert "midnight-oath-xyz" not in response.text
    seed = response.json()["seed"]
    # Lore presence must not surface in owner-visible output either.
    assert "hook_count" not in seed
    assert "lore_consumed" not in seed

    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        facts = db.execute(
            select(WorldFact).where(WorldFact.campaign_id == cid)
        ).scalars().all()
        # Raw lore content must not enter canon anywhere.
        for f in facts:
            assert secret not in (f.content or "")
        hook_facts = [f for f in facts if f.visibility == "dm_only"]
        assert hook_facts, "expected a DM-private lore hook fact"
        assert any("v3" in (f.content or "") for f in hook_facts)
        # Lore content informs the DM-private hook kind without copying text.
        assert any("oath" in (f.content or "") for f in hook_facts)
        knowledge = db.execute(
            select(WorldKnowledge).where(WorldKnowledge.campaign_id == cid)
        ).scalars().all()
        assert knowledge, "expected party/situation knowledge rows"


def test_world_seed_abandoned_lore_excluded(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    chars = _ready_lobby(factory, campaign["id"], [owner_id])
    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        abandoned = Character(owner_id=owner_id, name="Abandoned", system="dnd5e")
        db.add(abandoned)
        db.flush()
        db.add(CampaignCharacterLore(
            campaign_id=cid, character_id=abandoned.id, user_id=owner_id,
            content="abandoned secret never-seeded", visibility="private", version=1,
        ))
        db.commit()

    response = _seed(client, campaign["id"], "op-seed-abandoned")
    assert response.status_code == 200, response.text
    assert "hook_count" not in response.json()["seed"]
    assert "never-seeded" not in response.text
    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        facts = db.execute(
            select(WorldFact).where(WorldFact.campaign_id == cid)
        ).scalars().all()
        assert not [f for f in facts if "Unrevealed" in (f.content or "")]


def test_world_seed_member_lore_invisible_in_public_output(api):
    """Another player's lore presence must not change owner-visible output."""
    from models.campaigns import CampaignDomainEvent

    client, factory, actor, owner_id, member_id, _ = api

    def _seeded_campaign(key_suffix: str, with_member_lore: bool) -> dict:
        campaign = _create(client, required_players=2, name=f"Lore invis {key_suffix}")
        with factory() as db:
            db.add(CampaignMember(
                campaign_id=uuid.UUID(campaign["id"]), user_id=member_id,
            ))
            db.commit()
        chars = _ready_lobby(factory, campaign["id"], [owner_id, member_id])
        if with_member_lore:
            member_char = chars[str(member_id)]
            with factory() as db:
                db.add(CampaignCharacterLore(
                    campaign_id=uuid.UUID(campaign["id"]),
                    character_id=member_char.id, user_id=member_id,
                    content="I owe a blood debt to the Saltrow moneylender crimson-ledger-99",
                    visibility="private", version=1,
                ))
                db.commit()
        response = _seed(client, campaign["id"], f"op-seed-invis-{key_suffix}")
        assert response.status_code == 200, response.text
        return response.json()

    plain = _seeded_campaign("plain", with_member_lore=False)
    lored = _seeded_campaign("lored", with_member_lore=True)

    # Public projection shape is identical with or without member lore.
    assert set(lored["seed"].keys()) == set(plain["seed"].keys())
    assert "crimson-ledger-99" not in json.dumps(lored)
    assert "blood debt" not in json.dumps(lored)

    with factory() as db:
        event = db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.event_type == "world.seeded_245"
            ).order_by(CampaignDomainEvent.sequence.desc())
        ).scalars().first()
        assert event is not None
        assert event.visibility == "public"
        payload_text = json.dumps(event.payload or {})
        assert "crimson-ledger-99" not in payload_text
        assert "hook_count" not in payload_text
        assert "lore_consumed" not in payload_text
        # The DM-private hook still exists with a content-derived kind.
        facts = db.execute(select(WorldFact)).scalars().all()
        debt_hooks = [
            f for f in facts
            if f.visibility == "dm_only" and "debt hook" in (f.content or "")
        ]
        assert debt_hooks, "expected a DM-private debt hook fact"
        for f in facts:
            assert "crimson-ledger-99" not in (f.content or "")


def test_world_seed_denied_hook_suppressed_silently(api):
    """A boundary-violating secret hook is dropped with no visible signal.

    The seed still succeeds and the public projection is indistinguishable
    from a seed with no such hook — the owner cannot probe lore via hooks.
    """
    client, factory, actor, owner_id, member_id, _ = api
    campaign = _create(
        client, required_players=2, name="Hook suppression",
        content_boundaries={"exclude": ["brother"]},
    )
    with factory() as db:
        db.add(CampaignMember(
            campaign_id=uuid.UUID(campaign["id"]), user_id=member_id,
        ))
        db.commit()
    chars = _ready_lobby(factory, campaign["id"], [owner_id, member_id])
    member_char = chars[str(member_id)]
    with factory() as db:
        db.add(CampaignCharacterLore(
            campaign_id=uuid.UUID(campaign["id"]),
            character_id=member_char.id, user_id=member_id,
            content="My brother vanished beyond the fog and I seek him still",
            visibility="private", version=1,
        ))
        db.commit()

    response = _seed(client, campaign["id"], "op-seed-suppress")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign"]["status"] == "starting"
    # The deny phrase echoes in the owner-set campaign settings, but no
    # lore-derived surface may carry it — even though the hook label itself
    # ("secret_kin") no longer contains the denied lore term.
    assert "secret_kin" not in json.dumps(body["seed"])
    assert "brother" not in json.dumps(body["seed"])

    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        facts = db.execute(
            select(WorldFact).where(WorldFact.campaign_id == cid)
        ).scalars().all()
        assert not [f for f in facts if "secret_kin" in (f.content or "")]
        assert not [f for f in facts if "brother" in (f.content or "")]
        # The rest of the seed (situation, secret, clock) is intact.
        assert [f for f in facts if f.visibility == "campaign"]
        assert [f for f in facts if f.visibility == "dm_only"]


def test_world_seed_difficulty_shapes_pressure(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client, difficulty="hard")
    _ready_lobby(factory, campaign["id"], [owner_id])

    response = _seed(client, campaign["id"], "op-seed-hard")
    assert response.status_code == 200, response.text
    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        clock = db.execute(
            select(CampaignClock).where(CampaignClock.campaign_id == cid)
        ).scalars().one()
        assert clock.threshold == 4


def test_world_seed_boundary_rejection_stays_prestart(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(
        client, theme="Ashen frontier",
        content_boundaries={"exclude": ["ashen"]},
    )
    _ready_lobby(factory, campaign["id"], [owner_id])

    response = _seed(client, campaign["id"], "op-seed-blocked")
    assert response.status_code == 409, response.text
    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        from models.campaigns import Campaign

        assert db.get(Campaign, cid).status == "lobby"
        assert db.get(CampaignCurrentScene, cid) is None
        count = db.scalar(
            select(func.count()).select_from(WorldEntity).where(WorldEntity.campaign_id == cid)
        )
        assert count == 0


def test_world_seed_boundary_probe_cannot_reveal_private_lore(api):
    """Owner-controlled boundary phrases must not oracle another player's lore.

    A deny phrase matching private lore content must not change the
    owner-visible seed outcome: the seed succeeds and leaks nothing.
    """
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(
        client,
        content_boundaries={"exclude": ["xyzzy-moth-123"]},
    )
    chars = _ready_lobby(factory, campaign["id"], [owner_id])
    char = chars[str(owner_id)]
    secret = "My secret pact of the xyzzy-moth-123 sealed at midnight"
    with factory() as db:
        db.add(CampaignCharacterLore(
            campaign_id=uuid.UUID(campaign["id"]), character_id=char.id,
            user_id=owner_id, content=secret, visibility="private", version=1,
        ))
        db.commit()

    response = _seed(client, campaign["id"], "op-seed-probe")
    assert response.status_code == 200, response.text
    # The deny phrase itself echoes in the owner-set campaign settings, but
    # the lore-only remainder must never surface anywhere owner-visible.
    assert "sealed at midnight" not in response.text
    assert response.json()["campaign"]["status"] == "starting"


def test_world_seed_rejected_candidate_regenerates(api):
    """A boundary-rejected first candidate regenerates to a passing slot."""
    from app.campaigns.world_seed import build_seed_spec

    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    chars = _ready_lobby(factory, campaign["id"], [owner_id])
    char = chars[str(owner_id)]
    composition = {"members": [{
        "character_id": str(char.id), "character_name": char.name,
        "user_id": str(owner_id), "is_ready": True,
    }]}
    slot_locations = [
        build_seed_spec(
            campaign_id=campaign["id"], theme=None, brief=None,
            difficulty="medium", content_boundaries=None,
            composition=composition, lore_bundle=[], slot=slot,
        )["location"]["name"]
        for slot in range(6)
    ]
    denied = {slot_locations[0].lower()}
    expected_slot = next(
        slot for slot, name in enumerate(slot_locations)
        if name.lower() not in denied
    )
    expected_location = slot_locations[expected_slot]
    update = client.put(
        f"/api/campaigns/{campaign['id']}",
        json={"expected_revision": campaign["revision"],
              "content_boundaries": {"exclude": sorted(denied)}},
        headers={"Idempotency-Key": "op-seed-regen-settings"},
    )
    assert update.status_code == 200, update.text

    response = _seed(client, campaign["id"], "op-seed-regen")
    assert response.status_code == 200, response.text
    seed = response.json()["seed"]
    assert seed["location"]["name"] == expected_location
    assert seed["candidates_tried"] == expected_slot + 1
    assert seed["candidates_tried"] > 1


def test_world_seed_duplicate_retry_no_dupes(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    first = _seed(client, campaign["id"], "op-seed-dup")
    assert first.status_code == 200, first.text
    clock_id = first.json()["seed"]["clock"]["id"]

    replay = _seed(client, campaign["id"], "op-seed-dup")
    assert replay.status_code == 200, replay.text

    other_key = _seed(client, campaign["id"], "op-seed-dup-2")
    assert other_key.status_code == 200, other_key.text
    assert other_key.json()["seed"]["replayed"] is True
    assert other_key.json()["seed"]["clock_count"] == 1

    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        locations = db.execute(
            select(WorldEntity).where(
                WorldEntity.campaign_id == cid, WorldEntity.entity_type == "location"
            )
        ).scalars().all()
        assert len(locations) == 1
        clocks = db.execute(
            select(CampaignClock).where(CampaignClock.campaign_id == cid)
        ).scalars().all()
        assert len(clocks) == 1
        assert str(clocks[0].id) == clock_id
        assert other_key.json()["seed"]["clock"]["id"] == clock_id


def test_world_seed_unready_stays_lobby(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    # No readiness set.
    response = _seed(client, campaign["id"], "op-seed-unready")
    assert response.status_code == 409, response.text
    with factory() as db:
        from models.campaigns import Campaign

        assert db.get(Campaign, uuid.UUID(campaign["id"])).status == "lobby"


def test_world_seed_owner_only_and_not_found(api):
    client, factory, actor, owner_id, _, outsider_id = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    actor["id"] = outsider_id
    denied = _seed(client, campaign["id"], "op-seed-outsider")
    assert denied.status_code == 403, denied.text
    actor["id"] = owner_id

    missing = _seed(client, str(uuid.uuid4()), "op-seed-missing")
    assert missing.status_code == 404, missing.text


def test_world_seed_relations_and_secret_motive(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    response = _seed(client, campaign["id"], "op-seed-rel")
    assert response.status_code == 200, response.text
    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        relations = db.execute(
            select(WorldRelation).where(WorldRelation.campaign_id == cid)
        ).scalars().all()
        kinds = {r.relation_type for r in relations}
        assert "member_of" in kinds
        assert "present_at" in kinds
        assert all(r.epistemic_state == "confirmed" for r in relations)
        facts = db.execute(
            select(WorldFact).where(WorldFact.campaign_id == cid)
        ).scalars().all()
        by_vis = {}
        for f in facts:
            by_vis.setdefault(f.visibility, []).append(f)
        assert by_vis.get("campaign"), "expected a party-visible situation fact"
        assert by_vis.get("dm_only"), "expected a DM-private secret fact"
