"""Issue #246 — production campaign start: seed once, open the live table."""
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
from models.campaigns import CampaignMember  # noqa: E402
from models.characters import Character  # noqa: E402
from models.characters import Dnd5eCharacterSheet  # noqa: E402
from models.profiles import Profile  # noqa: E402


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
    payload = {"name": "Start Campaign", "required_players": 1, **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _seed(client: TestClient, campaign_id: str, key: str):
    return client.post(
        f"/api/campaigns/{campaign_id}/world-seed",
        json={"operation_id": key},
        headers={"Idempotency-Key": key},
    )


def _start(client: TestClient, campaign_id: str, key: str):
    return client.post(
        f"/api/campaigns/{campaign_id}/campaign-start",
        json={"operation_id": key},
        headers={"Idempotency-Key": key},
    )


def test_campaign_start_solo_happy_path(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    seed = _seed(client, campaign["id"], "op-seed-246")
    assert seed.status_code == 200, seed.text
    assert seed.json()["campaign"]["status"] == "starting"

    response = _start(client, campaign["id"], "op-start-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign"]["status"] == "active"
    assert body["thread_id"]
    assert body["dm_turn"]["id"]
    assert body["dm_attempt"]["id"]
    assert body.get("replayed") is False
    assert "solo-bootstrap-355" not in json.dumps(body)

    with factory() as db:
        from models.campaigns import Campaign, CampaignDomainEvent
        from models.dm import DmTurnAttempt
        from models.threads import PlayerSubmission

        camp = db.get(Campaign, uuid.UUID(campaign["id"]))
        assert camp.status == "active"
        # The opener is a tagged system submission, not player speech.
        opener = db.execute(
            select(PlayerSubmission).where(PlayerSubmission.campaign_id == camp.id)
        ).scalars().first()
        assert opener is not None
        assert opener.source == "campaign-start-246"
        # Regression: the opening attempt must carry the post-activation
        # revision, otherwise execution refuses it as stale (#246 ordering).
        attempt = db.get(DmTurnAttempt, uuid.UUID(body["dm_attempt"]["id"]))
        assert attempt is not None
        assert int(attempt.source_revision) == int(camp.revision)
        events = db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == camp.id,
                CampaignDomainEvent.event_type == "campaign.started_246",
            )
        ).scalars().all()
        assert len(events) == 1
        subs = db.execute(
            select(PlayerSubmission).where(PlayerSubmission.campaign_id == camp.id)
        ).scalars().all()
        assert len(subs) == 1
        assert subs[0].raw_content.startswith("[Campaign start #246]")


def test_campaign_start_replay_converges(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])
    assert _seed(client, campaign["id"], "op-seed-replay").status_code == 200

    first = _start(client, campaign["id"], "op-start-replay")
    assert first.status_code == 200, first.text
    turn_id = first.json()["dm_turn"]["id"]

    # Same key → idempotent replay.
    same = _start(client, campaign["id"], "op-start-replay")
    assert same.status_code == 200, same.text

    # Different key → state-guarded convergence, no duplicate opening.
    other = _start(client, campaign["id"], "op-start-replay-2")
    assert other.status_code == 200, other.text
    assert other.json()["replayed"] is True
    assert other.json()["dm_turn"]["id"] == turn_id

    with factory() as db:
        from models.campaigns import Campaign
        from models.threads import PlayerSubmission

        cid = uuid.UUID(campaign["id"])
        subs = db.execute(
            select(PlayerSubmission).where(PlayerSubmission.campaign_id == cid)
        ).scalars().all()
        assert len(subs) == 1
        assert db.get(Campaign, cid).status == "active"


def test_campaign_start_requires_seed(api):
    client, factory, actor, owner_id, _, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    response = _start(client, campaign["id"], "op-start-noseed")
    assert response.status_code == 409, response.text

    with factory() as db:
        from models.campaigns import Campaign

        assert db.get(Campaign, uuid.UUID(campaign["id"])).status == "lobby"


def test_campaign_start_unready_and_owner_only(api):
    client, factory, actor, owner_id, _, outsider_id = api
    campaign = _create(client)
    # Seeded campaigns are eligible, but start revalidates: make seed first
    # with a ready party, then flip one member unready before start.
    _ready_lobby(factory, campaign["id"], [owner_id])
    assert _seed(client, campaign["id"], "op-seed-elig").status_code == 200

    with factory() as db:
        member = db.get(CampaignMember, {"campaign_id": uuid.UUID(campaign["id"]), "user_id": owner_id})
        member.is_ready = False
        db.commit()
    denied = _start(client, campaign["id"], "op-start-unready")
    assert denied.status_code == 409, denied.text
    with factory() as db:
        member = db.get(CampaignMember, {"campaign_id": uuid.UUID(campaign["id"]), "user_id": owner_id})
        from datetime import datetime, timezone

        member.is_ready = True
        member.ready_at = datetime.now(timezone.utc)
        db.commit()

    actor["id"] = outsider_id
    forbidden = _start(client, campaign["id"], "op-start-outsider")
    assert forbidden.status_code == 403, forbidden.text
    actor["id"] = owner_id

    missing = _start(client, str(uuid.uuid4()), "op-start-missing")
    assert missing.status_code == 404, missing.text


def test_campaign_start_multiplayer_opens_shared_table(api):
    client, factory, actor, owner_id, member_id, _ = api
    campaign = _create(client, required_players=2)
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(campaign["id"]), user_id=member_id))
        db.commit()
    _ready_lobby(factory, campaign["id"], [owner_id, member_id])

    assert _seed(client, campaign["id"], "op-seed-mp246").status_code == 200
    response = _start(client, campaign["id"], "op-start-mp")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign"]["status"] == "active"
    assert body["dm_turn"]["id"]

    with factory() as db:
        from models.world import CampaignCurrentScene

        scene = db.get(CampaignCurrentScene, uuid.UUID(campaign["id"]))
        assert scene is not None
        names = json.dumps(scene.to_dict())
        # Both PCs are introduced in the seeded opening scene.
        assert "Hero" in names
