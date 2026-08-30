"""Durable timing and AI-attempt accounting, independent of game state."""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AIRun, OperationTrace
from .tracing import current_operation_id, current_trace_id, structured_log

logger = logging.getLogger(__name__)
MILESTONES = {"accepted", "worker_started", "first_visible", "narration_completed", "resolved"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def begin_operation(db: Session, *, operation_id: str | None = None, trace_id: str | None = None,
                    campaign_id=None, submitted_at: datetime | None = None, commit: bool = True) -> OperationTrace:
    trace_id = trace_id or current_trace_id() or uuid.uuid4().hex
    operation_id = operation_id or current_operation_id() or trace_id
    record = db.get(OperationTrace, trace_id)
    if record is None:
        record = OperationTrace(trace_id=trace_id, operation_id=operation_id, campaign_id=campaign_id,
                                submitted_at=submitted_at or utcnow(), status="submitted")
        db.add(record)
    if commit:
        db.commit(); db.refresh(record)
    return record


def mark_milestone(db: Session, trace_id: str, milestone: str, *, at: datetime | None = None,
                   commit: bool = True) -> OperationTrace:
    if milestone not in MILESTONES:
        raise ValueError(f"unknown milestone: {milestone}")
    record = db.get(OperationTrace, trace_id)
    if record is None:
        raise LookupError(f"trace {trace_id} has no submission record")
    setattr(record, f"{milestone}_at", at or utcnow())
    record.status = milestone
    if commit:
        db.commit(); db.refresh(record)
    return record


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def trace_summary(record: OperationTrace) -> dict:
    missing = [name for name in ("accepted", "worker_started", "first_visible", "narration_completed", "resolved")
               if getattr(record, f"{name}_at") is None]
    return {
        "trace_id": record.trace_id, "operation_id": record.operation_id, "status": record.status,
        "submission_to_acceptance_ms": _ms(record.submitted_at, record.accepted_at),
        "submission_to_first_visible_ms": _ms(record.submitted_at, record.first_visible_at),
        "stream_duration_ms": _ms(record.first_visible_at, record.narration_completed_at),
        "total_duration_ms": _ms(record.submitted_at, record.resolved_at),
        "missing_milestones": missing, "telemetry_complete": not missing and not record.telemetry_dropped,
    }


def start_ai_run(db: Session, *, logical_operation: str, role: str, provider: str, model: str,
                 attempt: int = 1, classification: str = "primary", billable: bool | None = None,
                 trace_id: str | None = None, operation_id: str | None = None,
                 parent_run_id=None, content_metadata: dict | None = None, commit: bool = True) -> AIRun:
    if classification not in {"primary", "recovery"}:
        raise ValueError("classification must be primary or recovery")
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if content_metadata and os.getenv("OBSERVABILITY_CAPTURE_CONTENT", "false").lower() != "true":
        content_metadata = None
    resolved_trace_id = trace_id or current_trace_id() or uuid.uuid4().hex
    resolved_operation_id = operation_id or current_operation_id() or resolved_trace_id
    run = AIRun(trace_id=resolved_trace_id, operation_id=resolved_operation_id,
                parent_run_id=parent_run_id, logical_operation=logical_operation, role=role,
                provider=provider, model=model, attempt=attempt, classification=classification,
                billable=(classification == "primary") if billable is None else billable,
                status="running", started_at=utcnow(), content_metadata=content_metadata)
    db.add(run)
    if commit:
        db.commit(); db.refresh(run)
    return run


def finish_ai_run(db: Session, run_id, *, status: str = "succeeded", first_token_at=None,
                  input_tokens=None, output_tokens=None, cost_usd=None, result_code=None,
                  error_type=None, commit: bool = True) -> AIRun:
    run = db.get(AIRun, run_id)
    if run is None:
        raise LookupError(f"AI run {run_id} not found")
    run.status = status; run.completed_at = utcnow(); run.first_token_at = first_token_at
    run.input_tokens = input_tokens; run.output_tokens = output_tokens; run.cost_usd = cost_usd
    run.result_code = result_code; run.error_type = error_type
    if commit:
        db.commit(); db.refresh(run)
    return run


def get_trace(db: Session, trace_id: str) -> dict | None:
    record = db.get(OperationTrace, trace_id)
    if record is None:
        return None
    runs = db.scalars(select(AIRun).where(AIRun.trace_id == trace_id).order_by(AIRun.attempt)).all()
    return {**trace_summary(record), "ai_runs": [{"id": str(run.id), "attempt": run.attempt,
        "classification": run.classification, "billable": run.billable, "status": run.status,
        "provider": run.provider, "model": run.model, "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens, "cost_usd": run.cost_usd} for run in runs]}


def fail_soft(callback, *args, marker_session_factory=None, trace_id: str | None = None, **kwargs):
    """Run telemetry without breaking gameplay, durably flagging a dropped write.

    ``marker_session_factory`` must create an independent session so marking a
    telemetry failure can neither commit nor roll back the gameplay transaction.
    """
    try:
        return callback(*args, **kwargs)
    except Exception as exc:
        structured_log(logger, logging.ERROR, "telemetry_dropped", error_type=type(exc).__name__)
        dropped_trace_id = trace_id or current_trace_id()
        if marker_session_factory is not None and dropped_trace_id:
            try:
                with marker_session_factory() as marker_db:
                    record = marker_db.get(OperationTrace, dropped_trace_id)
                    if record is not None:
                        record.telemetry_dropped = True
                        marker_db.commit()
            except Exception as marker_exc:
                structured_log(
                    logger,
                    logging.ERROR,
                    "telemetry_drop_marker_failed",
                    error_type=type(marker_exc).__name__,
                    dropped_trace_id=dropped_trace_id,
                )
        return None
