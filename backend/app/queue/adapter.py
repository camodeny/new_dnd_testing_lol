"""Queue adapter — issue #191.

Domain/application code publishes through this interface, not Vercel-specific
APIs. Provides InMemory adapter for local/tests and a Vercel Queues adapter
stub for production. Adapter substitution is testable.
"""
from __future__ import annotations

import logging
import os
import uuid
from abc import ABC, abstractmethod
from typing import Protocol

from app.queue.envelope import WorkerEnvelope

logger = logging.getLogger(__name__)


class QueueAdapter(ABC):
    """Replaceable durable queue transport."""

    @abstractmethod
    def publish(self, envelope: WorkerEnvelope) -> str:
        """Publish one logical job. Returns broker message id (often job_id)."""

    def publish_batch(self, envelopes: list[WorkerEnvelope]) -> list[str]:
        return [self.publish(e) for e in envelopes]

    @property
    @abstractmethod
    def name(self) -> str:
        ...


class InMemoryQueueAdapter(QueueAdapter):
    """Local / test adapter — in-process list, no external broker required."""

    def __init__(self):
        self._messages: list[WorkerEnvelope] = []
        self._by_job_id: dict[str, WorkerEnvelope] = {}

    @property
    def name(self) -> str:
        return "in_memory"

    def publish(self, envelope: WorkerEnvelope) -> str:
        if not isinstance(envelope, WorkerEnvelope):
            raise TypeError("envelope must be WorkerEnvelope")
        self._messages.append(envelope)
        self._by_job_id[str(envelope.job_id)] = envelope
        logger.info("queue publish in_memory job_id=%s type=%s trace=%s", envelope.job_id, envelope.job_type, envelope.trace_id or "-")
        return str(envelope.job_id)

    def consume_all(self) -> list[WorkerEnvelope]:
        msgs = list(self._messages)
        self._messages.clear()
        return msgs

    def peek_all(self) -> list[WorkerEnvelope]:
        return list(self._messages)

    def depth(self) -> int:
        return len(self._messages)

    def clear(self):
        self._messages.clear()
        self._by_job_id.clear()


class VercelQueueAdapter(QueueAdapter):
    """Vercel Queues transport — uses HTTP API when configured, otherwise logs.

    Domain code never calls Vercel APIs directly; it always goes through this
    adapter. In tests the InMemory adapter is substituted.
    """

    def __init__(self, *, queue_name: str | None = None, api_url: str | None = None):
        self.queue_name = queue_name or os.getenv("VERCEL_QUEUE_NAME", "worker-queue")
        self.api_url = api_url or os.getenv("VERCEL_QUEUE_URL", "")
        self._api_token = os.getenv("VERCEL_QUEUE_TOKEN", "")

    @property
    def name(self) -> str:
        return "vercel"

    def publish(self, envelope: WorkerEnvelope) -> str:
        if not isinstance(envelope, WorkerEnvelope):
            raise TypeError("envelope must be WorkerEnvelope")
        # Production misconfiguration must fail loudly — silent success would drop work.
        if not self.api_url or not self._api_token:
            raise RuntimeError(
                "Vercel queue not configured: set VERCEL_QUEUE_URL and VERCEL_QUEUE_TOKEN "
                f"(job_id={envelope.job_id} queue={self.queue_name})"
            )

        # Real Vercel Queues HTTP publish (best-effort)
        import json
        import urllib.request
        import urllib.error

        body = json.dumps(envelope.to_dict()).encode()
        req = urllib.request.Request(
            self.api_url.rstrip("/") + f"/queues/{self.queue_name}/messages",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_body = resp.read().decode()
                logger.info("queue publish vercel ok job_id=%s status=%s", envelope.job_id, resp.status)
                return str(envelope.job_id)
        except urllib.error.HTTPError as e:
            logger.warning("queue publish vercel failed job_id=%s code=%s body=%s", envelope.job_id, e.code, e.read().decode()[:500] if hasattr(e, 'read') else "")
            raise
        except Exception as e:
            logger.warning("queue publish vercel error job_id=%s error=%s", envelope.job_id, e)
            raise


# ── Factory / global adapter ───────────────────────────────────────────────

_global_adapter: QueueAdapter | None = None


def get_queue_adapter() -> QueueAdapter:
    """Return the active adapter (Vercel when configured, else in-memory)."""
    global _global_adapter
    if _global_adapter is not None:
        return _global_adapter
    # Prefer Vercel when env indicates production
    if os.getenv("VERCEL_QUEUE_URL") or os.getenv("VERCEL_ENV"):
        _global_adapter = VercelQueueAdapter()
    else:
        _global_adapter = InMemoryQueueAdapter()
    return _global_adapter


def set_queue_adapter(adapter: QueueAdapter) -> None:
    """Override adapter (used in tests to inject InMemory)."""
    global _global_adapter
    _global_adapter = adapter


def publish_envelope(envelope: WorkerEnvelope, *, adapter: QueueAdapter | None = None) -> str:
    """Domain helper — always publish via adapter, never directly to broker."""
    ad = adapter or get_queue_adapter()
    return ad.publish(envelope)


def new_envelope(
    *,
    job_type: str,
    campaign_id: uuid.UUID | str | None = None,
    aggregate_id: uuid.UUID | str | None = None,
    expected_revision: int | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
    trace_id: str | None = None,
    payload: dict | None = None,
    job_id: uuid.UUID | None = None,
) -> WorkerEnvelope:
    """Create envelope with identifiers only; validates no snapshot."""
    return WorkerEnvelope(
        job_id=job_id or uuid.uuid4(),
        job_type=job_type,
        campaign_id=campaign_id,
        aggregate_id=aggregate_id,
        expected_revision=expected_revision,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        payload=payload,
    )
