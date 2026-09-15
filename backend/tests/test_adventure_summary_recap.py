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
from models.campaigns import Adventure, AdventureSummary  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember  # noqa: E402
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


def _make_member(factory, campaign_id: str) -> uuid.UUID:
    """Add a second player member (role=player) to the campaign."""
    uid = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=uid, email=f"player-{uid.hex[:8]}@example.com"))
        db.flush()
        db.add(CampaignMember(campaign_id=uuid.UUID(campaign_id), user_id=uid, role="player"))
        db.commit()
    return uid


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


def test_historical_summary_is_owner_only_while_recap_is_member_visible(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    member = _make_member(factory, camp["id"])
    _seed_events(factory, camp["id"])
    with factory() as db:
        rev = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    adv = _open(client, camp["id"], key="op-open-owneronly")
    _complete(client, camp["id"], adv["id"], rev, "op-complete-owneronly")
    actor["id"] = member
    try:
        denied = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/summary")
        assert denied.status_code == 403, denied.text
        recap = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
        assert recap.status_code == 200, recap.text
        assert "zxqv-secret-phylactery" not in recap.json()["recap_text"]
    finally:
        actor["id"] = owner


def test_recap_is_viewer_scoped_private_actor_event_does_not_cross_viewers(api):
    from app.campaigns.events import commit_campaign_mutation

    client, factory, actor, owner = api
    camp = _campaign(client)
    member_a = _make_member(factory, camp["id"])
    member_b = _make_member(factory, camp["id"])
    cid = uuid.UUID(camp["id"])
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        rev = int(camp_row.revision)
        commit_campaign_mutation(
            db, cid, rev,
            event_type="dm.narration",
            payload={"summary": "the king arrives at dawn"},
            operation_id="seed-king-public", visibility="public",
        )
        commit_campaign_mutation(
            db, cid, rev + 1,
            event_type="dm.secret",
            payload={"summary": "the king is ill"},
            operation_id="seed-king-private", visibility="private",
            actor_id=member_a,
        )
    with factory() as db:
        rev = int(db.get(Campaign, cid).revision)
    adv = _open(client, camp["id"], key="op-open-scope")
    _complete(client, camp["id"], adv["id"], rev, "op-complete-scope")
    # Viewer B (not the private actor): no leak, even though every 4+ char
    # token overlaps public text.
    actor["id"] = member_b
    try:
        recap_b = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
        assert recap_b.status_code == 200, recap_b.text
        assert "ill" not in recap_b.json()["recap_text"]
        assert "arrives at dawn" in recap_b.json()["recap_text"]
    finally:
        actor["id"] = owner
    # Viewer A (the private actor): individualized projection includes it.
    actor["id"] = member_a
    try:
        recap_a = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
        assert recap_a.status_code == 200, recap_a.text
        assert "the king is ill" in recap_a.json()["recap_text"]
    finally:
        actor["id"] = owner


def test_player_members_cannot_perform_dm_declared_mutations(api):
    client, factory, actor, owner = api
    camp = _campaign(client)
    member = _make_member(factory, camp["id"])
    with factory() as db:
        rev0 = int(db.get(Campaign, uuid.UUID(camp["id"])).revision)
    actor["id"] = member
    try:
        assert client.post(
            f"/api/campaigns/{camp['id']}/adventures",
            json={"title": "Sneaky", "operation_id": "op-sneak"},
        ).status_code == 403
    finally:
        actor["id"] = owner
    adv = _open(client, camp["id"], key="op-open-owned")
    actor["id"] = member
    try:
        assert client.post(
            f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/complete",
            json={"expected_revision": rev0, "outcome": "victory", "operation_id": "op-sneak2"},
            headers={"Idempotency-Key": "op-sneak2"},
        ).status_code == 403
        assert client.post(
            f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/summaries/generate",
            json={},
        ).status_code == 403
        assert client.post(
            f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/summaries/mark-stale",
            json={"reason": "x"},
        ).status_code == 403
    finally:
        actor["id"] = owner
    with factory() as db:
        assert int(db.get(Campaign, uuid.UUID(camp["id"])).revision) == rev0
        assert db.get(Adventure, uuid.UUID(adv["id"])).status == "active"


def test_default_source_range_excludes_pre_open_event(api):
    from app.campaigns.events import commit_campaign_mutation

    client, factory, actor, owner = api
    camp = _campaign(client)
    cid = uuid.UUID(camp["id"])
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        commit_campaign_mutation(
            db, cid, int(camp_row.revision),
            event_type="dm.narration",
            payload={"summary": "pre-open happening at the old chapel"},
            operation_id="seed-pre-open", visibility="public",
        )
    # Open WITHOUT explicit start_sequence: defaults to post-cursor boundary.
    r = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Boundary Test", "operation_id": "op-open-boundary"},
    )
    assert r.status_code == 200, r.text
    adv = r.json()["adventure"]
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        rev = int(camp_row.revision)
        commit_campaign_mutation(
            db, cid, rev,
            event_type="dm.narration",
            payload={"summary": "post-open happening at the new chapel"},
            operation_id="seed-post-open", visibility="public",
        )
        rev2 = int(db.get(Campaign, cid).revision)
    out = _complete(client, camp["id"], adv["id"], rev2, "op-complete-boundary")
    historical = out["summary"]["historical_text"] or ""
    assert "post-open happening" in historical
    assert "pre-open happening" not in historical


def test_current_complete_path_also_produces_summary_and_recap(api):
    """Every supported completion path finalizes derived artifacts (#263).

    Adventures completed through the canonical /current/complete endpoint
    (not just the explicit-target endpoint) must bind the end cursor and
    produce an AdventureSummary so Review Adventure stays available.
    """
    client, factory, actor, owner = api
    camp = _campaign(client)
    cid = camp["id"]
    adv = _open(client, cid, key="op-open-current")
    revision = client.get(f"/api/campaigns/{cid}").json()["campaign"]["revision"]
    done = client.post(
        f"/api/campaigns/{cid}/adventures/current/complete",
        json={
            "expected_revision": revision,
            "outcome": "victory",
            "reason": "DM-only: the seal holds.",
            "public_summary": "The Sunken Chapel stands quiet.",
        },
        headers={"Idempotency-Key": "complete-current"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["adventure"]["status"] == "completed"
    recap = client.get(f"/api/campaigns/{cid}/adventures/{adv['id']}/recap")
    assert recap.status_code == 200, recap.text
    body = recap.json()
    assert body["is_derived"] is True
    assert "Sunken Chapel" in body["recap_text"]
    assert "the seal holds" not in body["recap_text"]
    durable = client.get(f"/api/campaigns/{cid}/adventures/{adv['id']}/summary")
    assert durable.status_code == 200, durable.text
    assert durable.json()["summary"]["status"] == "current"


def test_recap_keeps_viewer_authorized_private_tokens(api):
    """Viewer-relative leak validation: an actor's own private content with
    unique 4+ character tokens must survive their own projection while
    staying hidden from other viewers.
    """
    from app.campaigns.events import commit_campaign_mutation

    client, factory, actor, owner = api
    camp = _campaign(client)
    member_a = _make_member(factory, camp["id"])
    member_b = _make_member(factory, camp["id"])
    cid = uuid.UUID(camp["id"])
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        rev = int(camp_row.revision)
        commit_campaign_mutation(
            db, cid, rev,
            event_type="dm.narration",
            payload={"summary": "the king arrives at dawn"},
            operation_id="seed-king-public-2", visibility="public",
        )
        commit_campaign_mutation(
            db, cid, rev + 1,
            event_type="dm.secret",
            payload={"summary": "moonstone sigil revealed to the king"},
            operation_id="seed-moonstone-private", visibility="private",
            actor_id=member_a,
        )
    with factory() as db:
        rev = int(db.get(Campaign, cid).revision)
    adv = _open(client, camp["id"], key="op-open-moonstone")
    _complete(client, camp["id"], adv["id"], rev, "op-complete-moonstone")
    # Viewer B: authorized public text present, private token absent.
    actor["id"] = member_b
    try:
        recap_b = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
        assert recap_b.status_code == 200, recap_b.text
        assert "arrives at dawn" in recap_b.json()["recap_text"]
        assert "moonstone" not in recap_b.json()["recap_text"]
    finally:
        actor["id"] = owner
    # Viewer A (the private actor): their authorized token is NOT stripped.
    actor["id"] = member_a
    try:
        recap_a = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
        assert recap_a.status_code == 200, recap_a.text
        assert "moonstone sigil revealed" in recap_a.json()["recap_text"]
    finally:
        actor["id"] = owner


def test_migration_backfills_legacy_source_bounds():
    """d3a263a1f263 must bind deterministic bounds for pre-existing rows.

    Start backfill applies to every row still at 0 (all legacy by
    construction — the column did not exist before this revision), estimating
    the arc opening from the first event at/after opened-at. End backfill
    binds completed rows to their linked completion event.
    """
    from pathlib import Path

    src = (
        Path(__file__).parent.parent / "alembic" / "versions"
        / "d3a263a1f263_adventure_summary_recap_263.py"
    ).read_text(encoding="utf-8")
    assert "BACKFILL_START_SQL" in src
    assert "BACKFILL_END_SQL" in src
    assert "MIN(e2.sequence)" in src
    assert "e2.created_at >= a.started_at" in src
    assert "WHERE a.start_sequence = 0" in src
    assert "COALESCE(MAX(e3.sequence), 0) + 1" in src
    assert "a.source_event_id = e.id" in src
    assert "a.end_sequence IS NULL" in src


def test_unknown_visibility_matches_event_feed_fail_closed(api):
    """Fail-closed parity: an event with an unrecognized non-public
    visibility is hidden from both the canonical event feed and the recap
    for non-actor members."""
    from app.campaigns.events import commit_campaign_mutation

    client, factory, actor, owner = api
    camp = _campaign(client)
    member = _make_member(factory, camp["id"])
    cid = uuid.UUID(camp["id"])
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        rev = int(camp_row.revision)
        commit_campaign_mutation(
            db, cid, rev,
            event_type="dm.narration",
            payload={"summary": "public happening at the chapel"},
            operation_id="seed-pubvis-1", visibility="public",
        )
        commit_campaign_mutation(
            db, cid, rev + 1,
            event_type="dm.secret",
            payload={"summary": "zxqv-unrecognized-seclusion beneath the chapel"},
            operation_id="seed-pubvis-2", visibility="secret",
        )
    with factory() as db:
        rev = int(db.get(Campaign, cid).revision)
    adv = _open(client, camp["id"], key="op-open-secretvis")
    _complete(client, camp["id"], adv["id"], rev, "op-complete-secretvis")
    actor["id"] = member
    try:
        feed = client.get(f"/api/campaigns/{camp['id']}/events")
        assert feed.status_code == 200, feed.text
        assert "zxqv-unrecognized-seclusion" not in feed.text
        recap = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
        assert recap.status_code == 200, recap.text
        assert "zxqv-unrecognized-seclusion" not in recap.json()["recap_text"]
        assert "public happening" in recap.json()["recap_text"]
    finally:
        actor["id"] = owner


def test_open_cursor_excludes_concurrent_pre_insert_event(api, monkeypatch):
    """A mutation committing between the route's initial campaign read and
    adventure insertion must not enter the new arc: the default cursor
    derives from the locked (repopulated) campaign revision inside
    start_adventure.

    The racing mutation commits through a genuinely separate session so the
    request session holds a stale Campaign in its identity map — the exact
    case the lock + repopulation must defeat.
    """
    import sqlalchemy as sa

    import app.adventures.service as _adventure_service
    from app.campaigns.events import commit_campaign_mutation

    client, factory, actor, owner = api
    camp = _campaign(client)
    cid = uuid.UUID(camp["id"])
    real_start = _adventure_service.start_adventure

    def _racing_start(db, campaign_id, title, **kw):
        with factory() as other:
            camp_row = other.get(Campaign, campaign_id)
            commit_campaign_mutation(
                other, campaign_id, int(camp_row.revision),
                event_type="dm.narration",
                payload={"summary": "racing happening at the gate"},
                operation_id="seed-race-1", visibility="public",
            )
        return real_start(db, campaign_id, title, **kw)

    monkeypatch.setattr(
        "app.adventures.service.start_adventure", _racing_start
    )
    r = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Race arc", "operation_id": "op-open-race"},
    )
    assert r.status_code == 200, r.text
    adv = r.json()["adventure"]
    with factory() as db:
        adv_row = db.get(Adventure, uuid.UUID(adv["id"]))
        racing_seq = db.execute(
            sa.select(sa.func.max(CampaignDomainEvent.sequence)).where(
                CampaignDomainEvent.campaign_id == cid,
                CampaignDomainEvent.operation_id == "seed-race-1",
            )
        ).scalar()
        assert racing_seq is not None
        assert int(adv_row.start_sequence) == int(racing_seq) + 1
    with factory() as db:
        rev = int(db.get(Campaign, cid).revision)
    out = _complete(client, camp["id"], adv["id"], rev, "op-complete-race")
    assert "racing happening" not in (out["summary"]["historical_text"] or "")


def test_public_summary_token_shared_with_hidden_evidence_does_not_fail_generation(api):
    """A deliberately published token overlapping hidden evidence must not
    trip the leak detector: generation stays current, while unrelated
    hidden-only text stays out of the member recap."""
    from app.campaigns.events import commit_campaign_mutation

    client, factory, actor, owner = api
    camp = _campaign(client)
    member = _make_member(factory, camp["id"])
    cid = uuid.UUID(camp["id"])
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        rev = int(camp_row.revision)
        commit_campaign_mutation(
            db, cid, rev,
            event_type="dm.narration",
            payload={"summary": "the party entered the Sunken Chapel"},
            operation_id="seed-chapel-public", visibility="public",
        )
        commit_campaign_mutation(
            db, cid, rev + 1,
            event_type="dm.secret",
            payload={"summary": "moonstone sigil powers the hidden seal"},
            operation_id="seed-moonstone-hidden", visibility="dm_only",
        )
    with factory() as db:
        rev = int(db.get(Campaign, cid).revision)
    adv = _open(client, camp["id"], key="op-open-overlap")
    out = _complete(
        client, camp["id"], adv["id"], rev, "op-complete-overlap",
        outcome_reason="The moonstone was recovered.",
    )
    summary = out["summary"]
    assert summary["status"] == "current", summary.get("error")
    assert summary["leak_failures"] == 0
    actor["id"] = member
    try:
        recap = client.get(f"/api/campaigns/{camp['id']}/adventures/{adv['id']}/recap")
        assert recap.status_code == 200, recap.text
        text = recap.json()["recap_text"]
        assert "moonstone was recovered" in text
        assert "sigil" not in text
        assert "hidden seal" not in text
    finally:
        actor["id"] = owner


def _backfill_sql():
    """Load the migration's exact deploy-time backfill SQL."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "legacy_migration_d3a263a1f263",
        str(
            Path(__file__).parent.parent / "alembic" / "versions"
            / "d3a263a1f263_adventure_summary_recap_263.py"
        ),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BACKFILL_START_SQL, module.BACKFILL_END_SQL


def test_active_legacy_adventure_backfill_excludes_pre_open_events(api):
    """Deploy-time backfill for an already-active legacy adventure.

    Seeds pre-open events, shapes an active adventure like a pre-migration
    row (unknown start cursor, opened after the seeds), executes the
    migration's own backfill SQL, then completes and asserts the derived
    summary excludes pre-open history.
    """
    import sqlalchemy as sa

    from app.campaigns.events import commit_campaign_mutation
    from datetime import datetime, timezone

    client, factory, actor, owner = api
    camp = _campaign(client)
    cid = uuid.UUID(camp["id"])
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        commit_campaign_mutation(
            db, cid, int(camp_row.revision),
            event_type="dm.narration",
            payload={"summary": "ancient happening at the old chapel"},
            operation_id="seed-ancient-1", visibility="public",
        )
        # Age the seed like real legacy history (server defaults only carry
        # second precision, so explicit gaps stand in for deploy-time age).
        ancient = db.execute(
            sa.select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == cid
            ).order_by(CampaignDomainEvent.sequence.asc())
        ).scalars().first()
        ancient.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        db.commit()
    adv = _open(client, camp["id"], key="op-open-legacy-active")
    with factory() as db:
        # Simulate the pre-migration row shape: unknown start cursor, opened
        # after the ancient history.
        adv_row = db.get(Adventure, uuid.UUID(adv["id"]))
        adv_row.start_sequence = 0
        adv_row.started_at = datetime(2021, 1, 1, tzinfo=timezone.utc)
        camp_row = db.get(Campaign, cid)
        commit_campaign_mutation(
            db, cid, int(camp_row.revision),
            event_type="dm.narration",
            payload={"summary": "middle happening at the new chapel"},
            operation_id="seed-middle-1", visibility="public",
        )
        db.commit()
    # Execute the migration's exact deploy-time SQL.
    start_sql, end_sql = _backfill_sql()
    with factory() as db:
        db.execute(sa.text(start_sql))
        db.execute(sa.text(end_sql))
        db.commit()
        assert int(db.get(Adventure, uuid.UUID(adv["id"])).start_sequence) > 0
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        rev = int(camp_row.revision)
        commit_campaign_mutation(
            db, cid, rev,
            event_type="dm.narration",
            payload={"summary": "fresh happening at the far chapel"},
            operation_id="seed-fresh-1", visibility="public",
        )
        rev2 = int(db.get(Campaign, cid).revision)
    out = _complete(client, camp["id"], adv["id"], rev2, "op-complete-legacy-active")
    historical = out["summary"]["historical_text"] or ""
    assert "middle happening" in historical
    assert "fresh happening" in historical
    assert "ancient happening" not in historical


def test_active_legacy_adventure_without_events_starts_at_next_sequence(api):
    """No-event-at-migration edge: an active legacy adventure opened after
    the campaign's latest event must backfill to the next sequence (not 0),
    so post-deploy history stays in scope while pre-open history is out."""
    import sqlalchemy as sa

    from app.campaigns.events import commit_campaign_mutation
    from datetime import datetime, timezone

    client, factory, actor, owner = api
    camp = _campaign(client)
    cid = uuid.UUID(camp["id"])
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        commit_campaign_mutation(
            db, cid, int(camp_row.revision),
            event_type="dm.narration",
            payload={"summary": "elder happening at the old chapel"},
            operation_id="seed-elder-1", visibility="public",
        )
        elder = db.execute(
            sa.select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == cid
            ).order_by(CampaignDomainEvent.sequence.asc())
        ).scalars().first()
        elder.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        db.commit()
    adv = _open(client, camp["id"], key="op-open-legacy-noevent")
    with factory() as db:
        adv_row = db.get(Adventure, uuid.UUID(adv["id"]))
        adv_row.start_sequence = 0
        adv_row.started_at = datetime(2021, 1, 1, tzinfo=timezone.utc)
        db.commit()
    start_sql, end_sql = _backfill_sql()
    with factory() as db:
        db.execute(sa.text(start_sql))
        db.execute(sa.text(end_sql))
        db.commit()
        # No event qualified at deploy time: scope begins at the next
        # sequence (campaign max 1 + 1), never full history.
        assert int(db.get(Adventure, uuid.UUID(adv["id"])).start_sequence) == 2
    with factory() as db:
        camp_row = db.get(Campaign, cid)
        rev = int(camp_row.revision)
        commit_campaign_mutation(
            db, cid, rev,
            event_type="dm.narration",
            payload={"summary": "dawn happening at the far chapel"},
            operation_id="seed-dawn-1", visibility="public",
        )
        rev2 = int(db.get(Campaign, cid).revision)
    out = _complete(client, camp["id"], adv["id"], rev2, "op-complete-legacy-noevent")
    historical = out["summary"]["historical_text"] or ""
    assert "dawn happening" in historical
    assert "elder happening" not in historical


def test_legacy_completed_row_finalizes_from_source_event_not_max(api):
    """Repair of a legacy completed row (no end cursor) must not absorb
    post-completion events: the end binds to its own completion event."""
    from app.adventures.service import finalize_adventure_derived
    from app.campaigns.events import commit_campaign_mutation

    client, factory, actor, owner = api
    camp = _campaign(client)
    cid = uuid.UUID(camp["id"])
    _seed_events(factory, camp["id"])
    with factory() as db:
        rev = int(db.get(Campaign, cid).revision)
    adv = _open(client, camp["id"], key="op-open-legacy")
    _complete(client, camp["id"], adv["id"], rev, "op-complete-legacy")
    with factory() as db:
        adv_row = db.get(Adventure, uuid.UUID(adv["id"]))
        completion_seq = int(adv_row.end_sequence)
        # Simulate a pre-migration row: bounds never bound...
        adv_row.end_sequence = None
        adv_row.end_revision = None
        # ...and the campaign continued afterwards.
        camp_row = db.get(Campaign, cid)
        commit_campaign_mutation(
            db, cid, int(camp_row.revision),
            event_type="campaign.continued",
            payload={"summary": "later happenings at the far gate"},
            operation_id="seed-far-gate", visibility="public",
        )
        db.commit()
    with factory() as db:
        adv_row = db.get(Adventure, uuid.UUID(adv["id"]))
        row = finalize_adventure_derived(db, adv_row)
        db.commit()
        assert int(adv_row.end_sequence) == completion_seq
        assert int(adv_row.end_revision) == completion_seq
        assert row is not None and row.status == "current"
        assert "far gate" not in (row.historical_text or "")
