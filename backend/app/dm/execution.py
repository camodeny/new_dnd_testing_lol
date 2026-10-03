"""DM turn execution — how accepted player input becomes a committed turn.

1. ``runtime.router`` accepts a submission; ``dm.turns.coordinate_turn``
   groups unresolved input into a turn with one prepared attempt.
2. ``dm.recovery.execute_committed_attempt`` (post-response) or
   :func:`run_dm_execute_sweep` (``/api/cron/dm-execute``) calls
   :func:`execute_dm_attempt`, which takes ``dm.ownership`` of the attempt.
3. :func:`_claim` — idempotent skip, backpressure gate (``post_turn.backpressure``),
   then ``turns.mark_attempt_running``.
4. Context — ``dm.context.assemble_attempt_context`` builds the packet.
5. Adjudicate + validate — ``dm.adjudication.adjudicate_with_failover`` (role
   failover, ``providers.runner`` accounting) inside
   ``dm.evidence.run_bounded_evidence_loop``, checked by ``dm.validators``
   with bounded regeneration. A narration-only retry reuses a snapshot.
6. Mode dispatch — ``await_roll`` -> ``rolls.service.request_rolls``;
   ``silent`` -> ``turns.commit_turn(silent=True)``.
7. Narrate + commit — ``dm.narration.execute_validated_turn`` stages effects,
   streams durable narration (``dm.streams``), then ``turns.commit_turn``
   applies ``dm.effects`` via ``campaigns.events.commit_campaign_mutation``.
   Derived world work runs later in ``post_turn`` (``/api/cron/post-turn``).

Failures never fabricate a turn: :func:`_record_failure` requeues a
pre-visibility transient or leaves a visible failure with a generic retry
marker; archive mid-run defers via :func:`_defer_archived`.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log

logger = logging.getLogger(__name__)

_IDEMPOTENT_SKIP_STATUSES = ("succeeded", "streaming", "failed_visible", "superseded", "discarded", "abandoned")


def _is_config_error(exc: BaseException) -> bool:
    msg = str(exc)
    return "_API_KEY is not set" in msg or "_MODEL is not set" in msg


def retry_backoff_seconds(retry_count: int) -> int:
    """Bounded exponential backoff for retriable DM attempts (seconds)."""
    count = max(1, int(retry_count or 1))
    return min(30 * (2 ** (count - 1)), 600)


@dataclass
class _Run:
    """One claimed attempt moving through the spine."""

    db: Session
    attempt: Any
    turn: Any
    attempt_id: uuid.UUID
    turn_id: uuid.UUID
    campaign_id: uuid.UUID
    trace_id: str
    timeout_seconds: float
    is_recovery: bool
    provider: str | None = None
    model: str | None = None
    path_info: dict = field(default_factory=dict)


@dataclass
class NonNarratedResult:
    """Outcome of an ``await_roll`` or ``silent`` contract (no narration stream)."""

    turn: Any
    attempt: Any
    mode: str
    roll_requests: list = field(default_factory=list)
    narration: Any = None
    event: Any = None


def _execute_owned_attempt(
    db: Session,
    attempt_id: uuid.UUID,
    *,
    adjudicate=None,
    narrator=None,
    provider_name: str | None = None,
    timeout_seconds: float = 90,
    trace_id: str | None = None,
):
    """Claim and execute one prepared DM attempt end-to-end (idempotent).

    ``adjudicate``/``narrator`` are injectable seams for tests; production
    defaults resolve the configured provider via ``app.dm.adjudication``.
    Returns the narration ``ValidatedTurnResult`` or a
    :class:`NonNarratedResult`; ``None`` when skipped or deferred.
    """
    from app.campaigns.service import CampaignArchivedError
    from app.dm.narration import NarrationStreamError

    run = _claim(db, attempt_id, trace_id=trace_id or str(uuid.uuid4()), timeout_seconds=timeout_seconds)
    if run is None:
        return None
    try:
        packet = _assemble_production_context(db, run.attempt_id)
    except Exception as exc:
        # Missing authority fails identically on every retry: never requeue.
        _record_failure(run, exc, retryable=False)
        raise
    try:
        adjudicate = _build_adjudicator(run, adjudicate, provider_name)
    except Exception as exc:
        _record_failure(run, exc)
        raise
    snapshot = _narration_retry_contract(run)
    try:
        if snapshot is not None:
            contract = snapshot
        else:
            adjudicate = _with_deferral_advisory(run, adjudicate)
            contract, packet = _adjudicate_and_validate(run, adjudicate, packet)
    except Exception as exc:
        _record_failure(run, exc)
        raise
    # Issue #265 — dormancy re-check at the first durable visibility
    # boundary: archive may have committed during adjudication.
    if _campaign_archived(db, run.campaign_id):
        return _defer_archived(run, "archived_at_visibility_boundary")
    try:
        if contract.mode == "await_roll":
            return _complete_await_roll(db, run, contract)
        if contract.mode == "silent":
            return _complete_silent(db, run, contract)
        return _narrate_and_commit(
            run, contract, packet,
            narrator=_build_narrator(run, narrator),
            adjudicate=adjudicate,
            can_readjudicate=snapshot is None,
        )
    except NarrationStreamError as exc:
        _offer_narration_retry(run, exc)
        raise
    except CampaignArchivedError:
        # The commit or chunk-0 boundary refused the archived table and
        # rolled back with it, so nothing visible persisted.
        return _defer_archived(run, contract.mode if contract.mode in ("await_roll", "silent") else "first_visible_boundary")
    except Exception as exc:
        _record_failure(run, exc)
        raise


def execute_dm_attempt(db: Session, attempt_id: uuid.UUID, **kwargs):
    """Only the owner may claim, execute, or fail this attempt."""
    from app.dm.ownership import execution_ownership
    from models.dm import DmTurnAttempt

    # Issue #265 — dormancy freezes fictional time: a prepared attempt on an
    # archived campaign is deferred, not executed. It stays prepared so the
    # post-restore sweep resumes the exact same table; returning None keeps
    # the sweep's skipped accounting without failing the job.
    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is not None and _campaign_archived(db, attempt.campaign_id):
        logger.info(
            "dm_execute deferred attempt_id=%s campaign_id=%s reason=archived",
            attempt_id, attempt.campaign_id,
        )
        return None

    with execution_ownership(db, attempt_id) as acquired:
        if not acquired:
            return None
        return _execute_owned_attempt(db, attempt_id, **kwargs)


# ── Spine steps ─────────────────────────────────────────────────────────────


def _claim(db: Session, attempt_id: uuid.UUID, *, trace_id: str, timeout_seconds: float) -> _Run | None:
    """Gate and claim the attempt; ``None`` when there is nothing to run.

    Already terminal/streaming work is never re-executed. Issue #222 — while
    post-turn trails beyond the safe forward-DM context budget, new AI
    progression pauses BEFORE context becomes unreliable: the attempt stays
    prepared (backpressure is not failure) with a future retry-eligibility
    time, and resumes rebased onto current authority after catch-up. A
    caller observing running work never adopts another worker's claim.
    """
    from app.dm.turns import mark_attempt_running
    from app.post_turn.backpressure import pause_if_backpressured
    from models.dm import DmTurn, DmTurnAttempt

    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is None:
        raise ValueError(f"DM attempt {attempt_id} not found")
    if attempt.status in _IDEMPOTENT_SKIP_STATUSES:
        logger.info("dm_execute skip attempt_id=%s status=%s", attempt.id, attempt.status)
        return None
    if pause_if_backpressured(
        db, attempt.campaign_id, attempt_id=attempt.id, turn_id=attempt.turn_id,
    ) is not None:
        _defer_backpressured_attempt(db, attempt)
        return None
    attempt = _refresh_backpressured_stale_attempt(db, attempt)
    try:
        attempt = mark_attempt_running(db, attempt.id)
    except ValueError:
        db.rollback()
        return None
    return _Run(
        db=db,
        attempt=attempt,
        turn=db.get(DmTurn, attempt.turn_id),
        attempt_id=attempt.id,
        turn_id=attempt.turn_id,
        campaign_id=attempt.campaign_id,
        trace_id=trace_id,
        timeout_seconds=timeout_seconds,
        is_recovery=_is_recovery_attempt(db, attempt),
    )


def _is_recovery_attempt(db: Session, attempt) -> bool:
    """Whether provider calls for this attempt are recovery/non-billable.

    Only attempts whose parent was abandoned via explicit Retry count:
    ordinary pre-stream supersession also creates parented attempts, but
    those are first-try executions (primary/billable). Automatic
    same-attempt retries (a requeued transient, ``retry_count > 0``) are
    recovery work too.
    """
    from models.dm import DmTurnAttempt

    if int(getattr(attempt, "retry_count", 0) or 0) > 0:
        return True
    parent_id = getattr(attempt, "parent_attempt_id", None)
    if parent_id is None:
        return False
    try:
        parent = db.get(DmTurnAttempt, parent_id)
    except Exception:
        return False
    return (
        parent is not None
        and parent.status == "abandoned"
        and (parent.abandonment_reason or "") == "explicit_retry"
    )


def _assemble_production_context(db: Session, attempt_id: uuid.UUID):
    """Assemble the attempt context, scoping out a not-yet-established scene.

    Strict assembly first (fail-closed). When it fails only because the
    campaign has no current-scene row yet, retry once with that lane
    explicitly declared ``not_applicable`` and the downgrade recorded as a
    source error. Any other missing authority — or a scene row that exists
    but could not be read — still fails closed.
    """
    from app.dm.context import (
        LaneName,
        MissingAuthoritativeContextError,
        assemble_attempt_context,
    )
    from models.dm import DmTurnAttempt
    from models.world import CampaignCurrentScene

    try:
        return assemble_attempt_context(db, attempt_id)
    except MissingAuthoritativeContextError as exc:
        msg = str(exc)
        if LaneName.CURRENT_SCENE.value not in msg:
            raise
        attempt = db.get(DmTurnAttempt, attempt_id)
        if attempt is None or db.get(CampaignCurrentScene, attempt.campaign_id) is not None:
            raise
        logger.warning(
            "dm_execute context lane scoped attempt_id=%s lanes=%s",
            attempt_id, [LaneName.CURRENT_SCENE.value],
        )
        return assemble_attempt_context(
            db, attempt_id,
            supplemental_status={LaneName.CURRENT_SCENE: "not_applicable"},
            supplemental_errors={LaneName.CURRENT_SCENE: [f"declared not_applicable: {msg}"[:500]]},
        )


def _build_adjudicator(run: _Run, adjudicate, provider_name: str | None):
    """Production adjudicator through the role-aware failover path (#208)."""
    run.provider = provider_name
    if adjudicate is None:
        from app.providers import policy as role_policy

        path = role_policy.execution_path("forward_dm")
        run.provider = provider_name or path[0][0]
        run.model = path[0][1]
        adjudicate = _failover_adjudicator(run)
    structured_log(
        logger, logging.INFO, "dm_execute_start",
        submission_ids=[str(s) for s in run.attempt.submission_ids or []],
        turn_id=str(run.turn_id), attempt_id=str(run.attempt_id),
        provider=run.provider, model=run.model, trace_id=run.trace_id,
    )
    return adjudicate


def _failover_adjudicator(run: _Run):
    """``adjudicate(packet, feedback)`` over ``adjudicate_with_failover``, recording the path taken."""

    def adjudicate_via_failover(packet, feedback=None):
        from app.dm.adjudication import adjudicate_with_failover

        _ = feedback  # feedback reaches the model via the regeneration packet
        contract, info = adjudicate_with_failover(
            packet, db=run.db, role="forward_dm",
            timeout_seconds=run.timeout_seconds, trace_id=run.trace_id,
            # Explicit-Retry attempts carry retry lineage: even the first
            # provider call is recovery/non-billable so failed work is never
            # double-charged.
            is_retry=run.is_recovery,
            campaign_id=run.campaign_id,
        )
        run.path_info.update(info)
        return contract

    return adjudicate_via_failover


def _narration_retry_contract(run: _Run):
    """Narration-only retry (#208): reuse the parent's preserved valid contract.

    A fresh explicit-Retry attempt carrying ``contract_snapshot`` skips
    adjudication and goes straight to narration+commit; staged effects are
    re-staged from the snapshot (never copied as committed truth).
    """
    from app.dm.contract import normalize_contract

    attempt = run.attempt
    if getattr(attempt, "contract_snapshot", None) is None or getattr(attempt, "parent_attempt_id", None) is None:
        return None
    try:
        contract = normalize_contract(dict(attempt.contract_snapshot))
    except Exception:
        return None
    structured_log(
        logger, logging.INFO, "dm_execute_narration_retry",
        turn_id=str(run.turn_id), attempt_id=str(run.attempt_id), trace_id=run.trace_id,
    )
    return contract


def _with_deferral_advisory(run: _Run, adjudicate):
    """Attach the identity-deferral advisory to every adjudication packet.

    When an abandoned explicit-retry parent deferred a new-entity identity,
    deterministic adjudication would otherwise replay the identical frame
    into the same DEFER. Advisory only — the generative DM stays
    authoritative. Fail-soft: no advisory on error.
    """
    from app.dm.context import attach_retry_deferral_advisory, build_retry_deferral_advisory

    note = build_retry_deferral_advisory(run.db, run.attempt)
    if note is None:
        return adjudicate
    structured_log(
        logger, logging.INFO, "dm_execute_deferral_advisory",
        turn_id=str(run.turn_id), attempt_id=str(run.attempt_id), trace_id=run.trace_id,
    )

    def advised(packet, feedback=None):
        return adjudicate(attach_retry_deferral_advisory(packet, note), feedback=feedback)

    return advised


def _adjudicate_and_validate(run: _Run, adjudicate, start_packet):
    """Evidence loop + validation with bounded regeneration.

    Returns ``(contract, packet)`` where packet is the one the contract
    validated against: a perspective repair (issue #455) swaps in a packet
    carrying the resolved lane entries, and later validation must use it.
    """
    from app.dm.contract import ContractValidationError
    from app.dm.evidence import run_bounded_evidence_loop
    from app.dm.validators import default_pipeline, run_with_bounded_regeneration

    def repair_missing_perspectives(report, pkt):
        """Deterministic resolve-then-retry; None falls back to scope-narrowing."""
        try:
            from app.dm.context import repair_packet_missing_perspectives
            from app.dm.validators import missing_perspective_subjects
            from models.campaigns import Campaign

            if pkt is None:
                return None
            campaign = run.db.get(Campaign, run.campaign_id)
            subjects = missing_perspective_subjects(report)
            if campaign is None or not subjects:
                return None
            return repair_packet_missing_perspectives(pkt, run.db, campaign, subjects)
        except Exception as exc:
            logger.warning("dm_execute perspective repair failed: %s", exc)
            return None

    def regenerate(pkt):
        repaired_packets = []

        def repair_hook(report, current):
            repaired = repair_missing_perspectives(report, current)
            if repaired is not None:
                repaired_packets.append(repaired)
            return repaired

        contract, _ = run_with_bounded_regeneration(adjudicate, pkt, packet_repair=repair_hook)
        return contract, (repaired_packets[-1] if repaired_packets else pkt)

    validation_packet = start_packet

    def evidence_adjudicate(enriched_packet):
        nonlocal validation_packet
        validation_packet = enriched_packet
        try:
            return adjudicate(enriched_packet)
        except ContractValidationError:
            repaired, validation_packet = regenerate(enriched_packet)
            return repaired

    final_contract, _bundle = run_bounded_evidence_loop(
        initial_packet=start_packet, adjudicate=evidence_adjudicate, db=run.db,
    )
    if default_pipeline.validate(final_contract, validation_packet).passed:
        return final_contract, validation_packet
    return regenerate(validation_packet)


def _build_narrator(run: _Run, narrator):
    """Production streaming narrator, or the deterministic template on opt-in."""
    if narrator is None:
        from app.dm.adjudication import build_provider_narrator

        return build_provider_narrator(
            timeout_seconds=run.timeout_seconds,
            db=run.db, trace_id=run.trace_id, is_retry=run.is_recovery,
            campaign_id=run.campaign_id,
        )
    if narrator == "deterministic":
        # Explicit opt-in to the deterministic template narrator (no model
        # call): same production stream/commit path, used by tests.
        return None
    return narrator


def _narrate_and_commit(run: _Run, contract, packet, *, narrator, adjudicate, can_readjudicate: bool):
    """Stage, stream, and commit; re-adjudicate once on an identity conflict.

    Identity resolution runs after attempt-local staging but before chunk
    zero. On a conflict, roll back its uncommitted reads and re-adjudicate
    once against the resolved canonical identity; the next staging pass
    replaces the old snapshot/effects.
    """
    from app.dm.context import LaneName
    from app.dm.narration import execute_validated_turn
    from app.world.identity import IdentityReuseRequiresReadjudication
    from models.dm import DmTurnAttempt

    db = run.db
    repaired = False
    while True:
        try:
            result = execute_validated_turn(
                db,
                turn_id=run.turn_id,
                attempt_id=run.attempt_id,
                contract=contract,
                narrator=narrator,
                provider=run.provider or "dm-provider",
                publish_realtime=True,
                trace_id=run.trace_id,
            )
            structured_log(
                logger, logging.INFO, "dm_execute_complete",
                turn_id=str(result.turn.id), attempt_id=str(result.attempt.id),
                stream_id=str(result.narration.stream_id),
                audience=str(getattr(result.turn, "audience", "campaign")),
                provider=run.path_info.get("provider") or run.provider,
                model=run.path_info.get("model") or run.model,
                failover_reasons=run.path_info.get("failover_reasons") or [],
                ttft_added_ms=run.path_info.get("ttft_added_ms") or 0.0,
                trace_id=run.trace_id,
            )
            return result
        except IdentityReuseRequiresReadjudication as conflict:
            db.rollback()
            current = db.get(DmTurnAttempt, run.attempt_id)
            if current is not None:
                # Staging committed the original contract. It must not
                # become a narration-only retry candidate if correction
                # fails before the replacement contract is staged.
                current.contract_snapshot = None
                current.staged_effects = []
                current.identity_resolutions = None
                db.add(current)
                db.commit()
            if repaired or not can_readjudicate:
                raise
            repaired = True
            packet = packet.with_records(
                {LaneName.REPAIR_DIRECTIVES: [_identity_repair_record(packet, conflict)]},
                dependency="identity_repair",
                budget=packet.headroom_budget(4096, 1024),
            )
            structured_log(
                logger, logging.INFO, "dm_execute_identity_readjudication",
                turn_id=str(run.turn_id), attempt_id=str(run.attempt_id),
                canonical_id=conflict.canonical_id, trace_id=run.trace_id,
            )
            contract, packet = _adjudicate_and_validate(run, adjudicate, packet)


def _identity_repair_record(packet, conflict):
    """Required canonical identity feedback for one re-adjudication."""
    from app.dm.context import AuthorizationScope, ContextRecord, SourceRef

    return ContextRecord(
        record_id=f"identity-repair:{conflict.temp_id}:{conflict.canonical_id}",
        value={
            "proposed_temp_id": conflict.temp_id,
            "proposed_name": conflict.proposed_name,
            "canonical_entity": {
                "id": conflict.canonical_id,
                "name": conflict.canonical_name,
                "kind": conflict.canonical_kind,
                "summary": conflict.canonical_summary,
            },
            "directive": (
                "Identity resolution found an existing canonical entity for the "
                "proposed new NPC. Re-adjudicate the same player intent. If this "
                "is that person, remove the new_entities proposal, use the exact "
                "canonical EntityRef, and rewrite any new-person introduction. "
                "If a genuinely distinct person is needed, give the proposal a "
                "distinct name and distinguishing details."
            ),
        },
        sources=[SourceRef(
            source_type="world_entity",
            source_id=conflict.canonical_id,
            source_version=conflict.canonical_revision,
        )],
        authorization=AuthorizationScope(
            campaign_id=packet.audience.campaign_id,
            thread_ids=[packet.audience.thread_id],
        ),
        visibility="dm_only",
        use="adjudication_only",
        required=True,
        priority=100,
    )


# ── Non-narrated modes ──────────────────────────────────────────────────────


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


def _complete_await_roll(db: Session, run: _Run, contract) -> NonNarratedResult:
    """Persist a player-owned roll request and leave the same turn open."""
    from app.rolls.service import request_rolls

    turn, attempt = run.turn, run.attempt
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
            attempt.result = {**(attempt.result or {}), "prompt": prompt[:1000]}
            db.add(attempt)
        except Exception:
            pass
    db.commit()
    db.refresh(turn)
    db.refresh(attempt)
    structured_log(
        logger, logging.INFO, "dm_execute_await_roll",
        turn_id=str(turn.id), attempt_id=str(attempt.id),
        roll_request_ids=[str(r.id) for r in rows], trace_id=run.trace_id,
    )
    return NonNarratedResult(turn=turn, attempt=attempt, mode="await_roll", roll_requests=rows)


def _complete_silent(db: Session, run: _Run, contract) -> NonNarratedResult:
    """Complete a valid silent contract with no visible narration.

    Zero-visible-output still resolves through the shared ``commit_turn``
    path (revision bump + completion event, timestamps, submission
    resolution) so consumed input is never re-adjudicated.
    """
    from app.dm.turns import commit_turn, stage_validated_attempt

    stage_validated_attempt(db, run.attempt_id, contract)
    turn, attempt, event = commit_turn(db, run.turn_id, run.attempt_id, silent=True)
    structured_log(
        logger, logging.INFO, "dm_execute_silent",
        turn_id=str(turn.id), attempt_id=str(attempt.id),
        provider=run.provider or "dm-provider", trace_id=run.trace_id,
    )
    return NonNarratedResult(turn=turn, attempt=attempt, mode="silent", event=event)


# ── Failure, deferral, and backpressure handling ────────────────────────────


def _classify_failure(exc: BaseException) -> str:
    """Map execution failures to attempt error_class (retriable default)."""
    from app.providers import policy as role_policy

    if isinstance(exc, RuntimeError) and "Unapproved model substitution" in str(exc):
        return "terminal"
    try:
        cls, _reason = role_policy.classify_execution_failure(exc)
        return cls
    except Exception:
        pass
    from app.worker.executor import TERMINAL, classify_error

    if _is_config_error(exc):
        return "retriable"
    try:
        return classify_error(exc)
    except Exception:
        return TERMINAL


def _attach_public_retry_marker(db: Session, attempt_id: uuid.UUID) -> None:
    """Attach a generic player-visible retry marker without infra details.

    Internal diagnostics stay in ``last_error``/logs; ``result.public_error``
    is the only player-facing surface and never includes provider, model,
    status code, or exception text.
    """
    from app.providers import policy as role_policy

    from models.dm import DmTurnAttempt

    try:
        attempt = db.get(DmTurnAttempt, attempt_id)
        if attempt is None:
            return
        attempt.result = {
            **(attempt.result or {}),
            "public_error": role_policy.GENERIC_RETRYABLE_MESSAGE,
            "retryable": True,
        }
        db.add(attempt)
        db.flush()
    except Exception:
        pass


def _record_failure(run: _Run, exc: BaseException, *, retryable: bool = True) -> None:
    """The one pre-commit failure path for every spine step.

    Only acts while the attempt is still this worker's pre-stream claim
    (post-visibility remediation happens inside narration, and superseded
    work is never resurrected). A retriable failure before anything became
    visible requeues the claim BEHIND ready work (prepared, error kept,
    future ``next_retry_at``) so the next sweep retries it without starving
    newer attempts. Anything else — and every non-``retryable`` failure such
    as missing context authority, which fails identically on each retry —
    leaves a visible failure so the table never looks stuck "thinking".
    """
    from datetime import datetime, timedelta, timezone

    from app.dm.turns import ATTEMPT_PREPARED, ATTEMPT_RUNNING, mark_attempt_failed
    from app.worker.executor import RETRIABLE
    from models.dm import DmTurn, DmTurnAttempt

    db = run.db
    try:
        db.rollback()
    except Exception:
        pass
    error_class = _classify_failure(exc)
    error = f"{type(exc).__name__}: {exc}"[:2000]
    try:
        current = db.get(DmTurnAttempt, run.attempt_id)
        if current is None or current.status not in (ATTEMPT_PREPARED, ATTEMPT_RUNNING):
            return
        turn = db.get(DmTurn, run.turn_id)
        crossed = turn is not None and turn.status in ("streaming", "failed_visible")
        if retryable and error_class == RETRIABLE and not crossed:
            retries = int(getattr(current, "retry_count", 0) or 0) + 1
            current.last_error = error
            current.error_class = error_class
            current.status = ATTEMPT_PREPARED
            current.started_at = None
            current.completed_at = None
            current.retry_count = retries
            current.next_retry_at = datetime.now(timezone.utc) + timedelta(
                seconds=retry_backoff_seconds(retries)
            )
            db.add(current)
            db.commit()
            structured_log(
                logger, logging.WARNING, "dm_execute_retryable",
                attempt_id=str(run.attempt_id), turn_id=str(run.turn_id),
                error_class=error_class, error=str(exc)[:500], trace_id=run.trace_id,
            )
            return
        mark_attempt_failed(db, run.attempt_id, error=error, error_class=error_class, visible=True)
        if retryable:
            try:
                _attach_public_retry_marker(db, run.attempt_id)
                db.commit()
            except Exception:
                db.rollback()
    except Exception as mark_exc:
        logger.warning(
            "dm_execute failure-marking failed attempt_id=%s error=%s",
            run.attempt_id, mark_exc,
        )
    structured_log(
        logger, logging.WARNING, "dm_execute_failed",
        attempt_id=str(run.attempt_id), turn_id=str(run.turn_id),
        error_class=error_class, error=str(exc)[:500], trace_id=run.trace_id,
    )


def _offer_narration_retry(run: _Run, exc) -> None:
    """Post-visibility stream failure: offer narration-only retry.

    Remediation already happened inside ``execute_validated_turn``; the
    valid structured packet survives in ``contract_snapshot`` so narration
    can retry independently without re-adjudication.
    """
    from models.dm import DmTurnAttempt

    db = run.db
    try:
        current = db.get(DmTurnAttempt, run.attempt_id)
        if current is not None:
            current.result = {
                **(current.result or {}),
                "narration_retry_available": True,
                "partial_stream_id": str(exc.stream_id) if exc.stream_id else None,
            }
            db.add(current)
            _attach_public_retry_marker(db, run.attempt_id)
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    structured_log(
        logger, logging.WARNING, "dm_execute_stream_failed",
        turn_id=str(run.turn_id), attempt_id=str(run.attempt_id),
        stream_id=str(exc.stream_id), trace_id=run.trace_id,
    )


def _campaign_archived(db: Session, campaign_id: uuid.UUID) -> bool:
    from models.campaigns import Campaign

    try:
        campaign = db.get(Campaign, campaign_id)
        return campaign is not None and str(campaign.status or "").lower() == "archived"
    except Exception as exc:
        logger.warning("dm_execute archive check failed campaign_id=%s error=%s", campaign_id, exc)
        return False


def _defer_archived(run: _Run, reason: str) -> None:
    """Dormancy deferral — issue #265.

    Archive committed after this worker's last playability check. Reset the
    claim to prepared (no failure marker — dormancy is not failure) so the
    post-restore sweep resumes the exact same attempt.
    """
    from app.dm.turns import ATTEMPT_PREPARED, ATTEMPT_RUNNING
    from models.dm import DmTurnAttempt

    db = run.db
    try:
        db.rollback()
    except Exception:
        pass
    try:
        current = db.get(DmTurnAttempt, run.attempt_id)
        if current is not None and current.status == ATTEMPT_RUNNING:
            current.status = ATTEMPT_PREPARED
            current.started_at = None
            current.last_error = f"CampaignArchived: {reason}"
            db.add(current)
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    logger.info(
        "dm_execute deferred attempt_id=%s campaign_id=%s reason=%s",
        run.attempt_id, run.campaign_id, reason,
    )
    return None


def _defer_backpressured_attempt(db: Session, attempt) -> None:
    """Defer a backpressured attempt behind ready work — issue #222.

    A blocked attempt stays ``prepared`` (no failure marker — backpressure
    is not failure) but takes a future ``next_retry_at`` via the existing
    retry-eligibility/backoff mechanism, so ``find_prepared_attempts`` skips
    it on subsequent sweeps instead of letting persistently blocked attempts
    monopolize every sweep and starve unrelated ready turns. ``retry_count``
    is untouched (this is not a failure); the sweep retries the attempt once
    the eligibility time passes and catch-up has cleared the backlog.
    """
    from datetime import datetime, timedelta, timezone

    from models.dm import DmTurnAttempt

    try:
        fresh = db.get(DmTurnAttempt, attempt.id)
        if fresh is None or fresh.status != "prepared":
            return
        fresh.next_retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=retry_backoff_seconds(int(getattr(fresh, "retry_count", 0) or 0) + 1)
        )
        db.add(fresh)
        db.commit()
        logger.info(
            "dm_execute backpressure_deferred attempt_id=%s campaign_id=%s next_retry_at=%s",
            fresh.id, fresh.campaign_id, fresh.next_retry_at,
        )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "dm_execute backpressure deferral failed attempt_id=%s error=%s",
            getattr(attempt, "id", None), exc,
        )


def _refresh_backpressured_stale_attempt(db: Session, attempt):
    """Rebase a backpressure-delayed attempt onto current authority — #222.

    Normal post-turn catch-up can itself advance ``Campaign.revision``
    (e.g. clock advancement/completion emits a domain event). An attempt
    prepared before the backlog is therefore stale once the gate passes,
    and context assembly would reject it as a stale-revision failure
    instead of resolving the accepted input. When the attempt is still the
    current prepared attempt of its pending turn with an unchanged input
    set, supersede it (pre-stream coordination semantics: same accepted
    submissions, fresh ``source_revision``, new attempt id) so the durable
    input executes against current authority. The stale-revision guard
    itself is untouched — this only refreshes never-executed prepared work.
    The refresh applies only to attempts carrying the explicit backpressure-
    deferral signal (``next_retry_at`` set with no failure markers): the
    ordinary transient-retry path also sets ``next_retry_at`` but always
    alongside ``last_error``/``error_class``/``retry_count``, and such
    attempts keep the existing fail-visible behavior with their
    recovery/non-billable lineage intact. Retry lineage is preserved onto
    the replacement attempt either way.
    Returns the attempt to execute (possibly a fresh row).
    """
    from app.dm.turns import _now, create_attempt
    from models.campaigns import Campaign
    from models.dm import DmTurn, DmTurnAttempt

    try:
        if (
            getattr(attempt, "next_retry_at", None) is None
            or getattr(attempt, "last_error", None) is not None
            or getattr(attempt, "error_class", None) is not None
        ):
            return attempt
        campaign = db.get(Campaign, attempt.campaign_id)
        if campaign is None:
            return attempt
        if int(campaign.revision or 0) == int(attempt.source_revision or 0):
            return attempt
        turn = db.get(DmTurn, attempt.turn_id)
        if turn is None:
            return attempt
        if (
            turn.status != "pending"
            or attempt.status != "prepared"
            or str(turn.current_attempt_id) != str(attempt.id)
            or list(turn.submission_ids or []) != list(attempt.submission_ids or [])
        ):
            return attempt
        old = db.get(DmTurnAttempt, attempt.id)
        if old is None or old.status != "prepared":
            return attempt
        old.status = "superseded"
        old.invalidation_reason = "backpressure_revision_refresh"
        old.invalidated_at = _now()
        new_attempt = create_attempt(
            db, turn, source_revision=int(campaign.revision or 0), parent=old,
            submission_ids=old.submission_ids, input_set_revision=old.input_set_revision,
            assembly_window=(old.assembly_window_start, old.assembly_window_end),
            roll_evidence=old.roll_evidence,
            retry_count=int(getattr(old, "retry_count", 0) or 0),
        )
        turn.source_revision = int(campaign.revision or 0)
        db.add(turn)
        db.flush()
        db.commit()
        db.refresh(new_attempt)
        logger.info(
            "dm_execute backpressure_revision_refresh campaign_id=%s turn_id=%s "
            "old_attempt_id=%s new_attempt_id=%s old_source_revision=%s new_source_revision=%s",
            campaign.id, turn.id, old.id, new_attempt.id,
            old.source_revision, new_attempt.source_revision,
        )
        return new_attempt
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "dm_execute backpressure revision refresh failed attempt_id=%s error=%s",
            getattr(attempt, "id", None), exc,
        )
        return attempt


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
