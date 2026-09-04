"""Issue #342 — real Vercel Queues transport contract.

Verified against https://vercel.com/docs/queues/api (SendMessage):

    POST https://{region}.vercel-queue.com/api/v3/topic/{topic}

with ``Authorization: Bearer <vercel-oidc-token>`` and the outbox/job ID
as ``Vqs-Idempotency-Key``; the body is the raw JSON message payload.

Asserts the exact URL, auth header, idempotency header, and serialized
envelope — not just "publish was attempted".
"""
from __future__ import annotations

import json
import urllib.request
import uuid

import pytest

from app.queue import VercelQueueAdapter, new_envelope
from app.queue.consumer import (
    UnregisteredWorkerType,
    consume_queue_delivery,
    resolve_worker_handler,
)


class _FakeResp:
    def __init__(self, *, status=201, body=b'{"messageId": "msg_abc123"}'):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Capture:
    """Patch urllib.request.urlopen, capture the Request, replay a response."""

    def __init__(self, monkeypatch, *, status=201, body=b'{"messageId": "msg_abc123"}'):
        self.requests: list[urllib.request.Request] = []
        self.status = status
        self.body = body
        self._mp = monkeypatch

    def __enter__(self):
        def _fake(req, *args, **kwargs):
            self.requests.append(req)
            return _FakeResp(status=self.status, body=self.body)

        self._mp.setattr(urllib.request, "urlopen", _fake)
        return self

    def __exit__(self, *args):
        return False


def _clean_env(monkeypatch):
    for var in (
        "VERCEL_QUEUE_TOPIC",
        "VERCEL_QUEUE_NAME",
        "VERCEL_REGION",
        "VERCEL_QUEUE_BASE_URL",
        "VERCEL_QUEUE_TOKEN",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_DEPLOYMENT_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def test_publish_hits_real_send_message_api(monkeypatch):
    """Exact URL, OIDC auth, idempotency header, serialized envelope body."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-jwt")
    adapter = VercelQueueAdapter()
    env = new_envelope(job_type="turn.resolve", operation_id="op-342", payload={"n": 1})

    with _Capture(monkeypatch) as cap:
        result = adapter.publish(env)

    assert result == "msg_abc123"
    assert len(cap.requests) == 1
    req = cap.requests[0]
    assert req.full_url == f"https://iad1.vercel-queue.com/api/v3/topic/worker-queue"
    assert req.get_header("Authorization") == "Bearer oidc-jwt"
    assert req.get_header("Vqs-idempotency-key") == str(env.job_id)
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data.decode()) == env.to_dict()


def test_region_and_topic_env_routing(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-jwt")
    monkeypatch.setenv("VERCEL_REGION", "sfo1")
    monkeypatch.setenv("VERCEL_QUEUE_TOPIC", "game-jobs")
    adapter = VercelQueueAdapter()
    env = new_envelope(job_type="turn.resolve")

    with _Capture(monkeypatch) as cap:
        adapter.publish(env)

    assert cap.requests[0].full_url == "https://sfo1.vercel-queue.com/api/v3/topic/game-jobs"


def test_base_url_template_override(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-jwt")
    monkeypatch.setenv("VERCEL_QUEUE_BASE_URL", "https://proxy.example/queues/{region}")
    monkeypatch.setenv("VERCEL_REGION", "fra1")
    adapter = VercelQueueAdapter(topic="orders")

    with _Capture(monkeypatch) as cap:
        adapter.publish(new_envelope(job_type="orders.created"))

    assert cap.requests[0].full_url == "https://proxy.example/queues/fra1/api/v3/topic/orders"


def test_queue_token_override_takes_precedence_over_oidc(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-jwt")
    monkeypatch.setenv("VERCEL_QUEUE_TOKEN", "explicit-override")
    adapter = VercelQueueAdapter()

    with _Capture(monkeypatch) as cap:
        adapter.publish(new_envelope(job_type="turn.resolve"))

    assert cap.requests[0].get_header("Authorization") == "Bearer explicit-override"


def test_deployment_id_header_when_configured(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-jwt")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_abc123")
    adapter = VercelQueueAdapter()

    with _Capture(monkeypatch) as cap:
        adapter.publish(new_envelope(job_type="turn.resolve"))

    assert cap.requests[0].get_header("Vqs-deployment-id") == "dpl_abc123"


def test_deferred_ingestion_returns_job_id(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-jwt")
    adapter = VercelQueueAdapter()
    env = new_envelope(job_type="turn.resolve")

    with _Capture(monkeypatch, status=202, body=b'{"deferred": true}') as cap:
        assert adapter.publish(env) == str(env.job_id)
    assert len(cap.requests) == 1


def test_missing_oidc_token_fails_loudly(monkeypatch):
    _clean_env(monkeypatch)
    adapter = VercelQueueAdapter()
    with pytest.raises(RuntimeError, match="OIDC token unavailable"):
        adapter.publish(new_envelope(job_type="turn.resolve"))


def test_invalid_topic_rejected(monkeypatch):
    _clean_env(monkeypatch)
    with pytest.raises(ValueError, match="invalid Vercel Queues topic"):
        VercelQueueAdapter(topic="not a topic!")


def test_outbox_job_id_used_as_idempotency_key(monkeypatch):
    """Relay path: envelope_for_outbox(record) -> adapter sends record.id."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-jwt")
    from models.reliability import Outbox
    from app.outbox.service import envelope_for_outbox

    record = Outbox(
        id=uuid.uuid4(),
        aggregate_type="campaign",
        event_type="turn.resolve",
        operation_id="op-relay-342",
        trace_id="trace-342",
        payload={"n": 7},
    )
    envelope = envelope_for_outbox(record)
    assert str(envelope.job_id) == str(record.id)

    adapter = VercelQueueAdapter()
    with _Capture(monkeypatch) as cap:
        adapter.publish(envelope)

    req = cap.requests[0]
    assert req.get_header("Vqs-idempotency-key") == str(record.id)
    assert json.loads(req.data.decode())["operation_id"] == "op-relay-342"


def test_consumer_defers_without_business_worker():
    """No business worker ready: resolve fails loudly, nothing is pretended."""
    env = new_envelope(job_type="turn.resolve")
    with pytest.raises(UnregisteredWorkerType, match="no business worker registered"):
        resolve_worker_handler(env)
    with pytest.raises(UnregisteredWorkerType, match="no business worker registered"):
        consume_queue_delivery(db=None, body=env.to_dict())
