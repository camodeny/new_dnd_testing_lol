"""Issue #246 reviewer follow-up — directed opening-introduction progression.

Two-PC table: the intro cursor freezes the launch order at start, advances
one event per PC as their first shared-table turn commits, and releases to
freeform (complete) once every launch PC has acted. Tracking only — play is
never gated.
"""
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
from app.dm.execution import run_dm_execute_sweep  # noqa: E402
from app.dm.fake_provider import FakeDMProvider  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import CampaignDomainEvent, CampaignMember  # noqa: E402
from models.characters import Character  # noqa: E402
from models.characters import Dnd5eCharacterSheet  # noqa: E402
from models.profiles import Profile  # noqa: E402


MSG_1 = "Borin steps forward to face the riders."
MSG_2 = "Mira slips along the stalls to flank them."


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
        "app.runtime.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def fake_provider(monkeypatch):
    provider = FakeDMProvider()
    from app.dm.fake_provider import phase0_respond_contract
    provider.register_step(
        "play-1", phase0_respond_contract(marker="intro-reply-1", reason="intro pc one"),
        inputs=(MSG_1,),
    )
    provider.register_step(
        "play-2", phase0_respond_contract(marker="intro-reply-2", reason="intro pc two"),
        inputs=(MSG_2,),
    )
    provider.install(monkeypatch)
    return provider


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Intro Campaign", "required_players": 2, **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _post(client: TestClient, path: str, key: str, body: dict | None = None):
    return client.post(
        path, json=body or {"operation_id": key}, headers={"Idempotency-Key": key},
    )


def _sweep(factory) -> dict:
    with factory() as db:
        outcome = run_dm_execute_sweep(db, limit=10, narrator="deterministic")
        db.commit()
        return outcome


def test_multiplayer_intro_progresses_then_releases(api, fake_provider):
    client, factory, actor, owner_id, member_id = api
    campaign = _create(client)
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(campaign["id"]), user_id=member_id))
        db.commit()
    chars = _ready_lobby(factory, campaign["id"], [owner_id, member_id])

    assert _post(client, f"/api/campaigns/{campaign['id']}/world-seed", "op-seed").status_code == 200
    started = _post(client, f"/api/campaigns/{campaign['id']}/campaign-start", "op-start")
    assert started.status_code == 200, started.text
    intro = started.json()["opening_intro"]
    assert intro is not None and intro["complete"] is False
    assert {c["character_id"] for c in intro["order"]} == {
        str(chars[str(owner_id)].id), str(chars[str(member_id)].id)
    }
    assert len(intro["order"]) == 2
    first_id = intro["focused"]["character_id"]

    # First launch PC acts -> cursor advances exactly one step.
    first_uid = owner_id if str(chars[str(owner_id)].id) == first_id else member_id
    actor["id"] = first_uid
    first_msg = MSG_1 if first_uid == owner_id else MSG_2
    sub = _post(
        client, f"/api/campaigns/{campaign['id']}/submissions", "op-sub-1",
        {"content": first_msg, "operation_id": "op-sub-1"},
    )
    assert sub.status_code == 201, sub.text
    _sweep(factory)

    actor["id"] = owner_id
    again = _post(client, f"/api/campaigns/{campaign['id']}/campaign-start", "op-start-2")
    assert again.status_code == 200, again.text
    intro = again.json()["opening_intro"]
    assert intro["complete"] is False
    assert len(intro["introduced"]) == 1
    assert intro["introduced"][0]["character_id"] == first_id
    assert intro["focused"]["character_id"] != first_id

    # Second launch PC acts -> intro completes, table released to freeform.
    second_uid = member_id if first_uid == owner_id else owner_id
    actor["id"] = second_uid
    second_msg = MSG_2 if second_uid == member_id else MSG_1
    sub2 = _post(
        client, f"/api/campaigns/{campaign['id']}/submissions", "op-sub-2",
        {"content": second_msg, "operation_id": "op-sub-2"},
    )
    assert sub2.status_code == 201, sub2.text
    _sweep(factory)

    actor["id"] = owner_id
    final = _post(client, f"/api/campaigns/{campaign['id']}/campaign-start", "op-start-3")
    assert final.status_code == 200, final.text
    intro = final.json()["opening_intro"]
    assert intro["complete"] is True
    assert intro["focused"] is None
    assert len(intro["introduced"]) == 2

    with factory() as db:
        events = db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == uuid.UUID(campaign["id"]),
                CampaignDomainEvent.event_type == "campaign.opening_intro_advanced",
            )
        ).scalars().all()
        assert len(events) == 2
        assert {e.payload["character_id"] for e in events} == {
            c["character_id"] for c in intro["order"]
        }


def test_intro_replay_is_idempotent(api, fake_provider):
    client, factory, actor, owner_id, member_id = api
    campaign = _create(client)
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(campaign["id"]), user_id=member_id))
        db.commit()
    _ready_lobby(factory, campaign["id"], [owner_id, member_id])

    assert _post(client, f"/api/campaigns/{campaign['id']}/world-seed", "op-seed").status_code == 200
    assert _post(client, f"/api/campaigns/{campaign['id']}/campaign-start", "op-start").status_code == 200

    sub = _post(
        client, f"/api/campaigns/{campaign['id']}/submissions", "op-sub-1",
        {"content": MSG_1, "operation_id": "op-sub-1"},
    )
    assert sub.status_code == 201, sub.text
    _sweep(factory)
    # Same-key replay of the start converges without duplicating intro events.
    assert _post(client, f"/api/campaigns/{campaign['id']}/campaign-start", "op-start").status_code == 200
    assert _post(client, f"/api/campaigns/{campaign['id']}/campaign-start", "op-other").status_code == 200

    with factory() as db:
        events = db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == uuid.UUID(campaign["id"]),
                CampaignDomainEvent.event_type == "campaign.opening_intro_advanced",
            )
        ).scalars().all()
        assert len(events) == 1
