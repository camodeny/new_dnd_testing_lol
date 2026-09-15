"""Issue #266 — replacement characters after death/TPK without erasing history."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.auth.service import TEST_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember, CampaignPcLifecycle  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldFact, WorldRelation  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = TEST_USER_ID
    member_id = uuid.uuid4()
    with factory() as db:
        db.add_all([
            Profile(id=owner_id, email="owner@example.com"),
            Profile(id=member_id, email="member@example.com"),
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
    monkeypatch.setattr(
        "app.characters.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id
    finally:
        app.dependency_overrides.clear()


def _create_campaign(client, **overrides):
    payload = {"name": "Replacement Test", **overrides}
    resp = client.post("/api/campaigns", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["campaign"]


def _add_member(factory, campaign_id, user_id):
    with factory() as db:
        db.add(CampaignMember(
            campaign_id=uuid.UUID(campaign_id), user_id=user_id, role="player",
        ))
        db.commit()


def _make_character(factory, owner_id, name="Hero", race="Human", char_class="Fighter"):
    with factory() as db:
        char = Character(owner_id=owner_id, name=name, system="dnd5e")
        db.add(char)
        db.flush()
        db.add(Dnd5eCharacterSheet(
            character_id=char.id, owner_id=owner_id,
            character_name=name, race=race, char_class=char_class, level=1,
            equipment=[{"name": "Longsword", "quantity": 1}],
        ))
        db.commit()
        return str(char.id)


def _revision(client, campaign_id):
    resp = client.get(f"/api/campaigns/{campaign_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["campaign"]["revision"]


def _select(client, cid, char_id, key):
    return client.put(
        f"/api/campaigns/{cid}/members/me/character",
        json={"expected_revision": _revision(client, cid), "character_id": char_id},
        headers={"Idempotency-Key": key},
    )


def _go_active(factory, campaign_id):
    with factory() as db:
        camp = db.get(Campaign, uuid.UUID(campaign_id))
        camp.status = "active"
        db.commit()


def _declare(client, cid, char_id, key, **extra):
    return client.post(
        f"/api/campaigns/{cid}/pc-deaths",
        json={"expected_revision": _revision(client, cid), "character_id": char_id, **extra},
        headers={"Idempotency-Key": key},
    )


def _replace(client, cid, char_id, key):
    return client.post(
        f"/api/campaigns/{cid}/pc-replacements",
        json={"expected_revision": _revision(client, cid), "character_id": char_id},
        headers={"Idempotency-Key": key},
    )


def _introduce(client, cid, char_id, key):
    return client.post(
        f"/api/campaigns/{cid}/pc-replacements/{char_id}/introduce",
        json={"expected_revision": _revision(client, cid)},
        headers={"Idempotency-Key": key},
    )


def _launch_party(api, *, members=1):
    """Campaign with selected launch PCs, flipped to active (post-launch)."""
    client, factory, actor, owner_id, member_id = api
    camp = _create_campaign(client, required_players=1)
    cid = camp["id"]
    hero = _make_character(factory, owner_id, name="Hero")
    assert _select(client, cid, hero, "sel-owner").status_code == 200
    ids = {"owner_char": hero}
    if members > 1:
        _add_member(factory, cid, member_id)
        actor["id"] = member_id
        sidekick = _make_character(factory, member_id, name="Sidekick", race="Elf", char_class="Rogue")
        assert _select(client, cid, sidekick, "sel-member").status_code == 200
        actor["id"] = owner_id
        ids["member_char"] = sidekick
    _go_active(factory, cid)
    return cid, ids


def _sheet_snapshot(factory, char_id):
    with factory() as db:
        sheet = db.execute(
            select(Dnd5eCharacterSheet).where(
                Dnd5eCharacterSheet.character_id == uuid.UUID(char_id)
            )
        ).scalars().first()
        return dict(sheet.to_dict())


def _world_counts(factory, campaign_id):
    with factory() as db:
        cid = uuid.UUID(campaign_id)
        n_rel = len(db.execute(
            select(WorldRelation).where(WorldRelation.campaign_id == cid)
        ).scalars().all())
        n_fact = len(db.execute(
            select(WorldFact).where(WorldFact.campaign_id == cid)
        ).scalars().all())
        return n_rel, n_fact


def _seed_world(factory, campaign_id):
    from models.world import WorldEntity

    with factory() as db:
        camp = db.get(Campaign, uuid.UUID(campaign_id))
        npc = WorldEntity(
            campaign_id=camp.id, entity_type="npc", name="Old Thom",
            status="active", visibility="campaign",
        )
        db.add(npc)
        db.flush()
        db.add(WorldRelation(
            campaign_id=camp.id, subject_entity_id=npc.id,
            relation_type="owes_debt_to", object_label="Hero",
            epistemic_state="confirmed", status="active", version=1,
            visibility="campaign",
        ))
        db.add(WorldFact(
            campaign_id=camp.id, content="Old Thom saw the party enter the barrow.",
            entity_refs=[], epistemic_state="confirmed", status="active", version=1,
            visibility="campaign",
        ))
        db.commit()


def _event_types(factory, campaign_id):
    with factory() as db:
        rows = db.execute(
            select(CampaignDomainEvent)
            .where(CampaignDomainEvent.campaign_id == uuid.UUID(campaign_id))
            .order_by(CampaignDomainEvent.sequence.asc())
        ).scalars().all()
        return [r.event_type for r in rows]


def test_single_death_and_replacement(api):
    client, factory, actor, owner_id, _ = api
    cid, ids = _launch_party(api)
    hero = ids["owner_char"]
    before_sheet = _sheet_snapshot(factory, hero)

    death = _declare(client, cid, hero, "death-1", cause="dragon fire")
    assert death.status_code == 200, death.text
    assert death.json()["lifecycle"]["status"] == "dead"

    party = client.get(f"/api/campaigns/{cid}/party")
    assert party.status_code == 200, party.text
    body = party.json()["party"]
    assert [h["character_id"] for h in body["historical"]] == [hero]
    assert body["historical"][0]["cause"] == "dragon fire"
    assert body["active"] == []

    hero2 = _make_character(factory, owner_id, name="Hero II")
    repl = _replace(client, cid, hero2, "replace-1")
    assert repl.status_code == 200, repl.text
    payload = repl.json()["replacement"]
    assert payload["dead_character_id"] == hero
    assert payload["new_character_id"] == hero2
    # Solo death wipes the whole party, so it is a TPK by definition.
    assert payload["is_tpk"] is True

    party = client.get(f"/api/campaigns/{cid}/party").json()["party"]
    assert [a["character_id"] for a in party["active"]] == [hero2]
    assert party["active"][0]["replacement_of_character_id"] == hero
    assert len(party["pending_introductions"]) == 1
    hist = {h["character_id"]: h for h in party["historical"]}
    assert hist[hero]["replaced_by_character_id"] == hero2

    # Dead PC sheet/history preserved byte-for-byte; new PC got nothing copied.
    assert _sheet_snapshot(factory, hero) == before_sheet
    with factory() as db:
        assert db.get(Character, uuid.UUID(hero)) is not None
        new_sheet = db.execute(
            select(Dnd5eCharacterSheet).where(
                Dnd5eCharacterSheet.character_id == uuid.UUID(hero2)
            )
        ).scalars().first()
        assert new_sheet.backstory is None

    types = _event_types(factory, cid)
    assert "campaign.pc_dead" in types
    assert "campaign.pc_replaced" in types


def test_living_pc_replacement_denied(api):
    client, factory, actor, owner_id, _ = api
    cid, ids = _launch_party(api)
    spare = _make_character(factory, owner_id, name="Spare")
    resp = _replace(client, cid, spare, "replace-living")
    assert resp.status_code == 409, resp.text
    with factory() as db:
        member = db.get(CampaignMember, {"campaign_id": uuid.UUID(cid), "user_id": owner_id})
        assert str(member.selected_character_id) == ids["owner_char"]
        assert db.get(CampaignPcLifecycle, {
            "campaign_id": uuid.UUID(cid), "character_id": uuid.UUID(spare),
        }) is None


def test_dead_pc_canon_cannot_be_edited_or_deleted(api):
    client, factory, actor, owner_id, _ = api
    cid, ids = _launch_party(api)
    hero = ids["owner_char"]
    assert _declare(client, cid, hero, "death-canon").status_code == 200

    edit = client.put(f"/api/characters/{hero}", json={"name": "Rewritten"})
    assert edit.status_code == 409, edit.text
    delete = client.delete(f"/api/characters/{hero}")
    assert delete.status_code == 409, delete.text
    with factory() as db:
        char = db.get(Character, uuid.UUID(hero))
        assert char is not None and char.name == "Hero"


def test_no_automatic_knowledge_inheritance(api):
    client, factory, actor, owner_id, _ = api
    cid, ids = _launch_party(api)
    _seed_world(factory, cid)
    before_world = _world_counts(factory, cid)
    assert before_world != (0, 0)

    hero = ids["owner_char"]
    assert _declare(client, cid, hero, "death-know").status_code == 200
    hero2 = _make_character(factory, owner_id, name="Hero II")
    assert _replace(client, cid, hero2, "replace-know").status_code == 200

    # Prior party effects on the world are untouched; nothing was granted/copied.
    assert _world_counts(factory, cid) == before_world
    with factory() as db:
        for row in db.execute(select(WorldRelation)).scalars().all():
            assert (row.grants or {}) == {}
        for row in db.execute(select(WorldFact)).scalars().all():
            assert (row.grants or {}) == {}


def test_tpk_new_party_same_campaign(api):
    client, factory, actor, owner_id, member_id = api
    cid, ids = _launch_party(api, members=2)
    _seed_world(factory, cid)
    before_world = _world_counts(factory, cid)

    assert _declare(client, cid, ids["owner_char"], "death-tpk-1", is_tpk=True).status_code == 200
    assert _declare(client, cid, ids["member_char"], "death-tpk-2", is_tpk=True).status_code == 200

    party = client.get(f"/api/campaigns/{cid}/party").json()["party"]
    assert party["is_tpk"] is True
    assert party["active"] == []
    assert len(party["historical"]) == 2

    owner2 = _make_character(factory, owner_id, name="Owner II")
    assert _replace(client, cid, owner2, "replace-tpk-owner").status_code == 200
    actor["id"] = member_id
    member2 = _make_character(factory, member_id, name="Member II", race="Dwarf", char_class="Cleric")
    repl = _replace(client, cid, member2, "replace-tpk-member")
    assert repl.status_code == 200, repl.text
    assert repl.json()["replacement"]["is_tpk"] is True
    actor["id"] = owner_id

    party = client.get(f"/api/campaigns/{cid}/party").json()["party"]
    assert party["campaign_id"] == cid
    assert party["is_tpk"] is False
    assert {a["character_id"] for a in party["active"]} == {owner2, member2}
    assert len(party["historical"]) == 2
    assert _world_counts(factory, cid) == before_world
    with factory() as db:
        assert db.get(Campaign, uuid.UUID(cid)) is not None
        for old in (ids["owner_char"], ids["member_char"]):
            assert db.get(Character, uuid.UUID(old)) is not None


def test_duplicate_replacement_transition_rejected(api):
    client, factory, actor, owner_id, _ = api
    cid, ids = _launch_party(api)
    hero = ids["owner_char"]
    assert _declare(client, cid, hero, "death-dup").status_code == 200
    hero2 = _make_character(factory, owner_id, name="Hero II")
    rev_before_replace = _revision(client, cid)
    first = client.post(
        f"/api/campaigns/{cid}/pc-replacements",
        json={"expected_revision": rev_before_replace, "character_id": hero2},
        headers={"Idempotency-Key": "replace-dup"},
    )
    assert first.status_code == 200, first.text

    # A second, different replacement must not activate a second PC.
    hero3 = _make_character(factory, owner_id, name="Hero III")
    dup = _replace(client, cid, hero3, "replace-dup-other")
    assert dup.status_code == 409, dup.text

    # Idempotent replay of the original command (identical payload) converges
    # without side effects.
    replay = client.post(
        f"/api/campaigns/{cid}/pc-replacements",
        json={"expected_revision": rev_before_replace, "character_id": hero2},
        headers={"Idempotency-Key": "replace-dup"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("X-Idempotent-Replay") == "true"
    assert replay.json()["replacement"]["new_character_id"] == hero2

    party = client.get(f"/api/campaigns/{cid}/party").json()["party"]
    assert [a["character_id"] for a in party["active"]] == [hero2]


def test_failed_replacement_leaves_state_intact(api):
    client, factory, actor, owner_id, member_id = api
    cid, ids = _launch_party(api, members=2)
    hero = ids["owner_char"]
    assert _declare(client, cid, hero, "death-fail").status_code == 200
    before_sheet = _sheet_snapshot(factory, hero)
    before_world = _world_counts(factory, cid)
    before_rev = _revision(client, cid)

    # Someone else's character.
    actor["id"] = member_id
    foreign = _make_character(factory, member_id, name="Foreign")
    actor["id"] = owner_id
    resp = _replace(client, cid, foreign, "replace-foreign")
    assert resp.status_code == 403, resp.text

    # Incomplete replacement (missing race/class).
    with factory() as db:
        char = Character(owner_id=owner_id, name="Blank", system="dnd5e")
        db.add(char)
        db.flush()
        db.add(Dnd5eCharacterSheet(
            character_id=char.id, owner_id=owner_id, character_name="Blank",
        ))
        db.commit()
        blank_id = str(char.id)
    resp = _replace(client, cid, blank_id, "replace-blank")
    assert resp.status_code == 422, resp.text

    # Unknown character.
    client_nope = client.post(
        f"/api/campaigns/{cid}/pc-replacements",
        json={"expected_revision": _revision(client, cid), "character_id": str(uuid.uuid4())},
        headers={"Idempotency-Key": "replace-nope"},
    )
    assert client_nope.status_code == 404, client_nope.text

    # Prior dead lifecycle state intact and retryable; world + sheet untouched.
    with factory() as db:
        member = db.get(CampaignMember, {"campaign_id": uuid.UUID(cid), "user_id": owner_id})
        assert str(member.selected_character_id) == hero
        row = db.get(CampaignPcLifecycle, {
            "campaign_id": uuid.UUID(cid), "character_id": uuid.UUID(hero),
        })
        assert row.status == "dead" and row.replaced_by_character_id is None
    assert _sheet_snapshot(factory, hero) == before_sheet
    assert _world_counts(factory, cid) == before_world

    hero2 = _make_character(factory, owner_id, name="Hero II")
    assert _replace(client, cid, hero2, "replace-retry").status_code == 200
    assert _revision(client, cid) > before_rev


def test_introduction_flow(api):
    client, factory, actor, owner_id, _ = api
    cid, ids = _launch_party(api)
    hero = ids["owner_char"]
    assert _declare(client, cid, hero, "death-intro").status_code == 200
    hero2 = _make_character(factory, owner_id, name="Hero II")
    assert _replace(client, cid, hero2, "replace-intro").status_code == 200

    intro = _introduce(client, cid, hero2, "introduce-1")
    assert intro.status_code == 200, intro.text
    assert intro.json()["lifecycle"]["introduction_status"] == "introduced"

    party = client.get(f"/api/campaigns/{cid}/party").json()["party"]
    assert party["pending_introductions"] == []

    again = _introduce(client, cid, hero2, "introduce-2")
    assert again.status_code == 409, again.text

    # Launch PCs (no replacement link) need no introduction.
    launch_intro = _introduce(client, cid, hero, "introduce-launch")
    assert launch_intro.status_code == 409, launch_intro.text
    assert "campaign.pc_introduced" in _event_types(factory, cid)


def test_retirement_enables_replacement(api):
    client, factory, actor, owner_id, _ = api
    cid, ids = _launch_party(api)
    hero = ids["owner_char"]
    death = _declare(client, cid, hero, "retire-1", status="retired", cause="settled down")
    assert death.status_code == 200, death.text
    assert death.json()["lifecycle"]["status"] == "retired"

    party = client.get(f"/api/campaigns/{cid}/party").json()["party"]
    assert party["is_tpk"] is False
    assert [h["character_id"] for h in party["historical"]] == [hero]

    hero2 = _make_character(factory, owner_id, name="Hero II")
    assert _replace(client, cid, hero2, "replace-retired").status_code == 200


def test_replacement_rejected_before_launch(api):
    client, factory, actor, owner_id, _ = api
    camp = _create_campaign(client, required_players=1)
    cid = camp["id"]
    hero = _make_character(factory, owner_id, name="Hero")
    assert _select(client, cid, hero, "sel-pre").status_code == 200
    spare = _make_character(factory, owner_id, name="Spare")
    resp = _replace(client, cid, spare, "replace-pre")
    assert resp.status_code == 409, resp.text


def test_duplicate_death_declaration_rejected(api):
    client, factory, actor, owner_id, _ = api
    cid, ids = _launch_party(api)
    hero = ids["owner_char"]
    assert _declare(client, cid, hero, "death-once").status_code == 200
    again = _declare(client, cid, hero, "death-twice")
    assert again.status_code == 409, again.text

    # A non-party character cannot be declared dead.
    outsider = _make_character(factory, owner_id, name="Outsider")
    resp = _declare(client, cid, outsider, "death-outsider")
    assert resp.status_code == 409, resp.text
