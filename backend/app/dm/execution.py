"""Autonomous DM-turn execution spine — issue #354.

Handoff from coordinated ``prepared`` attempts to real model-backed execution
through the production pipeline:

  claim (``mark_attempt_running``) → ``assemble_attempt_context`` →
  provider adjudication (``app.dm.adjudication``) → evidence/tool loop →
  validation with bounded regeneration → ``execute_validated_turn``
  (stage → durable stream narration → atomic commit) → realtime projection.

Failures never fabricate a turn: any terminal error marks the attempt failed
(visible, so the live table shows a failure instead of stuck "thinking") and
staged effects are never committed without a valid DM result.

Entry points:
- :func:`execute_dm_attempt` — execute one attempt (idempotent).
- :func:`run_dm_execute_sweep` — claim + execute oldest prepared attempts;
  used by the ``/api/cron/dm-execute`` trigger and by the post-submission
  best-effort hook. A DB sweep (not queue-only) keeps serverless runtimes
  autonomous without a push-consumer trigger.
- ``dm.turn.execute`` queue handler — same orchestrator behind the worker
  envelope path for when a queue trigger is registered (#208 hardening).
"""
from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DM_TURN_EXECUTE_JOB = "dm.turn.execute"


def _is_config_error(exc: BaseException) -> bool:
    msg = str(exc)
    return "_API_KEY is not set" in msg or "_MODEL is not set" in msg


def _assemble_production_context(db: Session, attempt_id: uuid.UUID, *, supplemental_status=None):
    """Assemble the attempt context with explicit first-slice lane scoping.

    Strict assembly first (fail-closed). When the ONLY missing authority is
    a lane with no usable source for this attempt — no scene row established
    yet (``current_scene``) or no knowledge reader wired yet
    (``knowledge_visibility``) — retry with just those lanes explicitly
    declared ``not_applicable`` and the downgrade recorded as source errors.
    Any other missing lane still fails closed. Once a scene row or lane
    reader exists, strict assembly succeeds and no downgrade applies.
    """
    from app.dm.context import (
        LaneName,
        MissingAuthoritativeContextError,
        assemble_attempt_context,
    )

    extra = dict(supplemental_status or {})
    errors: dict = {}
    for _ in range(4):
        try:
            return assemble_attempt_context(
                db, attempt_id,
                supplemental_status=extra or None,
                supplemental_errors=errors or None,
            )
        except MissingAuthoritativeContextError as exc:
            msg = str(exc)
            downgraded: dict = {}
            for lane in (LaneName.CURRENT_SCENE, LaneName.KNOWLEDGE_VISIBILITY):
                key = lane.value
                if key in msg and lane not in extra and key not in extra:
                    extra[lane] = "not_applicable"
                    downgraded[lane] = [f"declared not_applicable: {msg}"[:500]]
            if not downgraded:
                raise
            errors.update(downgraded)
            logger.warning(
                "dm_execute context lane scoped attempt_id=%s lanes=%s",
                attempt_id, sorted(str(k) for k in downgraded),
            )
    return assemble_attempt_context(
        db, attempt_id,
        supplemental_status=extra or None,
        supplemental_errors=errors or None,
    )


def _classify_failure(exc: BaseException) -> str:
    """Map execution failures to attempt error_class (retriable default)."""
    from app.worker.executor import TERMINAL, classify_error

    if _is_config_error(exc):
        return "retriable"
    try:
        return classify_error(exc)
    except Exception:
        return TERMINAL


def execute_dm_attempt(
    db: Session,
    attempt_id: uuid.UUID,
    *,
    adjudicate=None,
    narrator=None,
    provider_name: str | None = None,
    timeout_seconds: float = 90,
    trace_id: str | None = None,
    supplemental_status=None,
):
    """Claim and execute one prepared DM attempt end-to-end (idempotent).

    ``adjudicate``/``narrator`` are injectable seams for tests; production
    defaults resolve the configured provider via ``app.dm.adjudication``.
    Returns the :class:`ValidatedTurnResult` on success.
    """
    from app.dm.turns import (
        ATTEMPT_PREPARED,
        ATTEMPT_RUNNING,
        mark_attempt_failed,
        mark_attempt_running,
    )
    from app.dm.evidence import run_bounded_evidence_loop
    from app.dm.validators import default_pipeline, run_with_bounded_regeneration
    from app.dm.narration import (
        NarrationStreamError,
        execute_validated_turn,
    )
    from app.observability.tracing import structured_log
    from models.dm import DmTurn, DmTurnAttempt

    tid = trace_id or str(uuid.uuid4())
    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is None:
        raise ValueError(f"DM attempt {attempt_id} not found")
    turn = db.get(DmTurn, attempt.turn_id)

    # Idempotent no-ops: already terminal/streaming work is never re-executed.
    if attempt.status in ("succeeded", "streaming", "failed_visible", "superseded", "discarded", "abandoned"):
        logger.info(
            "dm_execute skip attempt_id=%s status=%s", attempt.id, attempt.status,
        )
        return None
    submission_ids = list(attempt.submission_ids or [])
    campaign_id = attempt.campaign_id
    turn_id = attempt.turn_id

    # Claim: prepared -> running (recoverable if the worker crashes).
    if attempt.status == ATTEMPT_PREPARED:
        try:
            mark_attempt_running(db, attempt.id)
        except ValueError:
            db.rollback()
            attempt = db.get(DmTurnAttempt, attempt_id)
    elif attempt.status != ATTEMPT_RUNNING:
        raise ValueError(f"Attempt {attempt_id} cannot execute from status {attempt.status}")

    def _fail_visible(exc: BaseException) -> None:
        error_class = _classify_failure(exc)
        try:
            mark_attempt_failed(
                db, attempt.id,
                error=f"{type(exc).__name__}: {exc}"[:2000],
                error_class=error_class,
                visible=True,
            )
        except Exception as mark_exc:
            logger.warning(
                "dm_execute failure-marking failed attempt_id=%s error=%s",
                attempt_id, mark_exc,
            )
        structured_log(
            logger, logging.WARNING, "dm_execute_failed",
            attempt_id=str(attempt_id), turn_id=str(turn_id),
            error_class=error_class, error=str(exc)[:500], trace_id=tid,
        )

    try:
        packet = _assemble_production_context(
            db, attempt.id, supplemental_status=supplemental_status
        )
    except Exception as exc:
        db.rollback()
        _fail_visible(exc)
        raise

    # Resolve provider (fail-clear gate lives in adjudication/config).
    adapter = None
    model = None
    pname = provider_name
    if adjudicate is None or narrator is None:
        try:
            from app.dm.adjudication import resolve_dm_provider

            adapter, model, resolved = resolve_dm_provider()
            pname = pname or resolved
        except Exception as exc:
            db.rollback()
            _fail_visible(exc)
            raise

    structured_log(
        logger, logging.INFO, "dm_execute_start",
        submission_ids=[str(s) for s in submission_ids],
        turn_id=str(turn_id), attempt_id=str(attempt.id),
        provider=pname, model=model, trace_id=tid,
    )

    if adjudicate is None:
        def adjudicate(packet, feedback=None):  # type: ignore[misc]
            from app.dm.adjudication import adjudicate_with_provider

            _ = feedback  # feedback reaches the model via regeneration packet
            return adjudicate_with_provider(
                packet, adapter=adapter, model=model,
                timeout_seconds=timeout_seconds, trace_id=tid,
            )

    try:
        # Evidence/tool loop first (no-op when the model never asks for
        # evidence), then strict validation with bounded regeneration.
        final_contract, _bundle = run_bounded_evidence_loop(
            initial_packet=packet, adjudicate=adjudicate, db=db,
            tool_handlers=None,
        )
        report = default_pipeline.validate(final_contract, packet)
        if report.passed:
            contract = final_contract
        else:
            contract, report = run_with_bounded_regeneration(adjudicate, packet)
    except Exception as exc:
        db.rollback()
        _fail_visible(exc)
        raise

    if narrator is None:
        try:
            from app.dm.adjudication import build_provider_narrator

            narrator = build_provider_narrator(
                adapter=adapter, model=model, timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            db.rollback()
            _fail_visible(exc)
            raise
    elif narrator == "deterministic":
        # Explicit opt-in to the deterministic template narrator (no model
        # call): same production stream/commit path, used by tests.
        narrator = None

    try:
        result = execute_validated_turn(
            db,
            turn_id=turn.id,
            attempt_id=attempt.id,
            contract=contract,
            narrator=narrator,
            provider=pname or "dm-provider",
            publish_realtime=True,
            trace_id=tid,
        )
    except NarrationStreamError as exc:
        # Post-visibility remediation already applied inside
        # execute_validated_turn; just observe.
        structured_log(
            logger, logging.WARNING, "dm_execute_stream_failed",
            turn_id=str(turn_id), attempt_id=str(attempt.id),
            stream_id=str(exc.stream_id), trace_id=tid,
        )
        raise
    except Exception as exc:
        db.rollback()
        # Pre-visibility failure: nothing persisted, leave a visible marker
        # so the turn never looks stuck-thinking.
        try:
            from models.dm import DmTurnAttempt as _Att

            current = db.get(_Att, attempt.id)
            if current is not None and current.status in (ATTEMPT_PREPARED, ATTEMPT_RUNNING):
                _fail_visible(exc)
        except Exception:
            pass
        raise

    structured_log(
        logger, logging.INFO, "dm_execute_complete",
        turn_id=str(result.turn.id), attempt_id=str(result.attempt.id),
        stream_id=str(result.narration.stream_id),
        provider=pname, model=model, trace_id=tid,
    )
    return result


def find_prepared_attempts(db: Session, *, limit: int = 5):
    """Oldest prepared attempts eligible for autonomous execution."""
    from models.dm import DmTurnAttempt

    q = (
        select(DmTurnAttempt)
        .where(DmTurnAttempt.status == "prepared")
        .order_by(DmTurnAttempt.created_at)
        .limit(max(1, limit))
    )
    try:
        return db.execute(q).scalars().all()
    except Exception:
        db.rollback()
        return []


def run_dm_execute_sweep(
    db: Session,
    *,
    limit: int = 5,
    timeout_seconds: float = 90,
    adjudicate=None,
    narrator=None,
) -> dict:
    """Recover stuck claims, then execute oldest prepared attempts.

    Returns ``{"executed": [...], "failed": [...], "skipped": [...]}`` with
    string attempt ids. One attempt's failure never blocks the rest.
    """
    from app.dm.turns import recover_stuck_attempts

    lease = int(os.getenv("DM_EXECUTE_LEASE_SECONDS", "300") or 300)
    try:
        recovered = recover_stuck_attempts(db, lease_seconds=lease)
    except Exception as exc:
        logger.warning("dm_execute_sweep recover failed error=%s", exc)
        recovered = 0
    outcome: dict = {"executed": [], "failed": [], "skipped": [], "recovered": recovered}
    for attempt in find_prepared_attempts(db, limit=limit):
        aid = str(attempt.id)
        try:
            result = execute_dm_attempt(
                db, attempt.id,
                timeout_seconds=timeout_seconds,
                adjudicate=adjudicate, narrator=narrator,
            )
            if result is None:
                outcome["skipped"].append(aid)
            else:
                outcome["executed"].append(aid)
        except Exception as exc:
            db.rollback()
            logger.warning("dm_execute_sweep attempt_failed attempt_id=%s error=%s", aid, exc)
            outcome["failed"].append({"attempt_id": aid, "error": str(exc)[:300]})
    return outcome


def handle_dm_turn_execute(db: Session, envelope) -> dict:
    """Queue-worker handler for ``dm.turn.execute`` envelopes."""
    payload = getattr(envelope, "payload", None) or {}
    raw = payload.get("attempt_id") or payload.get("attemptId")
    if not raw:
        raise ValueError("dm.turn.execute envelope payload must include attempt_id")
    result = execute_dm_attempt(db, uuid.UUID(str(raw)))
    if result is None:
        return {"attempt_id": str(raw), "skipped": True}
    return {
        "attempt_id": str(raw),
        "turn_id": str(result.turn.id),
        "stream_id": str(result.narration.stream_id),
    }


def register_dm_worker() -> None:
    """Register the DM execution handler on the queue consumer."""
    from app.queue.consumer import WORKER_HANDLERS

    WORKER_HANDLERS[DM_TURN_EXECUTE_JOB] = handle_dm_turn_execute


register_dm_worker()
