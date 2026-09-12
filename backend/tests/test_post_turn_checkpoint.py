"""Issue #216 — post-turn durable checkpoint, batching, cumulative catch-up."""
from __future__ import annotations

import uuid

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
from app.post_turn.service import (  # noqa: E402
    get_batch_threshold,
    get_checkpoint,
    get_outstanding_range,
    get_post_turn_status,
    handle_post_turn_envelope,
    mark_post_turn_skipped,
    maybe_trigger_post_turn,
    run_post_turn_range,
    should_trigger_post_turn,
)
from app.queue.envelope import WorkerEnvelope  # noqa: E402
from app.worker.executor import execute_worker_job  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.post_turn import PostTurnCheckpoint, PostTurnRun  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import Outbox  # noqa: E402


def _factory():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _campaign(db, rev0=True):
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email=f"{owner}@x.com", username="t"))
    db.flush()
    c = Campaign(owner_id=owner, name="c")
    db.add(c)
    db.flush()
    db.commit()
    db.refresh(c)
    return c


def _commit(db, cid, rev, etype="game.play", op=None):
    return commit_campaign_mutation(db, cid, expected_revision=rev, event_type=etype,
                                    payload={"n": rev}, operation_id=op or f"op-{rev}")


def test_checkpoint_defaults_zero_and_exposed():
    F = _factory()
    db = F()
    c = _campaign(db)
    cp = get_checkpoint(db, c.id)
    assert cp.processed_through_sequence == 0
    st = get_post_turn_status(db, c.id)
    assert st["checkpoint"] == 0
    assert st["outstanding"]["outstanding"] == 0
    db.close()


def test_normal_batch_trigger_and_success_advances():
    F = _factory()
    db = F()
    c = _campaign(db)
    for i in range(4):
        _commit(db, c.id, i)
    run = maybe_trigger_post_turn(db, c.id)
    assert run is not None
    assert (run.from_sequence, run.to_sequence) == (1, 4)
    # outbox emitted with run id as job id
    assert db.get(Outbox, run.id) is not None
    out = run_post_turn_range(db, c.id, 1, 4, run_id=run.id)
    assert out["duplicate"] is False
    assert db.get(PostTurnCheckpoint, c.id).processed_through_sequence == 4
    assert db.get(PostTurnRun, run.id).status == "succeeded"
    db.close()


def test_below_threshold_does_not_trigger_but_force_does():
    F = _factory()
    db = F()
    c = _campaign(db)
    for i in range(2):
        _commit(db, c.id, i)
    assert maybe_trigger_post_turn(db, c.id) is None
    run = maybe_trigger_post_turn(db, c.id, trigger="force")
    assert run is not None and (run.from_sequence, run.to_sequence) == (1, 2)
    db.close()


def test_failure_does_not_advance_and_catchup_is_cumulative():
    F = _factory()
    db = F()
    c = _campaign(db)
    for i in range(4):
        _commit(db, c.id, i)
    run1 = maybe_trigger_post_turn(db, c.id, trigger="force")
    assert (run1.from_sequence, run1.to_sequence) == (1, 4)

    def boom(events):
        raise RuntimeError("model down")

    try:
        run_post_turn_range(db, c.id, 1, 4, run_id=run1.id, consolidate_fn=boom)
        assert False, "should raise"
    except RuntimeError:
        pass
    db.expire_all()
    assert db.get(PostTurnCheckpoint, c.id).processed_through_sequence == 0
    assert db.get(PostTurnRun, run1.id).status == "failed"
    assert "model down" in (db.get(PostTurnRun, run1.id).failure_reason or "")

    # Play continues 5..6 while 1..4 outstanding
    _commit(db, c.id, 4)
    _commit(db, c.id, 5)
    span = get_outstanding_range(db, c.id)
    assert (span["from_sequence"], span["to_sequence"]) == (1, 6)
    run2 = maybe_trigger_post_turn(db, c.id, trigger="force")
    assert (run2.from_sequence, run2.to_sequence) == (1, 6)
    out = run_post_turn_range(db, c.id, 1, 6, run_id=run2.id)
    assert out["processed_through"] == 6
    assert db.get(PostTurnCheckpoint, c.id).processed_through_sequence == 6
    db.close()


def test_duplicate_worker_delivery_idempotent():
    F = _factory()
    db = F()
    c = _campaign(db)
    for i in range(4):
        _commit(db, c.id, i)
    run = maybe_trigger_post_turn(db, c.id, trigger="force")
    env = WorkerEnvelope(job_id=run.id, job_type="post_turn.process", campaign_id=c.id,
                         payload={"run_id": str(run.id), "campaign_id": str(c.id),
                                  "from_sequence": 1, "to_sequence": 4, "trigger": "force"})
    r1, d1 = execute_worker_job(db, env, lambda e: handle_post_turn_envelope(e, db))
    assert d1 is False and r1["processed_through"] == 4
    # Second delivery of same logical job hits WorkerExecution dedupe
    r2, d2 = execute_worker_job(db, env, lambda e: handle_post_turn_envelope(e, db))
    assert d2 is True and r2 == r1
    # Direct range replay after checkpoint advanced is also a duplicate no-op
    out = run_post_turn_range(db, c.id, 1, 4, run_id=run.id)
    assert out["duplicate"] is True
    assert db.get(PostTurnCheckpoint, c.id).processed_through_sequence == 4
    db.close()


def test_consume_queue_delivery_path(monkeypatch):
    from app.queue.consumer import consume_queue_delivery
    import database
    F = _factory()
    monkeypatch.setattr(database, "SessionLocal", F)
    s = F()
    c = _campaign(s)
    for i in range(2):
        _commit(s, c.id, i)
    run = maybe_trigger_post_turn(s, c.id, trigger="force")
    body = WorkerEnvelope(job_id=run.id, job_type="post_turn.process", campaign_id=c.id,
                          payload={"run_id": str(run.id), "campaign_id": str(c.id),
                                   "from_sequence": 1, "to_sequence": 2, "trigger": "force"}).to_dict()
    result, dup = consume_queue_delivery(s, body)
    assert dup is False and result["processed_through"] == 2
    s.close()


def test_checkpoint_never_rolls_back_and_skip_requires_audit():
    F = _factory()
    db = F()
    c = _campaign(db)
    for i in range(5):
        _commit(db, c.id, i)
    run = maybe_trigger_post_turn(db, c.id, trigger="force")
    run_post_turn_range(db, c.id, 1, 5, run_id=run.id)
    assert db.get(PostTurnCheckpoint, c.id).processed_through_sequence == 5
    # stale range cannot move checkpoint back
    out = run_post_turn_range(db, c.id, 1, 3)
    assert out["duplicate"] is True
    assert db.get(PostTurnCheckpoint, c.id).processed_through_sequence == 5
    # skip without reason rejected
    try:
        mark_post_turn_skipped(db, c.id, 5, "")
        assert False
    except ValueError:
        pass
    db.rollback()
    # audited skip forward works (using new events)
    _commit(db, c.id, 5)
    _commit(db, c.id, 6)
    skipped = mark_post_turn_skipped(db, c.id, 7, "operator decision: corrupt span reviewed")
    assert skipped.status == "skipped"
    assert db.get(PostTurnCheckpoint, c.id).processed_through_sequence == 7
    # gap detection: deleting/never-committing a sequence fails instead of skipping
    _commit(db, c.id, 7)  # seq 8
    db.execute(Campaign.__table__.update().where(Campaign.id == c.id).values(revision=10))
    db.commit()
    try:
        run_post_turn_range(db, c.id, 8, 10)
        assert False
    except RuntimeError as e:
        assert "gaps" in str(e)
    db.rollback()
    assert db.get(PostTurnCheckpoint, c.id).processed_through_sequence == 7
    db.close()


def test_batch_threshold_configurable():
    import os
    os.environ["POST_TURN_BATCH_SIZE"] = "2"
    try:
        assert get_batch_threshold() == 2
        assert should_trigger_post_turn(2) is True
        assert should_trigger_post_turn(1) is False
        assert should_trigger_post_turn(1, "force") is True
        assert should_trigger_post_turn(1, "critical") is True
    finally:
        del os.environ["POST_TURN_BATCH_SIZE"]
    assert get_batch_threshold() == 4


def test_observability_status():
    F = _factory()
    db = F()
    c = _campaign(db)
    for i in range(4):
        _commit(db, c.id, i)
    run = maybe_trigger_post_turn(db, c.id, trigger="force")
    st = get_post_turn_status(db, c.id)
    assert st["checkpoint"] == 0
    assert st["outstanding"]["outstanding"] == 4
    assert st["run_attempts"] >= 1
    assert st["queue_lag_seconds"] >= 0
    run_post_turn_range(db, c.id, 1, 4, run_id=run.id)
    st2 = get_post_turn_status(db, c.id)
    assert st2["checkpoint"] == 4
    assert st2["outstanding"]["outstanding"] == 0
    db.close()
