"""Campaign deletion retains durable data and hides the campaign from users."""
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
from models.campaigns import Campaign, CampaignDomainEvent  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Profile(id=TEST_USER_ID, email="owner@example.com"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setattr(
        "app.campaigns.router.resolve_profile",
        lambda request, db: db.get(Profile, TEST_USER_ID),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient) -> dict:
    response = client.post("/api/campaigns", json={"name": "Retained campaign"})
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def test_delete_retains_data_and_hides_campaign_from_frontend(api):
    client, factory = api
    campaign = _create(client)
    campaign_id = uuid.UUID(campaign["id"])
    with factory() as db:
        row = db.get(Campaign, campaign_id)
        row.revision = 1
        db.add(CampaignDomainEvent(
            campaign_id=campaign_id,
            sequence=1,
            event_type="campaign.test_event",
            actor_id=TEST_USER_ID,
        ))
        db.commit()

    deleted = client.delete(f"/api/campaigns/{campaign_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}
    assert client.delete(f"/api/campaigns/{campaign_id}").status_code == 200

    assert str(campaign_id) not in {
        row["id"] for row in client.get("/api/campaigns").json()["campaigns"]
    }
    assert str(campaign_id) not in {
        row["id"] for row in client.get(
            "/api/campaigns", params={"include_archived": "true"}
        ).json()["campaigns"]
    }
    assert client.get(f"/api/campaigns/{campaign_id}").status_code == 404

    with factory() as db:
        retained = db.get(Campaign, campaign_id)
        assert retained is not None and retained.is_deleted is True
        assert db.scalar(
            select(func.count()).select_from(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == campaign_id
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(CampaignThread).where(
                CampaignThread.campaign_id == campaign_id
            )
        ) == 2


def test_deleted_campaign_invite_cannot_be_looked_up_or_accepted(api):
    client, _ = api
    campaign = _create(client)
    invite_response = client.post(f"/api/campaigns/{campaign['id']}/invites")
    assert invite_response.status_code == 200, invite_response.text
    code = invite_response.json()["code"]

    assert client.delete(f"/api/campaigns/{campaign['id']}").status_code == 200
    lookup = client.get("/api/invites/lookup", params={"code": code})
    accept = client.post("/api/invites/accept", json={"code": code})
    assert lookup.status_code == 404
    assert accept.status_code == 404
