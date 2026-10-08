"""Private player notes — own journal scratch space."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.auth.service import TEST_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import CampaignMember  # noqa: E402
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
        "app.deps.auth.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id
    finally:
        app.dependency_overrides.clear()


def test_crud_and_isolation(api):
    client, factory, actor, owner_id, member_id = api
    res = client.post("/api/campaigns", json={"name": "Notes Test"})
    assert res.status_code == 200, res.text
    cid = res.json()["campaign"]["id"]
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        db.commit()

    # Member creates + lists own note.
    actor["id"] = member_id
    res = client.post(f"/api/campaigns/{cid}/notes", json={"content": "  secret plan  "})
    assert res.status_code == 201, res.text
    note = res.json()["note"]
    assert note["content"] == "secret plan"
    res = client.get(f"/api/campaigns/{cid}/notes")
    assert res.status_code == 200, res.text
    assert [n["id"] for n in res.json()["notes"]] == [note["id"]]

    # Owner sees none (private to author).
    actor["id"] = owner_id
    res = client.get(f"/api/campaigns/{cid}/notes")
    assert res.status_code == 200, res.text
    assert res.json()["notes"] == []
    # Owner cannot edit/delete another player's note id — no leak.
    res = client.patch(f"/api/campaigns/{cid}/notes/{note['id']}", json={"content": "hijack"})
    assert res.status_code == 404, res.text
    res = client.delete(f"/api/campaigns/{cid}/notes/{note['id']}")
    assert res.status_code == 404, res.text

    # Author edits + deletes.
    actor["id"] = member_id
    res = client.patch(f"/api/campaigns/{cid}/notes/{note['id']}", json={"content": "updated"})
    assert res.status_code == 200, res.text
    assert res.json()["note"]["content"] == "updated"
    res = client.delete(f"/api/campaigns/{cid}/notes/{note['id']}")
    assert res.status_code == 200, res.text
    res = client.get(f"/api/campaigns/{cid}/notes")
    assert res.json()["notes"] == []


def test_validation(api):
    client, factory, actor, owner_id, member_id = api
    res = client.post("/api/campaigns", json={"name": "Notes Validation"})
    assert res.status_code == 200, res.text
    cid = res.json()["campaign"]["id"]
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        db.commit()
    actor["id"] = member_id
    assert client.post(f"/api/campaigns/{cid}/notes", json={"content": "   "}).status_code == 422
    assert client.post(f"/api/campaigns/{cid}/notes", json={"content": "x" * 2001}).status_code == 422
    assert client.post(f"/api/campaigns/{cid}/notes", json={}).status_code == 422
