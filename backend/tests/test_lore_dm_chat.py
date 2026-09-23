"""Private lore-DM setup chat — guided back-and-forth + lore proposals."""
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
from models.campaigns import Campaign, CampaignLoreChatMessage, CampaignMember  # noqa: E402
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
    # The streaming generator persists the assistant reply through the app
    # SessionLocal (same pattern as character chat) — route it at the test DB.
    import database

    monkeypatch.setattr(database, "SessionLocal", factory)
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Lore DM Test", **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _make_character(factory, owner_id, name="Hero"):
    with factory() as db:
        char = Character(owner_id=owner_id, name=name, system="dnd5e")
        db.add(char)
        db.flush()
        db.add(Dnd5eCharacterSheet(
            character_id=char.id, owner_id=owner_id,
            character_name=name, race="Human", char_class="Fighter", level=3,
        ))
        db.commit()
        return str(char.id)


def _add_member(factory, camp_id, user_id):
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(camp_id), user_id=user_id, role="player"))
        db.commit()


def _mock_dm(monkeypatch, *, tokens=("Hel", "lo"), proposal="You owe a harbor debt.", captured_requests=None):
    import app.providers as providers_pkg
    from app.providers.contracts import NormalizedStreamEvent, NormalizedToolCall

    class _FakeAdapter:
        def __init__(self, name="meta"):
            self.name = name

        def env_model(self):
            return "m"

    def _fake_stream(adapter, request):
        if captured_requests is not None:
            captured_requests.append(request)
        for t in tokens:
            yield NormalizedStreamEvent(kind="token", text=t)
        if proposal is not None:
            yield NormalizedStreamEvent(
                kind="tool_call",
                tool_call=NormalizedToolCall(
                    id="tc1", name="propose_lore_draft",
                    arguments=json.dumps({"lore_text": proposal}), raw={},
                ),
            )

    monkeypatch.setattr(providers_pkg, "stream_chat", _fake_stream)
    monkeypatch.setattr(
        "app.providers.areas.resolve_area",
        lambda area: (_FakeAdapter("meta"), "m", "meta"),
    )


def _chat_url(cid, char_id):
    return f"/api/campaigns/{cid}/characters/{char_id}/lore-chat"


def _sse_texts(body: str):
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            out.append(json.loads(line[5:].strip()))
        except ValueError:
            continue
    return out


def test_history_empty_and_post_streams_tokens_and_proposal(api, monkeypatch):
    client, factory, actor, owner_id, *_ = api
    captured_requests = []
    _mock_dm(monkeypatch, captured_requests=captured_requests)
    camp = _create(client)
    char_id = _make_character(factory, owner_id)

    history = client.get(_chat_url(camp["id"], char_id))
    assert history.status_code == 200, history.text
    assert history.json()["messages"] == []

    response = client.post(_chat_url(camp["id"], char_id), json={"content": "I want a secret"})
    assert response.status_code == 200, response.text
    user_turns = [
        message for message in captured_requests[0].messages
        if message["role"] == "user" and message["content"] == "I want a secret"
    ]
    assert len(user_turns) == 1
    events = _sse_texts(response.text)
    kinds = [e.get("type") for e in events]
    assert "token" in kinds
    proposals = [e for e in events if e.get("type") == "proposal"]
    assert len(proposals) == 1
    assert proposals[0]["lore_text"] == "You owe a harbor debt."
    assert kinds[-1] == "done"

    with factory() as db:
        rows = db.execute(select(CampaignLoreChatMessage)).scalars().all()
        roles = sorted(r.role for r in rows)
        assert roles == ["assistant", "user"]
        assistant = next(r for r in rows if r.role == "assistant")
        assert assistant.proposal_text == "You owe a harbor debt."
        assert assistant.content == "Hello"
        assert assistant.user_id == owner_id

    history = client.get(_chat_url(camp["id"], char_id))
    assert len(history.json()["messages"]) == 2
    assert history.json()["messages"][1].get("proposal") == "You owe a harbor debt."


def test_post_validation_rejects_empty_and_overlong(api, monkeypatch):
    client, factory, actor, owner_id, *_ = api
    _mock_dm(monkeypatch)
    camp = _create(client)
    char_id = _make_character(factory, owner_id)

    assert client.post(_chat_url(camp["id"], char_id), json={"content": "  "}).status_code == 422
    assert client.post(_chat_url(camp["id"], char_id), json={"content": "x" * 2001}).status_code == 422
    assert client.post(_chat_url(camp["id"], char_id), json={}).status_code == 422


def test_outsider_forbidden_and_stranger_character_404(api, monkeypatch):
    client, factory, actor, owner_id, member_id, outsider_id = api
    _mock_dm(monkeypatch)
    camp = _create(client)
    char_id = _make_character(factory, owner_id)
    other_char = _make_character(factory, member_id, name="Other")

    actor["id"] = outsider_id
    assert client.get(_chat_url(camp["id"], char_id)).status_code == 403
    assert client.post(_chat_url(camp["id"], char_id), json={"content": "hi"}).status_code == 403

    # Member cannot read or write another player's thread (fail closed, no oracle).
    _add_member(factory, camp["id"], member_id)
    actor["id"] = member_id
    assert client.get(_chat_url(camp["id"], char_id)).status_code == 404
    assert client.post(_chat_url(camp["id"], char_id), json={"content": "hi"}).status_code == 404

    # ...including when the other character belongs to the campaign owner.
    actor["id"] = owner_id
    assert client.get(_chat_url(camp["id"], other_char)).status_code == 404


def test_member_can_chat_own_thread(api, monkeypatch):
    client, factory, actor, owner_id, member_id, _ = api
    _mock_dm(monkeypatch, tokens=("Hi",), proposal=None)
    camp = _create(client, required_players=2)
    _add_member(factory, camp["id"], member_id)
    char_id = _make_character(factory, member_id, name="Sidekick")

    actor["id"] = member_id
    response = client.post(_chat_url(camp["id"], char_id), json={"content": "help me"})
    assert response.status_code == 200, response.text
    events = _sse_texts(response.text)
    assert [e.get("type") for e in events] == ["token", "done"]
    history = client.get(_chat_url(camp["id"], char_id))
    assert [m["role"] for m in history.json()["messages"]] == ["user", "assistant"]
    assert "proposal" not in history.json()["messages"][1]


def test_writes_lock_after_lobby_but_reads_survive(api, monkeypatch):
    client, factory, actor, owner_id, *_ = api
    _mock_dm(monkeypatch)
    camp = _create(client)
    char_id = _make_character(factory, owner_id)
    client.post(_chat_url(camp["id"], char_id), json={"content": "first"})

    with factory() as db:
        camp_row = db.get(Campaign, uuid.UUID(camp["id"]))
        camp_row.status = "active"
        db.commit()

    locked = client.post(_chat_url(camp["id"], char_id), json={"content": "second"})
    assert locked.status_code == 409, locked.text
    history = client.get(_chat_url(camp["id"], char_id))
    assert history.status_code == 200
    assert len(history.json()["messages"]) == 2
