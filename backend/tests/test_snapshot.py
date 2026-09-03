"""Issue #196 — authoritative live-table snapshot / reconnect-safe read model."""
import base64
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.deps.auth import MOCK_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import Campaign
from models.campaigns import CampaignMember
from models.profiles import Profile
from models.threads import CampaignThread
from models.threads import PlayerSubmission


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id = uuid.uuid4()
    member_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    owner_id = MOCK_USER_ID
    with factory() as db:
        # Use router's campaign creation path to ensure shared thread exists?
        # For this fixture we insert directly and also create shared thread,
        # mirroring the new campaign creation behavior.
        db.add_all(
            [
                Profile(id=owner_id, email="owner@example.com"),
                Profile(id=member_id, email="member@example.com"),
                Profile(id=outsider_id, email="outsider@example.com"),
                Campaign(id=campaign_id, owner_id=owner_id, name="Table", revision=0),
                CampaignMember(campaign_id=campaign_id, user_id=owner_id, role="owner"),
                CampaignMember(campaign_id=campaign_id, user_id=member_id, role="player"),
                CampaignMember(campaign_id=campaign_id, user_id=outsider_id, role="player"),
            ]
        )
        db.flush()
        db.add(
            CampaignThread(
                id=uuid.uuid4(),
                campaign_id=campaign_id,
                thread_type="campaign",
                title="Campaign",
                created_by=owner_id,
            )
        )
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "test")
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, campaign_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def api_via_router(monkeypatch):
    """Fixture that creates campaign via HTTP so shared-thread-on-creation is exercised."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Profile(id=MOCK_USER_ID, email="owner@example.com"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "test")
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        r = client.post("/api/campaigns", json={"name": "ViaRouter"})
        assert r.status_code == 200, r.text
        campaign_id = uuid.UUID(r.json()["campaign"]["id"])
        yield client, factory, campaign_id
    finally:
        app.dependency_overrides.clear()


def test_campaign_creation_creates_shared_thread(api_via_router):
    client, factory, campaign_id = api_via_router
    r = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r.status_code == 200
    snap = r.json()
    assert snap["active_thread"]["thread_type"] == "campaign"
    assert len(snap["threads"]) == 1
    # durable across reconnect
    r2 = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r2.json()["active_thread_id"] == snap["active_thread_id"]
    with factory() as db:
        count = db.scalar(select(func.count()).select_from(CampaignThread).where(CampaignThread.campaign_id == campaign_id))
        assert count == 1


def test_snapshot_reconstructs_current_state_after_disconnected_mutations(api):
    client, factory, campaign_id, member_id, _ = api
    from app.campaigns.events import commit_campaign_mutation

    # Initial snapshot: revision 0, no messages
    r0 = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r0.status_code == 200
    assert r0.json()["revision"] == 0
    assert r0.json()["history"]["messages"] == []

    # Simulate disconnection: mutate from another client (different session)
    with factory() as db:
        camp, evt = commit_campaign_mutation(
            db, campaign_id, expected_revision=0, event_type="campaign.test", operation_id="op1", actor_id=MOCK_USER_ID
        )
        db.commit()
        assert evt.sequence == 1

    # Another client posts a submission while disconnected
    r1 = client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": "hello"}, headers={"Idempotency-Key": "h1"})
    assert r1.status_code == 201

    with factory() as db:
        camp, evt = commit_campaign_mutation(
            db, campaign_id, expected_revision=1, event_type="campaign.test2", operation_id="op2", actor_id=MOCK_USER_ID
        )
        db.commit()
        assert evt.sequence == 2

    client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": "second"}, headers={"Idempotency-Key": "h2"})

    # Reconnect: snapshot must reflect exact current authorized state
    r = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r.status_code == 200
    snap = r.json()
    assert snap["revision"] == 2
    assert snap["reconciliation"]["snapshot_revision"] == 2
    assert snap["reconciliation"]["snapshot_sequence"] == 2
    assert [m["raw_content"] for m in snap["history"]["messages"]] == ["hello", "second"]
    assert snap["history"]["pagination"]["total_visible"] == 2
    # Realtime resume token encodes revision + high water
    assert snap["reconciliation"]["realtime_resume_token"] == "2:2"
    assert r.headers["X-Snapshot-Revision"] == "2"
    assert r.headers["X-Realtime-Resume-Token"] == "2:2"


def test_snapshot_includes_revision_for_reconciliation(api):
    client, factory, campaign_id, _, _ = api
    from app.campaigns.events import commit_campaign_mutation

    r = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r.json()["revision"] == 0
    assert r.json()["reconciliation"]["snapshot_revision"] == 0
    assert "realtime_resume_token" in r.json()["reconciliation"]

    with factory() as db:
        commit_campaign_mutation(db, campaign_id, expected_revision=0, event_type="campaign.bump", operation_id="b1")
        db.commit()
    r2 = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r2.json()["revision"] == 1
    assert r2.headers["X-Snapshot-Revision"] == "1"


def test_snapshot_pagination_does_not_force_full_transcript(api):
    client, factory, campaign_id, _, _ = api
    # Create 65 messages
    for i in range(65):
        client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": f"msg {i}"}, headers={"Idempotency-Key": f"p{i}"})
    # First page default limit 50
    r1 = client.get(f"/api/campaigns/{campaign_id}/snapshot?limit=50")
    assert r1.status_code == 200
    j1 = r1.json()
    assert len(j1["history"]["messages"]) == 50
    assert j1["history"]["pagination"]["has_more"] is True
    assert j1["history"]["pagination"]["next_cursor"] is not None
    assert j1["history"]["pagination"]["total_visible"] == 65
    # Cursor should be base64 of oldest sequence in page1 window
    cursor = j1["history"]["pagination"]["next_cursor"]
    # Decode to verify it points to sequence 16 (since latest 50 are 16..65)
    padded = cursor + "=" * (-len(cursor) % 4)
    decoded = int(base64.urlsafe_b64decode(padded.encode()).decode())
    assert decoded == 16
    # Fetch older page
    r2 = client.get(f"/api/campaigns/{campaign_id}/snapshot?limit=50&cursor={cursor}")
    j2 = r2.json()
    assert len(j2["history"]["messages"]) == 15  # 1..15
    assert j2["history"]["pagination"]["has_more"] is False
    assert j2["history"]["pagination"]["next_cursor"] is None
    # All messages are reachable via pagination, not forced into single response
    combined = j2["history"]["messages"] + j1["history"]["messages"]
    assert len(combined) == 65
    assert [m["sequence"] for m in combined] == list(range(1, 66))

    # limit validation
    r_bad = client.get(f"/api/campaigns/{campaign_id}/snapshot?limit=500")
    assert r_bad.status_code == 422
    r_bad2 = client.get(f"/api/campaigns/{campaign_id}/snapshot?cursor=!!!")
    assert r_bad2.status_code == 422


def test_snapshot_omits_private_data_server_side(api, monkeypatch):
    client, factory, campaign_id, member_id, outsider_id = api
    from app.runtime.threads import create_private_thread
    from app.runtime.submissions import accept_submission

    with factory() as db:
        t = create_private_thread(db, campaign_id=campaign_id, created_by=MOCK_USER_ID, member_ids=[member_id], title="secret")
        private_id = str(t.id)
        shared = db.scalar(select(CampaignThread).where(CampaignThread.campaign_id == campaign_id, CampaignThread.thread_type == "campaign"))
        shared_id = str(shared.id)
        accept_submission(db, campaign_id=campaign_id, user_id=MOCK_USER_ID, raw_content="shared msg", segments=[{"type": "ooc", "text": "shared msg"}], thread_id=shared_id, audience="campaign")
        accept_submission(db, campaign_id=campaign_id, user_id=MOCK_USER_ID, raw_content="private secret", segments=[{"type": "ooc", "text": "private secret"}], thread_id=private_id, audience="private")
        db.commit()

    # Owner snapshot of shared thread must not leak private msg
    r_shared = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert all("private secret" not in m["raw_content"] for m in r_shared.json()["history"]["messages"])
    # Owner can read private thread explicitly
    r_priv = client.get(f"/api/campaigns/{campaign_id}/snapshot?thread_id={private_id}")
    assert r_priv.status_code == 200, r_priv.text
    assert any(m["raw_content"] == "private secret" for m in r_priv.json()["history"]["messages"])
    # Threads list for outsider (campaign member but not private member) must not leak private thread
    monkeypatch.setattr("app.snapshot.router.resolve_profile", lambda req, db: db.get(Profile, outsider_id))
    r_outsider = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r_outsider.status_code == 200
    assert private_id not in [t["id"] for t in r_outsider.json()["threads"]]
    # Outsider requesting private thread directly must be hidden as 404
    r_outsider_priv = client.get(f"/api/campaigns/{campaign_id}/snapshot?thread_id={private_id}")
    assert r_outsider_priv.status_code == 404


def test_snapshot_remains_correct_with_zero_realtime_events(api):
    client, factory, campaign_id, _, _ = api
    # No realtime subscription, just poll snapshots
    for i in range(3):
        client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": f"msg {i}"}, headers={"Idempotency-Key": f"z{i}"})
    # Snapshot without ever subscribing to realtime must be correct
    r = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r.status_code == 200
    assert len(r.json()["history"]["messages"]) == 3


def test_snapshot_retry_is_side_effect_free(api):
    client, factory, campaign_id, _, _ = api
    from app.campaigns.events import commit_campaign_mutation

    with factory() as db:
        before_rev = db.get(Campaign, campaign_id).revision
        before_threads = db.scalar(select(func.count()).select_from(CampaignThread).where(CampaignThread.campaign_id == campaign_id))

    r1 = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    r2 = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    j1 = r1.json()
    j2 = r2.json()
    # generated_at differs by a few ms — compare deterministic fields
    for key in ("campaign", "revision", "threads", "active_thread_id", "history", "dm_state", "reconciliation"):
        assert j1[key] == j2[key]
    assert r1.headers["X-Snapshot-Revision"] == r2.headers["X-Snapshot-Revision"]

    with factory() as db:
        after_rev = db.get(Campaign, campaign_id).revision
        after_threads = db.scalar(select(func.count()).select_from(CampaignThread).where(CampaignThread.campaign_id == campaign_id))
    assert before_rev == after_rev
    assert before_threads == after_threads

    # Also ensure repeated reads after mutations don't create extra threads
    with factory() as db:
        commit_campaign_mutation(db, campaign_id, expected_revision=before_rev, event_type="campaign.bump2", operation_id="bump2")
        db.commit()
    r3 = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CampaignThread).where(CampaignThread.campaign_id == campaign_id)) == before_threads


def test_snapshot_authorization_visibility_aware(api, monkeypatch):
    client, factory, campaign_id, _, _ = api
    outsider = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=outsider, email="nonmember@example.com"))
        db.commit()
    monkeypatch.setattr("app.snapshot.router.resolve_profile", lambda req, db: db.get(Profile, outsider))
    r = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r.status_code == 403


def test_snapshot_includes_hooks_for_future_surfaces(api):
    client, _, campaign_id, _, _ = api
    r = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    snap = r.json()
    assert "dm_state" in snap
    assert snap["dm_state"]["status"] in ("idle", "active")
    assert "surfaces" in snap
    assert "extensions" in snap
    assert "reconciliation" in snap
    assert "meta" in snap


def test_snapshot_fails_closed_when_repeatable_read_setup_fails(api, monkeypatch):
    """Regression: Postgres REPEATABLE READ setup failure must not degrade to 200/READ COMMITTED."""
    client, factory, campaign_id, _, _ = api
    import app.snapshot.service as svc

    def failing_ensure(db):
        raise svc.SnapshotProjectionError("mock postgres setup failure")

    monkeypatch.setattr(svc, "_ensure_repeatable_read", failing_ensure)
    r = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r.status_code == 500
    assert r.status_code != 200
    # Failure is surfaced as clear projection error, not a stale snapshot
    assert "Failed to build snapshot" in r.text


def test_snapshot_postgres_setup_failure_raises_projection_error(monkeypatch):
    """Unit: Postgres SET TRANSACTION failure must raise SnapshotProjectionError, not degrade."""
    import app.snapshot.service as svc
    from unittest.mock import MagicMock
    from sqlalchemy.orm import Session

    mock_db = MagicMock(spec=Session)
    mock_bind = MagicMock()
    mock_bind.dialect.name = "postgresql"
    mock_db.get_bind.return_value = mock_bind
    mock_db.in_transaction.return_value = False

    def fake_execute(stmt, *a, **k):
        if "SET TRANSACTION" in str(stmt):
            raise RuntimeError("postgres SET TRANSACTION failed")
        return MagicMock()

    mock_db.execute.side_effect = fake_execute

    with pytest.raises(svc.SnapshotProjectionError, match="Failed to establish consistent snapshot"):
        svc._ensure_repeatable_read(mock_db)
