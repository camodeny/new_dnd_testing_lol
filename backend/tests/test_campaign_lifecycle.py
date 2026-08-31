"""Issue #240 — authoritative campaign setup lifecycle and membership lock."""
from __future__ import annotations

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

from app.deps.auth import MOCK_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Campaign,
    CampaignDomainEvent,
    CampaignInvite,
    CampaignMember,
    CampaignThread,
    CampaignThreadMember,
    Profile,
)


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
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Lifecycle Test", **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _transition(client: TestClient, campaign_id: str, revision: int, status: str, key: str):
    return client.post(
        f"/api/campaigns/{campaign_id}/lifecycle",
        json={"expected_revision": revision, "status": status},
        headers={"Idempotency-Key": key},
    )


def test_launch_settings_are_structured_and_player_range_is_strict(api):
    client, _, _, _, _, _ = api
    campaign = _create(
        client,
        required_players=6,
        theme="Gothic frontier",
        brief="A watchtower has gone silent.",
        difficulty="Hard",
        content_boundaries={"lines": ["harm to children"], "veils": ["romance"]},
        loot_mode="rare_quality",
    )
    assert campaign["status"] == "lobby"
    assert campaign["required_players"] == 6
    assert campaign["theme"] == "Gothic frontier"
    assert campaign["brief"] == "A watchtower has gone silent."
    assert campaign["difficulty"] == "hard"
    assert campaign["content_boundaries"] == {
        "lines": ["harm to children"], "veils": ["romance"]
    }

    solo = _create(client, name="Solo", required_players=1)
    assert solo["required_players"] == 1
    for invalid in (0, 7, "many"):
        response = client.post(
            "/api/campaigns", json={"name": "Invalid", "required_players": invalid}
        )
        assert response.status_code == 400
    boundaries = client.post(
        "/api/campaigns", json={"name": "Invalid", "content_boundaries": ["not", "object"]}
    )
    assert boundaries.status_code == 400


def test_lifecycle_is_authoritative_idempotent_and_recoverable(api):
    client, factory, actor, _, member_id, outsider_id = api
    campaign = _create(client, required_players=2)
    cid = campaign["id"]
    invite = client.post(f"/api/campaigns/{cid}/invites")
    assert invite.status_code == 200
    code = invite.json()["code"]

    not_ready = _transition(client, cid, 0, "starting", "start-not-ready")
    assert not_ready.status_code == 409
    with factory() as db:
        persisted = db.get(Campaign, uuid.UUID(cid))
        assert (persisted.status, persisted.revision) == ("lobby", 0)
        assert db.scalar(select(func.count()).select_from(CampaignDomainEvent)) == 0

        db.add(CampaignMember(campaign_id=persisted.id, user_id=member_id, role="player"))
        db.commit()

    started = _transition(client, cid, 0, "starting", "start-ready")
    replay = _transition(client, cid, 0, "starting", "start-ready")
    assert started.status_code == replay.status_code == 200
    assert started.json() == replay.json()
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert started.json()["campaign"]["status"] == "starting"
    assert started.json()["campaign"]["revision"] == 1

    locked_settings = client.put(
        f"/api/campaigns/{cid}",
        json={"expected_revision": 1, "theme": "Too late"},
        headers={"Idempotency-Key": "locked-settings"},
    )
    assert locked_settings.status_code == 409

    actor["id"] = outsider_id
    locked_join = client.post(f"/api/campaigns/{cid}/join", json={"code": code})
    assert locked_join.status_code == 409
    actor["id"] = member_id
    existing_member_retry = client.post(f"/api/campaigns/{cid}/join", json={"code": code})
    assert existing_member_retry.status_code == 200

    actor["id"] = MOCK_USER_ID
    recovered = _transition(client, cid, 1, "lobby", "recover-lobby")
    assert recovered.status_code == 200
    assert recovered.json()["campaign"]["status"] == "lobby"
    assert recovered.json()["campaign"]["revision"] == 2

    editable_again = client.put(
        f"/api/campaigns/{cid}",
        json={"expected_revision": 2, "theme": "Recovered"},
        headers={"Idempotency-Key": "recovered-settings"},
    )
    assert editable_again.status_code == 200


def test_owner_removal_count_reduction_and_invite_revocation_are_revisioned(api):
    client, factory, actor, owner_id, member_id, outsider_id = api
    campaign = _create(client, required_players=3)
    cid = uuid.UUID(campaign["id"])
    private_thread_id = uuid.uuid4()
    with factory() as db:
        db.add_all([
            CampaignMember(campaign_id=cid, user_id=member_id, role="player"),
            CampaignMember(campaign_id=cid, user_id=outsider_id, role="player"),
            CampaignThread(
                id=private_thread_id,
                campaign_id=cid,
                thread_type="private",
                title="Private",
                created_by=owner_id,
            ),
            CampaignThreadMember(thread_id=private_thread_id, user_id=outsider_id),
        ])
        db.commit()
    invite = client.post(f"/api/campaigns/{cid}/invites")
    assert invite.status_code == 200

    actor["id"] = member_id
    denied = client.put(
        f"/api/campaigns/{cid}",
        json={"expected_revision": 0, "difficulty": "easy"},
        headers={"Idempotency-Key": "non-owner"},
    )
    assert denied.status_code == 403

    actor["id"] = owner_id
    remove_body = {"expected_revision": 0}
    removed = client.request(
        "DELETE",
        f"/api/campaigns/{cid}/members/{outsider_id}",
        json=remove_body,
        headers={"Idempotency-Key": "remove-outsider"},
    )
    replay = client.request(
        "DELETE",
        f"/api/campaigns/{cid}/members/{outsider_id}",
        json=remove_body,
        headers={"Idempotency-Key": "remove-outsider"},
    )
    assert removed.status_code == replay.status_code == 200
    assert removed.json()["campaign"]["revision"] == 1
    assert replay.headers["X-Idempotent-Replay"] == "true"
    with factory() as db:
        assert db.get(CampaignMember, {"campaign_id": cid, "user_id": outsider_id}) is None
        assert db.get(
            CampaignThreadMember, {"thread_id": private_thread_id, "user_id": outsider_id}
        ) is None

    reduced = client.put(
        f"/api/campaigns/{cid}",
        json={"expected_revision": 1, "required_players": 2},
        headers={"Idempotency-Key": "reduce-count"},
    )
    assert reduced.status_code == 200
    assert reduced.json()["campaign"]["required_players"] == 2
    assert reduced.json()["campaign"]["revision"] == 2

    revoked = client.request(
        "DELETE",
        f"/api/campaigns/{cid}/invites",
        json={"expected_revision": 2},
        headers={"Idempotency-Key": "revoke-invite"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["campaign"]["revision"] == 3
    with factory() as db:
        assert db.get(CampaignInvite, cid) is None


def test_active_and_archived_transitions_lock_membership_and_invalid_edges(api):
    client, factory, _, _, _, _ = api
    campaign = _create(client)
    cid = campaign["id"]
    assert _transition(client, cid, 0, "active", "skip-start").status_code == 409
    assert _transition(client, cid, 0, "starting", "to-starting").status_code == 200
    assert _transition(client, cid, 1, "active", "to-active").status_code == 200
    assert client.post(f"/api/campaigns/{cid}/invites").status_code == 409
    archived = _transition(client, cid, 2, "archived", "to-archived")
    assert archived.status_code == 200
    assert archived.json()["campaign"]["status"] == "archived"
    terminal = _transition(client, cid, 3, "active", "unarchive")
    assert terminal.status_code == 409
    with factory() as db:
        persisted = db.get(Campaign, uuid.UUID(cid))
        assert (persisted.status, persisted.revision) == ("archived", 3)
