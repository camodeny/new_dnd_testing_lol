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


def retry_backoff_seconds(retry_count: int) -> int:
    """Bounded exponential backoff for retriable DM attempts (seconds)."""
    count = max(1, int(retry_count or 1))
    return min(30 * (2 ** (count - 1)), 600)


def _current_scene_explicitly_absent(db: Session, attempt_id: uuid.UUID) -> bool:
    """True only when positively identified: no scene row established.

    Any source failure (reader error, DB error) returns False so the caller
    stays fail-closed. Only the exact not-yet-migrated case (missing
    ``campaign_current_scenes`` relation) is treated as absent, using the
    same strict predicate as context assembly.
    """
    try:
        from models.dm import DmTurnAttempt
        from models.world import CampaignCurrentScene

        attempt = db.get(DmTurnAttempt, attempt_id)
        if attempt is None:
            return False
        scene = db.get(CampaignCurrentScene, attempt.campaign_id)
        return scene is None
    except Exception as exc:
        from app.dm.context import is_missing_current_scene_table_error

        if is_missing_current_scene_table_error(exc):
            logger.warning("dm_execute scene-absence check missing table: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass
            return True
        return False


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
            # current_scene: only downgrade on positively identified absence.
            # A reader/source failure raises through assemble_attempt_context
            # directly (fail-closed) or leaves a row present here — both must
            # NOT become not_applicable.
            if LaneName.CURRENT_SCENE.value in msg and LaneName.CURRENT_SCENE not in extra \
                    and LaneName.CURRENT_SCENE.value not in extra:
                if _current_scene_explicitly_absent(db, attempt_id):
                    extra[LaneName.CURRENT_SCENE] = "not_applicable"
                    downgraded[LaneName.CURRENT_SCENE] = [f"declared not_applicable: {msg}"[:500]]
                else:
                    raise
            if LaneName.KNOWLEDGE_VISIBILITY.value in msg \
                    and LaneName.KNOWLEDGE_VISIBILITY not in extra \
                    and LaneName.KNOWLEDGE_VISIBILITY.value not in extra:
                extra[LaneName.KNOWLEDGE_VISIBILITY] = "not_applicable"
                downgraded[LaneName.KNOWLEDGE_VISIBILITY] = [f"declared not_applicable: {msg}"[:500]]
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


def execute_dm_attempt(db: Session, attempt_id: uuid.UUID, **kwargs):
    """Only the owner may claim, execute, or fail this attempt."""
    from app.dm.ownership import execution_ownership

    with execution_ownership(db, attempt_id) as acquired:
        if not acquired:
            return None
        return _execute_owned_attempt(db, attempt_id, **kwargs)


def _await_roll_prompt_text(contract) -> str:
    parts: list[str] = []
    for beat in contract.beats or []:
        for claim in beat.claims or []:
            text = (claim.text or "").strip()
            if text:
                parts.append(text)
    return " ".join(parts).strip()


def _resolve_roll_participants(db: Session, turn, contract):
    """Derive (requested_user_id, character_id) for an await_roll contract.

    Prefers the contract's explicit character_id when it names a real
    character; otherwise falls back to the turn's submission character.
    """
    from models.characters import Character
    from models.threads import PlayerSubmission

    rr = contract.roll_request
    if rr is not None and rr.character_id not in (None, ""):
        try:
            cid = uuid.UUID(str(rr.character_id))
            char = db.get(Character, cid)
            if char is not None:
                return char.owner_id, char.id
        except (ValueError, TypeError):
            pass
    submission_ids = list(turn.submission_ids or [])
    if submission_ids:
        as_uuids = []
        for value in submission_ids:
            try:
                as_uuids.append(uuid.UUID(str(value)))
            except (ValueError, TypeError):
                continue
        subs = list(
            db.scalars(
                select(PlayerSubmission).where(
                    PlayerSubmission.id.in_(as_uuids)
                )
            ).all()
        ) if as_uuids else []
        for sub in subs:
            if sub.character_id:
                char = db.get(Character, sub.character_id)
                if char is not None:
                    return char.owner_id, char.id
                return sub.user_id, sub.character_id
    raise ValueError("await_roll requires a player-owned character to request the roll")


def _complete_await_roll(db: Session, *, turn, attempt, contract, trace_id: str):
    """Persist a player-owned roll request and leave the same turn open."""
    from app.rolls.service import request_rolls

    requested_user_id, character_id = _resolve_roll_participants(db, turn, contract)
    rr = contract.roll_request
    payload = {
        "request_key": str(rr.request_id),
        "requested_user_id": requested_user_id,
        "character_id": character_id,
        "roll_kind": str(rr.roll_kind),
        "ability_or_skill": str(rr.ability_or_skill),
        "label": str(rr.label),
        "advantage_state": str(rr.advantage_state or "normal"),
        "reason_public": str(rr.reason_public),
        "dc_private": rr.dc_private,
    }
    rows = request_rolls(
        db, campaign_id=turn.campaign_id, turn_id=turn.id,
        attempt_id=attempt.id, requests=[payload],
    )
    prompt = _await_roll_prompt_text(contract)
    if prompt:
        try:
            base = dict(attempt.result or {})
            base["prompt"] = prompt[:1000]
            attempt.result = base
            db.add(attempt)
        except Exception:
            pass
    db.commit()
    db.refresh(turn)
    db.refresh(attempt)
    from app.observability.tracing import structured_log

    structured_log(
        logger, logging.INFO, "dm_execute_await_roll",
        turn_id=str(turn.id), attempt_id=str(attempt.id),
        roll_request_ids=[str(r.id) for r in rows], trace_id=trace_id,
    )

    from dataclasses import dataclass

    @dataclass
    class AwaitRollResult:
        turn: object
        attempt: object
        roll_requests: list
        narration: object = None
        event: object = None
        mode: str = "await_roll"

    return AwaitRollResult(turn=turn, attempt=attempt, roll_requests=rows)


def _complete_silent(db: Session, *, turn, attempt, contract, provider: str, trace_id: str):
    """Complete a valid silent contract with no visible narration.

    Zero-visible-output still performs the normal resolved-turn bookkeeping
    (revision bump + completion event, resolved/committed timestamps,
    submission resolution) so consumed input is never re-adjudicated.
    """
    import time
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.campaigns.events import commit_campaign_mutation
    from app.dm.turns import stage_validated_attempt
    from models.threads import PlayerSubmission

    staged = stage_validated_attempt(db, attempt.id, contract)
    _ = staged
    expected = int(attempt.source_revision)
    submission_ids = list(attempt.submission_ids or [])
    duplicate_op = str(attempt.id)
    base_payload: dict = {
        "turn_id": str(turn.id),
        "attempt_id": str(attempt.id),
        "submission_ids": submission_ids,
        "mode": "silent",
    }
    if not attempt.commit_operation_id:
        attempt.commit_operation_id = duplicate_op
        db.flush()
    execute_start = time.monotonic()
    campaign_after, event = commit_campaign_mutation(
        db,
        turn.campaign_id,
        expected,
        event_type="dm.turn_resolved",
        payload=base_payload,
        operation_id=duplicate_op,
        commit=False,
        outbox_event_type="dm.turn_committed",
        outbox_payload={**base_payload, "operation_id": duplicate_op},
        outbox_operation_id=duplicate_op,
    )
    now = datetime.now(timezone.utc)
    commit_duration_ms = int((time.monotonic() - execute_start) * 1000)
    turn.status = "succeeded"
    turn.resolved_at = now
    turn.committed_at = now
    turn.commit_duration_ms = commit_duration_ms
    turn.time_executing_ms = commit_duration_ms
    attempt.status = "succeeded"
    attempt.completed_at = now
    try:
        attempt.result = event.to_dict() if hasattr(event, "to_dict") else {"event_id": str(event.id)}
        attempt.result = {**(attempt.result or {}), "mode": "silent"}
    except Exception:
        attempt.result = {"mode": "silent"}
    attempt.processing_duration_ms = commit_duration_ms
    attempt.last_error = None
    attempt.error_class = None
    if submission_ids:
        try:
            sub_uuids = [uuid.UUID(str(s)) for s in submission_ids]
            rows = db.execute(
                select(PlayerSubmission).where(PlayerSubmission.id.in_(sub_uuids))
            ).scalars().all()
            for row in rows:
                row.resolution_status = "resolved"
                row.resolved_at = now
        except Exception as exc:
            logger.warning("dm_execute_silent failed to resolve submissions turn_id=%s error=%s", turn.id, exc)
    db.flush()
    db.commit()
    db.refresh(turn)
    db.refresh(attempt)
    db.refresh(campaign_after)
    db.refresh(event)
    from app.observability.tracing import structured_log

    structured_log(
        logger, logging.INFO, "dm_execute_silent",
        turn_id=str(turn.id), attempt_id=str(attempt.id),
        provider=provider, trace_id=trace_id,
    )

    from dataclasses import dataclass

    @dataclass
    class SilentResult:
        turn: object
        attempt: object
        narration: object = None
        event: object = None
        mode: str = "silent"

    return SilentResult(turn=turn, attempt=attempt, event=event)


def _execute_owned_attempt(
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
    from app.dm.tools import handle_ask_character_sheet
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

    # A caller observing running work must never adopt another worker's claim.
    try:
        attempt = mark_attempt_running(db, attempt.id)
    except ValueError:
        db.rollback()
        return None

    def _fail_closed(exc: BaseException) -> None:
        """Authority/context failure: always visible, never retried blindly.

        A missing-authority or source-reader error will fail identically on
        every retry until the authority or code is fixed, so resetting to
        prepared would only burn sweeps. Leave a visible marker instead.
        """
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

    def _fail_visible(exc: BaseException) -> None:
        from app.worker.executor import RETRIABLE

        error_class = _classify_failure(exc)
        try:
            fresh_attempt = db.get(DmTurnAttempt, attempt.id)
            fresh_turn = db.get(DmTurn, turn_id) if turn_id else None
            crossed = (
                (fresh_attempt is not None and fresh_attempt.status in ("streaming", "failed_visible"))
                or (fresh_turn is not None and fresh_turn.status in ("streaming", "failed_visible"))
            )
            if error_class == RETRIABLE and not crossed:
                # Pre-visibility transient: keep retryable work but requeue it
                # BEHIND ready work. Reset the claim to prepared with a future
                # eligibility time (error preserved) so the next cron sweep or
                # queue redelivery retries without manual repair — without
                # letting one failing attempt starve newer prepared attempts.
                if fresh_attempt is not None:
                    from datetime import datetime, timezone
                    from datetime import timedelta as _td

                    now = datetime.now(timezone.utc)
                    retries = int(getattr(fresh_attempt, "retry_count", 0) or 0) + 1
                    fresh_attempt.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                    fresh_attempt.error_class = error_class
                    fresh_attempt.status = ATTEMPT_PREPARED
                    fresh_attempt.started_at = None
                    fresh_attempt.completed_at = None
                    fresh_attempt.retry_count = retries
                    fresh_attempt.next_retry_at = now + _td(
                        seconds=retry_backoff_seconds(retries)
                    )
                    db.add(fresh_attempt)
                    db.commit()
                structured_log(
                    logger, logging.WARNING, "dm_execute_retryable",
                    attempt_id=str(attempt_id), turn_id=str(turn_id),
                    error_class=error_class, error=str(exc)[:500], trace_id=tid,
                )
                return
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
        _fail_closed(exc)
        raise

    # Resolve providers per call area (fail-clear gates live in
    # adjudication/areas config). Narrator resolves its own pinned area
    # inside build_provider_narrator.
    adapter = None
    model = None
    pname = provider_name
    if adjudicate is None:
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
        # Preserve the exact packet the last evidence adjudication saw, including
        # its visibility filtering and per-round budget decisions.
        validation_packet = packet

        def evidence_adjudicate(enriched_packet):
            nonlocal validation_packet
            validation_packet = enriched_packet
            return adjudicate(enriched_packet)

        final_contract, _bundle = run_bounded_evidence_loop(
            initial_packet=packet, adjudicate=evidence_adjudicate, db=db,
            tool_handlers={"ask_character_sheet": handle_ask_character_sheet},
        )
        packet = validation_packet
        report = default_pipeline.validate(final_contract, packet)
        if report.passed:
            contract = final_contract
        else:
            contract, report = run_with_bounded_regeneration(adjudicate, packet)
    except Exception as exc:
        db.rollback()
        _fail_visible(exc)
        raise

    # Mode-aware lifecycle dispatch: non-final modes must not go through
    # the final narration-and-commit path.
    if contract.mode == "await_roll":
        try:
            return _complete_await_roll(
                db, turn=turn, attempt=attempt, contract=contract, trace_id=tid,
            )
        except Exception as exc:
            db.rollback()
            try:
                from models.dm import DmTurnAttempt as _Att

                current = db.get(_Att, attempt.id)
                if current is not None and current.status in (ATTEMPT_PREPARED, ATTEMPT_RUNNING):
                    _fail_visible(exc)
            except Exception:
                pass
            raise
    if contract.mode == "silent":
        try:
            return _complete_silent(
                db, turn=turn, attempt=attempt, contract=contract,
                provider=pname or "dm-provider", trace_id=tid,
            )
        except Exception as exc:
            db.rollback()
            try:
                from models.dm import DmTurnAttempt as _Att

                current = db.get(_Att, attempt.id)
                if current is not None and current.status in (ATTEMPT_PREPARED, ATTEMPT_RUNNING):
                    _fail_visible(exc)
            except Exception:
                pass
            raise

    if narrator is None:
        try:
            from app.dm.adjudication import build_provider_narrator

            narrator = build_provider_narrator(
                timeout_seconds=timeout_seconds,
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
    """Oldest retry-eligible prepared attempts for autonomous execution.

    Attempts in retry backoff (``next_retry_at`` in the future) are skipped
    so one failing attempt cannot starve newer prepared work.
    """
    from datetime import datetime, timezone

    from sqlalchemy import or_

    from models.dm import DmTurnAttempt

    now = datetime.now(timezone.utc)
    q = (
        select(DmTurnAttempt)
        .where(
            DmTurnAttempt.status == "prepared",
            or_(
                DmTurnAttempt.next_retry_at.is_(None),
                DmTurnAttempt.next_retry_at <= now,
            ),
        )
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


def handle_dm_turn_execute(envelope, db: Session | None = None) -> dict:
    """Queue-worker handler for ``dm.turn.execute`` envelopes.

    Worker contract is single-argument ``handler(envelope)`` (see
    ``app.worker.executor.execute_worker_job``); the handler owns its DB
    session via ``SessionLocal``. ``db`` is an optional seam for tests.
    """
    payload = getattr(envelope, "payload", None) or {}
    raw = payload.get("attempt_id") or payload.get("attemptId")
    if not raw:
        raise ValueError("dm.turn.execute envelope payload must include attempt_id")

    def _shape(result) -> dict:
        narration = getattr(result, "narration", None)
        stream_id = str(narration.stream_id) if narration is not None else None
        out: dict = {
            "attempt_id": str(raw),
            "turn_id": str(result.turn.id),
            "stream_id": stream_id,
        }
        mode = getattr(result, "mode", None)
        if mode:
            out["mode"] = mode
        return out

    if db is not None:
        result = execute_dm_attempt(db, uuid.UUID(str(raw)))
        if result is None:
            return {"attempt_id": str(raw), "skipped": True}
        return _shape(result)
    from database import SessionLocal

    if SessionLocal is None:
        raise RuntimeError("SessionLocal is not configured")
    with SessionLocal() as session:
        result = execute_dm_attempt(session, uuid.UUID(str(raw)))
        if result is None:
            return {"attempt_id": str(raw), "skipped": True}
        return _shape(result)


def register_dm_worker() -> None:
    """Register the DM execution handler on the queue consumer."""
    from app.queue.consumer import WORKER_HANDLERS

    WORKER_HANDLERS[DM_TURN_EXECUTE_JOB] = handle_dm_turn_execute


register_dm_worker()
