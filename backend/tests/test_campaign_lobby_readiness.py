"""Issue #241 — authoritative lobby character selection/readiness + start lock."""
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

from app.auth.service import MOCK_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.profiles import Profile  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = MOCK_USER_ID
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
    monkeypatch.setattr(
        "app.characters.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Lobby Test", **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _make_character(factory, owner_id, name="Hero", race="Human", char_class="Fighter"):
    with factory() as db:
        char = Character(owner_id=owner_id, name=name, system="dnd5e")
        db.add(char)
        db.flush()
        sheet = Dnd5eCharacterSheet(
            character_id=char.id, owner_id=owner_id,
            character_name=name, race=race, char_class=char_class, level=1,
        )
        db.add(sheet)
        db.commit()
        return str(char.id)


def _select(client, cid, rev, char_id, key):
    return client.put(
        f"/api/campaigns/{cid}/members/me/character",
        json={"expected_revision": rev, "character_id": char_id},
        headers={"Idempotency-Key": key},
    )


def _ready(client, cid, rev, ready, key):
    return client.put(
        f"/api/campaigns/{cid}/members/me/readiness",
        json={"expected_revision": rev, "ready": ready},
        headers={"Idempotency-Key": key},
    )


def _transition(client, cid, rev, status, key):
    return client.post(
        f"/api/campaigns/{cid}/lifecycle",
        json={"expected_revision": rev, "status": status},
        headers={"Idempotency-Key": key},
    )


def test_select_own_character_and_lobby_projection(api):
    client, factory, actor, owner_id, _, _ = api
    camp = _create(client, required_players=1)
    char_id = _make_character(factory, owner_id)
    resp = _select(client, camp["id"], 0, char_id, "select-own")
    assert resp.status_code == 200, resp.text
    assert resp.json()["member"]["selected_character_id"] == char_id
    lobby = client.get(f"/api/campaigns/{camp['id']}/lobby")
    assert lobby.status_code == 200
    me = [m for m in lobby.json()["members"] if m["user_id"] == str(owner_id)][0]
    assert me["character_name"] == "Hero"
    assert me["character_progress"]["percent"] == 100
    assert me["is_ready"] is False
    assert lobby.json()["eligibility"]["eligible"] is False  # not ready yet
    # roster exposes only public info
    roster = client.get(f"/api/campaigns/{camp['id']}/characters")
    assert roster.status_code == 200
    entry = roster.json()["characters"][0]
    assert entry["name"] == "Hero"
    assert "backstory" not in entry and "notes" not in entry


def test_select_other_users_character_rejected_without_mutation(api):
    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client, required_players=1)
    other_char = _make_character(factory, member_id, name=" чужой ".strip() or "Other")
    resp = _select(client, camp["id"], 0, other_char, "select-other")
    assert resp.status_code == 403
    with factory() as db:
        m = db.get(CampaignMember, {"campaign_id": uuid.UUID(camp["id"]), "user_id": owner_id})
        assert m.selected_character_id is None
        assert db.get(Campaign, uuid.UUID(camp["id"])).revision == 0


def test_select_missing_character_404_without_mutation(api):
    client, factory, _, owner_id, _, _ = api
    camp = _create(client, required_players=1)
    resp = _select(client, camp["id"], 0, str(uuid.uuid4()), "select-missing")
    assert resp.status_code == 404
    with factory() as db:
        m = db.get(CampaignMember, {"campaign_id": uuid.UUID(camp["id"]), "user_id": owner_id})
        assert m.selected_character_id is None


def test_ready_unready_reversible_and_edit_after_ready(api):
    client, factory, actor, owner_id, _, _ = api
    camp = _create(client, required_players=1)
    char_id = _make_character(factory, owner_id)
    assert _select(client, camp["id"], 0, char_id, "sel").status_code == 200
    assert _ready(client, camp["id"], 1, True, "ready").status_code == 200
    # readiness alone does not freeze character edits (still lobby)
    edit = client.put(f"/api/characters/{char_id}", json={"race": "Elf"})
    assert edit.status_code == 200, edit.text
    # unready is reversible
    assert _ready(client, camp["id"], 2, False, "unready").status_code == 200
    lobby = client.get(f"/api/campaigns/{camp['id']}/lobby").json()
    me = [m for m in lobby["members"] if m["user_id"] == str(owner_id)][0]
    assert me["is_ready"] is False
    # re-ready after edit
    assert _ready(client, camp["id"], 3, True, "re-ready").status_code == 200


def test_incomplete_character_cannot_ready(api):
    client, factory, _, owner_id, _, _ = api
    camp = _create(client, required_players=1)
    char_id = _make_character(factory, owner_id, race="", char_class="")
    assert _select(client, camp["id"], 0, char_id, "sel-incomplete").status_code == 200
    resp = _ready(client, camp["id"], 1, True, "ready-incomplete")
    assert resp.status_code == 422
    assert "missing" in resp.text


def test_non_member_cannot_select(api):
    client, factory, actor, _, _, outsider_id = api
    camp = _create(client, required_players=1)
    outsider_char = _make_character(factory, outsider_id)
    actor["id"] = outsider_id
    resp = _select(client, camp["id"], 0, outsider_char, "outsider-sel")
    assert resp.status_code == 403


def test_valid_solo_start_eligibility_and_lock(api):
    client, factory, actor, owner_id, _, _ = api
    camp = _create(client, required_players=1)
    char_id = _make_character(factory, owner_id)
    assert _select(client, camp["id"], 0, char_id, "sel").status_code == 200
    assert _ready(client, camp["id"], 1, True, "ready").status_code == 200
    lobby = client.get(f"/api/campaigns/{camp['id']}/lobby").json()
    assert lobby["eligibility"]["eligible"] is True
    started = _transition(client, camp["id"], 2, "starting", "start")
    assert started.status_code == 200, started.text
    # successful start locks assignment/edit flow
    char2 = _make_character(factory, owner_id, name="Second")
    with factory() as db:
        rev = db.get(Campaign, uuid.UUID(camp["id"])).revision
    assert _select(client, camp["id"], rev, char2, "sel-after-start").status_code == 409
    assert _ready(client, camp["id"], rev, False, "unready-after-start").status_code == 409
    edit = client.put(f"/api/characters/{char_id}", json={"race": "Dwarf"})
    assert edit.status_code == 409


def test_multiplayer_eligibility_requires_all_ready(api):
    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client, required_players=2)
    owner_char = _make_character(factory, owner_id)
    assert _select(client, camp["id"], 0, owner_char, "owner-sel").status_code == 200
    assert _ready(client, camp["id"], 1, True, "owner-ready").status_code == 200
    # only 1 member joined but 2 required → blocked
    blocked = _transition(client, camp["id"], 2, "starting", "start-blocked")
    assert blocked.status_code == 409
    # join second member without character → still blocked
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(camp["id"]), user_id=member_id, role="player"))
        db.commit()
    lobby = client.get(f"/api/campaigns/{camp['id']}/lobby").json()
    assert lobby["eligibility"]["eligible"] is False
    blocked2 = _transition(client, camp["id"], 2, "starting", "start-blocked-2")
    assert blocked2.status_code == 409


def test_failed_start_does_not_lock(api):
    client, factory, actor, owner_id, _, _ = api
    camp = _create(client, required_players=1)
    char_id = _make_character(factory, owner_id)
    assert _select(client, camp["id"], 0, char_id, "sel").status_code == 200
    # not ready → start fails, no lock
    failed = _transition(client, camp["id"], 1, "starting", "fail")
    assert failed.status_code == 409
    char2 = _make_character(factory, owner_id, name="Second")
    assert _select(client, camp["id"], 1, char2, "sel-after-fail").status_code == 200
