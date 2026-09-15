"""Issue #263 — durable adventure summary + player-facing recap.

Covers: normal recap, private-event omission, failed generation, retry,
repair-driven regeneration, continued-campaign review, and
summary-vs-authority precedence.
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
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.adventures import Adventure, AdventureSummary  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.profiles import Profile  # noqa: E402

import models as _models  # noqa: E402,F401  # register all tables


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = TEST_USER_ID
    with factory() as db:
        db.add(Profile(id=owner_id, email="owner@example.com"))
        db.commit()

    actor = {"id": owner_id}

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setattr(
        "app.adventures.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    monkeypatch.setattr(
        "app.campaigns.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id
    finally:
        app.dependency_overrides.clear()


def _campaign(client: TestClient) -> dict:
    r = client.post("/api/campaigns", json={"name": "Recap Campaign"})
    assert r.status_code == 200, r.text
    return r.json()["campaign"]


def _seed_events(factory, campaign_id: str, revision_start: int = 0):
    """Seed one public + one hidden event via authoritative mutations."""
    from app.campaigns.events import commit_campaign_mutation

    cid = uuid.UUID(campaign_id)
    with factory() as db:
        camp = db.get(Campaign, cid)
        rev = int(camp.revision)
        commit_campaign_mutation(
            db, cid, rev,
            event_type="dm.narration",
            payload={"summary": "The party bravely stormed the Sunken Chapel"},
            operation_id="seed-public-1",
            visibility="public",
        )
        commit_campaign_mutation(
            db, cid, rev + 1,
            event_type="dm.secret",
            payload={"summary": "zxqv-secret-phylactery hidden beneath the altar"},
            operation_id="seed-hidden-1",
            visibility="dm_only",
        )
    with factory() as db:
        return int(db.get(Campaign, cid).revision)


def _open(client: TestClient, cid: str, key: str = "op-open-1") -> dict:
    r = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "The Sunken Chapel", "operation_id": key, "start_sequence": 0},
    )
    assert r.status_code == 200, r.text
    return r.json()["adventure"]


def _complete(client: TestClient, cid: str, aid: str, rev: int, key: str, **kw) -> dict:
    body = {"expected_revision": rev, "outcome": "victory",
            "outcome_reason": "The chapel was reclaimed.", "operation_id": key}
    body.update(kw)
    r = client.post(
        f"/api/campaigns/{cid}/adventures/{aid}/complete", json=body,
        headers={"Idempotency-Key": key},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_normal_completion_produces_derived_summary_with_source_range(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    _seed_events(factory, camp["id"])
    with factory() as db:
        rev = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    adv = _open(client, camp["id"])
    out = _complete(client, camp["id"], adv["id"], rev, "op-complete-1")
    summary = out["summary"]
    assert summary["is_derived"] is True
    assert summary["status"] == "current"
    assert summary["source_event_from"] == 0
    assert summary["source_event_to"] is not None and summary["source_event_to"] >= 1
    assert summary["source_revision"] is not None
    assert "events/facts" in summary["authority"]
    assert "Sunken Chapel" in (summary["historical_text"] or "")
    recap = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
    assert recap.status_code == 200, recap.text
    body = recap.json()
    assert "stormed the Sunken Chapel" in body["recap_text"]
    assert body["is_derived"] is True


def test_private_event_omitted_from_recap_but_kept_in_historical(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    _seed_events(factory, camp["id"])
    with factory() as db:
        rev = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    adv = _open(client, camp["id"])
    out = _complete(client, camp["id"], adv["id"], rev, "op-complete-2")
    summary = out["summary"]
    assert "zxqv-secret-phylactery" in (summary["historical_text"] or "")
    recap = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
    assert recap.status_code == 200, recap.text
    assert "zxqv-secret-phylactery" not in recap.json()["recap_text"]


def test_generation_failure_does_not_invalidate_completion_and_retry_recovers(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    _seed_events(factory, camp["id"])
    with factory() as db:
        rev = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    adv = _open(client, camp["id"], key="op-open-fail")
    out = _complete(client, camp["id"], adv["id"], rev, "op-complete-fail")
    assert out["adventure"]["status"] == "completed"
    # Force a failed regeneration: prior artifact goes to failed, stays completed.
    r = client.post(
        f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/summaries/generate",
        json={"force_fail": True},
    )
    assert r.status_code == 200, r.text
    failed = r.json()["summary"]
    assert failed["status"] == "failed"
    assert failed["attempts"] >= 2
    assert failed["error"]
    with factory() as db:
        assert db.get(Adventure, uuid.UUID(adv["id"])).status == "completed"
    # Retry succeeds and rebuilds.
    r2 = client.post(
        f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/summaries/generate",
        json={},
    )
    assert r2.status_code == 200, r2.text
    recovered = r2.json()["summary"]
    assert recovered["status"] == "current"
    assert recovered["rebuild_count"] >= 1


def test_repair_marks_stale_and_regeneration_bumps_version(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    _seed_events(factory, camp["id"])
    with factory() as db:
        rev = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    adv = _open(client, camp["id"], key="op-open-stale")
    out = _complete(client, camp["id"], adv["id"], rev, "op-complete-stale")
    v0 = out["summary"]["version"]
    r = client.post(
        f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/summaries/mark-stale",
        json={"reason": "retcon: corrected chapel outcome"},
    )
    assert r.status_code == 200, r.text
    stale = r.json()["summary"]
    assert stale["status"] == "stale"
    assert stale["stale_count"] >= 1
    # Stale recap stays available with a warning, never silently current.
    recap = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
    assert recap.status_code == 200, recap.text
    assert recap.json()["stale_warning"]
    assert "zxqv-secret-phylactery" not in recap.json()["recap_text"]
    # Regenerate after repair.
    r2 = client.post(
        f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/summaries/mark-stale",
        json={"reason": "retcon", "regenerate": True},
    )
    assert r2.status_code == 200, r2.text
    rebuilt = r2.json()["summary"]
    assert rebuilt["status"] == "current"
    assert rebuilt["version"] == v0 + 1
    assert rebuilt["rebuild_count"] >= 1


def test_review_available_after_continuation_and_summary_never_overrides_authority(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    _seed_events(factory, camp["id"])
    with factory() as db:
        rev = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    adv = _open(client, camp["id"], key="op-open-cont")
    _complete(client, camp["id"], adv["id"], rev, "op-complete-cont", outcome="tpk",
              outcome_reason="The party fell; the world endures.")
    # Campaign continues: more authoritative events after completion.
    from app.campaigns.events import commit_campaign_mutation

    with factory() as db:
        camp_row = db.get(Campaign, uuid.UUID(camp["id"]))
        commit_campaign_mutation(
            db, camp_row.id, int(camp_row.revision),
            event_type="campaign.continued",
            payload={"summary": "A new party gathers at the chapel gate"},
            operation_id="seed-continued-1", visibility="public",
        )
    # Review remains available after continuation.
    recap = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
    assert recap.status_code == 200, recap.text
    assert "Sunken Chapel" in recap.json()["recap_text"]
    durable = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/summary")
    assert durable.status_code == 200, durable.text
    s = durable.json()["summary"]
    assert s["is_derived"] is True
    # Authority precedence: source events are unchanged by derived prose.
    with factory() as db:
        rows = db.execute(select(AdventureSummary)).scalars().all()
        assert len(rows) == 1 and bool(rows[0].is_derived) is True
        from models.campaigns import CampaignDomainEvent

        evs = db.execute(
            select(CampaignDomainEvent)
            .where(CampaignDomainEvent.campaign_id == uuid.UUID(camp["id"]))
            .order_by(CampaignDomainEvent.sequence.asc())
        ).scalars().all()
        assert any("zxqv-secret-phylactery" in str(e.payload) for e in evs)
        assert not any(e.event_type == "adventure.summary" for e in evs)


def test_duplicate_completion_is_idempotent(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    with factory() as db:
        rev = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    adv = _open(client, camp["id"], key="op-open-dup")
    first = _complete(client, camp["id"], adv["id"], rev, "op-complete-dup")
    second = _complete(client, camp["id"], adv["id"], rev + 1, "op-complete-dup")
    assert second["adventure"]["status"] == "completed"
    assert second.get("idempotent") is True
    with factory() as db:
        rows = db.execute(
            select(Adventure).where(Adventure.campaign_id == uuid.UUID(camp["id"]))
        ).scalars().all()
        assert len(rows) == 1
    assert first["summary"]["id"] == second["summary"]["id"]


def test_villain_victory_and_outcome_validation(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    with factory() as db:
        rev = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    adv = _open(client, camp["id"], key="op-open-vv")
    out = _complete(client, camp["id"], adv["id"], rev, "op-complete-vv",
                    outcome="villain_victory")
    assert out["adventure"]["outcome"] == "villain_victory"
    adv2 = _open(client, camp["id"], key="op-open-bad")
    r = client.post(
        f"/api/campaigns/{camp['id']}/adventures/{adv2['id']}/complete",
        json={"expected_revision": rev + 1, "outcome": "ascension", "operation_id": "op-bad"},
        headers={"Idempotency-Key": "op-bad"},
    )
    assert r.status_code == 400
