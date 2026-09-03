from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base
from app.campaigns.events import commit_campaign_mutation
from app.observability.service import begin_operation, fail_soft, finish_ai_run, get_trace, mark_milestone, start_ai_run
from app.observability.tracing import current_trace_id, structured_log, trace_context
from app.outbox.service import enqueue_outbox, envelope_for_outbox
from app.worker.executor import execute_worker_job
from models.campaigns import Campaign
from models.campaigns import CampaignDomainEvent
from models.profiles import Profile
from models.reliability import AIRun
from models.reliability import OperationTrace
from models.reliability import Outbox
from models.reliability import WorkerExecution


def _setup():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory, factory()


def test_trace_survives_api_context_outbox_envelope_and_worker():
    factory, db = _setup()
    trace_id = "trace-synthetic-192"
    operation_id = "turn-42"
    start = datetime.now(timezone.utc)
    with trace_context(trace_id, operation_id):
        trace = begin_operation(factory, submitted_at=start)
        mark_milestone(factory, trace_id, "accepted", at=start + timedelta(milliseconds=12))
        outbox = enqueue_outbox(db, event_type="turn.resolve", operation_id=operation_id, payload={"turn_id": "42"})
    envelope = envelope_for_outbox(outbox)
    assert envelope.trace_id == trace.trace_id == trace_id
    assert envelope.operation_id == operation_id

    mark_milestone(factory, trace_id, "worker_started", at=start + timedelta(milliseconds=20))
    seen = {}
    execute_worker_job(db, envelope, lambda _: seen.update(trace_id=trace_id) or {"ok": True})
    worker = db.get(WorkerExecution, envelope.job_id)
    assert worker.trace_id == trace_id and seen["trace_id"] == trace_id

    mark_milestone(factory, trace_id, "first_visible", at=start + timedelta(milliseconds=100))
    mark_milestone(factory, trace_id, "narration_completed", at=start + timedelta(milliseconds=180))
    mark_milestone(factory, trace_id, "resolved", at=start + timedelta(milliseconds=220))
    summary = get_trace(db, trace_id)
    assert summary["submission_to_acceptance_ms"] == 12
    assert summary["submission_to_first_visible_ms"] == 100
    assert summary["stream_duration_ms"] == 80
    assert summary["total_duration_ms"] == 220
    assert summary["telemetry_complete"] is True


def test_ai_retry_keeps_lineage_and_distinguishes_recovery(monkeypatch):
    factory, db = _setup()
    monkeypatch.delenv("OBSERVABILITY_CAPTURE_CONTENT", raising=False)
    begin_operation(factory, trace_id="trace-retry", operation_id="op-retry")
    primary = start_ai_run(factory, trace_id="trace-retry", operation_id="op-retry", logical_operation="narrate",
                           role="ai_dm", provider="test", model="primary", content_metadata={"prompt": "private"})
    finish_ai_run(factory, primary.id, status="failed", error_type="timeout")
    recovery = start_ai_run(factory, trace_id="trace-retry", operation_id="op-retry", parent_run_id=primary.id,
                            logical_operation="narrate", role="ai_dm", provider="test", model="fallback",
                            attempt=2, classification="recovery")
    finish_ai_run(factory, recovery.id, input_tokens=10, output_tokens=20, cost_usd=.01)
    assert primary.billable is True and recovery.billable is False
    assert primary.content_metadata is None
    assert {run.trace_id for run in db.query(AIRun).all()} == {"trace-retry"}


def test_missing_timestamps_are_null_and_explicit_not_zero():
    factory, db = _setup()
    begin_operation(factory, trace_id="incomplete", operation_id="op")
    summary = get_trace(db, "incomplete")
    assert summary["submission_to_first_visible_ms"] is None
    assert "first_visible" in summary["missing_milestones"]
    assert summary["telemetry_complete"] is False


def test_structured_logging_drops_private_content(caplog):
    with caplog.at_level(logging.INFO), trace_context("safe-trace", "safe-op"):
        structured_log(logging.getLogger("test"), logging.INFO, "ai_call", prompt="secret", content="private", model="x")
    assert "secret" not in caplog.text and "private" not in caplog.text
    assert "safe-trace" in caplog.text and '"model": "x"' in caplog.text


def test_trace_id_validation_matches_persisted_64_character_limit():
    boundary = "a" * 64
    with trace_context(boundary):
        assert current_trace_id() == boundary
    with trace_context("b" * 65):
        generated = current_trace_id()
        assert generated != "b" * 65
        assert len(generated) == 32


def test_campaign_domain_event_and_outbox_share_trace_lineage():
    _, db = _setup()
    owner_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    db.add(Profile(id=owner_id, email="owner@example.com"))
    db.add(Campaign(id=campaign_id, owner_id=owner_id, name="Trace test"))
    db.commit()

    with trace_context("domain-trace", "domain-operation"):
        _, event = commit_campaign_mutation(
            db,
            campaign_id,
            0,
            event_type="campaign.tested",
            operation_id="domain-operation",
            outbox_event_type="campaign.tested",
        )
    persisted = db.get(CampaignDomainEvent, event.id)
    assert persisted.trace_id == "domain-trace"
    assert envelope_for_outbox(db.query(Outbox).one()).trace_id == "domain-trace"


def test_fail_soft_durably_marks_dropped_telemetry_without_raising():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        begin_operation(factory, trace_id="dropped-trace", operation_id="dropped-op")

    result = fail_soft(
        lambda: (_ for _ in ()).throw(RuntimeError("telemetry unavailable")),
        marker_session_factory=factory,
        trace_id="dropped-trace",
    )
    assert result is None
    with factory() as db:
        summary = get_trace(db, "dropped-trace")
        assert summary["telemetry_complete"] is False
        assert db.get(OperationTrace, "dropped-trace").telemetry_dropped is True


def test_telemetry_transactions_cannot_commit_or_poison_gameplay(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'isolated.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    owner_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    with factory() as seed_db:
        seed_db.add(Profile(id=owner_id, email="isolation@example.com"))
        seed_db.add(Campaign(id=campaign_id, owner_id=owner_id, name="Committed"))
        seed_db.commit()

    with factory() as gameplay_db:
        campaign = gameplay_db.get(Campaign, campaign_id)
        campaign.name = "Pending gameplay"

        # A successful telemetry commit owns another session and cannot commit
        # the pending gameplay unit of work.
        begin_operation(factory, trace_id="isolated-trace", operation_id="isolated-op")
        with factory() as observer_db:
            assert observer_db.get(Campaign, campaign_id).name == "Committed"

        # A poisoned telemetry transaction is contained and marked; the
        # gameplay session remains usable and can commit on its own schedule.
        def unavailable_factory():
            raise RuntimeError("telemetry database unavailable")

        assert fail_soft(
            lambda: mark_milestone(unavailable_factory, "isolated-trace", "accepted"),
            marker_session_factory=factory,
            trace_id="isolated-trace",
        ) is None
        campaign.name = "Gameplay continued"
        gameplay_db.commit()

    with factory() as verification_db:
        assert verification_db.get(Campaign, campaign_id).name == "Gameplay continued"
        assert verification_db.get(OperationTrace, "isolated-trace").telemetry_dropped is True


def test_telemetry_write_helpers_reject_a_gameplay_session():
    _, gameplay_db = _setup()
    try:
        with pytest.raises(TypeError, match="dedicated session factory"):
            begin_operation(gameplay_db, trace_id="unsafe", operation_id="unsafe")
    finally:
        gameplay_db.close()
