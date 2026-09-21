"""Issue #244 — public party composition + private character lore (DM-only setup)."""
from __future__ import annotations

import json
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
from models.campaigns import Campaign, CampaignCharacterLore, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.profiles import Profile  # noqa: E402


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
    monkeypatch.setattr(
        "app.characters.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    monkeypatch.setattr(
        "app.characters.chat.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Party Lore Test", **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _make_character(factory, owner_id, name="Hero", race="Human", char_class="Fighter"):
    with factory() as db:
        char = Character(owner_id=owner_id, name=name, system="dnd5e")
        db.add(char)
        db.flush()
        db.add(Dnd5eCharacterSheet(
            character_id=char.id, owner_id=owner_id,
            character_name=name, race=race, char_class=char_class, level=3,
        ))
        db.commit()
        return str(char.id)


def _add_member(factory, camp_id, user_id):
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(camp_id), user_id=user_id, role="player"))
        db.commit()


def _select(client, cid, rev, char_id, key):
    return client.put(
        f"/api/campaigns/{cid}/members/me/character",
        json={"expected_revision": rev, "character_id": char_id},
        headers={"Idempotency-Key": key},
    )


def _put_lore(client, cid, char_id, rev, content, key, op=None):
    body = {"expected_revision": rev, "content": content}
    if op:
        body["operation_id"] = op
    return client.put(
        f"/api/campaigns/{cid}/characters/{char_id}/lore",
        json=body,
        headers={"Idempotency-Key": key},
    )


def test_lobby_party_composition_excludes_private_lore(api):
    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client, required_players=2)
    owner_char = _make_character(factory, owner_id, name="Tank", char_class="Fighter")
    _select(client, camp["id"], 0, owner_char, "sel-owner")
    _add_member(factory, camp["id"], member_id)
    actor["id"] = member_id
    member_char = _make_character(factory, member_id, name="Zap", race="Elf", char_class="Wizard")
    camp_rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    _select(client, camp["id"], camp_rev, member_char, "sel-member")
    secret = "my secret pact with the shadow dragon"
    camp_rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    put = _put_lore(client, camp["id"], member_char, camp_rev, secret, "lore-1")
    assert put.status_code == 200, put.text

    lobby = client.get(f"/api/campaigns/{camp['id']}/lobby")
    assert lobby.status_code == 200
    body = lobby.json()
    assert "party_composition" in body
    comp = body["party_composition"]
    assert comp["size"] == 2
    assert comp["class_counts"].get("Fighter") == 1
    assert comp["class_counts"].get("Wizard") == 1
    names = [e["character_name"] for e in comp["members"]]
    assert "Tank" in names and "Zap" in names
    # No private content anywhere in the public projection.
    assert secret not in json.dumps(body)
    # Dedicated composition endpoint matches.
    direct = client.get(f"/api/campaigns/{camp['id']}/party-composition")
    assert direct.status_code == 200
    assert direct.json()["party_composition"]["size"] == 2


def test_party_advice_is_advisory_and_never_enforced(api):
    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client)
    char_id = _make_character(factory, owner_id, char_class="Fighter")
    _select(client, camp["id"], 0, char_id, "sel-1")
    advice = client.get(f"/api/campaigns/{camp['id']}/party-advice")
    assert advice.status_code == 200
    payload = advice.json()["advice"]
    assert payload["enforced"] is False
    assert payload["suggestions"]
    # Advice never blocks character selection.
    other = _make_character(factory, owner_id, name="Second", char_class="Fighter")
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    resp = _select(client, camp["id"], rev, other, "sel-2")
    assert resp.status_code == 200


def test_private_lore_owner_denied_and_idempotent(api):
    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client)
    _add_member(factory, camp["id"], member_id)
    actor["id"] = member_id
    char_id = _make_character(factory, member_id, name="Sneak")
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    _select(client, camp["id"], rev, char_id, "sel-m")
    secret = "i stole the crown jewels"
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    first = _put_lore(client, camp["id"], char_id, rev, secret, "op-lore-1", op="op-lore-1")
    assert first.status_code == 200, first.text
    assert first.json()["lore"]["version"] == 1

    # Owner (campaign owner, not the character owner) gets fail-closed 404.
    actor["id"] = owner_id
    denied = client.get(f"/api/campaigns/{camp['id']}/characters/{char_id}/lore")
    assert denied.status_code == 404

    # Owning player reads back content; survives lobby GET round-trip.
    actor["id"] = member_id
    got = client.get(f"/api/campaigns/{camp['id']}/characters/{char_id}/lore")
    assert got.status_code == 200
    assert got.json()["lore"]["content"] == secret

    # Retry with a new key but identical content does not duplicate/bump.
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    retry = _put_lore(client, camp["id"], char_id, rev, secret, "op-lore-2")
    assert retry.status_code == 200
    assert retry.json()["lore"]["version"] == 1

    # Edit bumps version; delete removes; second delete is idempotent.
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    edited = _put_lore(client, camp["id"], char_id, rev, "updated secret", "op-lore-3")
    assert edited.status_code == 200
    assert edited.json()["lore"]["version"] == 2
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    deleted = client.request(
        "DELETE", f"/api/campaigns/{camp['id']}/characters/{char_id}/lore",
        json={"expected_revision": rev}, headers={"Idempotency-Key": "del-1"},
    )
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/campaigns/{camp['id']}/characters/{char_id}/lore").status_code == 404


def test_lore_validation_and_lifecycle_lock(api):
    client, factory, actor, owner_id, member_id, outsider_id = api
    camp = _create(client)
    char_id = _make_character(factory, owner_id)
    _select(client, camp["id"], 0, char_id, "sel-o")
    # Oversize content rejected.
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    bad = _put_lore(client, camp["id"], char_id, rev, "x" * 4001, "bad-1")
    assert bad.status_code == 422
    # Outsider cannot write or read.
    actor["id"] = outsider_id
    assert _put_lore(client, camp["id"], char_id, rev, "hi", "out-1").status_code in (403, 404)
    actor["id"] = owner_id
    # Valid write, then leave lobby -> writes lock, reads survive.
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    ok = _put_lore(client, camp["id"], char_id, rev, "pre-start secret", "good-1")
    assert ok.status_code == 200, ok.text
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    ready = client.put(
        f"/api/campaigns/{camp['id']}/members/me/readiness",
        json={"expected_revision": rev, "ready": True},
        headers={"Idempotency-Key": "ready-1"},
    )
    assert ready.status_code == 200, ready.text
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    started = client.post(
        f"/api/campaigns/{camp['id']}/lifecycle",
        json={"expected_revision": rev, "status": "starting"},
        headers={"Idempotency-Key": "start-1"},
    )
    assert started.status_code == 200, started.text
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    locked = _put_lore(client, camp["id"], char_id, rev, "late edit", "late-1")
    assert locked.status_code == 409
    readable = client.get(f"/api/campaigns/{camp['id']}/characters/{char_id}/lore")
    assert readable.status_code == 200
    assert readable.json()["lore"]["content"] == "pre-start secret"


def test_lore_secret_absent_from_idempotency_ledger(api):
    """Review #414: raw lore must never land in IdempotentCommand.result."""
    from models.reliability import IdempotentCommand

    client, factory, actor, owner_id, _, _ = api
    camp = _create(client)
    char_id = _make_character(factory, owner_id)
    _select(client, camp["id"], 0, char_id, "sel-o")
    secret = "ledger-leak-probe-secret"
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    put = _put_lore(client, camp["id"], char_id, rev, secret, "ledger-1")
    assert put.status_code == 200, put.text
    # PUT result itself is metadata-only.
    assert secret not in json.dumps(put.json())
    with factory() as db:
        rows = db.execute(
            select(IdempotentCommand).where(
                IdempotentCommand.command_type == "campaign.character.lore.put"
            )
        ).scalars().all()
    assert rows, "expected durable idempotency record"
    for row in rows:
        assert secret not in json.dumps(row.result or {}), "raw lore in idempotency ledger"


def test_character_creator_consumes_public_party_context(api):
    """Review #414: the AI-assisted creator resolves party context server-side."""
    from app.characters.chat.service import build_chat_messages, build_party_advisory_text
    from app.characters.chat.service import CharacterChatRequest

    client, factory, actor, owner_id, member_id, outsider_id = api
    camp = _create(client)
    char_id = _make_character(factory, owner_id, char_class="Fighter")
    _select(client, camp["id"], 0, char_id, "sel-o")

    # Pure advisory builder: public counts only, never prescriptive.
    text = build_party_advisory_text(
        {"size": 1, "class_counts": {"Fighter": 1}}, {"suggestions": ["hint"], "enforced": False},
    )
    assert "Fighter" in text and "never require" in text
    msgs = build_chat_messages(CharacterChatRequest(content="hi"), party_advisory=text)
    assert any("Party context" in m["content"] for m in msgs if m["role"] == "system")
    plain = build_chat_messages(CharacterChatRequest(content="hi"))
    assert not any("Party context" in m["content"] for m in plain)

    body = {"content": "help me build a rogue", "campaign_id": camp["id"]}
    # Outsider is refused before any provider work.
    actor["id"] = outsider_id
    denied = client.post(f"/api/characters/new/chat", json=body)
    assert denied.status_code in (403, 404)
    # Unknown campaign fails closed.
    actor["id"] = owner_id
    missing = client.post(
        "/api/characters/new/chat",
        json={"content": "hi", "campaign_id": str(uuid.uuid4())},
    )
    assert missing.status_code == 404
    # Member passes auth into the provider path (fallback SSE when no provider).
    ok = client.post("/api/characters/new/chat", json=body)
    assert ok.status_code == 200


def test_seed_bundle_consumes_lore_without_public_leak(api):
    from app.campaigns.party_lore import get_seed_lore_bundle

    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client)
    char_id = _make_character(factory, owner_id)
    _select(client, camp["id"], 0, char_id, "sel-o")
    rev = client.get(f"/api/campaigns/{camp['id']}/lobby").json()["campaign"]["revision"]
    _put_lore(client, camp["id"], char_id, rev, "seed-only secret", "seed-1")
    with factory() as db:
        bundle = get_seed_lore_bundle(db, campaign_id=uuid.UUID(camp["id"]))
    assert len(bundle) == 1
    assert bundle[0]["content"] == "seed-only secret"
    # Public lobby payload still carries no secret content.
    lobby = client.get(f"/api/campaigns/{camp['id']}/lobby")
    assert "seed-only secret" not in json.dumps(lobby.json())
