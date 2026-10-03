"""Post-turn clock commits serialize with in-flight DM turns.

A clock advancement bumps the campaign revision; a DM turn pinned its
``source_revision`` when its attempt was created. Committing a clock while
that turn is in flight would fail the turn stale after narration streamed,
so post-turn defers (retryable, no attempt spent) until the turn lands.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.campaigns.events import commit_campaign_mutation  # noqa: E402
from app.decisions import DecisionService  # noqa: E402
from app.dm.turns import commit_turn, coordinate_turn, mark_streaming_started  # noqa: E402
from app.idempotency import OPERATION_ID_MAX_LENGTH, compose_operation_id  # noqa: E402
from app.post_turn.service import FORCE, get_checkpoint, maybe_trigger_post_turn, run_post_turn_sweep  # noqa: E402
from app.submissions.service import accept_submission  # noqa: E402
from app.threads.service import get_or_create_campaign_thread  # noqa: E402
from app.world import clocks as C  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember  # noqa: E402
from models.dm import DMStream, DMStreamChunk  # noqa: E402
from models.post_turn import PostTurnRun  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import WorkerExecution  # noqa: E402
from models.world import CampaignClock  # noqa: E402
from tests.support.fake_decisions import FakeDecisionAdapter  # noqa: E402
from tests.support.world_writes import commit_world_write  # noqa: E402

QUESTION = "evaluate_campaign_clock"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("POST_TURN_AUTO_TRIGGER", "0")
    # Keep sweep repair from staging extra runs for the DM turn's own event.
    monkeypatch.setenv("POST_TURN_BATCH_SIZE", "100")


def _setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    F = sessionmaker(bind=eng, expire_on_commit=False)
    db = F()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@x.com"))
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="clock-race", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.commit()
    thread = get_or_create_campaign_thread(db, camp.id, created_by=owner)
    db.commit()
    return F, db, camp.id, owner, str(thread.id)


def _rev(db, cid):
    db.expire_all()
    return int(db.get(Campaign, cid).revision or 0)


def _play(db, cid, n):
    for _ in range(n):
        rev = _rev(db, cid)
        commit_campaign_mutation(db, cid, rev, event_type="game.play", payload={"n": rev + 1},
                                 operation_id=f"play-{rev + 1}")


def _mkclock(db, cid, criteria_kind="deterministic"):
    row, _ = commit_world_write(
        db, cid, _rev(db, cid), C.create_clock, name="Ritual", threshold=4,
        advancement_criteria={"kind": criteria_kind, "event_types": ["game.play"]},
        status="active", provenance={"source": "test"},
    )
    return row.id


def _start_turn(F, cid, owner, tid):
    """A player acts: coordinate a pending DM turn pinned to the current revision."""
    with F() as s:
        accept_submission(s, campaign_id=cid, user_id=owner, raw_content="I wait",
                          segments=[{"type": "ic", "text": "I wait"}], thread_id=tid)
        s.commit()
        turn, attempt = coordinate_turn(s, cid, tid)
        return turn.id, attempt.id, int(attempt.source_revision)


def _finish_turn(F, cid, tid, turn_id, attempt_id):
    """Stream the first chunk, then commit the turn against its source revision."""
    with F() as s:
        stream = DMStream(id=uuid.uuid4(), campaign_id=cid, thread_id=uuid.UUID(tid),
                          turn_id=str(turn_id), attempt_id=str(attempt_id),
                          status="streaming", audience="campaign")
        s.add(stream)
        s.flush()
        s.add(DMStreamChunk(id=uuid.uuid4(), stream_id=stream.id, sequence=0,
                            text="The DM narrates.", byte_length=16))
        stream.first_chunk_at = datetime.now(timezone.utc)
        stream.chunk_count = 1
        stream.last_sequence = 0
        s.commit()
        mark_streaming_started(s, turn_id, attempt_id, stream_id=stream.id)
    with F() as s:
        turn, _attempt, _event = commit_turn(s, turn_id, attempt_id)
        return turn.status


def _clock_events(db):
    return db.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.event_type.in_(sorted(C.CLOCK_LIFECYCLE_EVENT_TYPES)))).scalars().all()


def test_clock_advancement_defers_while_dm_turn_in_flight_then_applies():
    F, db, cid, owner, tid = _setup()
    clock_id = _mkclock(db, cid)
    _play(db, cid, 2)
    run = maybe_trigger_post_turn(db, cid, trigger=FORCE)
    turn_id, attempt_id, source_rev = _start_turn(F, cid, owner, tid)

    sweep = run_post_turn_sweep(db)
    assert sweep["deferred"] == [str(run.id)] and sweep["executed"] == [] and sweep["failed"] == []
    db.expire_all()
    assert _rev(db, cid) == source_rev
    assert _clock_events(db) == []
    assert int(db.get(CampaignClock, clock_id).progress) == 0
    fresh = db.get(PostTurnRun, run.id)
    assert (fresh.status, int(fresh.attempts or 0)) == ("pending", 0)
    assert db.get(WorkerExecution, run.id) is None  # never claimed
    assert get_checkpoint(db, cid).processed_through_sequence == 0

    assert _finish_turn(F, cid, tid, turn_id, attempt_id) == "succeeded"

    sweep = run_post_turn_sweep(db)
    assert str(run.id) in sweep["executed"] and sweep["deferred"] == [] and sweep["failed"] == []
    db.expire_all()
    assert int(db.get(CampaignClock, clock_id).progress) == 1
    assert [e.event_type for e in _clock_events(db)] == ["clock.advanced"]
    assert db.get(PostTurnRun, run.id).status == "succeeded"
    assert get_checkpoint(db, cid).processed_through_sequence >= run.to_sequence
    db.close()


class _TurnStartsDuringDecision(FakeDecisionAdapter):
    """A player submits while the clock decision model call is running."""

    def __init__(self, start_turn):
        super().__init__(answers={QUESTION: "ADVANCE_1"})
        self._start_turn = start_turn
        self.started: tuple | None = None

    def execute(self, request, *, model, timeout):
        if self.started is None:
            self.started = self._start_turn()
        return super().execute(request, model=model, timeout=timeout)


def test_dm_turn_started_during_clock_decision_commits_across_deferred_sweep(monkeypatch):
    F, db, cid, owner, tid = _setup()
    clock_id = _mkclock(db, cid, criteria_kind="semantic")
    _play(db, cid, 2)
    run = maybe_trigger_post_turn(db, cid, trigger=FORCE)
    adapter = _TurnStartsDuringDecision(lambda: _start_turn(F, cid, owner, tid))
    monkeypatch.setattr(C, "DecisionService", lambda: DecisionService(adapter))

    # The pre-claim and pre-model checks pass (no turn yet); the turn starts
    # during the model call, so the locked re-check vetoes the commit.
    sweep = run_post_turn_sweep(db)
    assert adapter.started is not None and len(adapter.calls) == 1
    assert sweep["deferred"] == [str(run.id)] and sweep["failed"] == []
    turn_id, attempt_id, source_rev = adapter.started
    db.expire_all()
    assert _rev(db, cid) == source_rev
    assert _clock_events(db) == []
    fresh = db.get(PostTurnRun, run.id)
    assert (fresh.status, int(fresh.attempts or 0)) == ("pending", 0)
    assert fresh.result == {"deferred": True, "reason": "dm_turn_in_flight"}
    assert db.get(WorkerExecution, run.id) is None  # ledger released for re-run

    # A sweep while the turn is still in flight defers again before any model call.
    assert run_post_turn_sweep(db)["deferred"] == [str(run.id)]
    assert len(adapter.calls) == 1

    # The in-flight turn commits against its original source revision.
    assert _finish_turn(F, cid, tid, turn_id, attempt_id) == "succeeded"

    sweep = run_post_turn_sweep(db)
    assert str(run.id) in sweep["executed"] and sweep["failed"] == []
    db.expire_all()
    assert int(db.get(CampaignClock, clock_id).progress) == 1
    assert db.get(PostTurnRun, run.id).status == "succeeded"
    db.close()


def test_compose_operation_id_bounds_long_keys():
    assert compose_operation_id("op", "clock", "x", "1-4") == "op:clock:x:1-4"
    assert compose_operation_id(None, "clock", "x") == "clock:x"
    base = f"opening-intro:{uuid.uuid4()}:{uuid.uuid4()}"
    a = compose_operation_id(base, "clock", uuid.uuid4(), "9-12")
    b = compose_operation_id(base, "clock", uuid.uuid4(), "9-12")
    assert len(a) <= OPERATION_ID_MAX_LENGTH and len(b) <= OPERATION_ID_MAX_LENGTH
    assert a != b and a.startswith("opening-intro:")
    long_key = "k" * 300
    assert compose_operation_id(long_key, "s") == compose_operation_id(long_key, "s")


def test_clock_event_operation_id_bounded_for_long_source_operation_id():
    _F, db, cid, *_ = _setup()
    _mkclock(db, cid)
    lo = _rev(db, cid) + 1
    _play(db, cid, 1)
    hi = _rev(db, cid)
    events = list(db.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.campaign_id == cid, CampaignDomainEvent.sequence >= lo,
        CampaignDomainEvent.sequence <= hi)).scalars().all())
    source_op = f"opening-intro:{cid}:{uuid.uuid4()}"
    out = C.consolidate_clocks_for_range(db, cid, lo, hi, events, operation_id=source_op)
    assert out["advanced"] == 1
    (event,) = _clock_events(db)
    assert len(event.operation_id) <= OPERATION_ID_MAX_LENGTH
    assert event.operation_id.startswith("opening-intro:")
    db.close()
