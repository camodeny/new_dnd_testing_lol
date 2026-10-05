"""Post-turn canon promotion of DM-proposed facts and relations — issue #468.

The DM model never writes ``confirmed`` itself: a world truth it narrates is
committed as a ``claimed`` fact/relation with ``propose_confirmed``. After
the turn commits, this stage decides whether each proposal becomes canon.

Authority split (under #379; the AI is the only DM):

- Code finds the exact committed row (the staged effect's durable key),
  requires it to still be the active ``claimed`` version, gathers the
  evidence (the turn's own labelled claims plus active confirmed canon
  sharing its entities), and performs the write: a new ``confirmed``
  version superseding the claim, history preserved.
- One bounded decision per proposal judges only the semantic residue:
  was it narrated as objective world truth (not merely what a character
  said or believed), and is it consistent with the supplied canon? Only
  ``ESTABLISHED`` promotes. Contradictions, mere character claims,
  uncertainty, and decision failures all leave the row ``claimed`` — safe
  by default, and recorded with a reason.

Runs inside the post-turn range transaction (flush only), after
materialization and before consistency verification, so incidents see the
promoted canon. Promotion writes are idempotent per row.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.decisions import (
    ACTIVE,
    ESCALATE,
    CandidateRecord,
    ChoiceResult,
    DecisionClassPolicy,
    DecisionFrame,
    DecisionService,
    build_frame,
    build_record,
    evaluate_execution,
    record_fail_soft,
    register_policy,
    to_decision_request,
)
from app.observability.tracing import structured_log
from app.world.facts import list_facts, list_relations, supersede_fact, supersede_relation
from models.campaigns import Campaign, CampaignDomainEvent
from models.world import WorldEntity, WorldFact

logger = logging.getLogger(__name__)

ESTABLISHED = "ESTABLISHED"
NOT_ESTABLISHED = "NOT_ESTABLISHED"
CONTRADICTS_CANON = "CONTRADICTS_CANON"
UNCERTAIN = "UNCERTAIN"

PROMOTION_DECISION_CLASS = "post_turn_canon_promotion"
PROMOTION_QUESTION_ID = "verify_canon_promotion"
PROMOTION_FRAME_INSTRUCTIONS = (
    "Decide whether the proposed record should become confirmed campaign canon. "
    "ESTABLISHED requires the turn to present it as objective world truth (a "
    "world_fact or observation, not only something a character said, claimed, "
    "or believed, and not from a deceptive or mistaken beat) AND that it does "
    "not conflict with any supplied confirmed canon. Select CONTRADICTS_CANON "
    "when it conflicts with supplied canon, NOT_ESTABLISHED when the turn only "
    "shows it as a character's claim or belief, and UNCERTAIN when the "
    "evidence is insufficient. Never invent facts outside the supplied evidence."
)
PROMOTION_FRAME_SCHEMA_VERSION = 1

_PROPOSING_EFFECTS = frozenset({"assert_fact", "upsert_relation"})
_MAX_TURN_CLAIMS = 40
_MAX_CANON = 24

register_policy(DecisionClassPolicy(
    decision_class=PROMOTION_DECISION_CLASS,
    min_probability_direct=.85, min_confidence_direct=.8,
    min_margin_direct=.25, near_tie_margin=.25,
    max_risk_for_direct="standard", allow_direct_when_irreversible=False,
))


# ── Evidence (code-owned) ──────────────────────────────────────────────────

def _entity_label(db: Session, entity_id: Any) -> str:
    entity = db.get(WorldEntity, entity_id) if entity_id is not None else None
    return entity.name if entity is not None else str(entity_id)


def _record_text(db: Session, row: Any) -> str:
    if isinstance(row, WorldFact):
        return row.content
    obj = (_entity_label(db, row.object_entity_id)
           if row.object_entity_id is not None else (row.object_label or ""))
    return f"{_entity_label(db, row.subject_entity_id)} {row.relation_type} {obj}".strip()


def _record_entities(row: Any) -> list[Any]:
    if isinstance(row, WorldFact):
        return list(row.entity_refs or [])
    return [e for e in (row.subject_entity_id, row.object_entity_id) if e is not None]


def _turn_claims(attempt: Any) -> list[dict[str, Any]]:
    """The committed turn's own claims, with the labels the DM gave them."""
    claims: list[dict[str, Any]] = []
    for beat in (attempt.contract_snapshot or {}).get("beats") or []:
        if not isinstance(beat, dict):
            continue
        for claim in beat.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            claims.append({
                "beat_type": beat.get("type"),
                "truth_status": beat.get("truth_status"),
                "claim_kind": claim.get("claim_kind"),
                "text": str(claim.get("text") or "")[:400],
            })
            if len(claims) >= _MAX_TURN_CLAIMS:
                return claims
    return claims


def _related_canon(db: Session, campaign_id: Any, row: Any) -> list[dict[str, Any]]:
    """Active confirmed canon sharing any entity with the proposal."""
    seen: set[Any] = set()
    canon: list[dict[str, Any]] = []
    for entity_id in _record_entities(row):
        for related in (
            *list_facts(db, campaign_id, entity_id=entity_id,
                        epistemic_state="confirmed", limit=_MAX_CANON),
            *list_relations(db, campaign_id, entity_id=entity_id,
                            epistemic_state="confirmed", limit=_MAX_CANON),
        ):
            if related.id in seen or related.id == row.id:
                continue
            seen.add(related.id)
            canon.append({"record_ref": str(related.id), "text": _record_text(db, related)[:600]})
            if len(canon) >= _MAX_CANON:
                return canon
    return canon


def build_promotion_frame(db: Session, campaign_id: Any, row: Any, attempt: Any) -> DecisionFrame:
    """Bounded frame over one proposal, its turn's claims, and related canon."""
    kind = "fact" if isinstance(row, WorldFact) else "relation"
    records = (
        CandidateRecord(id=ESTABLISHED,
                        label="Narrated as world truth and consistent with canon",
                        source="post-turn:canon_promotion"),
        CandidateRecord(id=NOT_ESTABLISHED,
                        label="Only a character's claim or belief in this turn",
                        source="post-turn:canon_promotion"),
        CandidateRecord(id=CONTRADICTS_CANON,
                        label="Conflicts with confirmed canon",
                        source="post-turn:canon_promotion"),
        CandidateRecord(id=UNCERTAIN,
                        label="Evidence is insufficient; leave it claimed",
                        source="post-turn:canon_promotion", risk="low"),
    )
    return build_frame(
        decision_class=PROMOTION_DECISION_CLASS,
        question_id=PROMOTION_QUESTION_ID,
        instructions=PROMOTION_FRAME_INSTRUCTIONS,
        state={
            "frame_schema": PROMOTION_FRAME_SCHEMA_VERSION,
            "proposal": {"kind": kind, "record_ref": str(row.id),
                         "text": _record_text(db, row)[:2000]},
            "turn_claims": _turn_claims(attempt),
            "confirmed_canon": _related_canon(db, campaign_id, row),
        },
        state_revision=f"canon-promotion:{row.id}",
        candidates=records,
        include_escapes=False,
    )


# ── Judgment (bounded) ─────────────────────────────────────────────────────

def decide_promotion(frame: DecisionFrame, service: DecisionService,
                     *, session_factory: Any = None) -> tuple[str, str | None]:
    """One bounded judgment; failures and policy escalation resolve to UNCERTAIN."""
    try:
        response = service.decide(to_decision_request(frame))
    except Exception as exc:
        logger.warning("canon promotion judgment failed error=%s", exc)
        return UNCERTAIN, f"{type(exc).__name__}: {exc}"[:500]
    result = response.results.get(frame.question_id)
    if not isinstance(result, ChoiceResult):
        return UNCERTAIN, "malformed: missing choice result"
    try:
        if result.selected_id not in {c.id for c in frame.candidates}:
            raise ValueError(f"unknown candidate {result.selected_id!r}")
        verdict = evaluate_execution(
            frame, result.selected_id, dict(result.probabilities),
            result.confidence, verified=True)
    except Exception as exc:
        logger.warning("canon promotion policy failed error=%s", exc)
        return UNCERTAIN, f"{type(exc).__name__}: {exc}"[:500]
    record_fail_soft(session_factory, build_record(
        frame, result, verdict, provider=response.provider,
        model=response.model or "unknown", mode=ACTIVE,
        trace_id=response.trace_id, operation_id=response.operation_id,
        campaign_id=None, latency_ms=response.latency_ms, verified=True))
    if verdict.directive == ESCALATE:
        return UNCERTAIN, "policy_escalated"
    return result.selected_id, None


# ── Entry point ────────────────────────────────────────────────────────────

def promote_proposed_canon(
    db: Session,
    campaign: Campaign,
    events: list[CampaignDomainEvent],
    *,
    decision_service: DecisionService | None = None,
    operation_id: str | None = None,
    session_factory: Any = None,
) -> dict[str, Any]:
    """Judge every ``propose_confirmed`` record committed in this range."""
    from app.dm.effects import committed_world_record
    from app.post_turn.materialize import TURN_EVENT_TYPES, _coerce_uuid_or_none
    from models.dm import DmTurnAttempt

    service = decision_service or DecisionService()
    tally = {ESTABLISHED: 0, NOT_ESTABLISHED: 0, CONTRADICTS_CANON: 0,
             UNCERTAIN: 0, "skipped": 0}
    outcomes: list[dict[str, Any]] = []
    for event in events:
        if event.event_type not in TURN_EVENT_TYPES:
            continue
        attempt_id = _coerce_uuid_or_none((event.payload or {}).get("attempt_id"))
        attempt = db.get(DmTurnAttempt, attempt_id) if attempt_id else None
        if attempt is None or attempt.campaign_id != campaign.id:
            continue
        for effect in attempt.staged_effects or []:
            if (not isinstance(effect, dict)
                    or effect.get("effect_type") not in _PROPOSING_EFFECTS
                    or not (effect.get("arguments") or {}).get("propose_confirmed")):
                continue
            outcome: dict[str, Any] = {"effect_id": effect.get("id"),
                                       "source_sequence": event.sequence}
            outcomes.append(outcome)
            row = committed_world_record(db, campaign.id, attempt, effect)
            # A later turn may already have superseded the claim; that
            # version carries (or drops) its own proposal.
            if row is None or row.status != "active" or row.epistemic_state != "claimed":
                tally["skipped"] += 1
                outcome.update(outcome="skipped", reason="not_active_claim")
                continue
            outcome.update(record_ref=str(row.id),
                           record_kind="fact" if isinstance(row, WorldFact) else "relation")
            frame = build_promotion_frame(db, campaign.id, row, attempt)
            selected, failure = decide_promotion(frame, service, session_factory=session_factory)
            outcome.update(outcome=selected, failure=failure)
            if selected != ESTABLISHED:
                tally[selected] += 1
                continue
            supersede = supersede_fact if isinstance(row, WorldFact) else supersede_relation
            try:
                promoted, _created = supersede(
                    db, campaign, row.id, epistemic_state="confirmed",
                    provenance={"canon_promotion": {
                        "promoted_from": str(row.id), "source_sequence": event.sequence}},
                    operation_id=operation_id,
                    idempotency_key=f"canon-promote:{row.id}",
                )
            except ValueError as exc:
                # Writer refusal (e.g. a referenced entity is gone): the
                # claim stays claimed; it must not wedge the checkpoint.
                outcome.update(outcome="skipped", reason=f"writer_refused: {exc}"[:300])
                tally["skipped"] += 1
                continue
            tally[ESTABLISHED] += 1
            outcome["promoted_ref"] = str(promoted.id)
    structured_log(
        logger, logging.INFO, "post_turn_canon_promotion",
        campaign_id=str(campaign.id), proposals=len(outcomes),
        promoted=tally[ESTABLISHED], operation_id=operation_id,
    )
    return {"proposals": len(outcomes), "tally": tally, "outcomes": outcomes}
