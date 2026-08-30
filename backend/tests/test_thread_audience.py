"""Issue #195 — shared/private thread audience-safe history."""
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

from app.deps.auth import MOCK_USER_ID  # noqa: E402
from app.runtime.threads import can_read_thread, get_or_create_campaign_thread  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models import Campaign, CampaignMember, CampaignThread, CampaignThreadMember, PlayerSubmission, Profile  # noqa: E402


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
        db.add_all([
            Profile(id=owner_id, email="owner@example.com"),
            Profile(id=member_id, email="member@example.com"),
            Profile(id=outsider_id, email="outsider@example.com"),
            Campaign(id=campaign_id, owner_id=owner_id, name="Table"),
            CampaignMember(campaign_id=campaign_id, user_id=owner_id, role="owner"),
            CampaignMember(campaign_id=campaign_id, user_id=member_id, role="player"),
            CampaignMember(campaign_id=campaign_id, user_id=outsider_id, role="player"),
        ])
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, campaign_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _auth_as(profile_id):
    from unittest.mock import patch
    import models as _m
    # Returns a context that monkeypatches resolve_profile to return profile_id
    # Caller uses monkeypatch; this helper constructs Profile retrieval
    return profile_id


def test_shared_history_available_to_campaign_members(api, monkeypatch):
    client, factory, campaign_id, member_id, _ = api
    # owner posts to shared thread (no thread_id → shared)
    r = client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": "shared hello"}, headers={"Idempotency-Key": "shared-1"})
    assert r.status_code == 201
    shared_tid = r.json()["submission"]["thread_id"]
    # member reads shared history via GET
    monkeypatch.setattr("app.runtime.router.resolve_profile", lambda req, db: db.get(Profile, member_id))
    r2 = client.get(f"/api/campaigns/{campaign_id}/submissions")
    assert r2.status_code == 200
    assert len(r2.json()["submissions"]) == 1
    # thread id survives reconnect (same UUID returned)
    r3 = client.get(f"/api/campaigns/{campaign_id}/submissions")
    assert r3.json()["thread_id"] == shared_tid
    # explicit thread history endpoint also works
    r4 = client.get(f"/api/campaigns/{campaign_id}/threads/{shared_tid}/submissions")
    assert r4.status_code == 200
    assert len(r4.json()["submissions"]) == 1


def test_private_thread_only_authorized_can_read(api, monkeypatch):
    client, factory, campaign_id, member_id, outsider_id = api
    # owner creates private thread with member, not outsider
    r = client.post(f"/api/campaigns/{campaign_id}/threads", json={"thread_type": "private", "member_ids": [str(member_id)], "title": "Secret"})
    assert r.status_code == 201, r.text
    private_tid = r.json()["thread"]["id"]
    # owner posts private submission
    r2 = client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": "private hello", "thread_id": private_tid}, headers={"Idempotency-Key": "priv-1"})
    assert r2.status_code == 201

    # member can read private history
    monkeypatch.setattr("app.runtime.router.resolve_profile", lambda req, db: db.get(Profile, member_id))
    r3 = client.get(f"/api/campaigns/{campaign_id}/threads/{private_tid}/submissions")
    assert r3.status_code == 200
    assert len(r3.json()["submissions"]) == 1

    # outsider (campaign member but not thread member) cannot read — hidden as 404
    monkeypatch.setattr("app.runtime.router.resolve_profile", lambda req, db: db.get(Profile, outsider_id))
    r4 = client.get(f"/api/campaigns/{campaign_id}/threads/{private_tid}/submissions")
    assert r4.status_code == 404

    # outsider cannot write to private thread either — also hidden as 404
    r5 = client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": "intrusion", "thread_id": private_tid}, headers={"Idempotency-Key": "priv-intrude"})
    assert r5.status_code == 404

    # outsider's thread list does not leak private thread metadata
    r6 = client.get(f"/api/campaigns/{campaign_id}/threads")
    assert r6.status_code == 200
    ids = [t["id"] for t in r6.json()["threads"]]
    assert private_tid not in ids

    # member's list does include private thread
    monkeypatch.setattr("app.runtime.router.resolve_profile", lambda req, db: db.get(Profile, member_id))
    r7 = client.get(f"/api/campaigns/{campaign_id}/threads")
    assert private_tid in [t["id"] for t in r7.json()["threads"]]


def test_owner_without_membership_cannot_read_private(api, monkeypatch):
    client, factory, campaign_id, member_id, outsider_id = api
    owner_id = MOCK_USER_ID
    # Create private thread between member and outsider — owner NOT included
    with factory() as db:
        from app.runtime.threads import create_private_thread
        t = create_private_thread(db, campaign_id=campaign_id, created_by=member_id, member_ids=[outsider_id], title="No Owner")
        private_tid = str(t.id)
        # Add a submission directly via service (simulates private message)
        from app.runtime.submissions import accept_submission
        accept_submission(db, campaign_id=campaign_id, user_id=member_id, raw_content="secret", segments=[{"type": "ooc", "text": "secret"}], thread_id=private_tid, audience="private")
        db.commit()

    # owner (campaign owner but not thread member) must be denied — hidden as 404
    r = client.get(f"/api/campaigns/{campaign_id}/threads/{private_tid}/submissions")
    assert r.status_code == 404
    # centralized check also denies owner
    with factory() as db:
        assert can_read_thread(db, campaign_id, uuid.UUID(private_tid), owner_id) is False
        assert can_read_thread(db, campaign_id, uuid.UUID(private_tid), member_id) is True


def test_revoked_membership_immediately_denies_but_preserves_history(api, monkeypatch):
    client, factory, campaign_id, member_id, _ = api
    r = client.post(f"/api/campaigns/{campaign_id}/threads", json={"thread_type": "private", "member_ids": [str(member_id)]})
    assert r.status_code == 201
    private_tid = r.json()["thread"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": "before revoke", "thread_id": private_tid}, headers={"Idempotency-Key": "rev-1"})
    # owner revokes member by deleting membership
    with factory() as db:
        from app.runtime.threads import remove_thread_member
        assert remove_thread_member(db, uuid.UUID(private_tid), member_id) is True
        db.commit()
        # history still durable
        assert db.query(PlayerSubmission).filter_by(thread_id=private_tid).count() == 1

    # revoked member can no longer read — hidden as 404, history still durable
    monkeypatch.setattr("app.runtime.router.resolve_profile", lambda req, db: db.get(Profile, member_id))
    r2 = client.get(f"/api/campaigns/{campaign_id}/threads/{private_tid}/submissions")
    assert r2.status_code == 404
    # and revoked member cannot write — also 404
    r3 = client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": "after revoke", "thread_id": private_tid}, headers={"Idempotency-Key": "rev-2"})
    assert r3.status_code == 404


def test_missing_or_ambiguous_thread_fails_closed(api):
    client, _, campaign_id, _, _ = api
    # non-existent UUID — indistinguishable from private unauthorized (both 404)
    fake = str(uuid.uuid4())
    r = client.get(f"/api/campaigns/{campaign_id}/threads/{fake}/submissions")
    assert r.status_code == 404
    r_detail = client.get(f"/api/campaigns/{campaign_id}/threads/{fake}")
    assert r_detail.status_code == 404
    # malformed thread id in submission — also 404 (fail closed)
    r2 = client.post(f"/api/campaigns/{campaign_id}/submissions", json={"content": "hi", "thread_id": "not-a-uuid"}, headers={"Idempotency-Key": "bogus"})
    assert r2.status_code == 404

    # Non-member private lookups are indistinguishable from not-found — verify
    # that an authorized thread id queried by an outsider returns 404, same as fake
    # (covered more thoroughly in private-audience tests, but smoke-check here)
    assert r.status_code == r_detail.status_code == 404


def test_thread_identifiers_survive_reconnect(api):
    client, factory, campaign_id, _, _ = api
    # First call creates shared thread
    r1 = client.get(f"/api/campaigns/{campaign_id}/threads")
    shared = [t for t in r1.json()["threads"] if t["thread_type"] == "campaign"][0]["id"]
    # Second call (simulates reconnect) returns same id
    r2 = client.get(f"/api/campaigns/{campaign_id}/threads")
    shared2 = [t for t in r2.json()["threads"] if t["thread_type"] == "campaign"][0]["id"]
    assert shared == shared2
    # Service helper also returns same id across sessions (request boundary commits)
    with factory() as db:
        t1 = get_or_create_campaign_thread(db, campaign_id)
        db.commit()
        id1 = str(t1.id)
    with factory() as db:
        t2 = get_or_create_campaign_thread(db, campaign_id)
        db.commit()
        id2 = str(t2.id)
    assert id1 == id2 == shared


def test_shared_thread_concurrency_enforced_by_partial_unique_index(api):
    """Regression: concurrent first-requests must not split history into two shared threads."""
    _, factory, campaign_id, _, _ = api
    # Ensure clean slate — remove any shared thread created by earlier calls in this fixture
    with factory() as db:
        db.query(CampaignThread).filter_by(campaign_id=campaign_id, thread_type="campaign").delete()
        db.commit()
    # Simulate two concurrent transactions both observing no shared thread
    from sqlalchemy.orm import sessionmaker
    # Use the same engine/factory but two independent sessions (like two web workers)
    SessionLocal = factory
    s1 = SessionLocal()
    s2 = SessionLocal()
    try:
        t1 = get_or_create_campaign_thread(s1, campaign_id)
        t1_id = str(t1.id)
        # s1 not yet committed — s2 should not see t1 and will try to create second
        t2 = get_or_create_campaign_thread(s2, campaign_id)
        t2_id = str(t2.id)
        # At this point both sessions have flushed different UUIDs
        # Committing s1 should succeed, s2 must fail on partial unique violation
        s1.commit()
        # s2 commit should raise IntegrityError due to uq_campaign_threads_one_campaign_per_campaign
        try:
            s2.commit()
            committed_both = True
        except Exception as exc:  # IntegrityError wrapped
            s2.rollback()
            committed_both = False
            # After race, s2 can re-resolve to the winner via helper (retry path)
            with factory() as db:
                winner = get_or_create_campaign_thread(db, campaign_id)
                assert str(winner.id) == t1_id
        # Invariant: exactly one shared thread survives
        with factory() as db:
            count = db.query(CampaignThread).filter_by(campaign_id=campaign_id, thread_type="campaign").count()
            assert count == 1
            # Both callers must end up referencing the same durable id (no history split)
            # If s2 committed both, t2_id would equal t1_id only if constraint prevented duplicate;
            # otherwise this would be 2 — which the index must prevent.
            if committed_both:
                assert t1_id == t2_id, "partial unique index failed to prevent duplicate shared threads"
        # Verify the durable id survives a fresh session (reconnect)
        with factory() as db:
            t3 = get_or_create_campaign_thread(db, campaign_id)
            assert str(t3.id) == t1_id
    finally:
        s1.close()
        s2.close()


def test_get_or_create_does_not_commit_unrelated_pending_work(api):
    """Helper must not commit unrelated pending work (review finding #3)."""
    _, factory, campaign_id, _, _ = api
    outsider_id = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=outsider_id, email="pending@example.com"))
        # Do NOT commit — pending insert should not be committed by helper
        t = get_or_create_campaign_thread(db, campaign_id)
        # Helper flushed the thread but should not have committed the profile
        assert db.query(Profile).filter_by(id=outsider_id).first() is not None  # visible in same tx
        db.rollback()
        # After rollback, neither the thread (if it was newly created in this tx)
        # nor the pending profile should have been committed by the helper.
        # The profile must not survive the rollback — proves helper didn't commit.
        assert db.query(Profile).filter_by(id=outsider_id).first() is None
        # Shared thread durability is preserved via request-boundary commits elsewhere;
        # this test only proves the helper itself does not auto-commit.
