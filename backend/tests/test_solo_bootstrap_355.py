"""Issue #355 — minimal solo campaign bootstrap into the production live-table runtime."""
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

from app.auth.service import TEST_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import CampaignMember  # noqa: E402
from models.characters import Character  # noqa: E402
from models.characters import Dnd5eCharacterSheet  # noqa: E402
from models.dm import DmTurn  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402
from models.world import CampaignCurrentScene  # noqa: E402


def _ready_lobby(factory, campaign_id: str, user_ids: list) -> None:
    from datetime import datetime, timezone

    cid = uuid.UUID(campaign_id)
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
        db.commit()


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = TEST_USER_ID
    outsider_id = uuid.uuid4()
    with factory() as db:
        db.add_all([
            Profile(id=owner_id, email="owner@example.com"),
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
        yield TestClient(app), factory, actor, owner_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Solo Bootstrap", "required_players": 1, **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _bootstrap(client: TestClient, campaign_id: str, key: str):
    return client.post(
        f"/api/campaigns/{campaign_id}/solo-bootstrap",
        json={"operation_id": key},
        headers={"Idempotency-Key": key},
    )


def test_solo_bootstrap_starts_active_table_with_opening_turn(api):
    client, factory, actor, owner_id, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    response = _bootstrap(client, campaign["id"], "op-solo-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign"]["status"] == "active"
    assert body["thread_id"]
    assert body["dm_turn"]["id"]
    assert body["dm_attempt"]["id"]
    assert body["scene"]["location_name"] == "Emberhold Tavern"
    assert body["solo_bootstrap"] is True

    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        scene = db.get(CampaignCurrentScene, cid)
        assert scene is not None
        assert (scene.environment or {}).get("bootstrap") == "solo-bootstrap-355"
        threads = db.execute(
            select(CampaignThread).where(CampaignThread.campaign_id == cid)
        ).scalars().all()
        assert len(threads) == 1
        turns = db.execute(
            select(DmTurn).where(DmTurn.campaign_id == cid)
        ).scalars().all()
        assert len(turns) == 1


def test_solo_bootstrap_duplicate_start_is_idempotent(api):
    client, factory, actor, owner_id, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    first = _bootstrap(client, campaign["id"], "op-solo-dup-1")
    assert first.status_code == 200, first.text
    # Different key — must still reuse thread + opening turn, not duplicate.
    second = _bootstrap(client, campaign["id"], "op-solo-dup-2")
    assert second.status_code == 200, second.text
    assert second.json()["thread_id"] == first.json()["thread_id"]
    assert second.json()["dm_turn"]["id"] == first.json()["dm_turn"]["id"]

    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        thread_count = db.scalar(
            select(func.count()).select_from(CampaignThread).where(CampaignThread.campaign_id == cid)
        )
        turn_count = db.scalar(
            select(func.count()).select_from(DmTurn).where(DmTurn.campaign_id == cid)
        )
        assert thread_count == 1
        assert turn_count == 1


def test_solo_bootstrap_rejects_non_solo_and_non_owner(api):
    client, factory, actor, owner_id, outsider_id = api
    multi = _create(client, name="Multi", required_players=2)
    _ready_lobby(factory, multi["id"], [owner_id])
    response = _bootstrap(client, multi["id"], "op-multi")
    assert response.status_code == 409, response.text

    solo = _create(client, name="Solo2")
    _ready_lobby(factory, solo["id"], [owner_id])
    actor["id"] = outsider_id
    response = _bootstrap(client, solo["id"], "op-outsider")
    assert response.status_code in (403, 404), response.text
    actor["id"] = owner_id


def test_solo_bootstrap_requires_ready_character(api):
    client, factory, actor, owner_id, _ = api
    campaign = _create(client)
    # No selection/readiness — must stay startable again (409, no side effects).
    response = _bootstrap(client, campaign["id"], "op-not-ready")
    assert response.status_code == 409, response.text
    with factory() as db:
        cid = uuid.UUID(campaign["id"])
        assert db.get(CampaignCurrentScene, cid) is None
        turns = db.execute(
            select(DmTurn).where(DmTurn.campaign_id == cid)
        ).scalars().all()
        assert turns == []


def test_solo_bootstrap_mid_request_failure_leaves_no_partial_state(api, monkeypatch):
    """Review #360: a failure after lifecycle/scene staging must not wedge retries.

    The bootstrap runs flush-only inside the idempotent command, so a crash
    before the atomic commit rolls back everything including the in_progress
    record — retrying the SAME key must succeed instead of 409-looping.
    """
    import app.dm.turns as dm_turns

    from models.campaigns import Campaign
    from models.reliability import IdempotentCommand

    client, factory, actor, owner_id, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    real_coordinate = dm_turns.coordinate_turn
    calls = {"n": 0}

    def _fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected failure after scene staging")
        return real_coordinate(*args, **kwargs)

    monkeypatch.setattr(dm_turns, "coordinate_turn", _fail_once)
    # coordinate_turn is imported into solo_bootstrap's namespace at call time
    # (function-level import), so patching app.dm.turns takes effect.
    with pytest.raises(RuntimeError, match="injected failure"):
        _bootstrap(client, campaign["id"], "op-fail-once")

    cid = uuid.UUID(campaign["id"])
    with factory() as db:
        # No partial bootstrap state committed …
        assert db.get(Campaign, cid).status == "lobby"
        assert db.get(CampaignCurrentScene, cid) is None
        assert db.execute(select(DmTurn).where(DmTurn.campaign_id == cid)).scalars().all() == []
        # … and no stranded in_progress idempotency record.
        rows = db.execute(
            select(IdempotentCommand).where(
                IdempotentCommand.idempotency_key == "op-fail-once",
                IdempotentCommand.command_type == "campaign.solo_bootstrap",
            )
        ).scalars().all()
        assert rows == []

    # Same-key retry succeeds end to end.
    monkeypatch.setattr(dm_turns, "coordinate_turn", real_coordinate)
    response = _bootstrap(client, campaign["id"], "op-fail-once")
    assert response.status_code == 200, response.text
    assert response.json()["campaign"]["status"] == "active"


def test_solo_bootstrap_revision_race_aborts_cleanly_then_reconverges(api, monkeypatch):
    """Review #360: a revision-conflict loser aborts the whole idempotent
    command (no partial state, no stranded record); retrying the SAME key is
    a fresh command that converges, and a further retry replays it."""
    import app.campaigns.events as campaign_events

    from app.campaigns.events import RevisionConflictError
    from models.campaigns import Campaign
    from models.reliability import IdempotentCommand

    client, factory, actor, owner_id, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    real_commit = campaign_events.commit_campaign_mutation
    calls = {"n": 0}

    def _race_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a concurrent winner committing first.
            raise RevisionConflictError(
                uuid.UUID(campaign["id"]), 0, 1, "op-race",
            )
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(campaign_events, "commit_campaign_mutation", _race_once)
    conflicted = _bootstrap(client, campaign["id"], "op-race")
    assert conflicted.status_code == 409, conflicted.text

    cid = uuid.UUID(campaign["id"])
    with factory() as db:
        # Aborted atomically: still lobby, no artifacts, no stranded record.
        assert db.get(Campaign, cid).status == "lobby"
        assert db.get(CampaignCurrentScene, cid) is None
        assert db.execute(select(DmTurn).where(DmTurn.campaign_id == cid)).scalars().all() == []
        assert db.execute(
            select(IdempotentCommand).where(
                IdempotentCommand.idempotency_key == "op-race",
                IdempotentCommand.command_type == "campaign.solo_bootstrap",
            )
        ).scalars().all() == []

    # Same-key retry converges end to end with exactly one completed record.
    retry = _bootstrap(client, campaign["id"], "op-race")
    assert retry.status_code == 200, retry.text
    assert retry.json()["campaign"]["status"] == "active"
    with factory() as db:
        rows = db.execute(
            select(IdempotentCommand).where(
                IdempotentCommand.idempotency_key == "op-race",
                IdempotentCommand.command_type == "campaign.solo_bootstrap",
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "completed"

    # Third same-key call replays without side effects.
    replay = _bootstrap(client, campaign["id"], "op-race")
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("X-Idempotent-Replay") == "true"
    assert replay.json()["dm_turn"]["id"] == retry.json()["dm_turn"]["id"]
    with factory() as db:
        assert db.scalar(
            select(func.count()).select_from(DmTurn).where(DmTurn.campaign_id == cid)
        ) == 1


def test_solo_bootstrap_ignores_unrelated_lobby_history(api):
    """Review #360: a pre-existing unrelated lobby turn must not suppress
    the bootstrap opening — the returned turn must include it."""
    from app.campaigns.solo_bootstrap import OPENING_OOC_TEXT

    from models.threads import PlayerSubmission

    client, factory, actor, owner_id, _ = api
    campaign = _create(client)
    _ready_lobby(factory, campaign["id"], [owner_id])

    from app.dm.turns import coordinate_turn
    from app.runtime.submissions import accept_submission

    cid = uuid.UUID(campaign["id"])
    with factory() as db:
        thread = db.execute(
            select(CampaignThread).where(CampaignThread.campaign_id == cid)
        ).scalars().first()
        assert thread is not None
        accept_submission(
            db, campaign_id=cid, user_id=owner_id,
            raw_content="Scouting ahead before we begin.",
            segments=[{"type": "ooc", "text": "Scouting ahead before we begin."}],
            thread_id=str(thread.id), audience="campaign",
        )
        db.commit()
        unrelated, _ = coordinate_turn(db, cid, str(thread.id), commit=True)
        unrelated_id = str(unrelated.id)
        db.commit()

    response = _bootstrap(client, campaign["id"], "op-unrelated-history")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign"]["status"] == "active"
    with factory() as db:
        opening = db.execute(
            select(PlayerSubmission).where(
                PlayerSubmission.campaign_id == cid,
                PlayerSubmission.raw_content == OPENING_OOC_TEXT,
            )
        ).scalars().first()
        assert opening is not None
        assert str(opening.id) in (body["dm_turn"]["submission_ids"] or [])
