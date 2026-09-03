"""Issue #204 — durable player-owned roll request and fulfillment lifecycle."""
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

from app.auth.service import MOCK_USER_ID  # noqa: E402
from app.dm.turns import coordinate_turn, mark_streaming_started  # noqa: E402
from app.rolls.service import RollAuthorizationError, fulfill_roll  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Campaign, CampaignMember, Character, DmTurn, DmTurnAttempt,
    PlayerRollFulfillment, PlayerRollRequest, Profile,
)


@pytest.fixture
def roll_api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner = MOCK_USER_ID
    player = uuid.uuid4()
    outsider = uuid.uuid4()
    campaign_id = uuid.uuid4()
    owner_character = uuid.uuid4()
    player_character = uuid.uuid4()
    with factory() as db:
        db.add_all([
            Profile(id=owner, email="owner@example.com"), Profile(id=player, email="player@example.com"),
            Profile(id=outsider, email="outsider@example.com"),
            Campaign(id=campaign_id, owner_id=owner, name="Roll Table", revision=0),
            CampaignMember(campaign_id=campaign_id, user_id=owner, role="owner"),
            CampaignMember(campaign_id=campaign_id, user_id=player, role="player"),
            Character(id=owner_character, owner_id=owner, name="Owner PC", system="dnd5e"),
            Character(id=player_character, owner_id=player, name="Player PC", system="dnd5e"),
        ])
        db.commit()
        thread = get_or_create_campaign_thread(db, campaign_id, created_by=owner)
        submission = accept_submission(
            db, campaign_id=campaign_id, user_id=owner, character_id=owner_character,
            raw_content="I inspect the door", segments=[{"type": "ic", "text": "I inspect the door"}],
            thread_id=str(thread.id),
        )
        db.commit()
        turn, attempt = coordinate_turn(db, campaign_id, str(thread.id))

    def override_db():
        with factory() as db:
            yield db

    def resolve_test_profile(request, db):
        raw = request.headers.get("X-Test-User")
        return db.get(Profile, uuid.UUID(raw)) if raw else db.get(Profile, owner)

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setattr("app.rolls.router.resolve_profile", resolve_test_profile)
    monkeypatch.setattr("app.snapshot.router.resolve_profile", resolve_test_profile)
    app.dependency_overrides[get_db] = override_db
    try:
        yield {
            "client": TestClient(app), "factory": factory, "campaign_id": campaign_id, "thread_id": str(thread.id),
            "owner": owner, "player": player, "outsider": outsider, "owner_character": owner_character,
            "player_character": player_character, "turn_id": turn.id, "attempt_id": attempt.id,
        }
    finally:
        app.dependency_overrides.clear()


def request_payload(ctx, *, two=False, private_dc=17):
    requests = [{
        "request_key": "owner-check", "requested_user_id": str(ctx["owner"]),
        "character_id": str(ctx["owner_character"]), "roll_kind": "check",
        "ability_or_skill": "Investigation", "label": "Investigation check",
        "advantage_state": "normal", "reason_public": "Inspect the sealed door", "dc_private": private_dc,
    }]
    if two:
        requests.append({
            "request_key": "player-save", "requested_user_id": str(ctx["player"]),
            "character_id": str(ctx["player_character"]), "roll_kind": "save",
            "ability_or_skill": "Dexterity", "label": "Dexterity save",
            "advantage_state": "advantage", "reason_public": "Avoid falling debris", "dc_private": 15,
        })
    return {"attempt_id": str(ctx["attempt_id"]), "requests": requests}


def create_requests(ctx, *, two=False):
    response = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/dm-turns/{ctx["turn_id"]}/roll-requests',
        json=request_payload(ctx, two=two), headers={"Idempotency-Key": "request-rolls"},
    )
    assert response.status_code == 201, response.text
    return response


def test_normal_roll_duplicate_retry_resumes_same_logical_turn(roll_api):
    ctx = roll_api
    create = create_requests(ctx)
    roll_id = create.json()["roll_requests"][0]["id"]
    body = {"source": "app", "raw_rolls": [14], "modifier": 3, "total": 17, "visibility": "public"}
    first = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{roll_id}/fulfill', json=body,
        headers={"Idempotency-Key": "fulfill-owner"},
    )
    assert first.status_code == 200, first.text
    assert first.headers["X-Idempotent-Replay"] == "false"
    resumed = first.json()["resumed_attempt"]
    assert resumed["turn_id"] == str(ctx["turn_id"])
    assert resumed["roll_evidence"][0]["fulfillment"]["total"] == 17
    retry = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{roll_id}/fulfill', json=body,
        headers={"Idempotency-Key": "fulfill-owner"},
    )
    assert retry.status_code == 200
    assert retry.headers["X-Idempotent-Replay"] == "true"
    assert retry.json() == first.json()
    with ctx["factory"]() as db:
        assert len(db.execute(select(PlayerRollFulfillment)).scalars().all()) == 1
        turn = db.get(DmTurn, ctx["turn_id"])
        assert turn.status == "pending"
        assert turn.current_attempt_id != ctx["attempt_id"]


def test_multiple_players_remain_independently_pending_and_authorized(roll_api):
    ctx = roll_api
    rows = create_requests(ctx, two=True).json()["roll_requests"]
    owner_req, player_req = rows
    owner_result = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{owner_req["id"]}/fulfill',
        json={"source": "physical", "raw_rolls": [12], "modifier": 2, "total": 14},
        headers={"Idempotency-Key": "owner-physical"},
    )
    assert owner_result.status_code == 200
    assert owner_result.json()["resumed_attempt"] is None
    unauthorized = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{player_req["id"]}/fulfill',
        json={"source": "app", "raw_rolls": [18, 7], "modifier": 1, "total": 19},
        headers={"Idempotency-Key": "wrong-player"},
    )
    assert unauthorized.status_code == 403
    player_result = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{player_req["id"]}/fulfill',
        json={"source": "app", "raw_rolls": [18, 7], "modifier": 1, "total": 19, "visibility": "private"},
        headers={"Idempotency-Key": "right-player", "X-Test-User": str(ctx["player"])},
    )
    assert player_result.status_code == 200, player_result.text
    assert player_result.json()["resumed_attempt"]["turn_id"] == str(ctx["turn_id"])


def test_snapshot_survives_reconnect_without_leaking_private_dc_or_result(roll_api):
    ctx = roll_api
    rows = create_requests(ctx, two=True).json()["roll_requests"]
    player_req = rows[1]
    pending_snapshot = ctx["client"].get(
        f'/api/campaigns/{ctx["campaign_id"]}/snapshot', headers={"X-Test-User": str(ctx["player"])}
    )
    assert pending_snapshot.status_code == 200
    encoded = json.dumps(pending_snapshot.json())
    assert "dc_private" not in encoded
    assert any(row["id"] == player_req["id"] and row["status"] == "pending" for row in pending_snapshot.json()["roll_requests"])
    result = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{player_req["id"]}/fulfill',
        json={"source": "physical", "raw_rolls": [], "modifier": 4, "total": 11, "visibility": "private"},
        headers={"Idempotency-Key": "physical-private", "X-Test-User": str(ctx["player"])},
    )
    assert result.status_code == 200
    player_reconnect = ctx["client"].get(
        f'/api/campaigns/{ctx["campaign_id"]}/snapshot', headers={"X-Test-User": str(ctx["player"])}
    ).json()
    own = next(row for row in player_reconnect["roll_requests"] if row["id"] == player_req["id"])
    assert own["fulfillment"]["total"] == 11


def test_cancel_and_replace_are_durable_and_final_submission_cannot_change(roll_api):
    ctx = roll_api
    row = create_requests(ctx).json()["roll_requests"][0]
    replacement = request_payload(ctx)["requests"][0] | {"request_key": "owner-check-replacement", "advantage_state": "advantage"}
    changed = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{row["id"]}/cancel',
        json={"replacement": replacement}, headers={"Idempotency-Key": "replace-roll"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["roll_request"]["status"] == "replaced"
    replacement_id = changed.json()["replacement"]["id"]
    fulfilled = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{replacement_id}/fulfill',
        json={"source": "physical", "raw_rolls": [20, 5], "modifier": 0, "total": 20},
        headers={"Idempotency-Key": "replacement-result"},
    )
    assert fulfilled.status_code == 200
    cannot_cancel = ctx["client"].post(
        f'/api/campaigns/{ctx["campaign_id"]}/roll-requests/{replacement_id}/cancel',
        json={}, headers={"Idempotency-Key": "too-late"},
    )
    assert cannot_cancel.status_code == 409


def test_pending_roll_prohibits_streaming_outcome(roll_api):
    ctx = roll_api
    create_requests(ctx)
    with ctx["factory"]() as db:
        with pytest.raises(ValueError, match="pending player-owned rolls"):
            mark_streaming_started(db, ctx["turn_id"], ctx["attempt_id"])


def test_service_rejects_other_human_without_mutating_request(roll_api):
    ctx = roll_api
    row = create_requests(ctx, two=True).json()["roll_requests"][1]
    with ctx["factory"]() as db:
        with pytest.raises(RollAuthorizationError):
            fulfill_roll(db, request_id=uuid.UUID(row["id"]), actor_id=ctx["outsider"], payload={
                "source": "physical", "raw_rolls": [10], "modifier": 0, "total": 10,
            })
        db.rollback()
    with ctx["factory"]() as db:
        assert db.get(PlayerRollRequest, uuid.UUID(row["id"])).status == "pending"
