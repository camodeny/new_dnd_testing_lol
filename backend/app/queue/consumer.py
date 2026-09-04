"""Queue consumer — issue #342.

Delivery-side counterpart to the relay publish path
(cron -> ``run_outbox_relay_once`` -> ``process_outbox_batch`` ->
``envelope_for_outbox`` -> queue adapter). Takes a raw queue message body,
rebuilds the :class:`WorkerEnvelope`, resolves the business handler for its
``job_type``, and executes it idempotently via ``execute_worker_job``.

DELIBERATELY DEFERRED (documented, not pretended): no business worker is
registered yet — ``WORKER_HANDLERS`` is empty and no push-consumer trigger
is registered in ``vercel.json``. Registering a trigger (or a public HTTP
consumer route, which would be invocable by anyone without queue-signature
verification) with no handler behind it would silently accept work and then
fail every delivery until messages expire. Instead:

- published messages remain durable in the Vercel Queues topic for their
  retention window, deduplicated by ``Vqs-Idempotency-Key`` (outbox/job ID)
  and idempotent downstream via ``WorkerExecution`` keyed on ``job_id``;
- the owning feature adds a ``job_type -> handler`` entry to
  ``WORKER_HANDLERS`` (or replaces ``resolve_worker_handler`` with its own
  registry) and then registers the push trigger for the topic
  (``queue/v2beta`` trigger on the consumer function / ``[[tool.vercel
  .subscribers]]`` entrypoint, per https://vercel.com/docs/queues) with
  this module's ``consume_queue_delivery`` as the dispatch entrypoint.

Until then ``resolve_worker_handler`` raises ``UnregisteredWorkerType`` so
a premature consumer wiring fails loudly instead of dropping work.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Business handlers keyed by envelope job_type. Empty until the owning
#: feature registers a real worker (see module docstring).
WORKER_HANDLERS: dict[str, Callable[..., Any]] = {}


class UnregisteredWorkerType(RuntimeError):
    """No business worker registered for this job_type (consumer deferred)."""


def resolve_worker_handler(envelope) -> Callable[..., Any]:
    """Return the business handler for ``envelope.job_type``.

    Raises :class:`UnregisteredWorkerType` while no business worker is
    ready — the queue consumer trigger stays unwired until the owning
    feature registers one (see module docstring).
    """
    try:
        return WORKER_HANDLERS[envelope.job_type]
    except KeyError:
        raise UnregisteredWorkerType(
            f"no business worker registered for job_type={envelope.job_type!r}: "
            "queue consumer trigger is deferred to the owning feature; "
            "register a handler in app.queue.consumer.WORKER_HANDLERS first"
        ) from None


def consume_queue_delivery(db, body: dict, *, max_attempts: int = 5, lease_seconds: int = 300) -> tuple[Any, bool]:
    """Execute one queue delivery idempotently.

    Args:
        db: SQLAlchemy session for ``WorkerExecution`` ledger.
        body: Raw JSON message payload (the serialized worker envelope
            published by :meth:`VercelQueueAdapter.publish`).

    Returns ``(result, duplicate)`` from ``execute_worker_job``.
    Raises ``UnregisteredWorkerType`` while no business worker is ready.
    """
    from app.queue.envelope import WorkerEnvelope
    from app.worker.executor import execute_worker_job

    if not isinstance(body, dict):
        raise ValueError("queue delivery body must be the JSON envelope dict")
    envelope = WorkerEnvelope.from_dict(body)
    handler = resolve_worker_handler(envelope)
    logger.info(
        "queue consume job_id=%s type=%s trace=%s",
        envelope.job_id,
        envelope.job_type,
        envelope.trace_id or "-",
    )
    return execute_worker_job(db, envelope, handler, max_attempts=max_attempts, lease_seconds=lease_seconds)
