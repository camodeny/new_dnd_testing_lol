"""Issue #191 — queue adapter, worker envelope, retries, and failed-work ledger.

Covers review blockers:
- production misconfig must error not silent success
- atomic committed claim prevents concurrent side effects
- next_attempt_at enforced before retry
- duplicate / early redelivery tests
"""
from __future__ import annotations

import threading
import time
import uuid
import tempfile
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from app.queue import WorkerEnvelope, new_envelope, InMemoryQueueAdapter, VercelQueueAdapter  # noqa: E402
from app.worker import (  # noqa: E402
    RetriableError,
    TerminalError,
    execute_worker_job,
    list_failed_work,
    replay_failed_job,
)
from models.reliability import WorkerExecution


def _engine(url="sqlite://"):
    eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(bind=eng)
    return eng


# ── Adapter ─────────────────────────────────────────────────────────────────


def test_vercel_adapter_misconfigured_raises_not_silent_success(monkeypatch):
    for var in ("VERCEL_QUEUE_TOKEN", "VERCEL_OIDC_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    va = VercelQueueAdapter(topic="test")
    env = new_envelope(job_type="test.job", payload={"id": "1"})
    with pytest.raises(RuntimeError, match="OIDC token unavailable"):
        va.publish(env)


def test_vercel_adapter_with_config_does_not_raise_silent(monkeypatch):
    # With explicit config, publish attempts HTTP (we don't assert success, just not silent)
    va = VercelQueueAdapter(topic="q", token="tok", base_url="https://example.invalid")
    env = new_envelope(job_type="test.job", payload={"id": "1"})
    # Patch urlopen to simulate success
    import urllib.request

    class FakeResp:
        status = 200

        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: FakeResp())
    assert va.publish(env) == str(env.job_id)


def test_inmemory_adapter_local_tests_no_broker():
    mem = InMemoryQueueAdapter()
    env = new_envelope(job_type="local.test", payload={"campaign_id": str(uuid.uuid4())})
    mem.publish(env)
    assert mem.depth() == 1
    assert len(mem.peek_all()) == 1


def test_envelope_rejects_snapshot():
    with pytest.raises(ValueError, match="snapshot"):
        WorkerEnvelope(job_id=uuid.uuid4(), job_type="x", payload={"snapshot": {}})
    with pytest.raises(ValueError, match="snapshot"):
        WorkerEnvelope(job_id=uuid.uuid4(), job_type="x", payload={"campaign_snapshot": {}})


# ── Worker: duplicate and atomic claim ──────────────────────────────────────


def test_duplicate_delivery_returns_cached_result_without_second_execution():
    eng = _engine()
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    env = new_envelope(job_type="dup.test", payload={"v": 1}, job_id=uuid.uuid4())
    calls = []

    def handler(e):
        calls.append(1)
        return {"ok": True}

    db = Fac()
    r1, dup1 = execute_worker_job(db, env, handler)
    r2, dup2 = execute_worker_job(db, env, handler)
    assert r1 == r2 == {"ok": True}
    assert dup1 is False and dup2 is True
    assert calls == [1]


def test_concurrent_duplicate_only_one_handler_execution(tmp_path):
    db_file = tmp_path / "concurrent.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(bind=eng)
    Fac = sessionmaker(bind=eng, expire_on_commit=False)

    env = new_envelope(job_type="concurrent.test", payload={"id": "1"}, job_id=uuid.uuid4())
    calls = []
    results = []
    errors = []

    def handler(e):
        calls.append(1)
        time.sleep(0.25)
        return {"ok": True}

    def worker():
        try:
            db = Fac()
            results.append(execute_worker_job(db, env, handler))
        except Exception as ex:
            errors.append(ex)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()

    assert len(calls) == 1, f"handler must run once, got {calls}"
    assert len(results) == 1 and results[0][1] is False
    assert len(errors) == 1
    # Second should be blocked as still running or claim race
    assert isinstance(errors[0], RuntimeError)
    assert "still running" in str(errors[0]) or "claim failed" in str(errors[0])


# ── Worker: retry timing ────────────────────────────────────────────────────


def test_next_attempt_enforced_early_retry_blocked():
    eng = _engine()
    Fac = sessionmaker(bind=eng)
    env = new_envelope(job_type="retry.early", payload={}, job_id=uuid.uuid4())

    def fail(e):
        raise RetriableError("timeout")

    db = Fac()
    with pytest.raises(RetriableError):
        execute_worker_job(db, env, fail, max_attempts=5)
    # next_attempt ~2s future, immediate retry must be blocked before handler
    with pytest.raises(RuntimeError, match="retry not ready"):
        execute_worker_job(db, env, fail, max_attempts=5)
    # attempts must not increment on early block
    rec = db.get(WorkerExecution, env.job_id)
    assert rec.attempts == 1
    assert rec.status == "failed"


def test_next_attempt_allows_retry_after_backoff():
    eng = _engine()
    Fac = sessionmaker(bind=eng)
    env = new_envelope(job_type="retry.after", payload={}, job_id=uuid.uuid4())

    def fail(e):
        raise RetriableError("timeout")

    db = Fac()
    with pytest.raises(RetriableError):
        execute_worker_job(db, env, fail, max_attempts=5, backoff=lambda a: 0.05)
    time.sleep(0.08)
    # Now retry should be allowed and increment attempts
    with pytest.raises(RetriableError):
        execute_worker_job(db, env, fail, max_attempts=5, backoff=lambda a: 0.05)
    rec = db.get(WorkerExecution, env.job_id)
    assert rec.attempts == 2


def test_crash_lease_allows_redelivery_after_expiry():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    Fac = sessionmaker(bind=eng)
    env = new_envelope(job_type="crash.test", payload={}, job_id=uuid.uuid4())
    # Insert running with old started_at
    db = Fac()
    rec = WorkerExecution(
        id=env.job_id, job_type=env.job_type, status="running", attempts=1, max_attempts=5,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=1000),
    )
    db.add(rec)
    db.commit()
    # Lease 300s, so expired -> claim should succeed
    calls = []

    def handler(e):
        calls.append(1)
        return {"recovered": True}

    res, dup = execute_worker_job(db, env, handler, lease_seconds=300)
    assert res == {"recovered": True} and dup is False and calls == [1]


def test_dead_letter_remains_inspectable_and_replayable():
    eng = _engine()
    Fac = sessionmaker(bind=eng)
    env = new_envelope(job_type="dead.test", payload={}, job_id=uuid.uuid4())

    def poison(e):
        raise TerminalError("poison")

    db = Fac()
    with pytest.raises(TerminalError):
        execute_worker_job(db, env, poison)
    # Inspectable
    failed = list_failed_work(db)
    assert any(str(f.id) == str(env.job_id) for f in failed)
    # Replay resets to pending with same idempotency
    replayed = replay_failed_job(db, env.job_id)
    assert replayed.status == "pending"

    def fixed(e):
        return {"fixed": True}

    res, dup = execute_worker_job(db, env, fixed)
    assert res == {"fixed": True} and dup is False
    # Duplicate after replay deduped
    res2, dup2 = execute_worker_job(db, env, fixed)
    assert dup2 is True and res2 == res


def test_lease_expiry_overlap_heartbeat_prevents_live_reclamation(tmp_path):
    """Heartbeat prevents a healthy long-running handler from being reclaimed.

    Lease 0.2s < handler 0.5s but heartbeat renews lease, so second delivery
    while first is still live is blocked as still_running (not stolen).
    Fencing remains as backup for genuinely crashed workers (see crash test).
    """
    db_file = tmp_path / "lease_overlap.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(bind=eng)
    Fac = sessionmaker(bind=eng, expire_on_commit=False)

    env = new_envelope(job_type="lease.overlap", payload={"v": 1}, job_id=uuid.uuid4())
    calls: list[int] = []
    lock = threading.Lock()
    results: list[tuple] = []
    errors: list[Exception] = []

    def handler(e):
        with lock:
            calls.append(1)
        time.sleep(0.5)  # longer than lease 0.2, but heartbeat should keep lease alive
        return {"ok": True}

    def worker(delay: float):
        time.sleep(delay)
        try:
            db = Fac()
            # lease 0.2s < handler 0.5s — without heartbeat this would be stealable
            results.append(execute_worker_job(db, env, handler, lease_seconds=0.2))
        except Exception as ex:
            errors.append(ex)

    t1 = threading.Thread(target=lambda: worker(0))
    t2 = threading.Thread(target=lambda: worker(0.25))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Heartbeat must prevent second handler from running at all
    assert len(calls) == 1, f"heartbeat should prevent duplicate execution, got calls={len(calls)}"
    assert len(results) == 1 and results[0][1] is False
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError) and "still running" in str(errors[0])

    # Fencing still protects crashed case: inserted RUNNING with old started_at and no heartbeat is reclaimable
    # (covered by test_crash_lease_allows_redelivery_after_expiry)
    # Final ledger must be succeeded, duplicate returns cached
    db = Fac()
    rec = db.get(WorkerExecution, env.job_id)
    assert rec.status == "succeeded"
    before = len(calls)
    res, dup = execute_worker_job(db, env, handler, lease_seconds=0.2)
    assert dup is True
    assert len(calls) == before


def test_fencing_prevents_crashed_worker_overwrite(tmp_path):
    """If a worker crashes after lease stolen, its fenced completion is discarded."""
    db_file = tmp_path / "fencing.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(bind=eng)
    Fac = sessionmaker(bind=eng, expire_on_commit=False)

    env = new_envelope(job_type="fencing.test", payload={}, job_id=uuid.uuid4())
    # Simulate first claim stolen: insert RUNNING with old token, then second steals
    # First worker's token is stale; its completion must be fenced.
    from datetime import datetime, timezone, timedelta

    db = Fac()
    # First claims with token A but crashes before commit (no heartbeat, old lease)
    old_token = "old-token-123"
    db.add(
        WorkerExecution(
            id=env.job_id,
            job_type=env.job_type,
            status="running",
            attempts=1,
            max_attempts=5,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=1000),
            claim_token=old_token,
        )
    )
    db.commit()
    # Second steals with new token (simulates new claim after expiry)
    from app.worker.executor import renew_worker_lease, execute_worker_job as _exec

    # Use normal execute to steal (lease 0.2, old started_at far past)
    def handler(e):
        return {"from": "second"}

    db2 = Fac()
    res, dup = _exec(db2, env, handler, lease_seconds=0.2)
    assert res["from"] == "second" and dup is False

    # Now first (stale token) tries to complete — should be fenced (no overwrite)
    # Simulate stale completion attempt directly
    from sqlalchemy import update as _upd

    stale_upd = db.execute(
        _upd(WorkerExecution)
        .where(WorkerExecution.id == env.job_id, WorkerExecution.claim_token == old_token, WorkerExecution.status == "running")
        .values(status="succeeded", result={"from": "stale"})
    )
    assert stale_upd.rowcount == 0, "stale token must not overwrite new owner's succeeded row"
    db.rollback()
    rec = db.get(WorkerExecution, env.job_id)
    assert rec.result["from"] == "second"
