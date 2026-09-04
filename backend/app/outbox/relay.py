"""Transactional outbox production relay — issue #331.

The outbox service (claiming, leasing, retries, acks, envelope conversion)
existed, but ``process_outbox_batch()`` was only invoked by reliability
tests — rows sat ``pending`` forever in production. This module is the
runtime connection: API -> durable outbox -> relay -> queue -> worker.

Cold-start safe: importing this module performs no DDL, no engine
connect, and no background threads. All DB/queue handles are resolved
lazily inside :func:`run_outbox_relay_once`.
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def run_outbox_relay_once(
    *,
    db=None,
    adapter=None,
    batch_size: int | None = None,
    claimed_by: str | None = None,
    lease_seconds: int | None = None,
    retry_delay_seconds: int | None = None,
) -> dict:
    """Claim one batch from the durable outbox and publish to the queue.

    Publish path is always ``envelope_for_outbox`` -> queue adapter, so
    correlation lineage (job_id / operation_id / trace_id) is preserved
    and the worker path (``execute_worker_job``) is exercised downstream.

    Returns a dict with ``claimed`` / ``succeeded`` / ``failed`` /
    ``recovered`` counts. When no database is configured (local dev
    without DB, cold import), returns zero counts with
    ``skipped="db_unconfigured"`` instead of raising.
    """
    from app.outbox.service import (
        envelope_for_outbox,
        process_outbox_batch,
        recover_expired_claims,
    )
    from app.queue.adapter import get_queue_adapter

    _db = db
    _close = False
    if _db is None:
        from database import SessionLocal

        if SessionLocal is None:
            logger.info("outbox relay skipped: database unconfigured")
            return {
                "claimed": 0,
                "succeeded": 0,
                "failed": 0,
                "recovered": 0,
                "skipped": "db_unconfigured",
            }
        _db = SessionLocal()
        _close = True

    try:
        ad = adapter if adapter is not None else get_queue_adapter()
        size = batch_size if batch_size is not None else _int_env("OUTBOX_RELAY_BATCH_SIZE", 10)
        owner = claimed_by or os.getenv("OUTBOX_RELAY_CLAIMED_BY", "relay")
        lease = lease_seconds if lease_seconds is not None else _int_env("OUTBOX_RELAY_LEASE_SECONDS", 300)
        retry_delay = (
            retry_delay_seconds
            if retry_delay_seconds is not None
            else _int_env("OUTBOX_RELAY_RETRY_DELAY_SECONDS", 60)
        )

        try:
            recovered = recover_expired_claims(_db, lease_seconds=lease)
        except Exception as e:
            logger.warning("outbox relay sweeper failed: %s", e)
            recovered = 0

        def _publish(record) -> None:
            ad.publish(envelope_for_outbox(record))

        result = process_outbox_batch(
            _db,
            publish=_publish,
            batch_size=size,
            claimed_by=owner,
            lease_seconds=lease,
            retry_delay_seconds=retry_delay,
        )
        result["recovered"] = recovered
        return result
    finally:
        if _close:
            try:
                _db.close()
            except Exception:
                pass


async def outbox_relay_background_loop(*, interval_seconds: float | None = None) -> None:
    """Long-lived runtime loop: relay a batch every interval (lifespan worker).

    Runs until cancelled. Never raises — per-tick errors are logged so one
    bad tick cannot kill the worker. DB access runs in a thread so the
    event loop is not blocked by sync SQLAlchemy sessions.
    """
    interval = interval_seconds
    if interval is None:
        interval = _float_env("OUTBOX_RELAY_INTERVAL_SECONDS", 5.0)
    interval = max(0.5, interval)
    logger.info("outbox relay background loop started interval=%.1fs", interval)
    while True:
        try:
            await asyncio.to_thread(run_outbox_relay_once)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — loop must survive bad ticks
            logger.warning("outbox relay tick failed: %s", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
