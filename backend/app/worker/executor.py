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
- Atomic committed claim prevents concurrent side effects; next_attempt_at enforced.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
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


def _ensure_execution_exists(db: Session, envelope: WorkerEnvelope, *, max_attempts: int = 5) -> WorkerExecution:
    """Get or insert execution row; handles concurrent insert via unique PK."""
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
    try:
        db.flush()
        db.commit()
        db.refresh(rec)
    except IntegrityError:
        # Concurrent inserter won; read its row
        db.rollback()
        rec = db.get(WorkerExecution, envelope.job_id)
        if rec is None:
            raise
    return rec


def _atomic_claim(db: Session, job_id: uuid.UUID, *, lease_seconds: int, trace_id: str | None) -> str | None:
    """Try to atomically transition row to RUNNING and commit.

    Succeeds only if row is:
    - pending, or
    - failed where next_attempt_at is due, or
    - running but lease expired (crash recovery)

    Returns claim_token if claim acquired (committed), None otherwise.
    Token fences completion: a slow worker that lost its lease cannot
    later commit over a new owner's claim.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=lease_seconds)
    token = uuid.uuid4().hex
    result = db.execute(
        update(WorkerExecution)
        .where(WorkerExecution.id == job_id)
        .where(
            (WorkerExecution.status == PENDING)
            | (
                (WorkerExecution.status == FAILED)
                & ((WorkerExecution.next_attempt_at == None) | (WorkerExecution.next_attempt_at <= now))  # noqa
            )
            | (
                (WorkerExecution.status == RUNNING) & (WorkerExecution.started_at < cutoff)
            )
        )
        .values(
            status=RUNNING,
            started_at=now,
            attempts=WorkerExecution.attempts + 1,
            updated_at=now,
            claim_token=token,
            **({"trace_id": trace_id} if trace_id else {}),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        db.commit()
        return token
    db.rollback()
    return None


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
    """Execute a worker job idempotently with atomic claim.

    Claim is committed before handler side effects so concurrent deliveries
    cannot both run the handler. next_attempt_at is enforced.
    """
    classify_fn = classify or classify_error
    backoff_fn = backoff or compute_backoff
    now = datetime.now(timezone.utc)

    rec = _ensure_execution_exists(db, envelope, max_attempts=max_attempts)
    # Ensure we see latest committed state; other concurrent claim may have updated row
    try:
        db.expire_all()
    except Exception:
        pass
    rec = db.get(WorkerExecution, envelope.job_id)
    assert rec is not None
    # Apply max_attempts override for this invocation if needed
    if rec.max_attempts != max_attempts:
        rec.max_attempts = max_attempts
        db.add(rec)
        db.commit()
        db.refresh(rec)

    # Fast-path: already succeeded -> duplicate without claim
    if rec.status == SUCCEEDED and rec.result is not None:
        logger.info("worker duplicate_hit job_id=%s type=%s trace=%s", envelope.job_id, envelope.job_type, envelope.trace_id or "-")
        return rec.result, True

    if rec.status == DEAD_LETTER:
        logger.warning("worker dead_letter_hit job_id=%s type=%s", envelope.job_id, envelope.job_type)
        raise TerminalError(f"Job {envelope.job_id} is in dead_letter (terminal); replay required")

    # Enforce next_attempt_at before attempting claim: early redelivery must wait
    if rec.status == FAILED and rec.next_attempt_at is not None:
        nxt = rec.next_attempt_at
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        if nxt > now:
            remaining = (nxt - now).total_seconds()
            logger.info("worker early_retry job_id=%s next_in=%.1fs", envelope.job_id, remaining)
            raise RuntimeError(
                f"Job {envelope.job_id} retry not ready; next_attempt_at in {remaining:.1f}s"
            )

    # If still running and lease not expired, block concurrent execution
    if rec.status == RUNNING and rec.started_at is not None:
        started = rec.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (now - started).total_seconds()
        if age < lease_seconds:
            logger.info("worker still_running job_id=%s age=%.1fs", envelope.job_id, age)
            raise RuntimeError(f"Job {envelope.job_id} still running (age {age:.1f}s); redelivery should retry later")

    # Atomic committed claim before side effects — returns ownership token
    claim_token = _atomic_claim(db, envelope.job_id, lease_seconds=lease_seconds, trace_id=envelope.trace_id)
    if not claim_token:
        # Claim failed: diagnose why (concurrent winner, early retry, still running, already succeeded)
        # Session may have stale identity map after failed UPDATE (synchronize disabled)
        try:
            db.expire_all()
        except Exception:
            pass
        fresh = db.get(WorkerExecution, envelope.job_id)
        if fresh is None:
            raise RuntimeError(f"Job {envelope.job_id} claim failed: row disappeared")
        if fresh.status == SUCCEEDED and fresh.result is not None:
            logger.info("worker duplicate_hit after claim race job_id=%s", envelope.job_id)
            return fresh.result, True
        if fresh.status == DEAD_LETTER:
            raise TerminalError(f"Job {envelope.job_id} is in dead_letter (terminal); replay required")
        if fresh.status == RUNNING and fresh.started_at is not None:
            started = fresh.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            age = (now - started).total_seconds()
            if age < lease_seconds:
                raise RuntimeError(f"Job {envelope.job_id} still running (age {age:.1f}s); redelivery should retry later")
            # else lease expired but we still lost race: let caller retry
            raise RuntimeError(f"Job {envelope.job_id} claim race lost; retry")
        if fresh.status == FAILED and fresh.next_attempt_at is not None:
            nxt = fresh.next_attempt_at
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            if nxt > now:
                remaining = (nxt - now).total_seconds()
                raise RuntimeError(f"Job {envelope.job_id} retry not ready; next_attempt_at in {remaining:.1f}s")
        # Generic concurrent claim loss
        raise RuntimeError(f"Job {envelope.job_id} claim failed (status={fresh.status}); concurrent delivery won")

    # Re-fetch claimed row (now RUNNING, attempts incremented)
    # UPDATE was done with synchronize_session=False, so expire cache
    try:
        db.expire_all()
    except Exception:
        pass
    rec = db.get(WorkerExecution, envelope.job_id)
    assert rec is not None and rec.status == RUNNING, f"claim succeeded but status={rec.status if rec else None}"
    assert rec.claim_token == claim_token, "claim token mismatch after claim"

    # Invoke handler with timing (outside claim transaction)
    # Handler side effects MUST be idempotent on job_id (e.g. PK=job_id or
    # check ledger before mutating) so lease-expiry overlap cannot duplicate
    # external effects. Ledger commit is fenced by claim_token.
    t0 = time.monotonic()
    try:
        result = handler(envelope)
        # Validate result is JSON-serializable
        import json
        json.dumps(result, default=str)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        payload_result = result if isinstance(result, (dict, list)) else {"result": result}
        # Fenced completion: only succeed if we still own the lease
        completed_at = datetime.now(timezone.utc)
        upd = db.execute(
            update(WorkerExecution)
            .where(WorkerExecution.id == envelope.job_id, WorkerExecution.claim_token == claim_token, WorkerExecution.status == RUNNING)
            .values(
                status=SUCCEEDED,
                result=payload_result,
                completed_at=completed_at,
                processing_duration_ms=elapsed_ms,
                last_error=None,
                error_class=None,
                next_attempt_at=None,
                started_at=None,
                claim_token=None,
            )
            .execution_options(synchronize_session=False)
        )
        if upd.rowcount == 0:
            # Lost ownership — another worker stole the lease while we were running
            db.rollback()
            try:
                db.expire_all()
            except Exception:
                pass
            fresh = db.get(WorkerExecution, envelope.job_id)
            if fresh and fresh.status == SUCCEEDED and fresh.result is not None:
                logger.warning("worker lease lost but job already succeeded job_id=%s", envelope.job_id)
                return fresh.result, True
            logger.warning("worker lease lost on success job_id=%s fresh_status=%s", envelope.job_id, fresh.status if fresh else None)
            raise RuntimeError(f"Job {envelope.job_id} lost ownership during execution; lease expired")
        db.commit()
        try:
            db.expire_all()
        except Exception:
            pass
        rec = db.get(WorkerExecution, envelope.job_id)
        logger.info(
            "worker succeeded job_id=%s type=%s attempt=%s duration_ms=%s trace=%s",
            envelope.job_id, envelope.job_type, rec.attempts, elapsed_ms, envelope.trace_id or "-",
        )
        return rec.result, False

    except BaseException as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error_class = classify_fn(exc)
        # Need fresh rec for error handling
        try:
            db.expire_all()
        except Exception:
            pass
        rec = db.get(WorkerExecution, envelope.job_id)
        if rec is None:
            raise
        # If we already lost ownership, don't overwrite new owner's state
        if rec.claim_token != claim_token:
            db.rollback()
            try:
                db.expire_all()
            except Exception:
                pass
            fresh = db.get(WorkerExecution, envelope.job_id)
            logger.warning("worker lease lost on error job_id=%s error=%s fresh_status=%s", envelope.job_id, exc, fresh.status if fresh else None)
            # If fresh already succeeded, return it; otherwise propagate original error but don't mark failed
            if fresh and fresh.status == SUCCEEDED and fresh.result is not None:
                return fresh.result, True
            raise

        # We still own the claim — record failure fenced by token
        rec.processing_duration_ms = elapsed_ms
        rec.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        rec.error_class = error_class
        rec.completed_at = None

        if error_class == TERMINAL:
            upd = db.execute(
                update(WorkerExecution)
                .where(WorkerExecution.id == envelope.job_id, WorkerExecution.claim_token == claim_token)
                .values(status=DEAD_LETTER, next_attempt_at=None, started_at=None, claim_token=None, last_error=rec.last_error, error_class=error_class, processing_duration_ms=elapsed_ms)
                .execution_options(synchronize_session=False)
            )
            if upd.rowcount:
                db.commit()
            else:
                db.rollback()
            logger.warning(
                "worker terminal job_id=%s type=%s attempt=%s error=%s trace=%s",
                envelope.job_id, envelope.job_type, rec.attempts, rec.last_error[:200], envelope.trace_id or "-",
            )
            raise

        # Retriable path: check exhaustion
        if rec.attempts >= rec.max_attempts:
            upd = db.execute(
                update(WorkerExecution)
                .where(WorkerExecution.id == envelope.job_id, WorkerExecution.claim_token == claim_token)
                .values(status=DEAD_LETTER, next_attempt_at=None, started_at=None, claim_token=None, last_error=rec.last_error, error_class=error_class, processing_duration_ms=elapsed_ms)
                .execution_options(synchronize_session=False)
            )
            if upd.rowcount:
                db.commit()
            else:
                db.rollback()
            logger.warning(
                "worker exhausted job_id=%s type=%s attempts=%s max=%s -> dead_letter trace=%s",
                envelope.job_id, envelope.job_type, rec.attempts, rec.max_attempts, envelope.trace_id or "-",
            )
            raise TerminalError(f"Job {envelope.job_id} exhausted after {rec.attempts} attempts: {exc}") from exc

        # Schedule retry with backoff
        delay = backoff_fn(rec.attempts)
        next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        upd = db.execute(
            update(WorkerExecution)
            .where(WorkerExecution.id == envelope.job_id, WorkerExecution.claim_token == claim_token)
            .values(status=FAILED, next_attempt_at=next_at, started_at=None, claim_token=None, last_error=rec.last_error, error_class=error_class, processing_duration_ms=elapsed_ms)
            .execution_options(synchronize_session=False)
        )
        if upd.rowcount:
            db.commit()
        else:
            db.rollback()
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
    rec.claim_token = None
    if reset_attempts:
        rec.attempts = 0
    # keep payload/trace for replay; bump updated_at
    rec.updated_at = datetime.now(timezone.utc)
    db.add(rec)
    if commit:
        db.commit()
        db.refresh(rec)
    else:
        db.flush()
    logger.info("worker replay job_id=%s type=%s attempts=%s", job_id, rec.job_type, rec.attempts)
    return rec


def recover_stuck_executions(db: Session, *, lease_seconds: int = 300, commit: bool = True) -> int:
    """Sweeper: reset RUNNING executions whose lease expired (worker crash before completion)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
    result = db.execute(
        update(WorkerExecution)
        .where(WorkerExecution.status == RUNNING, WorkerExecution.started_at < cutoff)
        .values(status=PENDING, started_at=None, next_attempt_at=None, claim_token=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        if commit:
            db.commit()
        else:
            db.flush()
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
