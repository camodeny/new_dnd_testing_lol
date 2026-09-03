"""Issue #270: first cross-layer deterministic fault-injection slice.

No real queue, provider, credentials, or production data are used.  The main
scenario deliberately loses the relay acknowledgement after queue publication,
then proves convergence after lease recovery and duplicate delivery.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import requests
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.campaigns.events import commit_campaign_mutation
from app.outbox.service import (
    claim_outbox_batch,
    envelope_for_outbox,
    process_outbox_batch,
    recover_expired_claims,
)
from app.observability.tracing import trace_context
from app.queue import InMemoryQueueAdapter
from app.worker import execute_worker_job
from database import Base
from app.providers import (
    LLMProviderAdapter,
    NormalizedChatResponse,
    ProviderError,
    ProviderRequest,
    TransportHooks,
    execute_chat,
)
from models.campaigns import Campaign
from models.campaigns import CampaignDomainEvent
from models.profiles import Profile
from models.reliability import Outbox
from models.reliability import WorkerExecution
from tests.reliability.faults import FaultScenario


def _safe_engine(tmp_path):
    """Use the disposable CI DB when explicitly supplied, otherwise SQLite."""
    url = os.getenv("FAULT_TEST_DATABASE_URL")
    if not url:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'fault-injection.sqlite'}",
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
            db.execute(text("INSERT INTO auth.users (id) VALUES (:id) ON CONFLICT DO NOTHING"), {"id": owner_id})
        db.add(Profile(id=owner_id, email=f"fault-{owner_id}@example.invalid"))
        db.add(Campaign(id=campaign_id, owner_id=owner_id, name="Synthetic reliability campaign"))
        db.commit()
    return campaign_id


def test_fault_database_guard_rejects_nonlocal_target(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "FAULT_TEST_DATABASE_URL",
        "postgresql://synthetic:synthetic@production.example.com/customer_data",
    )
    with pytest.raises(pytest.fail.Exception, match="disposable"):
        _safe_engine(tmp_path)


def test_commit_publish_ack_loss_redelivery_converges_without_duplicate_effect(tmp_path):
    scenario = FaultScenario("outbox_ack_loss_duplicate_delivery")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id = _seed_campaign(factory)
    queue = InMemoryQueueAdapter()
    operation_id = f"fault-op-{uuid.uuid4()}"
    trace_id = f"fault-trace-{uuid.uuid4().hex[:16]}"

    # API/domain commit completes, but the process disappears before any relay
    # publication. A fresh session must still find the durable obligation.
    with trace_context(trace_id, operation_id):
        with factory() as command_db:
            _, command_event = commit_campaign_mutation(
                command_db,
                campaign_id,
                expected_revision=0,
                event_type="turn.requested",
                operation_id=operation_id,
                payload={"synthetic": True},
                outbox_event_type="turn.resolve",
                outbox_payload={"campaign_id": str(campaign_id)},
            )
            outbox_id = command_db.execute(
                select(Outbox.id).where(Outbox.operation_id == operation_id)
            ).scalar_one()
    scenario.record(
        "commit_survived_process_loss",
        campaign_id=campaign_id,
        event_id=command_event.id,
        operation_id=operation_id,
        outbox_id=outbox_id,
        trace_id=trace_id,
    )

    # Relay publishes, then loses the acknowledgement. Leave the committed
    # claim untouched, exactly as a killed relay process would.
    with factory() as crashed_relay_db:
        claimed = claim_outbox_batch(
            crashed_relay_db, batch_size=1, claimed_by="fault-relay-1", lease_seconds=30
        )
        assert [record.id for record in claimed] == [outbox_id]
        queue.publish(envelope_for_outbox(claimed[0]))
        assert scenario.hit_once(
            "after_queue_publish_before_outbox_ack",
            job_id=claimed[0].id,
            operation_id=claimed[0].operation_id,
        )

    # Deterministically age the abandoned lease instead of sleeping.
    with factory() as recovery_db:
        abandoned = recovery_db.get(Outbox, outbox_id)
        abandoned.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        recovery_db.commit()
        assert recover_expired_claims(recovery_db, lease_seconds=30) == 1
        relay_result = process_outbox_batch(
            recovery_db,
            publish=lambda record: queue.publish(envelope_for_outbox(record)),
            batch_size=1,
            claimed_by="fault-relay-2",
            lease_seconds=30,
        )
        assert relay_result == {"claimed": 1, "succeeded": 1, "failed": 0}

    deliveries = queue.consume_all()
    assert len(deliveries) == 2
    assert deliveries[0].job_id == deliveries[1].job_id == outbox_id
    assert deliveries[0].operation_id == deliveries[1].operation_id == operation_id
    assert deliveries[0].trace_id == deliveries[1].trace_id == trace_id

    handler_calls = 0

    def apply_gameplay_effect(envelope):
        nonlocal handler_calls
        handler_calls += 1
        with factory() as effect_db:
            campaign, event = commit_campaign_mutation(
                effect_db,
                campaign_id,
                expected_revision=1,
                event_type="turn.resolved",
                operation_id=envelope.operation_id,
                payload={"synthetic": True, "source_job_id": str(envelope.job_id)},
                mutate=lambda record: setattr(record, "description", "resolved exactly once"),
            )
            return {"campaign_revision": campaign.revision, "event_id": str(event.id)}

    with factory() as worker_db:
        first_result, first_duplicate = execute_worker_job(worker_db, deliveries[0], apply_gameplay_effect)
        replay_result, replay_duplicate = execute_worker_job(worker_db, deliveries[1], apply_gameplay_effect)

    with factory() as verification_db:
        campaign = verification_db.get(Campaign, campaign_id)
        events = verification_db.execute(
            select(CampaignDomainEvent)
            .where(CampaignDomainEvent.campaign_id == campaign_id)
            .order_by(CampaignDomainEvent.sequence)
        ).scalars().all()
        outbox = verification_db.get(Outbox, outbox_id)
        execution = verification_db.get(WorkerExecution, outbox_id)
        assert campaign.revision == 2
        assert campaign.description == "resolved exactly once"
        assert [event.event_type for event in events] == ["turn.requested", "turn.resolved"]
        assert outbox.status == "published" and outbox.attempts == 2
        assert outbox.trace_id == trace_id
        assert execution.status == "succeeded" and execution.attempts == 1
        assert execution.trace_id == trace_id

    assert handler_calls == 1
    assert first_duplicate is False and replay_duplicate is True
    assert first_result == replay_result
    scenario.record(
        "converged",
        final_revision=campaign.revision,
        domain_event_count=len(events),
        handler_calls=handler_calls,
        outbox_attempts=outbox.attempts,
        queue_deliveries=len(deliveries),
        worker_attempts=execution.attempts,
        duplicate_suppressed=replay_duplicate,
    )


class _FakeAdapter(LLMProviderAdapter):
    name = "fault_fake"
    env_prefix = "FAULT_FAKE"
    default_base_url = "https://provider.invalid/chat"

    def require_config(self, model=None):
        return None

    def classify_error(self, error):
        if isinstance(error, ProviderError):
            return error
        return ProviderError(str(error), provider=self.name, retryable=False, original=error)

    def parse_response(self, data):
        return NormalizedChatResponse(
            provider=self.name,
            model=data["model"],
            content=data["content"],
            tool_calls=[],
            finish_reason="stop",
            usage=data.get("usage", {}),
            reasoning=None,
            reasoning_details=None,
            raw=data,
        )


class _Hooks(TransportHooks):
    def __init__(self):
        self.retries = []
        self.errors = []

    def on_retry(self, attempt, max_attempts, delay_seconds, error):
        self.retries.append((attempt, max_attempts, delay_seconds, str(error)))

    def on_error(self, error):
        self.errors.append(str(error))


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


@pytest.mark.parametrize(
    ("retryable", "expected_calls", "expect_success"),
    [(True, 2, True), (False, 1, False)],
    ids=["transient-recovers", "terminal-stops"],
)
def test_provider_failure_policy_without_network_or_paid_calls(
    monkeypatch, retryable, expected_calls, expect_success
):
    scenario = FaultScenario(f"provider_retryable_{retryable}")
    adapter = _FakeAdapter()
    hooks = _Hooks()
    calls = 0

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            scenario.record("provider_failure", retryable=retryable, attempt=calls)
            raise ProviderError(
                "synthetic provider failure",
                provider=adapter.name,
                retryable=retryable,
                kind="timeout" if retryable else "malformed",
            )
        return _Response({"model": "fake-model", "content": "recovered", "usage": {}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr("app.providers.transport.time.sleep", lambda _: None)
    request = ProviderRequest(messages=[{"role": "user", "content": "synthetic"}], model="fake-model", max_attempts=3)

    if expect_success:
        response = execute_chat(adapter, request, hooks=hooks)
        assert response.content == "recovered"
        assert len(hooks.retries) == 1 and hooks.errors == []
    else:
        with pytest.raises(ProviderError, match="synthetic provider failure"):
            execute_chat(adapter, request, hooks=hooks)
        assert hooks.retries == [] and hooks.errors == ["synthetic provider failure"]

    assert calls == expected_calls
    scenario.record(
        "provider_policy_complete",
        calls=calls,
        retries=len(hooks.retries),
        terminal_errors=len(hooks.errors),
        network_used=False,
        successful_completions=1 if expect_success else 0,
    )
