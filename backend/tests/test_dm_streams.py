"""Issue #197 — persist DM stream chunks independently of client connections."""
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
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import Campaign
from models.campaigns import CampaignMember
from models.dm import DMStream
from models.dm import DMStreamChunk
from models.profiles import Profile
from models.threads import CampaignThread


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
            Campaign(id=campaign_id, owner_id=owner_id, name="Table", revision=0),
            CampaignMember(campaign_id=campaign_id, user_id=owner_id, role="owner"),
            CampaignMember(campaign_id=campaign_id, user_id=member_id, role="player"),
            CampaignMember(campaign_id=campaign_id, user_id=outsider_id, role="player"),
        ])
        db.commit()
        # ensure shared thread exists (campaign creation path)
        from app.runtime.threads import get_or_create_campaign_thread
        get_or_create_campaign_thread(db, campaign_id, created_by=owner_id)
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


def test_stream_chunks_ordered_and_associated_with_one_attempt(api):
    client, factory, campaign_id, _, _ = api
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "turn-100", "attempt_id": "att-100"})
    assert r.status_code == 201, r.text
    stream_id = r.json()["stream"]["id"]
    assert r.json()["stream"]["turn_id"] == "turn-100"
    assert r.json()["stream"]["attempt_id"] == "att-100"
    # ordered append
    for i, txt in enumerate(["Alpha ", "Beta ", "Gamma"]):
        rr = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{stream_id}/chunks", json={"sequence": i, "text": txt})
        assert rr.status_code == 201, rr.text
        assert rr.json()["stream"]["last_sequence"] == i
        assert rr.json()["stream"]["chunk_count"] == i + 1
    # fetch ordered
    r2 = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{stream_id}")
    assert r2.status_code == 200
    chunks = r2.json()["chunks"]
    assert [c["sequence"] for c in chunks] == [0, 1, 2]
    assert [c["text"] for c in chunks] == ["Alpha ", "Beta ", "Gamma"]
    assert r2.json()["visible_text"] == "Alpha Beta Gamma"
    # distinct turn/attempt is separate stream
    r3 = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "turn-100", "attempt_id": "att-101"})
    assert r3.status_code == 201
    assert r3.json()["stream"]["id"] != stream_id


def test_duplicate_append_cannot_create_duplicate_visible_text(api):
    client, _, campaign_id, _, _ = api
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-dup", "attempt_id": "a-dup"})
    sid = r.json()["stream"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 0, "text": "Hello "})
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 1, "text": "world"})
    # idempotent duplicate same sequence+text
    r_dup = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 1, "text": "world"})
    assert r_dup.status_code == 201
    # ensure no duplicate visible text
    r2 = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid}")
    assert len(r2.json()["chunks"]) == 2
    assert r2.json()["visible_text"] == "Hello world"
    # same sequence different text must be rejected
    r_conflict = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 1, "text": "WORLD"})
    assert r_conflict.status_code == 409
    # out of order must be rejected
    r_ooo = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 3, "text": "!"})
    assert r_ooo.status_code == 409
    # persisted state unchanged after failures
    r3 = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid}")
    assert r3.json()["visible_text"] == "Hello world"
    assert r3.json()["stream"]["chunk_count"] == 2


def test_client_can_fetch_after_disconnect_and_reconstruct(api):
    client, _, campaign_id, _, _ = api
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-disc", "attempt_id": "a-disc"})
    sid = r.json()["stream"]["id"]
    for i, txt in enumerate(["chunk0 ", "chunk1 ", "chunk2 "]):
        client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": i, "text": txt})
    # simulate disconnect after chunk 1: client reloads
    r_fetch = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid}")
    assert r_fetch.status_code == 200
    assert r_fetch.json()["visible_text"] == "chunk0 chunk1 chunk2 "
    assert r_fetch.json()["stream"]["last_sequence"] == 2
    assert r_fetch.json()["stream"]["chunk_count"] == 3
    # continue streaming after reconstruct
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 3, "text": "chunk3"})
    r_fetch2 = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid}")
    assert r_fetch2.json()["visible_text"] == "chunk0 chunk1 chunk2 chunk3"


def test_completed_stream_exposes_final_message_without_losing_provenance(api):
    client, factory, campaign_id, _, _ = api
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-fin", "attempt_id": "a-fin"})
    sid = r.json()["stream"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 0, "text": "Final "})
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 1, "text": "message"})
    # complete materializes final_text
    r_complete = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/complete", json={"reason": "completed"})
    assert r_complete.status_code == 200
    assert r_complete.json()["stream"]["status"] == "completed"
    assert r_complete.json()["final_text"] == "Final message"
    assert r_complete.json()["stream"]["final_text"] == "Final message"
    # provenance retained: chunks still queryable
    r_get = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid}")
    assert r_get.json()["final_text"] == "Final message"
    assert len(r_get.json()["chunks"]) == 2
    assert r_get.json()["visible_text"] == "Final message"
    # snapshot includes completed dm_message
    r_snap = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    snap = r_snap.json()
    assert any(m["final_text"] == "Final message" for m in snap.get("dm_messages", []))
    # further appends rejected after completion
    r_append = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 2, "text": " extra"})
    assert r_append.status_code == 409
    # direct db check: observability fields
    with factory() as db:
        s = db.get(DMStream, uuid.UUID(sid))
        assert s.first_chunk_at is not None
        assert s.last_chunk_at is not None
        assert s.chunk_count == 2
        assert s.total_bytes == len("Final message".encode())
        assert s.last_sequence == 1
        assert s.completion_reason == "completed"
        assert s.completed_at is not None


def test_failed_abandoned_partial_stream_auditable_but_excluded_from_canonical(api):
    client, factory, campaign_id, _, _ = api
    # completed stream for canonical history
    r_ok = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-ok", "attempt_id": "a-ok"})
    sid_ok = r_ok.json()["stream"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid_ok}/chunks", json={"sequence": 0, "text": "ok"})
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid_ok}/complete", json={})
    # partial abandoned stream
    r_part = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-part", "attempt_id": "a-part"})
    sid_part = r_part.json()["stream"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid_part}/chunks", json={"sequence": 0, "text": "partial "})
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid_part}/chunks", json={"sequence": 1, "text": "data"})
    r_abandon = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid_part}/abandon", json={"reason": "worker_failed"})
    assert r_abandon.status_code == 200
    assert r_abandon.json()["stream"]["status"] == "abandoned"
    # canonical list excludes abandoned
    r_list = client.get(f"/api/campaigns/{campaign_id}/dm-streams")
    canonical_ids = [s["stream"]["id"] for s in r_list.json()["streams"]]
    assert sid_ok in canonical_ids
    assert sid_part not in canonical_ids
    # auditable with include_abandoned
    r_all = client.get(f"/api/campaigns/{campaign_id}/dm-streams?include_abandoned=true")
    all_ids = [s["stream"]["id"] for s in r_all.json()["streams"]]
    assert sid_ok in all_ids
    assert sid_part in all_ids
    # partial still fetchable directly with chunks
    r_fetch = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid_part}")
    assert r_fetch.status_code == 200
    assert r_fetch.json()["visible_text"] == "partial data"
    assert len(r_fetch.json()["chunks"]) == 2
    # snapshot excludes abandoned from dm_messages
    r_snap = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    dm_ids = [m["id"] for m in r_snap.json().get("dm_messages", [])]
    assert sid_ok in dm_ids
    assert sid_part not in dm_ids
    # check abandonment observability
    with factory() as db:
        s = db.get(DMStream, uuid.UUID(sid_part))
        assert s.abandonment_reason == "worker_failed"
        assert s.abandoned_at is not None
        assert s.chunk_count == 2
        assert s.last_sequence == 1
        assert s.first_chunk_at is not None


def test_chunk_write_failure_not_reported_as_visible_until_persisted(api):
    client, factory, campaign_id, _, _ = api
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-fail", "attempt_id": "a-fail"})
    sid = r.json()["stream"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 0, "text": "good"})
    # out-of-order attempt is not persisted as visible
    r_bad = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 5, "text": "bad"})
    assert r_bad.status_code == 409
    r_get = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid}")
    assert r_get.json()["visible_text"] == "good"
    assert r_get.json()["stream"]["chunk_count"] == 1
    assert r_get.json()["stream"]["last_sequence"] == 0
    with factory() as db:
        count = db.scalar(select(func.count()).select_from(DMStreamChunk).where(DMStreamChunk.stream_id == uuid.UUID(sid)))
        assert count == 1


def test_private_stream_chunks_never_in_shared_projection(api, monkeypatch):
    client, factory, campaign_id, member_id, outsider_id = api
    from app.runtime.threads import create_private_thread

    # create private thread between owner and member
    with factory() as db:
        t = create_private_thread(db, campaign_id=campaign_id, created_by=MOCK_USER_ID, member_ids=[member_id], title="secret-dm")
        priv_id = str(t.id)
        shared = db.scalar(select(CampaignThread).where(CampaignThread.campaign_id == campaign_id, CampaignThread.thread_type == "campaign"))
        shared_id = str(shared.id)
        db.commit()

    # owner creates private DM stream
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-priv", "attempt_id": "a-priv", "thread_id": priv_id})
    assert r.status_code == 201, r.text
    priv_sid = r.json()["stream"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{priv_sid}/chunks", json={"sequence": 0, "text": "secret"})
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{priv_sid}/complete", json={})

    # shared projection (snapshot and dm-streams without private) must not leak
    r_shared_streams = client.get(f"/api/campaigns/{campaign_id}/dm-streams?thread_id=main")
    assert all(s["stream"]["id"] != priv_sid for s in r_shared_streams.json()["streams"])
    r_snapshot = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r_snapshot.status_code == 200
    # owner snapshot of shared thread has no secret
    assert not any("secret" in (m.get("final_text") or "") for m in r_snapshot.json().get("dm_messages", []))
    # owner can read private stream directly
    r_priv = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{priv_sid}")
    assert r_priv.status_code == 200
    assert r_priv.json()["visible_text"] == "secret"
    # outsider (campaign member but not private member) cannot read private stream
    monkeypatch.setattr("app.dm_streams.router.resolve_profile", lambda req, db: db.get(Profile, outsider_id))
    # need to also patch snapshot router for outsider snapshot test
    import app.snapshot.router as smr
    orig = smr.resolve_profile
    monkeypatch.setattr(smr, "resolve_profile", lambda req, db: db.get(Profile, outsider_id))
    r_outsider = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{priv_sid}")
    assert r_outsider.status_code in (403, 404)
    r_outsider_list = client.get(f"/api/campaigns/{campaign_id}/dm-streams?thread_id=" + priv_id)
    assert r_outsider_list.status_code in (403, 404)
    # outsider snapshot must not leak private dm_messages
    r_out_snap = client.get(f"/api/campaigns/{campaign_id}/snapshot?thread_id=" + priv_id)
    assert r_out_snap.status_code == 404  # hidden as not found


def test_observability_fields_recorded(api):
    client, factory, campaign_id, _, _ = api
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-obs", "attempt_id": "a-obs"})
    sid = r.json()["stream"]["id"]
    with factory() as db:
        s = db.get(DMStream, uuid.UUID(sid))
        assert s.chunk_count == 0
        assert s.total_bytes == 0
        assert s.first_chunk_at is None
        assert s.last_sequence is None
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 0, "text": "hi"})
    with factory() as db:
        s = db.get(DMStream, uuid.UUID(sid))
        assert s.first_chunk_at is not None
        assert s.last_chunk_at is not None
        assert s.chunk_count == 1
        assert s.total_bytes == 2
        assert s.last_sequence == 0
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 1, "text": " there"})
    with factory() as db:
        s = db.get(DMStream, uuid.UUID(sid))
        assert s.chunk_count == 2
        assert s.total_bytes == 2 + len(" there".encode())
        assert s.last_sequence == 1
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/complete", json={"reason": "done"})
    with factory() as db:
        s = db.get(DMStream, uuid.UUID(sid))
        assert s.completion_reason == "done"
        assert s.completed_at is not None


def test_snapshot_shows_active_stream_and_reconstructs_visible_text(api):
    client, _, campaign_id, _, _ = api
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-active", "attempt_id": "a-active"})
    sid = r.json()["stream"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 0, "text": "Streaming "})
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 1, "text": "text"})
    r_snap = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    snap = r_snap.json()
    dm_state = snap["dm_state"]
    assert dm_state["streaming"] is True
    assert dm_state["stream_id"] == sid
    assert dm_state["visible_text"] == "Streaming text"
    assert dm_state["chunk_count"] == 2
    # after completion, snapshot returns idle
    client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/complete", json={})
    r_snap2 = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r_snap2.json()["dm_state"]["streaming"] is False
    assert any(m["id"] == sid for m in r_snap2.json()["dm_messages"])


def test_player_can_read_but_cannot_mutate_dm_streams(api, monkeypatch):
    """Review feedback: ordinary player can read an authorized stream but
    cannot create, append, complete, abandon, or fail one."""
    client, factory, campaign_id, member_id, _ = api
    # Owner creates and partially fills a stream
    r = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-player-read", "attempt_id": "a-player-read"})
    assert r.status_code == 201
    sid = r.json()["stream"]["id"]
    r2 = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 0, "text": "owner narration"})
    assert r2.status_code == 201
    # Switch auth to ordinary campaign member (player)
    monkeypatch.setattr("app.dm_streams.router.resolve_profile", lambda req, db: db.get(Profile, member_id))
    import app.snapshot.router as smr
    monkeypatch.setattr(smr, "resolve_profile", lambda req, db: db.get(Profile, member_id))
    # Player can read the stream and snapshot
    r_read = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid}")
    assert r_read.status_code == 200, r_read.text
    assert r_read.json()["visible_text"] == "owner narration"
    r_list = client.get(f"/api/campaigns/{campaign_id}/dm-streams?thread_id=main")
    assert r_list.status_code == 200
    r_snap = client.get(f"/api/campaigns/{campaign_id}/snapshot")
    assert r_snap.status_code == 200
    # Player cannot create a new DM stream
    r_create = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-forged", "attempt_id": "a-forged"})
    assert r_create.status_code == 403
    # Player cannot append to existing stream
    r_append = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks", json={"sequence": 1, "text": "forged"})
    assert r_append.status_code == 403
    # Player cannot complete/abandon/fail
    r_complete = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/complete", json={})
    assert r_complete.status_code == 403
    r_abandon = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/abandon", json={"reason": "forged"})
    assert r_abandon.status_code == 403
    r_fail = client.post(f"/api/campaigns/{campaign_id}/dm-streams/{sid}/abandon", json={"status": "failed", "reason": "forged"})
    assert r_fail.status_code == 403
    # Verify no forgery persisted
    monkeypatch.setattr("app.dm_streams.router.resolve_profile", lambda req, db: db.get(Profile, MOCK_USER_ID))
    monkeypatch.setattr(smr, "resolve_profile", lambda req, db: db.get(Profile, MOCK_USER_ID))
    r_verify = client.get(f"/api/campaigns/{campaign_id}/dm-streams/{sid}")
    assert r_verify.json()["visible_text"] == "owner narration"
    assert len(r_verify.json()["chunks"]) == 1


def test_internal_worker_token_can_mutate_when_owner_not_caller(api, monkeypatch):
    """Worker/internal token path allows server-authorized mutation without owner impersonation."""
    client, factory, campaign_id, member_id, _ = api
    monkeypatch.setenv("DM_STREAM_WORKER_TOKEN", "secret-worker-token")
    # Act as ordinary player but present worker token
    monkeypatch.setattr("app.dm_streams.router.resolve_profile", lambda req, db: db.get(Profile, member_id))
    import app.snapshot.router as smr
    monkeypatch.setattr(smr, "resolve_profile", lambda req, db: db.get(Profile, member_id))
    # Without token, player is blocked
    r_no_token = client.post(f"/api/campaigns/{campaign_id}/dm-streams", json={"turn_id": "t-worker-no", "attempt_id": "a-worker-no"})
    assert r_no_token.status_code == 403
    # With correct worker token, allowed
    r = client.post(
        f"/api/campaigns/{campaign_id}/dm-streams",
        json={"turn_id": "t-worker", "attempt_id": "a-worker"},
        headers={"x-worker-token": "secret-worker-token"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["stream"]["id"]
    r2 = client.post(
        f"/api/campaigns/{campaign_id}/dm-streams/{sid}/chunks",
        json={"sequence": 0, "text": "worker text"},
        headers={"x-worker-token": "secret-worker-token"},
    )
    assert r2.status_code == 201
    r3 = client.post(
        f"/api/campaigns/{campaign_id}/dm-streams/{sid}/complete",
        json={},
        headers={"x-worker-token": "secret-worker-token"},
    )
    assert r3.status_code == 200
    assert r3.json()["stream"]["status"] == "completed"
