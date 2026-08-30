"""Worker execution ledger — issue #191.

Guarantees:
- Duplicate delivery of same logical job_id does not duplicate side effects
  (returns existing successful result).
- Retriable vs terminal failures are explicit via error classification and
  backoff hooks.
- Worker crash before completion leaves execution in running/pending so
  redelivery is safe (lease expiry / sweeper).
- Exhausted/terminal jobs stay durably inspectable and replayable.
- Manual replay reuses the same logical idempotency.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.queue.envelope import WorkerEnvelope
from models import WorkerExecution

logger = logging.getLogger(__name__)

# Status constants
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
DEAD_LETTER = "dead_letter"

# Error classes
RETRIABLE = "retriable"
TERMINAL = "terminal"


# ── Error classification ────────────────────────────────────────────────────

class RetriableError(Exception):
    """Transient / retryable failure (e.g. provider timeout, rate limit)."""
    pass


class TerminalError(Exception):
    """Permanent / poison message — must not be retried automatically."""
    pass


def classify_error(exc: BaseException) -> str:
    """Classify an exception as retriable or terminal.

    Hook for callers to override; default heuristics:
    - TerminalError -> terminal
    - RetriableError -> retriable
    - Provider-like names (timeout, rate_limited, unavailable) -> retriable
    - Everything else -> terminal (fail safe: poison messages don't retry forever)
    """
    if isinstance(exc, TerminalError):
        return TERMINAL
    if isinstance(exc, RetriableError):
        return RETRIABLE
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    # Common retriable signals (mirrors provider errors)
    retriable_tokens = ("timeout", "rate_limit", "rate_limited", "429", "503", "504", "unavailable", "connection", "temporarily")
    if any(t in name for t in retriable_tokens) or any(t in msg for t in retriable_tokens):
        return RETRIABLE
    # ValueError / validation errors are terminal poison
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return TERMINAL
    # Default to retriable for unknown transient, but cap via max_attempts
    # Prefer retriable so transient infra blips recover; poison detection
    # should raise TerminalError explicitly.
    return RETRIABLE


def compute_backoff(attempt: int, base_seconds: float = 2.0, max_seconds: float = 600.0) -> float:
    """Exponential backoff with jitter cap. Hook for policy overrides."""
    delay = base_seconds * (2 ** max(0, attempt - 1))
    return min(delay, max_seconds)


# ── Execution record helpers ────────────────────────────────────────────────


def _get_or_create_execution(db: Session, envelope: WorkerEnvelope, *, max_attempts: int = 5) -> WorkerExecution:
    rec = db.get(WorkerExecution, envelope.job_id)
    if rec is not None:
        return rec
    rec = WorkerExecution(
        id=envelope.job_id,
        job_type=envelope.job_type,
        campaign_id=envelope.campaign_id,
        aggregate_type=envelope.aggregate_type,
        aggregate_id=envelope.aggregate_id,
        expected_revision=envelope.expected_revision,
        operation_id=envelope.operation_id,
        idempotency_key=envelope.idempotency_key or envelope.operation_id,
        payload=envelope.payload,
        status=PENDING,
        attempts=0,
        max_attempts=max_attempts,
        trace_id=envelope.trace_id,
    )
    db.add(rec)
    db.flush()
    return rec


# ── Core execution ──────────────────────────────────────────────────────────


def execute_worker_job(
    db: Session,
    envelope: WorkerEnvelope,
    handler: Callable[[WorkerEnvelope], dict | list | Any],
    *,
    max_attempts: int = 5,
    lease_seconds: int = 300,
    classify: Callable[[BaseException], str] | None = None,
    backoff: Callable[[int], float] | None = None,
    commit: bool = True,
) -> tuple[Any, bool]:
    """Execute a worker job idempotently.

    Args:
        db: Session
        envelope: logical job envelope (job_id is dedupe key)
        handler: callable(envelope) -> result (JSON-serializable)
        max_attempts: after this many retriable failures job becomes dead_letter
        lease_seconds: running lease for crash detection
        classify: override error classification hook
        backoff: override backoff hook
        commit: whether to commit (False lets caller manage txn)

    Returns:
        (result, is_duplicate)  — duplicate True means existing succeeded result returned
        For failed/retriable without success, raises after recording failure state.

    Duplicate semantics: if record is already succeeded, return its result immediately
    without invoking handler, guaranteeing no duplicate side effects.
    """
    classify_fn = classify or classify_error
    backoff_fn = backoff or compute_backoff
    now = datetime.now(timezone.utc)

    rec = _get_or_create_execution(db, envelope, max_attempts=max_attempts)

    # Duplicate success: at-least-once delivery must not duplicate effects
    if rec.status == SUCCEEDED and rec.result is not None:
        logger.info("worker duplicate_hit job_id=%s type=%s trace=%s", envelope.job_id, envelope.job_type, envelope.trace_id or "-")
        return rec.result, True

    # Terminal/dead_letter: do not auto-execute; caller must replay explicitly
    if rec.status == DEAD_LETTER:
        logger.warning("worker dead_letter_hit job_id=%s type=%s", envelope.job_id, envelope.job_type)
        # Still raise to surface terminal, but record is inspectable
        raise TerminalError(f"Job {envelope.job_id} is in dead_letter (terminal); replay required")

    # Crash recovery: if RUNNING but lease not expired, treat as in-progress duplicate
    if rec.status == RUNNING and rec.started_at is not None:
        started = rec.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (now - started).total_seconds()
        if age < lease_seconds:
            logger.info("worker still_running job_id=%s age=%.1fs", envelope.job_id, age)
            # Caller should wait / redeliver later; don't double-execute
            raise RuntimeError(f"Job {envelope.job_id} still running (age {age:.1f}s); redelivery should retry later")
        else:
            logger.warning("worker lease_expired job_id=%s age=%.1fs -> allowing redelivery", envelope.job_id, age)
            # fall through to retry

    # Mark running
    rec.status = RUNNING
    rec.started_at = now
    rec.attempts = (rec.attempts or 0) + 1
    rec.trace_id = envelope.trace_id or rec.trace_id
    rec.updated_at = now
    db.flush()

    # Invoke handler with timing
    t0 = time.monotonic()
    try:
        result = handler(envelope)
        # Validate result is JSON-serializable
        import json
        json.dumps(result, default=str)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        rec.status = SUCCEEDED
        rec.result = result if isinstance(result, (dict, list)) else {"result": result}
        rec.completed_at = datetime.now(timezone.utc)
        rec.processing_duration_ms = elapsed_ms
        rec.last_error = None
        rec.error_class = None
        rec.next_attempt_at = None
        db.flush()
        if commit:
            db.commit()
            db.refresh(rec)
        logger.info(
            "worker succeeded job_id=%s type=%s attempt=%s duration_ms=%s trace=%s",
            envelope.job_id, envelope.job_type, rec.attempts, elapsed_ms, envelope.trace_id or "-",
        )
        return rec.result, False

    except BaseException as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error_class = classify_fn(exc)
        rec.processing_duration_ms = elapsed_ms
        rec.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        rec.error_class = error_class
        rec.completed_at = None

        if error_class == TERMINAL:
            rec.status = DEAD_LETTER
            rec.next_attempt_at = None
            db.flush()
            if commit:
                db.commit()
                db.refresh(rec)
            logger.warning(
                "worker terminal job_id=%s type=%s attempt=%s error=%s trace=%s",
                envelope.job_id, envelope.job_type, rec.attempts, rec.last_error[:200], envelope.trace_id or "-",
            )
            raise

        # Retriable path: check exhaustion
        if rec.attempts >= rec.max_attempts:
            rec.status = DEAD_LETTER
            rec.next_attempt_at = None
            db.flush()
            if commit:
                db.commit()
                db.refresh(rec)
            logger.warning(
                "worker exhausted job_id=%s type=%s attempts=%s max=%s -> dead_letter trace=%s",
                envelope.job_id, envelope.job_type, rec.attempts, rec.max_attempts, envelope.trace_id or "-",
            )
            raise TerminalError(f"Job {envelope.job_id} exhausted after {rec.attempts} attempts: {exc}") from exc

        # Schedule retry with backoff
        delay = backoff_fn(rec.attempts)
        rec.status = FAILED
        rec.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        rec.started_at = None
        db.flush()
        if commit:
            db.commit()
            db.refresh(rec)
        logger.info(
            "worker retriable job_id=%s type=%s attempt=%s next_in=%.1fs error=%s trace=%s",
            envelope.job_id, envelope.job_type, rec.attempts, delay, rec.last_error[:200], envelope.trace_id or "-",
        )
        raise


# ── Failed-work / dead-letter ledger ────────────────────────────────────────


def list_failed_work(db: Session, *, limit: int = 100, job_type: str | None = None) -> list[WorkerExecution]:
    q = select(WorkerExecution).where(WorkerExecution.status == DEAD_LETTER)
    if job_type:
        q = q.where(WorkerExecution.job_type == job_type)
    q = q.order_by(WorkerExecution.updated_at.desc()).limit(limit)
    return list(db.execute(q).scalars().all())


def list_retryable_failed(db: Session, *, limit: int = 100) -> list[WorkerExecution]:
    """Jobs in FAILED that are scheduled for retry (not yet terminal)."""
    q = select(WorkerExecution).where(WorkerExecution.status == FAILED).order_by(WorkerExecution.next_attempt_at.asc()).limit(limit)
    return list(db.execute(q).scalars().all())


def get_worker_execution(db: Session, job_id: uuid.UUID) -> WorkerExecution | None:
    return db.get(WorkerExecution, job_id)


def replay_failed_job(
    db: Session,
    job_id: uuid.UUID,
    *,
    reset_attempts: bool = False,
    commit: bool = True,
) -> WorkerExecution | None:
    """Manual replay of a dead_letter/failed job — resets to pending for redelivery.

    Uses same logical job_id so idempotency guarantees still apply.
    """
    rec = db.get(WorkerExecution, job_id)
    if rec is None:
        return None
    if rec.status not in (DEAD_LETTER, FAILED):
        logger.warning("replay not applicable job_id=%s status=%s", job_id, rec.status)
        return rec
    rec.status = PENDING
    rec.next_attempt_at = None
    rec.started_at = None
    rec.last_error = None
    rec.error_class = None
    if reset_attempts:
        rec.attempts = 0
    # keep payload/trace for replay; bump updated_at
    rec.updated_at = datetime.now(timezone.utc)
    db.flush()
    if commit:
        db.commit()
        db.refresh(rec)
    logger.info("worker replay job_id=%s type=%s attempts=%s", job_id, rec.job_type, rec.attempts)
    return rec


def recover_stuck_executions(db: Session, *, lease_seconds: int = 300, commit: bool = True) -> int:
    """Sweeper: reset RUNNING executions whose lease expired (worker crash before completion)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
    result = db.execute(
        update(WorkerExecution)
        .where(WorkerExecution.status == RUNNING, WorkerExecution.started_at < cutoff)
        .values(status=PENDING, started_at=None, next_attempt_at=None)
    )
    if result.rowcount:
        if commit:
            db.commit()
        logger.info("worker sweeper recovered %s stuck executions", result.rowcount)
    return result.rowcount or 0


# ── Observability ───────────────────────────────────────────────────────────


def get_worker_metrics(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    rows = db.execute(select(WorkerExecution.status, func.count()).group_by(WorkerExecution.status)).all()
    by_status = {k: v for k, v in rows}
    # attempts / retries / terminal
    max_attempts = db.execute(select(func.max(WorkerExecution.attempts))).scalar() or 0
    avg_duration = db.execute(
        select(func.avg(WorkerExecution.processing_duration_ms)).where(WorkerExecution.processing_duration_ms != None)  # noqa
    ).scalar()
    oldest_pending = db.execute(
        select(func.min(WorkerExecution.created_at)).where(WorkerExecution.status.in_([PENDING, FAILED, RUNNING]))
    ).scalar()
    if oldest_pending is not None and oldest_pending.tzinfo is None:
        oldest_pending = oldest_pending.replace(tzinfo=timezone.utc)
    lag_s = (now - oldest_pending).total_seconds() if oldest_pending else 0
    return {
        "by_status": by_status,
        "total": sum(by_status.values()),
        "pending": by_status.get(PENDING, 0),
        "running": by_status.get(RUNNING, 0),
        "succeeded": by_status.get(SUCCEEDED, 0),
        "failed": by_status.get(FAILED, 0),
        "dead_letter": by_status.get(DEAD_LETTER, 0),
        "retries": by_status.get(FAILED, 0),
        "terminal_failures": by_status.get(DEAD_LETTER, 0),
        "max_attempts": max_attempts,
        "avg_processing_duration_ms": float(avg_duration) if avg_duration else 0,
        "oldest_pending_lag_seconds": lag_s,
    }


def get_queue_metrics(db: Session, *, adapter: Any | None = None) -> dict:
    """Unified queue+worker observability.

    Includes logical job IDs and processing duration; adapter depth when available.
    """
    wm = get_worker_metrics(db)
    depth = None
    if adapter is not None and hasattr(adapter, "depth"):
        try:
            depth = adapter.depth()
        except Exception:
            depth = None
    return {**wm, "queue_depth": depth}
