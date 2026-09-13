"""Queue consumer — issue #342.

Delivery-side counterpart to the relay publish path
(cron -> ``run_outbox_relay_once`` -> ``process_outbox_batch`` ->
``envelope_for_outbox`` -> queue adapter). Takes a raw queue message body,
rebuilds the :class:`WorkerEnvelope`, resolves the business handler for its
``job_type``, and executes it idempotently via ``execute_worker_job``.

Registered business workers (see ``WORKER_HANDLERS``) include
``dm.turn.execute`` and ``post_turn.process``. Queue push delivery requires
a subscriber/trigger on the topic; until one is configured, consumption is
driven by the cron sweeps (``/api/cron/dm-execute``, ``/api/cron/post-turn``,
``/api/cron/outbox-relay`` in ``vercel.json``). Published messages remain
durable in the topic for their retention window either way.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Business handlers keyed by envelope job_type. Features register their
#: workers here (e.g. dm.turn.execute, post_turn.process); the push-consumer
#: trigger itself stays deferred (see module docstring).
WORKER_HANDLERS: dict[str, Callable[..., Any]] = {}


class UnregisteredWorkerType(RuntimeError):
    """No business worker registered for this job_type (consumer deferred)."""


def resolve_worker_handler(envelope) -> Callable[..., Any]:
    """Return the business handler for ``envelope.job_type``.

    Raises :class:`UnregisteredWorkerType` for unknown job types so a
    miswired producer fails loudly instead of dropping work.
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
