"""Queue adapter — issue #191 (transport corrected in #342).

Domain/application code publishes through this interface, not Vercel-specific
APIs. Provides InMemory adapter for local/tests and a Vercel Queues adapter
for production. Adapter substitution is testable.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from abc import ABC, abstractmethod
from typing import Callable, Protocol

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
    """Vercel Queues transport — issue #342.

    Publishes against the real Vercel Queues HTTP API (verified against
    https://vercel.com/docs/queues/api)::

        POST https://{region}.vercel-queue.com/api/v3/topic/{topic}

    with ``Authorization: Bearer <vercel-oidc-token>`` and the outbox/job
    ID as ``Vqs-Idempotency-Key``. The request body is the raw JSON
    message payload (the serialized worker envelope).

    Configuration (env, matching the official SDK names where they exist):

    - ``VERCEL_QUEUE_TOPIC`` — topic to publish to. Default ``worker-queue``.
    - ``VERCEL_REGION`` — queue region (``iad1``, ``sfo1``, ...).
      Default ``iad1`` (same fallback as the official SDK).
    - ``VERCEL_QUEUE_BASE_URL`` — fixed base URL or ``{region}`` template
      override (local devserver / proxy). Default
      ``https://{region}.vercel-queue.com``.
    - Auth: explicit ``token=`` constructor arg, else ``VERCEL_QUEUE_TOKEN``
      (documented SDK bearer-token override, e.g. from ``vercel env pull``),
      else ``VERCEL_OIDC_TOKEN`` (Vercel OIDC). No other token scheme is
      supported — the previous ``VERCEL_QUEUE_URL`` + static-token transport
      targeted a non-existent API and has been removed.
    - ``VERCEL_DEPLOYMENT_ID`` — when set, sent as ``Vqs-Deployment-Id``
      for per-deployment isolation (same semantics as the SDK default).

    Domain code never calls Vercel APIs directly; it always goes through this
    adapter. In tests the InMemory adapter is substituted.
    """

    DEFAULT_BASE_URL_TEMPLATE = "https://{region}.vercel-queue.com"
    DEFAULT_REGION = "iad1"
    DEFAULT_TOPIC = "worker-queue"
    TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")

    def __init__(
        self,
        *,
        topic: str | None = None,
        region: str | None = None,
        base_url: str | None = None,
        token: str | None = None,
        deployment_id: str | None = None,
        oidc_token_provider: Callable[[], str | None] | None = None,
    ):
        resolved_topic = topic or os.getenv("VERCEL_QUEUE_TOPIC") or self.DEFAULT_TOPIC
        if not resolved_topic or not self.TOPIC_PATTERN.match(resolved_topic):
            raise ValueError(
                f"invalid Vercel Queues topic {resolved_topic!r}: must match ^[A-Za-z0-9_\\-]+$"
            )
        self.topic = resolved_topic
        self.region = (region or os.getenv("VERCEL_REGION", "") or self.DEFAULT_REGION).strip() or self.DEFAULT_REGION
        template = base_url or os.getenv("VERCEL_QUEUE_BASE_URL", "") or self.DEFAULT_BASE_URL_TEMPLATE
        if "{region}" in template:
            template = template.replace("{region}", self.region)
        self.base_url = template.rstrip("/")
        self._explicit_token = token or ""
        self._oidc_token_provider = oidc_token_provider
        if deployment_id is not None:
            self._deployment_id = deployment_id
        else:
            self._deployment_id = os.getenv("VERCEL_DEPLOYMENT_ID", "")

    @property
    def name(self) -> str:
        return "vercel"

    @property
    def publish_url(self) -> str:
        """Exact SendMessage endpoint for this adapter's topic/region."""
        return f"{self.base_url}/api/v3/topic/{self.topic}"

    def _bearer_token(self) -> str:
        if self._explicit_token:
            return self._explicit_token
        override = os.getenv("VERCEL_QUEUE_TOKEN", "")
        if override:
            return override
        oidc = os.getenv("VERCEL_OIDC_TOKEN", "")
        if oidc:
            return oidc
        if self._oidc_token_provider is not None:
            provided = self._oidc_token_provider()
            if provided:
                return provided
        raise RuntimeError(
            "Vercel OIDC token unavailable: set VERCEL_OIDC_TOKEN (via `vercel env pull`), "
            "VERCEL_QUEUE_TOKEN, or pass token=/oidc_token_provider explicitly"
        )

    def publish(self, envelope: WorkerEnvelope) -> str:
        if not isinstance(envelope, WorkerEnvelope):
            raise TypeError("envelope must be WorkerEnvelope")
        # Production misconfiguration must fail loudly — silent success would drop work.
        token = self._bearer_token()

        # Outbox/job ID is the dedupe key: Vercel drops duplicates out-of-band
        # for the message lifetime; at-least-once redelivery stays idempotent
        # downstream via WorkerExecution keyed on job_id.
        idempotency_key = envelope.idempotency_key or str(envelope.job_id)

        import json
        import urllib.request
        import urllib.error

        body = json.dumps(envelope.to_dict()).encode()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Vqs-Idempotency-Key": idempotency_key,
        }
        if self._deployment_id:
            headers["Vqs-Deployment-Id"] = self._deployment_id
        req = urllib.request.Request(
            self.publish_url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = getattr(resp, "status", 200)
                try:
                    resp_body = resp.read().decode()
                except Exception:
                    resp_body = ""
                logger.info(
                    "queue publish vercel ok job_id=%s topic=%s status=%s",
                    envelope.job_id,
                    self.topic,
                    status,
                )
                # 201 returns {"messageId": ...}; 202 (deferred ingestion)
                # returns {"deferred": true} with no message id.
                if resp_body:
                    try:
                        payload = json.loads(resp_body)
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict) and payload.get("messageId"):
                        return str(payload["messageId"])
                return str(envelope.job_id)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()[:500]
            except Exception:
                err_body = ""
            logger.warning(
                "queue publish vercel failed job_id=%s topic=%s code=%s body=%s",
                envelope.job_id,
                self.topic,
                e.code,
                err_body,
            )
            raise
        except Exception as e:
            logger.warning("queue publish vercel error job_id=%s topic=%s error=%s", envelope.job_id, self.topic, e)
            raise


# ── Factory / global adapter ───────────────────────────────────────────────

_global_adapter: QueueAdapter | None = None


def get_queue_adapter() -> QueueAdapter:
    """Return the active adapter (Vercel when configured, else in-memory)."""
    global _global_adapter
    if _global_adapter is not None:
        return _global_adapter
    # Prefer Vercel when env indicates production
    if (
        os.getenv("VERCEL_ENV")
        or os.getenv("VERCEL_QUEUE_ENABLED", "").lower() in ("1", "true", "yes", "on")
        or os.getenv("VERCEL_QUEUE_TOPIC")
        or os.getenv("VERCEL_QUEUE_BASE_URL")
    ):
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
    if trace_id is None:
        from app.observability.tracing import current_trace_id
        trace_id = current_trace_id()
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
