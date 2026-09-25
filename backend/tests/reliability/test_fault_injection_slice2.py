"""Issue #270 slice 2: further deterministic fault-injection cases.

Hermetic conventions (same as slice 1): synthetic data only, SQLite unless
``FAULT_TEST_DATABASE_URL`` names a disposable local database, no real queue /
provider / credentials / production data.

Covered here:
- API response loss replays the committed idempotent result (no re-execution).
- DB failure mid-transaction rolls back with no partial state.
- Worker crash before completion is recovered by the stuck-execution sweeper
  and safely redelivered.
- Stale concurrent campaign mutation loses cleanly via revision conflict.
- Terminal poison work stays durable/inspectable and replays after correction.
- Provider-like transient failure recovers through worker retry; terminal
  failure goes straight to dead letter (billing assertions skipped: #259 open).
- Telemetry failure cannot corrupt gameplay.

Billing / recovery-accounting assertions are out of scope for this slice
(open #259); the placeholder below is skipped with reason.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.campaigns.events import RevisionConflictError, commit_campaign_mutation
from app.idempotency import (
    IdempotencyConflictError,
    execute_idempotent_command,
)
from app.observability.service import (
    begin_operation,
    fail_soft,
    get_trace,
    mark_milestone,
)
from app.observability.tracing import trace_context
from app.queue.envelope import WorkerEnvelope
from app.worker import (
    RetriableError,
    TerminalError,
    execute_worker_job,
    list_failed_work,
    recover_stuck_executions,
    replay_failed_job,
)
from database import Base
from models.campaigns import Campaign, CampaignDomainEvent
from models.profiles import Profile
from models.reliability import OperationTrace, Outbox, WorkerExecution
from tests.reliability.faults import FaultScenario


def _safe_engine(tmp_path):
    url = os.getenv("FAULT_TEST_DATABASE_URL")
    if not url:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'fault-injection-slice2.sqlite'}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        Base.metadata.create_all(engine)
        return engine
    parsed = urlparse(url)
    database = parsed.path.lstrip("/")
    local_hosts = {"localhost", "127.0.0.1", "postgres"}
    if parsed.hostname not in local_hosts or database not in {"ci_test", "test", "reliability_test"}:
        pytest.fail(
            "FAULT_TEST_DATABASE_URL must target an explicitly named disposable "
            "database on localhost/127.0.0.1/postgres"
        )
    return create_engine(url)


def _seed_campaign(factory):
    owner_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    with factory() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(
                text("INSERT INTO auth.users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"),
                {"id": owner_id},
            )
            db.flush()
        db.add(Profile(id=owner_id, email=f"fault2-{owner_id}@example.invalid"))
        db.commit()
    with factory() as db:
        db.add(Campaign(id=campaign_id, owner_id=owner_id, name="Synthetic reliability campaign"))
        db.commit()
    return campaign_id, owner_id


def _envelope(campaign_id, job_type="turn.resolve", operation_id=None):
    return WorkerEnvelope(
        job_id=uuid.uuid4(),
        job_type=job_type,
        campaign_id=campaign_id,
        operation_id=operation_id or f"fault2-op-{uuid.uuid4()}",
        idempotency_key=f"fault2-key-{uuid.uuid4()}",
        trace_id=f"fault2-trace-{uuid.uuid4().hex[:16]}",
        payload={"campaign_id": str(campaign_id), "synthetic": True},
    )


# ── API failure: response loss replays committed result ──────────────────────


def test_api_response_loss_replays_committed_result_without_reexecution(tmp_path):
    scenario = FaultScenario("api_response_loss_idempotent_replay")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _, owner_id = _seed_campaign(factory)
    calls = 0

    def execute():
        nonlocal calls
        calls += 1
        return {"turn": "resolved", "synthetic": True}

    identity = {
        "actor_id": owner_id,
        "idempotency_key": f"fault2-api-{uuid.uuid4()}",
        "command_type": "turn.resolve",
        "scope_type": "campaign",
        "scope_id": "synthetic-scope",
        "payload": {"action": "resolve"},
    }
    with factory() as db:
        result, duplicate = execute_idempotent_command(db, **identity, execute=execute)
    assert scenario.hit_once("response_lost_after_commit", result=result)
    # Caller never saw the response; deterministic retry replays the commit.
    with factory() as db:
        replayed, replay_duplicate = execute_idempotent_command(db, **identity, execute=execute)
    assert replayed == result
    assert (duplicate, replay_duplicate) == (False, True)
    assert calls == 1
    # Same key with different payload is a conflict, not a silent overwrite.
    with factory() as db:
        with pytest.raises(IdempotencyConflictError):
            execute_idempotent_command(
                db, **{**identity, "payload": {"action": "different"}}, execute=execute
            )
    assert calls == 1
    scenario.record("converged", handler_calls=calls, duplicate_suppressed=replay_duplicate)


# ── DB failure: mid-transaction abort leaves no partial state ────────────────


def test_db_failure_mid_transaction_rolls_back_without_partial_state(tmp_path):
    scenario = FaultScenario("db_failure_rollback")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id, _ = _seed_campaign(factory)
    operation_id = f"fault2-op-{uuid.uuid4()}"

    def poisoned_mutate(_campaign):
        scenario.record("db_fault_raised_mid_transaction", operation_id=operation_id)
        raise RuntimeError("synthetic db constraint failure")

    with factory() as db:
        with pytest.raises(RuntimeError, match="synthetic db constraint failure"):
            commit_campaign_mutation(
                db,
                campaign_id,
                expected_revision=0,
                event_type="turn.requested",
                operation_id=operation_id,
                payload={"synthetic": True},
                outbox_event_type="turn.resolve",
                outbox_payload={"campaign_id": str(campaign_id)},
                mutate=poisoned_mutate,
            )
    with factory() as db:
        campaign = db.get(Campaign, campaign_id)
        events = db.execute(
            select(CampaignDomainEvent).where(CampaignDomainEvent.campaign_id == campaign_id)
        ).scalars().all()
        outbox_rows = db.execute(
            select(Outbox).where(Outbox.operation_id == operation_id)
        ).scalars().all()
        assert campaign.revision == 0
        assert events == []
        assert outbox_rows == []
    scenario.record("converged", final_revision=campaign.revision, domain_events=0, outbox_rows=0)


# ── Worker crash before completion: sweeper recovers, redelivery succeeds ────


def test_worker_crash_before_completion_recovers_and_redelivers_once(tmp_path):
    scenario = FaultScenario("worker_crash_before_completion")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id, _ = _seed_campaign(factory)
    envelope = _envelope(campaign_id)

    # Committed claim, then the process dies before the fenced completion.
    crashed_at = datetime.now(timezone.utc) - timedelta(seconds=601)
    with factory() as db:
        db.add(
            WorkerExecution(
                id=envelope.job_id,
                job_type=envelope.job_type,
                campaign_id=campaign_id,
                operation_id=envelope.operation_id,
                idempotency_key=envelope.idempotency_key,
                payload=envelope.payload,
                status="running",
                attempts=1,
                max_attempts=5,
                trace_id=envelope.trace_id,
                started_at=crashed_at,
            )
        )
        db.commit()
    assert scenario.hit_once("worker_crashed_while_running", job_id=envelope.job_id)

    with factory() as db:
        assert recover_stuck_executions(db, lease_seconds=300) == 1
    handler_calls = 0

    def apply_gameplay_effect(_envelope):
        nonlocal handler_calls
        handler_calls += 1
        with factory() as effect_db:
            campaign, event = commit_campaign_mutation(
                effect_db,
                campaign_id,
                expected_revision=0,
                event_type="turn.resolved",
                operation_id=envelope.operation_id,
                payload={"synthetic": True},
            )
            return {"campaign_revision": campaign.revision, "event_id": str(event.id)}

    with factory() as db:
        result, duplicate = execute_worker_job(db, envelope, apply_gameplay_effect)
    with factory() as db:
        execution = db.get(WorkerExecution, envelope.job_id)
        campaign = db.get(Campaign, campaign_id)
        assert execution.status == "succeeded"
        assert campaign.revision == 1
    assert handler_calls == 1 and duplicate is False
    assert result["campaign_revision"] == 1
    scenario.record(
        "converged",
        handler_calls=handler_calls,
        worker_attempts=execution.attempts,
        final_revision=campaign.revision,
    )


# ── Stale concurrent mutation loses cleanly ──────────────────────────────────


def test_stale_concurrent_mutation_loses_cleanly_via_revision_conflict(tmp_path):
    scenario = FaultScenario("stale_concurrent_mutation_conflict")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id, _ = _seed_campaign(factory)

    with factory() as db:
        commit_campaign_mutation(
            db,
            campaign_id,
            expected_revision=0,
            event_type="turn.requested",
            operation_id=f"fault2-winner-{uuid.uuid4()}",
            payload={"synthetic": True},
        )
    assert scenario.hit_once("winner_committed_revision_1", campaign_id=campaign_id)

    with factory() as db:
        with pytest.raises(RevisionConflictError) as excinfo:
            commit_campaign_mutation(
                db,
                campaign_id,
                expected_revision=0,  # stale read: winner already moved to 1
                event_type="turn.requested",
                operation_id=f"fault2-stale-{uuid.uuid4()}",
                payload={"synthetic": True},
            )
        assert (excinfo.value.expected_revision, excinfo.value.actual_revision) == (0, 1)
    with factory() as db:
        campaign = db.get(Campaign, campaign_id)
        events = db.execute(
            select(CampaignDomainEvent)
            .where(CampaignDomainEvent.campaign_id == campaign_id)
            .order_by(CampaignDomainEvent.sequence)
        ).scalars().all()
        assert campaign.revision == 1
        assert [e.event_type for e in events] == ["turn.requested"]
    scenario.record("converged", final_revision=1, domain_event_count=len(events))


# ── Terminal poison work: durable, inspectable, replayable after correction ──


def test_terminal_poison_work_replays_after_correction(tmp_path):
    scenario = FaultScenario("terminal_poison_work_replay")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id, _ = _seed_campaign(factory)
    envelope = _envelope(campaign_id)

    def poisoned(_envelope):
        raise TerminalError("synthetic poison: malformed payload")

    with factory() as db:
        with pytest.raises(TerminalError, match="synthetic poison"):
            execute_worker_job(db, envelope, poisoned)
    assert scenario.hit_once("poison_detected_terminal", job_id=envelope.job_id)

    with factory() as db:
        execution = db.get(WorkerExecution, envelope.job_id)
        assert execution.status == "dead_letter"
        assert execution.error_class == "terminal"
        failed = list_failed_work(db)
        assert [row.id for row in failed] == [envelope.job_id]
        # Direct redelivery without replay stays terminal and inspectable.
        with pytest.raises(TerminalError, match="dead_letter"):
            execute_worker_job(db, envelope, poisoned)
        assert replay_failed_job(db, envelope.job_id) is not None

    def corrected(_envelope):
        return {"recovered": True}

    with factory() as db:
        result, duplicate = execute_worker_job(db, envelope, corrected)
    with factory() as db:
        execution = db.get(WorkerExecution, envelope.job_id)
        assert execution.status == "succeeded"
        assert list_failed_work(db) == []
    assert result == {"recovered": True} and duplicate is False
    scenario.record("converged", replayed=True, final_status=execution.status)


# ── Provider failure recovery through the worker retry path ──────────────────


def test_provider_transient_failure_recovers_via_worker_retry(tmp_path):
    scenario = FaultScenario("provider_transient_worker_retry")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id, _ = _seed_campaign(factory)
    envelope = _envelope(campaign_id, job_type="narration.generate")
    attempts = 0

    def flaky_provider(_envelope):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            scenario.record("provider_failure", error_class="retriable", attempt=attempts)
            raise RetriableError("synthetic provider timeout")
        return {"content": "recovered narration", "network_used": False}

    with factory() as db:
        with pytest.raises(RetriableError, match="synthetic provider timeout"):
            execute_worker_job(db, envelope, flaky_provider, backoff=lambda _attempt: 0)
        db.expire_all()  # executor fences via UPDATE bypassing the identity map
        pending = db.get(WorkerExecution, envelope.job_id)
        assert pending.status == "failed" and pending.error_class == "retriable"
        assert pending.next_attempt_at is not None
    with factory() as db:
        result, duplicate = execute_worker_job(db, envelope, flaky_provider, backoff=lambda _attempt: 0)
    assert result["content"] == "recovered narration"
    assert duplicate is False and attempts == 2
    scenario.record("converged", attempts=attempts, network_used=False)


def test_provider_terminal_failure_goes_straight_to_dead_letter(tmp_path):
    scenario = FaultScenario("provider_terminal_no_retry")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id, _ = _seed_campaign(factory)
    envelope = _envelope(campaign_id, job_type="narration.generate")

    def terminal_provider(_envelope):
        raise TerminalError("synthetic provider malformed response")

    with factory() as db:
        with pytest.raises(TerminalError, match="synthetic provider malformed"):
            execute_worker_job(db, envelope, terminal_provider)
        db.expire_all()  # executor fences via UPDATE bypassing the identity map
        execution = db.get(WorkerExecution, envelope.job_id)
        assert execution.status == "dead_letter"
        assert execution.attempts == 1
    scenario.record("converged", attempts=execution.attempts, retried=False)


@pytest.mark.skip(reason="Billing/recovery-accounting assertions depend on open #259")
def test_provider_failure_non_billing_accounting():
    raise AssertionError("unreachable: skipped pending #259")


# ── Telemetry failure cannot corrupt gameplay ─────────────────────────────────


def test_telemetry_failure_cannot_corrupt_gameplay(tmp_path):
    scenario = FaultScenario("telemetry_failure_gameplay_isolation")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id, _ = _seed_campaign(factory)
    trace_id = f"fault2-trace-{uuid.uuid4().hex[:16]}"
    operation_id = f"fault2-op-{uuid.uuid4()}"

    with factory() as db:
        begin_operation(factory, trace_id=trace_id, operation_id=operation_id)
    assert scenario.hit_once("telemetry_backend_unavailable", trace_id=trace_id)

    # Gameplay commits on its own session while telemetry is down.
    with trace_context(trace_id, operation_id):
        with factory() as gameplay_db:
            campaign, event = commit_campaign_mutation(
                gameplay_db,
                campaign_id,
                expected_revision=0,
                event_type="turn.requested",
                operation_id=operation_id,
                payload={"synthetic": True},
                outbox_event_type="turn.resolve",
                outbox_payload={"campaign_id": str(campaign_id)},
            )
            assert campaign.revision == 1

    def unavailable_factory():
        raise RuntimeError("telemetry database unavailable")

    assert (
        fail_soft(
            lambda: mark_milestone(unavailable_factory, trace_id, "accepted"),
            marker_session_factory=factory,
            trace_id=trace_id,
        )
        is None
    )

    with factory() as db:
        assert db.get(Campaign, campaign_id).revision == 1
        assert (
            db.execute(
                select(CampaignDomainEvent).where(CampaignDomainEvent.id == event.id)
            ).scalars().one()
            is not None
        )
        assert (
            db.execute(select(Outbox).where(Outbox.operation_id == operation_id)).scalars().one()
            is not None
        )
        assert get_trace(db, trace_id)["telemetry_complete"] is False
        assert db.get(OperationTrace, trace_id).telemetry_dropped is True
    scenario.record("converged", final_revision=1, telemetry_dropped=True)
