"""Issue #188 — campaign revision ordering and immutable domain events.

Covers all acceptance criteria:
- expected revision required, increment exactly once
- monotonic sequence == resulting revision
- stale writes fail without partial mutation
- non-fictional derived updates don't bump revision
- concurrent competing commits cannot both claim same prior revision
- rollback leaves revision + events unchanged
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

# Patch sqlite compiler to handle postgres-specific types for in-memory tests
if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base, get_db

# Import after patch to ensure models import uses patched compiler
import models  # noqa: E402
from app.campaigns.events import (  # noqa: E402
    RevisionConflictError,
    commit_campaign_mutation,
    list_campaign_events,
    update_campaign_derived,
)
from models.campaigns import Campaign
from models.campaigns import CampaignDomainEvent
from models.campaigns import CampaignMember
from models.profiles import Profile


def _sqlite_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(bind=eng)
    return eng


def _new_campaign(db: Session, name: str = "Test Campaign") -> Campaign:
    owner_id = uuid.uuid4()
    # ensure profile exists for FK
    prof = Profile(id=owner_id, email="test@example.com", username="tester")
    db.add(prof)
    db.flush()
    camp = Campaign(owner_id=owner_id, name=name)
    db.add(camp)
    db.flush()
    db.commit()
    db.refresh(camp)
    return camp


# ── basics ──────────────────────────────────────────────────────────────────


def test_revision_defaults_to_zero():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    assert camp.revision == 0
    # to_dict includes revision
    assert camp.to_dict()["revision"] == 0
    db.close()


def test_successful_mutation_increments_revision_exactly_once():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    cid = camp.id

    def mutate(c: Campaign):
        c.name = "Renamed"

    camp2, evt = commit_campaign_mutation(
        db,
        cid,
        expected_revision=0,
        event_type="campaign.renamed",
        payload={"name": "Renamed"},
        operation_id="op-1",
        mutate=mutate,
    )
    assert camp2.revision == 1
    assert evt.sequence == 1
    assert evt.campaign_id == cid
    assert evt.event_type == "campaign.renamed"
    assert evt.payload == {"name": "Renamed"}
    assert evt.operation_id == "op-1"
    # persisted
    fresh = db.get(Campaign, cid)
    assert fresh.revision == 1
    assert fresh.name == "Renamed"
    db.close()


def test_domain_events_monotonic_ordered_sequence():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    cid = camp.id

    for i in range(1, 4):
        _, evt = commit_campaign_mutation(
            db,
            cid,
            expected_revision=i - 1,
            event_type=f"test.event_{i}",
            payload={"i": i},
            operation_id=f"op-{i}",
        )
        assert evt.sequence == i

    events = list_campaign_events(db, cid)
    assert [e.sequence for e in events] == [1, 2, 3]
    assert [e.event_type for e in events] == ["test.event_1", "test.event_2", "test.event_3"]
    # sequence equals resulting revision
    camp_final = db.get(Campaign, cid)
    assert camp_final.revision == 3
    db.close()


def test_stale_expected_revision_fails_without_partial_mutation():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    cid = camp.id
    original_name = camp.name

    # first commit succeeds
    commit_campaign_mutation(db, cid, expected_revision=0, event_type="campaign.update", payload={"x": 1})

    # stale attempt with old revision 0 should fail, and mutate must not apply
    def mutate(c: Campaign):
        c.name = "HACKED"

    with pytest.raises(RevisionConflictError) as ei:
        commit_campaign_mutation(
            db,
            cid,
            expected_revision=0,
            event_type="campaign.update",
            mutate=mutate,
            operation_id="stale-op",
        )
    exc = ei.value
    assert exc.expected_revision == 0
    assert exc.actual_revision == 1

    # ensure no partial mutation: name unchanged, revision still 1, only 1 event
    fresh = db.get(Campaign, cid)
    assert fresh.name == original_name
    assert fresh.revision == 1
    events = list_campaign_events(db, cid)
    assert len(events) == 1
    db.close()


def test_stale_write_does_not_create_event():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    cid = camp.id
    commit_campaign_mutation(db, cid, expected_revision=0, event_type="campaign.first")

    # count before stale
    before = len(list_campaign_events(db, cid))
    with pytest.raises(RevisionConflictError):
        commit_campaign_mutation(db, cid, expected_revision=0, event_type="campaign.stale")
    after = len(list_campaign_events(db, cid))
    assert after == before
    db.close()


def test_non_fictional_derived_update_does_not_advance_revision():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    cid = camp.id

    # fictional mutation to get to revision 1
    commit_campaign_mutation(db, cid, expected_revision=0, event_type="campaign.fictional")

    # derived metadata update must not bump
    derived_timestamp = datetime.now(timezone.utc)
    fresh = update_campaign_derived(db, cid, updated_at=derived_timestamp)
    assert fresh.revision == 1

    # another fictional mutation should still require revision 1 and go to 2
    _, evt = commit_campaign_mutation(db, cid, expected_revision=1, event_type="campaign.fictional2")
    assert evt.sequence == 2
    assert db.get(Campaign, cid).revision == 2

    # ensure no extra domain events were created for derived update
    events = list_campaign_events(db, cid)
    assert len(events) == 2
    assert all(e.sequence in (1, 2) for e in events)
    db.close()


def test_derived_update_rejects_fictional_campaign_fields():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)

    unchanged = update_campaign_derived(db, camp.id, name="revision bypass")

    assert unchanged.name == "Test Campaign"
    assert unchanged.revision == 0
    db.close()


def test_concurrent_competing_commits_cannot_both_claim_same_revision():
    """Two sessions both read revision 0; only one can commit with expected 0."""
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)

    # create campaign in one session
    db0 = S()
    camp = _new_campaign(db0)
    cid = camp.id
    db0.close()

    db_a = S()
    db_b = S()

    # both read prior revision 0 (simulating concurrent reads)
    rev_a = db_a.get(Campaign, cid).revision
    rev_b = db_b.get(Campaign, cid).revision
    assert rev_a == 0 and rev_b == 0

    # A commits first
    _, evt_a = commit_campaign_mutation(
        db_a, cid, expected_revision=0, event_type="concurrent.a", operation_id="op-a"
    )
    assert evt_a.sequence == 1

    # B now tries same prior revision — must fail (optimistic lock)
    # Need to rollback B's stale read transaction if any
    db_b.rollback()
    with pytest.raises(RevisionConflictError):
        commit_campaign_mutation(
            db_b, cid, expected_revision=0, event_type="concurrent.b", operation_id="op-b"
        )

    # Verify only A's event exists, revision is 1
    db_check = S()
    events = list_campaign_events(db_check, cid)
    assert len(events) == 1
    assert events[0].event_type == "concurrent.a"
    assert db_check.get(Campaign, cid).revision == 1
    db_a.close()
    db_b.close()
    db_check.close()


def test_rollback_leaves_revision_and_events_unchanged_on_mutate_exception():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    cid = camp.id
    commit_campaign_mutation(db, cid, expected_revision=0, event_type="campaign.ok")

    def bad_mutate(c: Campaign):
        c.name = "partial"
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        commit_campaign_mutation(
            db, cid, expected_revision=1, event_type="campaign.will_fail", mutate=bad_mutate
        )

    # after exception, session was rolled back — need to recover
    db.rollback()
    fresh = db.get(Campaign, cid)
    assert fresh.revision == 1  # not incremented to 2
    assert fresh.name != "partial"
    events = list_campaign_events(db, cid)
    assert len(events) == 1
    assert events[0].sequence == 1
    db.close()


def test_event_visibility_and_provenance_hooks():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    cid = camp.id
    actor = camp.owner_id

    _, evt = commit_campaign_mutation(
        db,
        cid,
        expected_revision=0,
        event_type="campaign.secret",
        visibility="private",
        provenance={"source": "ai_dm", "model": "test"},
        actor_id=actor,
        targets=[str(uuid.uuid4())],
        payload={"secret": True},
    )
    assert evt.visibility == "private"
    assert evt.provenance == {"source": "ai_dm", "model": "test"}
    assert str(evt.actor_id) == str(actor)
    assert evt.targets is not None
    # read back via to_dict
    d = evt.to_dict()
    assert d["visibility"] == "private"
    assert d["provenance"]["source"] == "ai_dm"
    db.close()


def test_event_reads_filter_private_events_to_the_actor():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    actor = camp.owner_id
    other_viewer = uuid.uuid4()

    commit_campaign_mutation(
        db,
        camp.id,
        expected_revision=0,
        event_type="campaign.public",
        actor_id=actor,
        visibility="public",
    )
    commit_campaign_mutation(
        db,
        camp.id,
        expected_revision=1,
        event_type="campaign.private",
        actor_id=actor,
        visibility="private",
        payload={"secret": True},
    )

    actor_events = list_campaign_events(db, camp.id, viewer_id=actor)
    other_events = list_campaign_events(db, camp.id, viewer_id=other_viewer)
    assert [event.event_type for event in actor_events] == ["campaign.public", "campaign.private"]
    assert [event.event_type for event in other_events] == ["campaign.public"]
    db.close()


def test_two_sequential_fictional_mutations_deterministic_ordering():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    camp = _new_campaign(db)
    cid = camp.id

    # Simulate rapid successive mutations
    for i in range(5):
        _, evt = commit_campaign_mutation(
            db,
            cid,
            expected_revision=i,
            event_type="campaign.tick",
            payload={"tick": i},
        )
        assert evt.sequence == i + 1

    events = list_campaign_events(db, cid)
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs) == list(range(1, 6))
    # No gaps, no duplicates
    assert len(set(seqs)) == 5
    db.close()


def test_missing_campaign_raises_value_error():
    eng = _sqlite_engine()
    S = sessionmaker(bind=eng)
    db = S()
    fake = uuid.uuid4()
    with pytest.raises(ValueError, match="not found"):
        commit_campaign_mutation(db, fake, expected_revision=0, event_type="test")
    db.close()


def test_campaign_update_http_contract_requires_and_checks_revision(monkeypatch):
    from app.deps.auth import MOCK_USER_ID
    from main import app

    eng = _sqlite_engine()
    factory = sessionmaker(bind=eng)
    campaign_id = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=MOCK_USER_ID, email="mock@example.com"))
        db.add(Campaign(id=campaign_id, owner_id=MOCK_USER_ID, name="Before"))
        db.add(CampaignMember(campaign_id=campaign_id, user_id=MOCK_USER_ID, role="owner"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "test")
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        missing = client.put(
            f"/api/campaigns/{campaign_id}",
            json={"name": "No revision", "operation_id": "missing-revision"},
        )
        assert missing.status_code == 400
        assert missing.json()["detail"] == "expected_revision is required"

        success = client.put(
            f"/api/campaigns/{campaign_id}",
            json={"name": "After", "expected_revision": 0, "operation_id": "update-1"},
        )
        assert success.status_code == 200
        assert success.json()["campaign"]["revision"] == 1
        assert success.json()["event"]["sequence"] == 1

        stale = client.put(
            f"/api/campaigns/{campaign_id}",
            json={"name": "Stale", "expected_revision": 0, "operation_id": "update-2"},
        )
        assert stale.status_code == 409
        assert stale.headers["X-Current-Revision"] == "1"

        events = client.get(f"/api/campaigns/{campaign_id}/events")
        assert events.status_code == 200
        assert [event["sequence"] for event in events.json()["events"]] == [1]
    finally:
        app.dependency_overrides.clear()
