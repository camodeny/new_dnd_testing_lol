"""Contested secret actions against other PCs — issue #249.

Orchestration between a private initiating turn (canonical #248 private
action) and one or more affected human PCs. At a real table one PC may
secretly pickpocket, deceive, or spy on another PC: the DM needs a
check/save/contest from the target player without revealing the hidden
cause, and neither side may author the other's dice or voluntary behavior.

Responsibility split (never inverted):

- Deterministic code owns authorization, ownership, visibility scope, roll
  ownership, and dice arithmetic. Target rolls are ordinary #204
  ``PlayerRollRequest`` rows linked via ``secret_contest_id``: only the
  requested target user can fulfill them (``fulfill_roll`` enforcement),
  dice are always player-supplied (``resolve_d20_roll`` with
  ``roller="pc"`` raises instead of generating), and outcomes resolve
  through the normal #225 primitives.
- The initiating player can never supply the target's roll nor declare the
  target PC's voluntary action/thought/dialogue: structural agency checks
  reject ``player_declaration`` claims and roll requests naming a target
  character in initiator-only input.
- Hidden cause, initiator identity, DC/opposed state, and dice stay in
  DM-authorized projections only. Targets receive a mechanically valid roll
  request carrying just the safe public reason. Shared realtime/event
  payloads never reveal that a private action exists unless the
  rules/fiction outcome explicitly reveals it — and then only through an
  explicit #211 visibility grant plus a targeted notice, never shared
  metadata.
- Missing target input blocks resolution (``ContestNotReady``); there is no
  AI-takeover path — a contested mechanic waits for the human or for the
  normal DM cancel/replace availability path. Retries replay the committed
  outcome instead of double-applying.

Observability (IDs only, never secret content): lifecycle, hidden-cause
roll latency, blocked-on-human time, resolution/outcome category, and
leakage-guard failures.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Errors ────────────────────────────────────────────────────────────────


class SecretContestError(ValueError):
    """Base error for contested-secret orchestration."""


class ContestedAgencyViolation(PermissionError):
    """Initiating input authors a target PC's roll or voluntary behavior."""


class ContestNotReady(SecretContestError):
    """Target input is still missing; the mechanic stays blocked on humans."""


class ContestUnavailable(SecretContestError):
    """Contest was cancelled; resolution is no longer available."""


class ContestLeakageError(SecretContestError):
    """A caller-supplied safe projection embeds hidden-cause material."""


# ── Observability (process-local counters, IDs only) ──────────────────────

_counters: Counter = Counter(
    {
        "contests_started": 0,
        "contests_resolved": 0,
        "resolve_replays": 0,
        "not_ready_blocks": 0,
        "leakage_guard_failures": 0,
        "agency_rejections": 0,
        "unauthorized_projections": 0,
    }
)


def get_secret_contest_metrics() -> dict[str, int]:
    return dict(_counters)


def _inc(key: str, n: int = 1) -> None:
    _counters[key] += n


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Constants ─────────────────────────────────────────────────────────────

CONTEST_MODES = ("opposed", "target_vs_dc")
CONTEST_ROLL_KINDS = ("check", "save")
OUTCOME_INITIATOR_SUCCEEDS = "initiator_succeeds"
OUTCOME_TARGET_HOLDS = "target_holds"
STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_CANCELLED = "cancelled"

_EVENT_TYPE = "dm.secret_contest_resolved"

# Branch-fact visibility stays restricted at rest; reveal expands access via
# explicit #211 grants rather than by widening stored visibility.
_BRANCH_VISIBILITIES = ("dm_only", "private")


# ── Result shapes ─────────────────────────────────────────────────────────


@dataclass
class ResolveResult:
    contest: Any
    event: Any
    outcome: str
    revealed: bool
    replayed: bool = False


# ── Internal helpers ──────────────────────────────────────────────────────


def _coerce_uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SecretContestError(f"{field} must be a UUID") from exc


def _text(value: Any, *, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise SecretContestError(f"{field} must be between 1 and {maximum} characters")
    return text


def _structured(event: str, level: int = logging.INFO, **fields: Any) -> None:
    try:
        from app.observability.tracing import structured_log

        structured_log(logger, level, event, **fields)
    except Exception:
        logger.log(level, "secret_contest %s %s", event, {k: str(v)[:120] for k, v in fields.items()})


def _publish_best_effort(channel: str, event: str, payload: dict[str, Any]) -> None:
    """Realtime delivery never rolls back authoritative state."""
    try:
        from app.realtime.service import get_realtime_publisher

        get_realtime_publisher().publish(channel, event, payload)
    except Exception as exc:
        logger.warning("secret_contest realtime dropped channel=%s event=%s error=%s", channel, event, exc)


def _target_channel(db: Session, campaign_id: uuid.UUID, target_user_id: uuid.UUID) -> str | None:
    """Private DM channel for a target user (their authorized audience only)."""
    try:
        from app.realtime.channels import live_table_channel
        from app.runtime.threads import get_or_create_private_gameplay_thread

        thread, _ = get_or_create_private_gameplay_thread(
            db, campaign_id=campaign_id, created_by=target_user_id,
            private_kind="dm", participant_ids=[], title="Private with AI DM",
        )
        db.flush()
        return live_table_channel(campaign_id, thread.id)
    except Exception as exc:
        logger.warning("secret_contest target channel unavailable: %s", exc)
        return None


def _private_authorized_turn(db: Session, turn_id: uuid.UUID):
    """Load the initiating turn; require private audience on its thread."""
    from models.dm import DmTurn

    turn = db.get(DmTurn, turn_id)
    if turn is None:
        raise SecretContestError("initiating turn not found")
    if str(getattr(turn, "audience", "") or "") != "private":
        raise SecretContestError("contested secret actions require a private initiating turn")
    try:
        import uuid as _uuid

        from models.threads import CampaignThread

        thread = db.get(CampaignThread, _uuid.UUID(str(turn.thread_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SecretContestError("initiating turn has no valid private thread") from exc
    if thread is None or str(thread.thread_type) != "private":
        raise SecretContestError("contested secret actions require a private initiating thread")
    if str(thread.campaign_id) != str(turn.campaign_id):
        raise SecretContestError("initiating thread belongs to another campaign")
    return turn


def _check_membership(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID, *, field: str) -> None:
    from models.campaigns import Campaign, CampaignMember

    campaign = db.get(Campaign, campaign_id)
    member = db.get(CampaignMember, {"campaign_id": campaign_id, "user_id": user_id})
    if campaign is None or (campaign.owner_id != user_id and member is None):
        raise SecretContestError(f"{field} must be a campaign member")


def _check_character_owner(db: Session, character_id: uuid.UUID, user_id: uuid.UUID, *, field: str):
    from models.characters import Character

    character = db.get(Character, character_id)
    if character is None or character.owner_id != user_id:
        raise SecretContestError(f"{field} must be controlled by its requesting user")
    return character


def _validate_branch_facts(facts: Any, *, field: str) -> list[dict[str, Any]]:
    from app.world.knowledge import validate_epistemic_state

    items = list(facts or [])
    if len(items) > 8:
        raise SecretContestError(f"{field} may hold at most 8 facts")
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise SecretContestError(f"{field} entries must be objects")
        content = _text(item.get("content"), field=f"{field}.content", maximum=2000)
        visibility = str(item.get("visibility") or "dm_only")
        if visibility not in _BRANCH_VISIBILITIES:
            raise SecretContestError(f"{field}.visibility must stay restricted (dm_only/private)")
        epistemic = str(item.get("epistemic_state") or "confirmed")
        validate_epistemic_state(epistemic)
        cleaned.append({"content": content, "visibility": visibility, "epistemic_state": epistemic})
    return cleaned


def validate_initiator_agency(
    contract: Any,
    initiator_character_id: uuid.UUID,
    target_character_ids: set[str],
) -> None:
    """Reject initiator-authored target-PC rolls or voluntary behavior.

    Structural check over an adjudication contract produced from
    initiator-only input: any ``player_declaration`` naming a target
    character, or a roll request naming a target character, means the
    initiating player declared what another human's PC does, thinks, says,
    or rolls. Raises :exc:`ContestedAgencyViolation` (IDs only, never
    secret content). ``None`` contracts pass (nothing to author).
    """
    if contract is None:
        return
    targets = {str(c).lower() for c in target_character_ids}
    if not targets:
        return
    beats = getattr(contract, "beats", None) or []
    for beat in beats:
        for claim in getattr(beat, "claims", None) or []:
            actor = getattr(claim, "actor_ref", None)
            if actor is None or str(getattr(actor, "type", "")) != "character":
                continue
            if str(getattr(actor, "id", "")).lower() not in targets:
                continue
            if str(getattr(claim, "claim_kind", "")) == "player_declaration":
                _inc("agency_rejections")
                _structured(
                    "secret_contest_agency_rejected", level=logging.WARNING,
                    actor_character_id=str(getattr(actor, "id", "")),
                    claim_kind="player_declaration",
                )
                raise ContestedAgencyViolation(
                    "initiating input declares a target PC's voluntary action; "
                    "only the target player may declare it"
                )
    roll_request = getattr(contract, "roll_request", None)
    if roll_request is not None:
        named = getattr(roll_request, "character_id", None)
        if named not in (None, "") and str(named).lower() in targets:
            _inc("agency_rejections")
            _structured("secret_contest_agency_rejected", level=logging.WARNING, claim_kind="roll_request")
            raise ContestedAgencyViolation(
                "initiating input requests a roll for a target PC; "
                "only the target player may supply it"
            )
    _ = initiator_character_id


def _guard_safe_reason(reason_public: str, hidden_cause: str | None) -> None:
    """Fail closed when a caller embeds hidden cause in a safe projection."""
    cause = (hidden_cause or "").strip()
    if cause and cause in (reason_public or ""):
        _inc("leakage_guard_failures")
        _structured("secret_contest_leakage_guard", level=logging.WARNING, guard="reason_embeds_cause")
        raise ContestLeakageError("safe projection must not embed hidden-cause material")


# ── Start ─────────────────────────────────────────────────────────────────


def start_secret_contest(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    initiating_turn_id: uuid.UUID,
    initiator_user_id: uuid.UUID,
    initiator_character_id: uuid.UUID,
    targets: list[dict[str, Any]],
    contest_key: str,
    mode: str = "opposed",
    dc_private: int | None = None,
    initiator_roll_request_id: uuid.UUID | None = None,
    hidden_cause: str | None = None,
    reveal_on_success: bool = False,
    reveal_on_failure: bool = False,
    success_facts: list[dict[str, Any]] | None = None,
    failure_facts: list[dict[str, Any]] | None = None,
    operation_id: str | None = None,
    initiating_contract: Any | None = None,
):
    """Open a contested secret action and request hidden-cause target rolls.

    The initiating turn must be a private (#248) turn. Each target receives
    an ordinary #204 roll request carrying only ``reason_public`` — no hidden
    cause, no initiator identity, no DC. Target dice stay player-supplied;
    resolution is an explicit :func:`resolve_secret_contest` call that blocks
    until every target fulfills. Retried starts with the same
    ``(initiating_turn_id, contest_key)`` return the existing contest.
    """
    from app.rolls.service import ROLL_KINDS, request_rolls
    from models.dm import DmTurnAttempt, PlayerRollRequest, SecretContest

    campaign_id = _coerce_uuid(campaign_id, field="campaign_id")
    turn_id = _coerce_uuid(initiating_turn_id, field="initiating_turn_id")
    initiator_user_id = _coerce_uuid(initiator_user_id, field="initiator_user_id")
    initiator_character_id = _coerce_uuid(initiator_character_id, field="initiator_character_id")
    key = _text(contest_key, field="contest_key", maximum=32)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise SecretContestError("contest_key must match [A-Za-z0-9_-]+")
    if mode not in CONTEST_MODES:
        raise SecretContestError(f"mode must be one of {CONTEST_MODES}")
    if not targets or len(targets) > 8:
        raise SecretContestError("targets must contain between 1 and 8 affected PCs")

    turn = _private_authorized_turn(db, turn_id)
    if turn.campaign_id != campaign_id:
        raise SecretContestError("initiating turn belongs to another campaign")
    _check_membership(db, campaign_id, initiator_user_id, field="initiator_user_id")
    _check_character_owner(db, initiator_character_id, initiator_user_id, field="initiator_character_id")

    if mode == "target_vs_dc":
        if dc_private is None or type(dc_private) is not int or not 1 <= dc_private <= 1000:
            raise SecretContestError("target_vs_dc requires dc_private between 1 and 1000")
    elif dc_private is not None:
        raise SecretContestError("opposed contests resolve PC-vs-PC; dc_private is not used")

    initiator_roll_row = None
    if mode == "opposed":
        if initiator_roll_request_id is None:
            raise SecretContestError("opposed contests require the initiator's fulfilled roll reference")
        initiator_roll_row = db.get(PlayerRollRequest, _coerce_uuid(initiator_roll_request_id, field="initiator_roll_request_id"))
        if initiator_roll_row is None or initiator_roll_row.turn_id != turn.id:
            raise SecretContestError("initiator roll must belong to the initiating turn")
        if initiator_roll_row.requested_user_id != initiator_user_id:
            raise SecretContestError("initiator roll must be owned by the initiating player")

    # Idempotent start: same logical contest returns the existing row.
    existing = db.execute(
        select(SecretContest).where(
            SecretContest.initiating_turn_id == turn.id, SecretContest.contest_key == key
        )
    ).scalars().first()
    if existing is not None:
        return existing

    cleaned_targets: list[dict[str, Any]] = []
    seen_chars: set[str] = set()
    for item in targets:
        if not isinstance(item, dict):
            raise SecretContestError("targets entries must be objects")
        target_user = _coerce_uuid(item.get("target_user_id"), field="targets.target_user_id")
        target_char = _coerce_uuid(item.get("target_character_id"), field="targets.target_character_id")
        if target_user == initiator_user_id or target_char == initiator_character_id:
            raise SecretContestError("a PC cannot contest itself in secret")
        _check_membership(db, campaign_id, target_user, field="targets.target_user_id")
        _check_character_owner(db, target_char, target_user, field="targets.target_character_id")
        kind = str(item.get("roll_kind") or "")
        if kind not in CONTEST_ROLL_KINDS:
            raise SecretContestError("contested target rolls must be check or save")
        advantage = str(item.get("advantage_state") or "normal")
        if advantage not in {"normal", "advantage", "disadvantage"}:
            raise SecretContestError("advantage_state is invalid")
        reason = _text(item.get("reason_public"), field="targets.reason_public", maximum=600)
        _guard_safe_reason(reason, hidden_cause)
        label = _text(item.get("label"), field="targets.label", maximum=120)
        ability = _text(item.get("ability_or_skill"), field="targets.ability_or_skill", maximum=64)
        lowered = str(target_char).lower()
        if lowered in seen_chars:
            raise SecretContestError("targets must not repeat a character")
        seen_chars.add(lowered)
        cleaned_targets.append({
            "target_user_id": target_user, "target_character_id": target_char,
            "roll_kind": kind, "advantage_state": advantage,
            "reason_public": reason, "label": label, "ability_or_skill": ability,
        })

    # Initiator agency: initiator-only input must never declare a target PC.
    validate_initiator_agency(initiating_contract, initiator_character_id, seen_chars)

    success_branch = _validate_branch_facts(success_facts, field="success_facts")
    failure_branch = _validate_branch_facts(failure_facts, field="failure_facts")
    cause = (hidden_cause or "").strip() or None
    if cause is not None and len(cause) > 2000:
        raise SecretContestError("hidden_cause must be at most 2000 characters")

    attempt = db.get(DmTurnAttempt, turn.current_attempt_id) if turn.current_attempt_id else None
    if attempt is None:
        raise SecretContestError("initiating turn has no current attempt")

    contest = SecretContest(
        campaign_id=campaign_id, initiating_turn_id=turn.id,
        initiating_attempt_id=attempt.id,
        initiator_user_id=initiator_user_id, initiator_character_id=initiator_character_id,
        contest_key=key, mode=mode, dc_private=dc_private,
        initiator_roll_request_id=initiator_roll_row.id if initiator_roll_row is not None else None,
        target_user_ids=[str(t["target_user_id"]) for t in cleaned_targets],
        target_roll_request_ids=[],
        hidden_cause=cause,
        reveal_on_success=bool(reveal_on_success), reveal_on_failure=bool(reveal_on_failure),
        success_facts=success_branch, failure_facts=failure_branch,
        status=STATUS_PENDING,
        operation_id=(operation_id or f"secret-contest:{turn.id}:{key}")[:128],
    )
    db.add(contest)
    db.flush()

    request_payloads = [{
        "request_key": f"contest-{key}-t{i}",
        "requested_user_id": t["target_user_id"], "character_id": t["target_character_id"],
        "roll_kind": t["roll_kind"], "ability_or_skill": t["ability_or_skill"],
        "label": t["label"], "advantage_state": t["advantage_state"],
        "reason_public": t["reason_public"], "dc_private": dc_private,
    } for i, t in enumerate(cleaned_targets)]
    rows = request_rolls(
        db, campaign_id=campaign_id, turn_id=turn.id, attempt_id=attempt.id,
        requests=request_payloads,
    )
    for row in rows:
        row.secret_contest_id = contest.id
    db.flush()
    contest.target_roll_request_ids = [str(r.id) for r in rows]
    db.flush()
    db.commit()
    db.refresh(contest)

    # Target notification on the target's own private channel only: redacted
    # request (safe reason + mechanics), never cause/initiator/DC.
    for row, spec in zip(rows, cleaned_targets):
        channel = _target_channel(db, campaign_id, spec["target_user_id"])
        db.commit()
        if channel is not None:
            _publish_best_effort(channel, "secret_contest.roll_requested", {
                "type": "secret_contest.roll_requested",
                "event_id": f"secret-contest-roll:{contest.id}:{row.id}",
                "campaign_id": str(campaign_id), "contest_id": str(contest.id),
                "request_id": str(row.id), "request_key": row.request_key,
                "roll_kind": row.roll_kind, "ability_or_skill": row.ability_or_skill,
                "label": row.label, "advantage_state": row.advantage_state,
                "reason_public": row.reason_public,
            })
    _inc("contests_started")
    _structured(
        "secret_contest_started", contest_id=str(contest.id), turn_id=str(turn.id),
        mode=mode, target_count=len(rows),
    )
    return contest


# ── Target reads + fulfillment ────────────────────────────────────────────


def pending_hidden_cause_rolls_for_user(
    db: Session, *, campaign_id: uuid.UUID, user_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Redacted pending hidden-cause rolls for one target user.

    Server-side filtered by ``requested_user_id``; the public projection
    already strips ``dc_private``. Hidden cause and initiator identity are
    stored only on the contest row, which this projection never touches.
    """
    from models.dm import PlayerRollRequest

    campaign_id = _coerce_uuid(campaign_id, field="campaign_id")
    user_id = _coerce_uuid(user_id, field="user_id")
    rows = list(db.execute(
        select(PlayerRollRequest).where(
            PlayerRollRequest.campaign_id == campaign_id,
            PlayerRollRequest.requested_user_id == user_id,
            PlayerRollRequest.status == "pending",
            PlayerRollRequest.secret_contest_id.is_not(None),
        ).order_by(PlayerRollRequest.requested_at, PlayerRollRequest.id)
    ).scalars().all())
    return [row.to_dict() for row in rows]


def fulfill_hidden_cause_roll(
    db: Session, *, request_id: uuid.UUID, actor_id: uuid.UUID, payload: dict
):
    """Fulfill one hidden-cause target roll as its owning human player.

    Delegates to the #204 lifecycle: only the requested target user may
    fulfill (an initiating player attempting it receives the authorization
    error), dice stay player-supplied, and duplicates are rejected. The
    contest is never auto-resolved here — resolution stays an explicit,
    code-owned step once every target has answered.
    """
    from app.rolls.service import fulfill_roll
    from models.dm import PlayerRollRequest, SecretContest

    request_id = _coerce_uuid(request_id, field="request_id")
    actor_id = _coerce_uuid(actor_id, field="actor_id")
    req = db.get(PlayerRollRequest, request_id)
    if req is None or req.secret_contest_id is None:
        raise SecretContestError("hidden-cause roll request not found")
    contest = db.get(SecretContest, req.secret_contest_id)
    if contest is None or contest.status != STATUS_PENDING:
        raise SecretContestError("hidden-cause roll is no longer available")
    req_row, fulfillment, resumed, extra = fulfill_roll(
        db, request_id=req.id, actor_id=actor_id, payload=payload)
    db.commit()
    try:
        latency_ms = (fulfillment.submitted_at - req.requested_at).total_seconds() * 1000 \
            if fulfillment.submitted_at and req.requested_at else None
    except Exception:
        latency_ms = None
    _structured(
        "secret_contest_target_fulfilled", contest_id=str(contest.id),
        request_id=str(req.id),
        latency_ms=round(latency_ms, 2) if latency_ms is not None else None,
    )
    return req_row, fulfillment, resumed, contest


def get_contest_status(db: Session, contest_id: uuid.UUID) -> dict[str, Any]:
    """Lifecycle status with blocked-on-human timing (IDs only)."""
    from models.dm import PlayerRollRequest, SecretContest

    contest = db.get(SecretContest, _coerce_uuid(contest_id, field="contest_id"))
    if contest is None:
        raise SecretContestError("secret contest not found")
    pending: list[str] = []
    if contest.status == STATUS_PENDING:
        ids = []
        for raw in contest.target_roll_request_ids or []:
            try:
                ids.append(uuid.UUID(str(raw)))
            except (ValueError, TypeError):
                continue
        if ids:
            rows = list(db.execute(
                select(PlayerRollRequest).where(PlayerRollRequest.id.in_(ids))
            ).scalars().all())
            pending = [str(r.id) for r in rows if r.status == "pending"]
    blocked_ms: int | None = None
    if contest.status == STATUS_PENDING and contest.requested_at is not None:
        ref = contest.resolved_at or _now()
        blocked_ms = max(0, int((ref - contest.requested_at).total_seconds() * 1000))
    return {
        "contest_id": str(contest.id), "status": contest.status,
        "outcome": contest.outcome, "revealed": bool(contest.revealed),
        "pending_target_roll_ids": pending,
        "blocked_on_human_ms": blocked_ms,
    }


# ── Resolution ────────────────────────────────────────────────────────────


def _load_target_evidence(db: Session, contest):
    """Fulfilled target evidence re-read from durable roll state.

    A lost acknowledgement is recovered here: fulfillment rows are the
    source of truth, so a disconnect/reconnect between fulfill and resolve
    changes nothing. Raises :exc:`ContestNotReady` (generic, no cause)
    while any target input is missing.
    """
    from app.rolls.service import get_fulfillment
    from models.dm import PlayerRollRequest

    evidence: list[tuple[Any, Any]] = []
    for raw in contest.target_roll_request_ids or []:
        req = db.get(PlayerRollRequest, _coerce_uuid(raw, field="target_roll_request_id"))
        if req is None or req.secret_contest_id != contest.id:
            raise SecretContestError("contested target roll is missing")
        if req.status != "fulfilled":
            _inc("not_ready_blocks")
            _structured(
                "secret_contest_blocked_on_human", contest_id=str(contest.id),
                pending_request_id=str(req.id),
            )
            raise ContestNotReady("a target PC has not answered yet; the contest waits for its player")
        fulfillment = get_fulfillment(db, req.id)
        if fulfillment is None:
            _inc("not_ready_blocks")
            raise ContestNotReady("a target PC has not answered yet; the contest waits for its player")
        evidence.append((req, fulfillment))
    if not evidence:
        raise SecretContestError("secret contest has no target rolls")
    return evidence


def _resolve_pc_side(*, roll_id: str, req, fulfillment):
    """One player-owned side through the normal #225 d20 primitive.

    ``roller="pc"`` enforces player-supplied dice: nothing is generated.
    """
    from app.rules.resolution import ResolutionError, resolve_d20_roll

    dice = list(fulfillment.raw_rolls or [])
    try:
        return resolve_d20_roll(
            kind=req.roll_kind, roller="pc",
            skill=req.ability_or_skill, ability=req.ability_or_skill,
            modifier=int(fulfillment.modifier),
            dice=dice, advantage_state=str(req.advantage_state or "normal"),
            dc=None, dc_visibility="hidden", die_visibility="hidden",
            roll_id=roll_id,
        )
    except ResolutionError as exc:
        raise SecretContestError("contested roll could not be resolved") from exc


def _compute_outcome(db: Session, contest, evidence) -> tuple[str, dict[str, Any]]:
    from app.rolls.service import get_fulfillment
    from models.dm import PlayerRollRequest

    target_totals: dict[str, int] = {}
    target_success: dict[str, bool] = {}
    for req, fulfillment in evidence:
        if contest.mode == "target_vs_dc":
            from app.rules.resolution import ResolutionError, resolve_d20_roll

            try:
                resolution = resolve_d20_roll(
                    kind=req.roll_kind, roller="pc",
                    skill=req.ability_or_skill, ability=req.ability_or_skill,
                    modifier=int(fulfillment.modifier),
                    dice=list(fulfillment.raw_rolls or []),
                    advantage_state=str(req.advantage_state or "normal"),
                    dc=int(contest.dc_private), dc_visibility="hidden",
                    die_visibility="hidden",
                    roll_id=f"249:{contest.id}:t:{req.id}",
                )
            except ResolutionError as exc:
                raise SecretContestError("contested roll could not be resolved") from exc
            target_totals[str(req.id)] = int(resolution.total)
            target_success[str(req.id)] = bool(resolution.success)
        else:
            resolution = _resolve_pc_side(
                roll_id=f"249:{contest.id}:t:{req.id}", req=req, fulfillment=fulfillment)
            target_totals[str(req.id)] = int(resolution.total)
    if contest.mode == "target_vs_dc":
        initiator_succeeds = not any(target_success.values())
        detail = {"target_success": target_success}
    else:
        initiator_req = db.get(PlayerRollRequest, contest.initiator_roll_request_id)
        if initiator_req is None or initiator_req.status != "fulfilled":
            _inc("not_ready_blocks")
            raise ContestNotReady("the initiating roll is not available yet")
        initiator_fulfillment = get_fulfillment(db, initiator_req.id)
        if initiator_fulfillment is None:
            _inc("not_ready_blocks")
            raise ContestNotReady("the initiating roll is not available yet")
        initiator_resolution = _resolve_pc_side(
            roll_id=f"249:{contest.id}:initiator",
            req=initiator_req, fulfillment=initiator_fulfillment,
        )
        initiator_total = int(initiator_resolution.total)
        # Ties hold the status quo: the acting PC must strictly beat every
        # opposed total to succeed.
        initiator_succeeds = all(initiator_total > total for total in target_totals.values())
        detail = {"beat_all_targets": initiator_succeeds, "target_count": len(target_totals)}
    outcome = OUTCOME_INITIATOR_SUCCEEDS if initiator_succeeds else OUTCOME_TARGET_HOLDS
    return outcome, detail


def _find_event_by_operation(db: Session, campaign_id: uuid.UUID, operation_id: str):
    from models.campaigns import CampaignDomainEvent

    return db.execute(
        select(CampaignDomainEvent).where(
            CampaignDomainEvent.campaign_id == campaign_id,
            CampaignDomainEvent.operation_id == operation_id,
        )
    ).scalars().first()


def resolve_secret_contest(
    db: Session, *, contest_id: uuid.UUID, operation_id: str | None = None
) -> ResolveResult:
    """Resolve a fully-answered contested secret action exactly once.

    Blocks with :exc:`ContestNotReady` while any target input is missing —
    the AI never takes over a human PC. Applies the winning branch's
    knowledge facts with restricted visibility through one private domain
    event; when the outcome explicitly reveals the action, visibility
    expands via #211 grants plus targeted notices, never shared metadata.
    Duplicate retries replay the committed outcome.
    """
    from app.campaigns.events import commit_campaign_mutation
    from app.world.epistemics import grant_visibility_inline
    from app.world.knowledge import create_fact_inline
    from models.campaigns import Campaign, CampaignDomainEvent
    from models.dm import SecretContest

    contest = db.get(SecretContest, _coerce_uuid(contest_id, field="contest_id"))
    if contest is None:
        raise SecretContestError("secret contest not found")
    try:
        locked = db.execute(
            select(SecretContest).where(SecretContest.id == contest.id).with_for_update()
        ).scalars().first()
        if locked is not None:
            contest = locked
    except Exception:
        pass

    if contest.status == STATUS_RESOLVED:
        _inc("resolve_replays")
        event = db.get(CampaignDomainEvent, contest.resolved_event_id) if contest.resolved_event_id else None
        _structured("secret_contest_resolve_replay", contest_id=str(contest.id), outcome=contest.outcome)
        return ResolveResult(contest=contest, event=event, outcome=contest.outcome or "", revealed=bool(contest.revealed), replayed=True)
    if contest.status == STATUS_CANCELLED:
        raise ContestUnavailable("this secret contest was cancelled")

    resolve_op = (operation_id or f"249:{contest.id}:resolve")[:128]
    evidence = _load_target_evidence(db, contest)
    outcome, _detail = _compute_outcome(db, contest, evidence)
    initiator_succeeds = outcome == OUTCOME_INITIATOR_SUCCEEDS
    revealed = bool(contest.reveal_on_success if initiator_succeeds else contest.reveal_on_failure)
    branch = list(contest.success_facts if initiator_succeeds else contest.failure_facts) or []

    campaign = db.get(Campaign, contest.campaign_id)
    if campaign is None:
        raise SecretContestError("campaign not found")
    expected_revision = int(campaign.revision or 0)

    # Crash recovery: a committed resolve event without the resolved marker
    # replays instead of double-applying.
    recovered = _find_event_by_operation(db, contest.campaign_id, resolve_op)

    created_fact_ids: list[str] = []

    def _mutate(locked_campaign) -> None:
        for index, spec in enumerate(branch):
            fact, _created = create_fact_inline(
                db, locked_campaign, content=spec["content"],
                visibility=spec["visibility"], epistemic_state=spec["epistemic_state"],
                provenance={
                    "source": "secret_contest_249", "contest_id": str(contest.id),
                    "initiating_turn_id": str(contest.initiating_turn_id),
                    "outcome": outcome, "revealed": revealed,
                },
                operation_id=f"249:{contest.id}:f{index}",
            )
            created_fact_ids.append(str(fact.id))

    if recovered is not None:
        event = recovered
        created_fact_ids = list((event.payload or {}).get("fact_ids") or [])
    else:
        target_ids = [str(req.id) for req, _ in evidence]
        _, event = commit_campaign_mutation(
            db, contest.campaign_id, expected_revision,
            event_type=_EVENT_TYPE,
            payload={
                "contest_id": str(contest.id),
                "initiating_turn_id": str(contest.initiating_turn_id),
                "outcome": outcome, "revealed": revealed,
                "fact_ids": [],  # filled below; payload stays free of cause/dice
            },
            operation_id=resolve_op,
            actor_id=contest.initiator_user_id,
            visibility="private",
            provenance={
                "source": "secret_contest_249", "contest_id": str(contest.id),
                "initiating_turn_id": str(contest.initiating_turn_id),
                "initiating_attempt_id": str(contest.initiating_attempt_id) if contest.initiating_attempt_id else None,
                "target_roll_ids": target_ids,
                "initiator_roll_id": str(contest.initiator_roll_request_id) if contest.initiator_roll_request_id else None,
                "outcome": outcome, "revealed": revealed,
            },
            mutate=_mutate,
            commit=False,
            payload_builder=lambda: {
                "contest_id": str(contest.id),
                "initiating_turn_id": str(contest.initiating_turn_id),
                "outcome": outcome, "revealed": revealed,
                "fact_ids": list(created_fact_ids),
            },
        )

    if revealed:
        # Explicit expansion only: the outcome fact becomes visible to each
        # affected target through an audited grant (stored visibility stays
        # restricted), plus a targeted private notice. Never shared metadata.
        reveal_grantees = list(contest.target_user_ids or [])
    else:
        reveal_grantees = []
    # The initiating player always learns what their own secret action
    # established: branch facts are granted to the initiator explicitly.
    # Targets are added only on explicit reveal (above).
    grantees = [str(contest.initiator_user_id)] + reveal_grantees
    from models.world import WorldFact

    for fact_index, fact_id in enumerate(created_fact_ids):
        try:
            fact = db.get(WorldFact, uuid.UUID(str(fact_id)))
        except (ValueError, TypeError):
            fact = None
        if fact is None:
            continue
        for user_index, raw_user in enumerate(grantees):
            try:
                grant_visibility_inline(
                    db, campaign, target_kind="fact", target_id=fact.id,
                    grantee_user_id=uuid.UUID(str(raw_user)),
                    granted_by=contest.initiator_user_id,
                    operation_id=f"249:{contest.id}:g{fact_index}:{user_index}",
                )
            except Exception as exc:
                logger.warning("secret_contest reveal grant failed: %s", exc)

    contest.status = STATUS_RESOLVED
    contest.outcome = outcome
    contest.revealed = revealed
    contest.resolved_event_id = event.id
    contest.resolved_at = _now()
    db.add(contest)
    db.commit()
    db.refresh(contest)
    db.refresh(event)

    # Realtime: initiator's private channel always; targets' private channels
    # only when explicitly revealed. Shared channels never see this contest.
    try:
        from app.realtime.channels import live_table_channel

        initiator_channel = live_table_channel(contest.campaign_id, _initiator_thread(db, contest))
        _publish_best_effort(initiator_channel, "secret_contest.resolved", {
            "type": "secret_contest.resolved",
            "event_id": f"secret-contest-resolved:{contest.id}",
            "campaign_id": str(contest.campaign_id), "contest_id": str(contest.id),
            "outcome": outcome, "revealed": revealed,
        })
    except Exception as exc:
        logger.warning("secret_contest initiator notice dropped: %s", exc)
    if revealed:
        for raw_user in contest.target_user_ids or []:
            try:
                target_user = uuid.UUID(str(raw_user))
            except (ValueError, TypeError):
                continue
            channel = _target_channel(db, contest.campaign_id, target_user)
            db.commit()
            if channel is not None:
                _publish_best_effort(channel, "secret_contest.observed", {
                    "type": "secret_contest.observed",
                    "event_id": f"secret-contest-observed:{contest.id}:{target_user}",
                    "campaign_id": str(contest.campaign_id), "contest_id": str(contest.id),
                    "outcome": outcome,
                })

    try:
        first = evidence[0][0]
        blocked_ms = None
        if first.requested_at:
            fulfilled_at = [f.submitted_at for _, f in evidence if f.submitted_at]
            if fulfilled_at:
                blocked_ms = int((max(fulfilled_at) - first.requested_at).total_seconds() * 1000)
    except Exception:
        blocked_ms = None
    _inc("contests_resolved")
    _structured(
        "secret_contest_resolved", contest_id=str(contest.id), outcome=outcome,
        revealed=revealed, target_count=len(evidence),
        fact_count=len(created_fact_ids),
        blocked_on_human_ms=blocked_ms,
        replayed=recovered is not None,
    )
    return ResolveResult(contest=contest, event=event, outcome=outcome, revealed=revealed, replayed=recovered is not None)


def _initiator_thread(db: Session, contest) -> str:
    from models.dm import DmTurn

    turn = db.get(DmTurn, contest.initiating_turn_id)
    if turn is None:
        raise SecretContestError("initiating turn not found")
    return str(turn.thread_id)


def cancel_secret_contest(db: Session, *, contest_id: uuid.UUID) -> Any:
    """Cancel a pending contest via the DM availability path.

    Cancellation is the explicit escape hatch when target input will never
    arrive (the normal skip/availability policy surface). It never resolves
    or applies the contest, and the AI never fulfills the missing rolls.
    """
    from models.dm import SecretContest

    contest = db.get(SecretContest, _coerce_uuid(contest_id, field="contest_id"))
    if contest is None:
        raise SecretContestError("secret contest not found")
    if contest.status == STATUS_RESOLVED:
        raise SecretContestError("secret contest already resolved")
    contest.status = STATUS_CANCELLED
    db.add(contest)
    db.commit()
    db.refresh(contest)
    _structured("secret_contest_cancelled", contest_id=str(contest.id))
    return contest


# ── Authorized projections ────────────────────────────────────────────────


def project_contest_for_viewer(
    db: Session, contest_id: uuid.UUID, viewer_id: uuid.UUID
) -> dict[str, Any] | None:
    """Audience-safe contest projection, or None when unauthorized.

    - Initiator: lifecycle + own outcome category. Never target dice,
      totals, DC, or opposed state beyond the category.
    - Target: own pending roll (safe reason + mechanics) and, only when the
      outcome explicitly revealed the action, the observable outcome. Never
      hidden cause, initiator identity, DC, or other PCs' rolls.
    - Anyone else: None (fail closed; logged with IDs only).
    """
    from models.dm import PlayerRollRequest, SecretContest

    contest = db.get(SecretContest, _coerce_uuid(contest_id, field="contest_id"))
    viewer = _coerce_uuid(viewer_id, field="viewer_id")
    if contest is None:
        return None
    if viewer == contest.initiator_user_id:
        status = get_contest_status(db, contest.id)
        return {
            "contest_id": str(contest.id), "role": "initiator",
            "status": contest.status,
            "outcome": contest.outcome if contest.status == STATUS_RESOLVED else None,
            "revealed": bool(contest.revealed),
            "target_count": len(contest.target_user_ids or []),
            "pending_target_roll_ids": status["pending_target_roll_ids"],
        }
    target_ids = {str(u).lower() for u in (contest.target_user_ids or [])}
    if str(viewer).lower() in target_ids:
        mine = []
        for raw in contest.target_roll_request_ids or []:
            try:
                row = db.get(PlayerRollRequest, uuid.UUID(str(raw)))
            except (ValueError, TypeError):
                row = None
            if row is not None and row.requested_user_id == viewer:
                entry = row.to_dict()
                entry["reason_public"] = row.reason_public
                mine.append(entry)
        observable = None
        if contest.status == STATUS_RESOLVED and contest.revealed:
            observable = contest.outcome
        state = "observed" if observable else (
            "pending_input" if any(r.get("status") == "pending" for r in mine) else "recorded")
        return {
            "contest_id": str(contest.id), "role": "target",
            "status": state,
            "my_rolls": mine,
            "observable_outcome": observable,
        }
    _inc("unauthorized_projections")
    _structured(
        "secret_contest_projection_denied", level=logging.WARNING,
        contest_id=str(contest.id),
    )
    return None
