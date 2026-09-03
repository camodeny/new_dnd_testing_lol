"""Issue #194 — durable ordered mixed IC/OOC player submissions."""

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

from app.auth.service import MOCK_USER_ID  # noqa: E402
from app.runtime.submissions import SubmissionValidationError, parse_tagged_content  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import Campaign
from models.campaigns import CampaignMember
from models.profiles import Profile
from models.reliability import IdempotentCommand
from models.threads import PlayerSubmission
from models.threads import PlayerSubmissionSegment


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    with factory() as db:
        db.add_all([
            Profile(id=MOCK_USER_ID, email="player@example.com"),
            Profile(id=outsider_id, email="outsider@example.com"),
            Campaign(id=campaign_id, owner_id=MOCK_USER_ID, name="Table"),
            CampaignMember(campaign_id=campaign_id, user_id=MOCK_USER_ID, role="owner"),
        ])
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "test")
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, campaign_id
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("kind", ["ic", "ooc"])
def test_accepts_single_segment_types(api, kind):
    client, _, campaign_id = api
    body = {"content": "Exact wording", "segments": [{"type": kind, "text": "Exact wording"}]}
    response = client.post(
        f"/api/campaigns/{campaign_id}/submissions",
        json=body,
        headers={"Idempotency-Key": f"single-{kind}"},
    )
    assert response.status_code == 201
    submission = response.json()["submission"]
    assert submission["raw_content"] == "Exact wording"
    assert submission["segments"] == [{"position": 0, "type": kind, "text": "Exact wording"}]


@pytest.mark.parametrize(
    "segments",
    [
        [{"type": "ooc", "text": "Question: "}, {"type": "ic", "text": "I open it."}],
        [{"type": "ic", "text": "Stop!"}, {"type": "ooc", "text": "I use Persuasion."}],
    ],
)
def test_mixed_segments_preserve_order_and_semantics(api, segments):
    client, _, campaign_id = api
    response = client.post(
        f"/api/campaigns/{campaign_id}/submissions",
        json={"content": "raw original", "segments": segments},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201
    stored = response.json()["submission"]["segments"]
    assert [(part["type"], part["text"]) for part in stored] == [
        (part["type"], part["text"]) for part in segments
    ]
    assert [part["position"] for part in stored] == list(range(len(segments)))


def test_duplicate_retry_and_snapshot_recover_lost_ack(api):
    client, factory, campaign_id = api
    body = {
        "content": "<ooc>Rules?</ooc><ic>I try the door.</ic>",
    }
    headers = {"Idempotency-Key": "lost-ack-key"}
    first = client.post(f"/api/campaigns/{campaign_id}/submissions", json=body, headers=headers)
    retry = client.post(f"/api/campaigns/{campaign_id}/submissions", json=body, headers=headers)
    snapshot = client.get(f"/api/campaigns/{campaign_id}/submissions")

    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json()
    assert first.headers["X-Idempotent-Replay"] == "false"
    assert retry.headers["X-Idempotent-Replay"] == "true"
    assert snapshot.status_code == 200
    assert snapshot.json()["submissions"] == [first.json()["submission"]]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PlayerSubmission)) == 1
        assert db.scalar(select(func.count()).select_from(PlayerSubmissionSegment)) == 2
        assert db.scalar(select(func.count()).select_from(IdempotentCommand)) == 1


def test_submission_sequence_is_deterministic(api):
    client, _, campaign_id = api
    for index in range(3):
        response = client.post(
            f"/api/campaigns/{campaign_id}/submissions",
            json={"content": f"message {index}"},
            headers={"Idempotency-Key": f"ordered-{index}"},
        )
        assert response.json()["submission"]["sequence"] == index + 1


def test_malformed_tags_are_actionable_and_store_nothing(api):
    client, factory, campaign_id = api
    response = client.post(
        f"/api/campaigns/{campaign_id}/submissions",
        json={"content": "<ic>Unclosed"},
        headers={"Idempotency-Key": "malformed"},
    )
    assert response.status_code == 422
    assert "matched <ic>...</ic>" in response.json()["detail"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PlayerSubmission)) == 0
        assert db.scalar(select(func.count()).select_from(IdempotentCommand)) == 0


def test_server_rejects_unowned_thread_and_audience_claims(api):
    client, _, campaign_id = api
    response = client.post(
        f"/api/campaigns/{campaign_id}/submissions",
        json={"content": "secret", "thread_id": "private-anyone", "audience": "private"},
        headers={"Idempotency-Key": "forged-thread"},
    )
    # #195: forged/unauthorized threads fail closed and are hidden as 404
    assert response.status_code == 404


def test_non_member_cannot_submit(monkeypatch, api):
    client, factory, campaign_id = api
    outsider = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=outsider, email="second-outsider@example.com"))
        db.commit()

    monkeypatch.setattr("app.runtime.router.resolve_profile", lambda request, db: db.get(Profile, outsider))
    response = client.post(
        f"/api/campaigns/{campaign_id}/submissions",
        json={"content": "intrusion"},
        headers={"Idempotency-Key": "unauthorized"},
    )
    assert response.status_code == 403


def test_tag_parser_does_not_flatten_outside_text_into_ic():
    assert parse_tagged_content("table talk <ic>Character words</ic> intent") == [
        {"type": "ooc", "text": "table talk "},
        {"type": "ic", "text": "Character words"},
        {"type": "ooc", "text": " intent"},
    ]
    with pytest.raises(SubmissionValidationError):
        parse_tagged_content("<ooc>broken</ic>")
    with pytest.raises(SubmissionValidationError):
        parse_tagged_content("<ic>fiction <ooc>table talk</ooc></ic>")
