"""Issue #243 — shared OOC lobby chat without fictional state advancement."""
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
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import PlayerSubmission  # noqa: E402


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

    def _as(request, db):
        return db.get(Profile, actor["id"])

    monkeypatch.setattr("app.campaigns.router.resolve_profile", _as)
    monkeypatch.setattr("app.runtime.router.resolve_profile", _as)
    monkeypatch.setattr("app.realtime.router.resolve_profile", _as)
    monkeypatch.setattr("app.snapshot.router.resolve_profile", _as)
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Lobby Chat Test", **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _join(factory, campaign_id: str, user_id) -> None:
    with factory() as db:
        db.add(CampaignMember(
            campaign_id=uuid.UUID(campaign_id), user_id=user_id, role="player",
        ))
        db.commit()


def _post(client: TestClient, cid: str, content: str, key: str):
    return client.post(
        f"/api/campaigns/{cid}/lobby/chat",
        json={"content": content},
        headers={"Idempotency-Key": key},
    )


def _get(client: TestClient, cid: str):
    return client.get(f"/api/campaigns/{cid}/lobby/chat")


def _make_character(factory, owner_id, name="Hero", race="Human", char_class="Fighter"):
    with factory() as db:
        char = Character(owner_id=owner_id, name=name, system="dnd5e")
        db.add(char)
        db.flush()
        db.add(Dnd5eCharacterSheet(
            character_id=char.id, owner_id=owner_id,
            character_name=name, race=race, char_class=char_class, level=1,
        ))
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


def _lobby_thread_id(factory, cid: str) -> str:
    from models.threads import CampaignThread

    with factory() as db:
        row = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == uuid.UUID(cid),
                CampaignThread.thread_type == "lobby",
            )
        ).scalars().one()
        return str(row.id)


def test_two_player_exchange_and_refresh_reconstructs(api):
    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client, required_players=2)
    cid = camp["id"]
    _join(factory, cid, member_id)

    actor["id"] = owner_id
    r1 = _post(client, cid, "Hey, what time works for session zero?", "ooc-1")
    assert r1.status_code == 201, r1.text
    actor["id"] = member_id
    r2 = _post(client, cid, "Evenings work for me!", "ooc-2")
    assert r2.status_code == 201, r2.text
    assert r2.json()["message"]["sequence"] == r1.json()["message"]["sequence"] + 1

    # Both members see the full ordered history...
    snap = _get(client, cid)
    assert snap.status_code == 200, snap.text
    body = snap.json()
    assert body["thread"]["thread_type"] == "lobby"
    texts = [m["raw_content"] for m in body["messages"]]
    assert texts == ["Hey, what time works for session zero?", "Evenings work for me!"]
    assert body["channel"].endswith(f":thread:{body['thread']['id']}")

    # ...and a refresh (fresh GET) reconstructs the identical history.
    actor["id"] = owner_id
    again = _get(client, cid)
    assert again.status_code == 200
    assert [m["id"] for m in again.json()["messages"]] == [m["id"] for m in body["messages"]]

    # Lobby discovery is exposed on the lobby projection.
    lobby = client.get(f"/api/campaigns/{cid}/lobby")
    assert lobby.status_code == 200
    assert lobby.json()["lobby_thread_id"] == body["thread"]["id"]


def test_messages_are_forced_ooc_with_no_fictional_side_effects(api):
    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client)
    cid = camp["id"]
    _join(factory, cid, member_id)

    actor["id"] = member_id
    resp = _post(client, cid, "<ic>I draw my sword</ic> and <ooc>brb</ooc>", "ooc-ic")
    assert resp.status_code == 201, resp.text
    msg = resp.json()["message"]
    assert msg["audience"] == "lobby"
    assert len(msg["segments"]) == 1
    assert msg["segments"][0]["type"] == "ooc"

    with factory() as db:
        stored = db.get(PlayerSubmission, uuid.UUID(msg["id"]))
        assert stored.thread_id != "main"
        assert stored.audience == "lobby"
        assert stored.character_id is None
        # Provably side-effect-free: no revision bump, no domain events,
        # no DM turns/attempts anywhere for this campaign.
        campaign = db.get(Campaign, uuid.UUID(cid))
        assert campaign.revision == 0
        from app.campaigns.events import list_campaign_events

        assert list_campaign_events(db, uuid.UUID(cid), limit=200) == []
        assert db.execute(
            select(DmTurn).where(DmTurn.campaign_id == uuid.UUID(cid))
        ).scalars().all() == []
        assert db.execute(
            select(DmTurnAttempt).where(DmTurnAttempt.campaign_id == uuid.UUID(cid))
        ).scalars().all() == []


def test_shared_thread_history_excludes_lobby_chat(api):
    client, factory, actor, owner_id, _, _ = api
    camp = _create(client)
    cid = camp["id"]
    assert _post(client, cid, "table talk", "ooc-x").status_code == 201

    history = client.get(f"/api/campaigns/{cid}/submissions?thread_id=main")
    assert history.status_code == 200
    assert history.json()["submissions"] == []
    lobby_tid = _lobby_thread_id(factory, cid)
    assert history.json()["thread_id"] != lobby_tid


def test_duplicate_send_does_not_duplicate(api):
    client, factory, actor, owner_id, _, _ = api
    camp = _create(client)
    cid = camp["id"]

    first = _post(client, cid, "same message twice", "dup-key")
    assert first.status_code == 201, first.text
    assert first.headers.get("X-Idempotent-Replay") == "false"
    second = _post(client, cid, "same message twice", "dup-key")
    assert second.status_code == 201
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert second.json()["message"]["id"] == first.json()["message"]["id"]

    with factory() as db:
        rows = db.execute(
            select(PlayerSubmission).where(
                PlayerSubmission.campaign_id == uuid.UUID(cid),
                PlayerSubmission.thread_id == _lobby_thread_id(factory, cid),
            )
        ).scalars().all()
        assert len(rows) == 1


def test_missing_idempotency_key_rejected(api):
    client, _, _, _, _, _ = api
    camp = _create(client)
    resp = client.post(f"/api/campaigns/{camp['id']}/lobby/chat", json={"content": "hi"})
    assert resp.status_code == 400


def test_realtime_publish_and_channel_auth(api, monkeypatch):
    from app.realtime.channels import live_table_channel
    from app.realtime.service import InMemoryRealtimePublisher, set_realtime_publisher

    client, factory, actor, owner_id, member_id, outsider_id = api
    camp = _create(client)
    cid = camp["id"]
    _join(factory, cid, member_id)

    publisher = InMemoryRealtimePublisher()
    set_realtime_publisher(publisher)
    try:
        actor["id"] = member_id
        resp = _post(client, cid, "realtime hello", "rt-1")
        assert resp.status_code == 201, resp.text
        lobby_tid = _lobby_thread_id(factory, cid)
        channel = live_table_channel(uuid.UUID(cid), uuid.UUID(lobby_tid))
        events = publisher.events_for_channel(channel)
        assert len(events) == 1
        assert events[0]["event"] == "submission.created"
        assert events[0]["payload"]["event_id"] == f"submission:{resp.json()['message']['id']}"

        # Member can authorize the lobby channel...
        auth = client.post(
            f"/api/campaigns/{cid}/realtime/authorize", json={"thread_id": lobby_tid}
        )
        assert auth.status_code == 200
        assert auth.json()["channel"] == channel
        channels = client.get(f"/api/campaigns/{cid}/realtime/channels")
        assert channels.status_code == 200
        assert lobby_tid in {c["thread_id"] for c in channels.json()["channels"]}

        # ...outsiders cannot.
        actor["id"] = outsider_id
        assert _get(client, cid).status_code == 403
        assert _post(client, cid, "sneaky", "rt-evil").status_code == 403
        denied = client.post(
            f"/api/campaigns/{cid}/realtime/authorize", json={"thread_id": lobby_tid}
        )
        assert denied.status_code == 403
    finally:
        set_realtime_publisher(None)


def test_removed_member_loses_access(api):
    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client, required_players=2)
    cid = camp["id"]
    _join(factory, cid, member_id)

    actor["id"] = member_id
    assert _post(client, cid, "before removal", "rm-1").status_code == 201

    actor["id"] = owner_id
    lobby = client.get(f"/api/campaigns/{cid}/lobby").json()
    rev = lobby["campaign"]["revision"]
    removal = client.request(
        "DELETE",
        f"/api/campaigns/{cid}/members/{member_id}",
        json={"expected_revision": rev},
        headers={"Idempotency-Key": "remove-member"},
    )
    assert removal.status_code == 200, removal.text

    actor["id"] = member_id
    assert _get(client, cid).status_code == 403
    assert _post(client, cid, "after removal", "rm-2").status_code == 403

    # Snapshot service enforces the same boundary (fail closed).
    from app.snapshot.service import SnapshotAuthorizationError, build_live_table_snapshot

    lobby_tid = _lobby_thread_id(factory, cid)
    with factory() as db:
        with pytest.raises(SnapshotAuthorizationError):
            build_live_table_snapshot(db, uuid.UUID(cid), member_id, thread_id=lobby_tid)
        snap = build_live_table_snapshot(db, uuid.UUID(cid), owner_id, thread_id=lobby_tid)
        assert [m["raw_content"] for m in snap["history"]["messages"]] == ["before removal"]


def test_generic_gameplay_submissions_reject_lobby_thread(api):
    client, factory, actor, owner_id, _, _ = api
    camp = _create(client)
    cid = camp["id"]
    assert _post(client, cid, "table talk", "ooc-g").status_code == 201
    lobby_tid = _lobby_thread_id(factory, cid)

    resp = client.post(
        f"/api/campaigns/{cid}/submissions",
        json={"thread_id": lobby_tid, "content": "<ic>I attack</ic>"},
        headers={"Idempotency-Key": "gameplay-on-lobby"},
    )
    assert resp.status_code == 409, resp.text
    with factory() as db:
        assert db.execute(
            select(DmTurn).where(DmTurn.campaign_id == uuid.UUID(cid))
        ).scalars().all() == []


def test_coordinate_turn_refuses_lobby_thread(api):
    from app.dm.turns import coordinate_turn

    client, factory, actor, owner_id, _, _ = api
    camp = _create(client)
    cid = camp["id"]
    assert _post(client, cid, "unresolved table talk", "ooc-coord").status_code == 201
    lobby_tid = _lobby_thread_id(factory, cid)

    with factory() as db:
        assert coordinate_turn(db, uuid.UUID(cid), lobby_tid) is None
        assert db.execute(
            select(DmTurn).where(
                DmTurn.campaign_id == uuid.UUID(cid),
                DmTurn.thread_id == lobby_tid,
            )
        ).scalars().all() == []


def test_concurrent_start_transition_refused_under_lock(api, monkeypatch):
    """TOCTOU race: starting->active committing after the transport-level
    checks but before persistence must still refuse the lobby write."""
    import app.campaigns.router as campaign_router

    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client)
    cid = camp["id"]
    _join(factory, cid, member_id)

    real_require_key = campaign_router.require_idempotency_key

    def _flip_to_active_then_delegate(request, fallback=None):
        with factory() as db:
            row = db.get(Campaign, uuid.UUID(cid))
            row.status = "active"
            db.commit()
        return real_require_key(request, fallback)

    monkeypatch.setattr(
        campaign_router, "require_idempotency_key", _flip_to_active_then_delegate
    )
    actor["id"] = member_id
    resp = _post(client, cid, "sneaks in after start", "race-start-1")
    assert resp.status_code == 409, resp.text
    with factory() as db:
        assert db.execute(
            select(PlayerSubmission).where(
                PlayerSubmission.campaign_id == uuid.UUID(cid)
            )
        ).scalars().all() == []


def test_concurrent_member_removal_refused_under_lock(api, monkeypatch):
    """TOCTOU race: member removal committing after the transport-level
    checks but before persistence must still refuse the lobby write."""
    import app.campaigns.router as campaign_router

    client, factory, actor, owner_id, member_id, _ = api
    camp = _create(client)
    cid = camp["id"]
    _join(factory, cid, member_id)

    real_require_key = campaign_router.require_idempotency_key

    def _remove_then_delegate(request, fallback=None):
        with factory() as db:
            db.delete(db.get(
                CampaignMember,
                {"campaign_id": uuid.UUID(cid), "user_id": member_id},
            ))
            db.commit()
        return real_require_key(request, fallback)

    monkeypatch.setattr(
        campaign_router, "require_idempotency_key", _remove_then_delegate
    )
    actor["id"] = member_id
    resp = _post(client, cid, "sneaks in after removal", "race-remove-1")
    assert resp.status_code == 403, resp.text
    with factory() as db:
        assert db.execute(
            select(PlayerSubmission).where(
                PlayerSubmission.campaign_id == uuid.UUID(cid)
            )
        ).scalars().all() == []


def test_start_transition_preserves_lobby_without_mixing(api):
    client, factory, actor, owner_id, _, _ = api
    camp = _create(client, required_players=1)
    cid = camp["id"]
    char_id = _make_character(factory, owner_id)
    assert _select(client, cid, 0, char_id, "sel-243").status_code == 200
    assert _ready(client, cid, 1, True, "ready-243").status_code == 200
    assert _post(client, cid, "see you at session one", "ooc-pre").status_code == 201
    lobby_tid = _lobby_thread_id(factory, cid)

    # lobby -> starting: chat stays fully available, writes still allowed pre-start.
    assert _transition(client, cid, 2, "starting", "to-starting").status_code == 200
    assert _post(client, cid, "still coordinating", "ooc-starting").status_code == 201
    assert [m["raw_content"] for m in _get(client, cid).json()["messages"]] == [
        "see you at session one", "still coordinating",
    ]

    # starting -> active: lobby history is preserved read-only, never gameplay.
    assert _transition(client, cid, 3, "active", "to-active").status_code == 200
    snap = _get(client, cid)
    assert snap.status_code == 200
    assert len(snap.json()["messages"]) == 2
    assert _post(client, cid, "too late", "ooc-active").status_code == 409

    # archived: dormant but still readable, never writable.
    assert _transition(client, cid, 4, "archived", "to-archived").status_code == 200
    assert _get(client, cid).status_code == 200
    assert _post(client, cid, "dormant", "ooc-archived").status_code == 409

    with factory() as db:
        # No DM turn was ever assembled for the lobby thread, and the shared
        # game transcript holds none of the lobby table talk.
        assert db.execute(
            select(DmTurn).where(
                DmTurn.campaign_id == uuid.UUID(cid),
                DmTurn.thread_id == lobby_tid,
            )
        ).scalars().all() == []
        shared = db.execute(
            select(PlayerSubmission).where(
                PlayerSubmission.campaign_id == uuid.UUID(cid),
            )
        ).scalars().all()
        assert shared != []
        assert {s.thread_id for s in shared} == {lobby_tid}
