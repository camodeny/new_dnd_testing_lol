"""Pre-narration validator pipeline — issue #205.

Model proposes; runtime verifies. Validators run after contract normalization but
before the first visible narration chunk. Failures are machine-readable and drive
bounded automatic regeneration.

Ordered pipeline is explicit and observable; adding later rules/combat/content-
boundary validators does not require rewriting orchestration.

Guarantees:
- Validator execution errors fail closed (attempt closed, not skipped).
- Repeated invalid attempts surface a generic terminal/retriable DM failure.
- Visibility checks inspect semantic claims/effects, not merely message flags.
- Per-validator latency, pass/fail, rejection category, regeneration count recorded.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, Protocol

from pydantic import Field

from app.dm.context import ForwardDmContextPacket, LaneName
from app.dm.contract import (
    NON_NARRATED_MODES, Claim, ContractValidationError, DmTurnContractV1, has_audience_visible_content,
)
from app.observability.tracing import structured_log
from app.schema import StrictModel

logger = logging.getLogger(__name__)


# ── Violation / result models ────────────────────────────────────────────────


class ValidationViolation(StrictModel):
    validator: str
    category: str
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    claim_index: tuple[int, int] | None = None  # (beat_idx, claim_idx)
    entity_ref: str | None = None


class ValidatorResult(StrictModel):
    validator: str
    category: str
    passed: bool
    violations: list[ValidationViolation] = Field(default_factory=list)
    latency_ms: float = Field(ge=0)


class ValidationReport(StrictModel):
    passed: bool
    violations: list[ValidationViolation]
    results: list[ValidatorResult]
    regeneration_index: int = Field(ge=0)
    total_latency_ms: float = Field(ge=0)
    correlation_id: str


class ValidatorError(RuntimeError):
    code = "validator_execution_failed"


class ValidatorRejectionError(ValueError):
    """Structured rejection before streaming — drives bounded retry."""

    def __init__(self, message: str, report: ValidationReport):
        super().__init__(message)
        self.report = report
        self.code = "validator_rejection"
        # legacy alias for callers that inspect .details
        self.details = {
            "violations": [v.model_dump(mode="json") for v in report.violations],
            "correlation_id": report.correlation_id,
            "regeneration_index": report.regeneration_index,
        }


# ── Protocol ─────────────────────────────────────────────────────────────────

class Validator(Protocol):
    name: str
    category: str

    def validate(
        self,
        contract: DmTurnContractV1,
        packet: ForwardDmContextPacket | None,
    ) -> ValidatorResult: ...


# ── Helpers ──────────────────────────────────────────────────────────────────

def _all_claims(contract: DmTurnContractV1) -> list[tuple[int, int, Claim]]:
    out: list[tuple[int, int, Claim]] = []
    for bi, beat in enumerate(contract.beats):
        for ci, claim in enumerate(beat.claims):
            out.append((bi, ci, claim))
    return out


def _norm_id(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _extract_pc_ownership(packet: ForwardDmContextPacket | None) -> dict[str, str]:
    if packet is None:
        return {}
    lane = next((lane for lane in packet.lanes if lane.name == LaneName.PROTECTED_PCS), None)
    if lane is None:
        return {}
    out: dict[str, str] = {}
    for rec in lane.records:
        cid = rec.value.get("character_id")
        owner = rec.value.get("owner_user_id")
        if cid and owner:
            out[str(cid)] = str(owner)
    return out


def _extract_submission_map(
    packet: ForwardDmContextPacket | None,
) -> dict[str, dict[str, str]]:
    """submission_id -> {user_id, character_id}"""
    if packet is None:
        return {}
    lane = next((lane for lane in packet.lanes if lane.name == LaneName.PLAYER_INPUTS), None)
    if lane is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    for rec in lane.records:
        sid = rec.value.get("submission_id")
        if sid:
            out[str(sid)] = {
                "user_id": str(rec.value.get("user_id") or ""),
                "character_id": str(rec.value.get("character_id") or ""),
            }
    return out


def _known_ids_from_packet(packet: ForwardDmContextPacket | None) -> set[str]:
    """All source IDs for provenance (submission/event/character)."""
    if packet is None:
        return set()
    ids: set[str] = set()
    for lane in packet.lanes:
        for rec in lane.records:
            v = rec.value
            for key in ("character_id", "submission_id", "entity_id", "event_id"):
                if key in v and v[key]:
                    ids.add(str(v[key]))
            if rec.record_id.startswith("pc-control:"):
                ids.add(rec.record_id.split(":", 1)[1])
            if rec.record_id.startswith("submission:"):
                ids.add(rec.record_id.split(":", 1)[1])
    return ids


def _known_entities_map_from_packet(packet: ForwardDmContextPacket | None) -> dict[str, str | None]:
    """Typed allowlist for identity validation — never mixes submission/event IDs.

    Returns dict full_id_lower -> expected_type (or None for bare).
    """
    if packet is None:
        return {}
    out: dict[str, str | None] = {}
    def _norm_full(fid: str) -> str:
        s = fid.strip().lower()
        if s.startswith("char:"):
            return "character:" + s[5:]
        return s

    for lane in packet.lanes:
        if lane.name == LaneName.PROTECTED_PCS:
            for rec in lane.records:
                cid = rec.value.get("character_id")
                if cid:
                    full = str(cid).strip().lower()
                    norm_full = _norm_full(full)
                    if ":" in full:
                        prefix = full.split(":", 1)[0].strip().lower()
                        if prefix in ("char", "character"):
                            out[norm_full] = "character"
                        elif prefix in ("npc", "location", "object", "entity"):
                            out[norm_full] = prefix if prefix != "char" else "character"
                        else:
                            out[norm_full] = "character"
                    else:
                        out[norm_full] = "character"
        elif lane.name in (
            LaneName.RELEVANT_CANON, LaneName.CURRENT_SCENE,
            LaneName.REPAIR_DIRECTIVES,
        ):
            for rec in lane.records:
                v = rec.value
                if isinstance(v, dict):
                    # The scene reader supplies its canonical location as a
                    # foreign key, not as a top-level entity record. Never
                    # treat campaign IDs or names as entity authority.
                    if lane.name == LaneName.CURRENT_SCENE and v.get("location_entity_id"):
                        out[str(v["location_entity_id"]).strip().lower()] = "location"
                    # Present actors carrying a durable entity_id (#459) are
                    # identity authority too: the scene is a required lane,
                    # while the entity registry is optional and is the first
                    # record budget trimming drops. Without this the model
                    # reuses an NPC id the scene shows it and is refused.
                    if lane.name == LaneName.CURRENT_SCENE:
                        for actor in v.get("present_actors") or []:
                            if isinstance(actor, dict) and actor.get("entity_id") and actor.get("kind") == "npc":
                                out[str(actor["entity_id"]).strip().lower()] = "npc"
                        # Combatants in the active encounter (#236) are identity
                        # authority for the attacks the DM rolls for NPCs.
                        encounter = v.get("active_encounter")
                        for entry in (encounter or {}).get("turn_order") or []:
                            ref = entry.get("ref") if isinstance(entry, dict) else None
                            if isinstance(ref, dict) and ref.get("type") == "npc" and ref.get("id"):
                                out.setdefault(str(ref["id"]).strip().lower(), "npc")
                    # A code-owned identity repair can name a canonical
                    # entity even when the optional registry was budgeted
                    # out. Its required record is identity authority for the
                    # re-adjudicated contract.
                    canonical = v.get("canonical_entity")
                    if lane.name == LaneName.REPAIR_DIRECTIVES and isinstance(canonical, dict):
                        cid, kind = canonical.get("id"), canonical.get("kind")
                        if cid and kind:
                            out[str(cid).strip().lower()] = str(kind).strip().lower()
                    for k in ("entity_type", "type"):
                        t = v.get(k)
                        eid = v.get("entity_id") or v.get("id")
                        if t and eid:
                            full = str(eid).strip().lower()
                            out[full] = str(t).strip().lower()
                    # Complete entity registry record: nested id/name/kind
                    # list the adjudicator references by exact canonical ID.
                    # Without this, registry IDs the DM correctly reuses
                    # would fail as unknown_canonical_id.
                    nested = v.get("entities")
                    if isinstance(nested, list):
                        for item in nested:
                            if not isinstance(item, dict):
                                continue
                            nid = item.get("id")
                            nkind = item.get("kind")
                            if nid:
                                out[str(nid).strip().lower()] = (
                                    str(nkind).strip().lower() if nkind else None
                                )
                    if rec.record_id.startswith("entity:"):
                        parts = rec.record_id.split(":")
                        if len(parts) >= 3:
                            full = ":".join(parts[2:]).lower()
                            out[full] = parts[1].lower()
    return out


# ── Concrete validators ──────────────────────────────────────────────────────

class AgencyValidator:
    """Reject voluntary PC actions not declared by controlling player.

    Only structured adjudication semantics distinguish involuntary consequences:
    ``roll_outcome``/``roll_request_id`` or ``roll_adjudication``/``resolver_evidence``
    with an explicit provenance link (evidence/trigger refs). Lexical keyword
    matching is intentionally not used — otherwise ``moved``/``hit``/``healed``
    would let voluntary actions through.
    """

    name = "agency_validator"
    category = "agency"

    def _is_involuntary(self, claim: Claim) -> bool:
        # Only dice-constrained outcomes are allowed to be character-authored
        # without a player_declaration. All other imposed consequences must be
        # modeled with an external actor (or no actor) and the PC as target.
        if claim.claim_kind == "roll_outcome" or claim.roll_request_id is not None:
            return True
        return False

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        for bi, ci, claim in _all_claims(contract):
            if claim.actor_ref is None or claim.actor_ref.type != "character":
                continue
            if self._is_involuntary(claim):
                continue
            # Only player_declaration with player_transcript origin is voluntary PC agency
            if claim.claim_kind != "player_declaration" or claim.origin != "player_transcript":
                violations.append(
                    ValidationViolation(
                        validator=self.name,
                        category=self.category,
                        code="voluntary_pc_action_without_player_declaration",
                        message=f"Character {claim.actor_ref.id!r} action must be player_declaration with origin=player_transcript, got kind={claim.claim_kind!r} origin={claim.origin!r}",
                        details={"beat": bi, "claim": ci, "actor": str(claim.actor_ref.id), "claim_kind": claim.claim_kind, "origin": claim.origin},
                        claim_index=(bi, ci),
                        entity_ref=str(claim.actor_ref.id),
                    )
                )
            elif not claim.evidence_refs and not claim.trigger_refs:
                # player_declaration should cite its source submission(s) via evidence/trigger refs
                # warn but not fail if packet not available — soft check
                pass
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class OwnershipValidator:
    """One player cannot author another player's PC actions."""

    name = "ownership_validator"
    category = "ownership"

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        pc_owner = _extract_pc_ownership(packet)
        submission_map = _extract_submission_map(packet)

        # Build set of submitting users per character from packet
        # If packet missing, fall back to empty (cannot validate ownership, fail open for that case)
        if not pc_owner:
            latency = (time.monotonic() - t0) * 1000
            return ValidatorResult(validator=self.name, category=self.category, passed=True, violations=[], latency_ms=latency)

        for bi, ci, claim in _all_claims(contract):
            if claim.claim_kind != "player_declaration" or claim.actor_ref is None:
                continue
            char_id = str(claim.actor_ref.id)
            owner = pc_owner.get(char_id)
            if owner is None:
                # unknown character — handled by entity validator, not ownership
                continue
            # Determine if any submission for this character is from owner
            # If contract provides adjudication_input, use its submission_ids
            declared_via_input = False
            if contract.adjudication_input and contract.adjudication_input.submission_ids:
                for sid in contract.adjudication_input.submission_ids:
                    info = submission_map.get(str(sid))
                    if info and info.get("character_id") == char_id and info.get("user_id") == owner:
                        declared_via_input = True
                        break
                    # also check if submission's user matches owner even without character_id filter
                    if info and info.get("user_id") == owner:
                        # ambiguous but allow if any owner submission present
                        declared_via_input = True
                        break
                if not declared_via_input:
                    violations.append(
                        ValidationViolation(
                            validator=self.name,
                            category=self.category,
                            code="pc_action_by_non_owner",
                            message=f"Player declaration for {char_id!r} not from owning user {owner!r}",
                            details={"beat": bi, "claim": ci, "character_id": char_id, "owner": owner, "submission_ids": contract.adjudication_input.submission_ids},
                            claim_index=(bi, ci),
                            entity_ref=char_id,
                        )
                    )
            else:
                # No adjudication_input — check that at least one packet submission for that character belongs to owner
                has_owner_submission = any(
                    info.get("character_id") == char_id and info.get("user_id") == owner for info in submission_map.values()
                )
                # If no submission map at all, cannot validate — skip
                if submission_map and not has_owner_submission:
                    violations.append(
                        ValidationViolation(
                            validator=self.name,
                            category=self.category,
                            code="pc_action_without_submission",
                            message=f"No submission from owner {owner!r} for character {char_id!r}",
                            details={"beat": bi, "claim": ci, "character_id": char_id, "owner": owner},
                            claim_index=(bi, ci),
                            entity_ref=char_id,
                        )
                    )

            # Check that a declaration's evidence_refs don't point to another player's submission
            if claim.evidence_refs and submission_map:
                for ref in claim.evidence_refs:
                    info = submission_map.get(str(ref))
                    if info and info.get("user_id") and info.get("user_id") != owner:
                        violations.append(
                            ValidationViolation(
                                validator=self.name,
                                category=self.category,
                                code="pc_action_evidence_from_other_player",
                                message=f"Declaration for {char_id!r} cites submission {ref!r} from non-owner {info.get('user_id')!r}",
                                details={"beat": bi, "claim": ci, "character_id": char_id, "ref": ref, "ref_user": info.get("user_id"), "owner": owner},
                                claim_index=(bi, ci),
                                entity_ref=char_id,
                            )
                        )
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class EntityValidator:
    """Reject unknown/invented canonical IDs unless declared as valid new entities."""

    name = "entity_validator"
    category = "identity"

    def _normalize_type(self, t: str | None) -> str | None:
        if t is None:
            return None
        s = str(t).strip().lower()
        if s in ("char", "character"):
            return "character"
        if s in ("npc", "location", "object", "entity"):
            return s
        return s

    def _normalize_full_id(self, fid: str) -> str:
        s = fid.strip().lower()
        if s.startswith("char:"):
            return "character:" + s[5:]
        return s

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []

        known_map: dict[str, str | None] = {}
        # packet-derived typed entities (never includes submission/event IDs)
        known_map.update({self._normalize_full_id(k): v for k, v in _known_entities_map_from_packet(packet).items()})

        temp_ids = {e.temp_id for e in contract.new_entities}
        # check duplicate temp ids
        if len(temp_ids) != len(contract.new_entities):
            violations.append(
                ValidationViolation(
                    validator=self.name, category=self.category, code="duplicate_temp_id",
                    message="Duplicate temp_id in new_entities", details={"temp_ids": [e.temp_id for e in contract.new_entities]}
                )
            )

        # collect all EntityRefs
        refs: list[tuple[str, Any]] = []  # (location, ref)
        for bi, beat in enumerate(contract.beats):
            if beat.speaker_ref:
                refs.append((f"beat[{bi}].speaker_ref", beat.speaker_ref))
            for ci, claim in enumerate(beat.claims):
                if claim.actor_ref:
                    refs.append((f"beat[{bi}].claim[{ci}].actor_ref", claim.actor_ref))
                if claim.location_ref:
                    refs.append((f"beat[{bi}].claim[{ci}].location_ref", claim.location_ref))
                for idx, r in enumerate(claim.target_refs):
                    refs.append((f"beat[{bi}].claim[{ci}].target_refs[{idx}]", r))
                for idx, r in enumerate(claim.topic_refs):
                    refs.append((f"beat[{bi}].claim[{ci}].topic_refs[{idx}]", r))
        for ne in contract.new_entities:
            if ne.location_ref:
                refs.append((f"new_entity[{ne.temp_id}].location_ref", ne.location_ref))

        for loc, ref in refs:
            if hasattr(ref, "id"):
                rid = str(ref.id)
                rtype = str(getattr(ref, "type", "") or "").strip().lower()
                rtype = self._normalize_type(rtype) or rtype
            else:
                rid = str(ref.get("id") if isinstance(ref, dict) else ref)
                rtype = str(ref.get("type") if isinstance(ref, dict) and ref.get("type") else "").strip().lower()
                rtype = self._normalize_type(rtype) or rtype
            rid_norm = rid.strip().lower()
            norm_rid = self._normalize_full_id(rid_norm)
            if rid in temp_ids:
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category, code="temp_id_used_as_canonical",
                        message=f"Temporary id {rid!r} used as canonical EntityRef at {loc}; EntityRef.id must not be a temp id",
                        details={"location": loc, "id": rid}, entity_ref=rid
                    )
                )
            elif norm_rid in known_map:
                expected = known_map[norm_rid]
                if expected is None or expected == rtype:
                    continue
                # typed mismatch — e.g. character:123 used as npc
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category, code="unknown_canonical_id",
                        message=f"Type mismatch for canonical {rtype or 'entity'}:{rid!r} at {loc}: expected type {expected!r}",
                        details={"location": loc, "id": rid, "type": rtype, "expected_type": expected}, entity_ref=rid
                    )
                )
            elif rid_norm in known_map:
                # bare fallback (unnormalized) — only for bare ids
                expected = known_map[rid_norm]
                if expected is None or expected == rtype:
                    continue
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category, code="unknown_canonical_id",
                        message=f"Type mismatch for canonical {rtype or 'entity'}:{rid!r} at {loc}: expected type {expected!r}",
                        details={"location": loc, "id": rid, "type": rtype, "expected_type": expected}, entity_ref=rid
                    )
                )
            else:
                # No match — fail closed if references exist but authority is missing
                if not known_map:
                    violations.append(
                        ValidationViolation(
                            validator=self.name, category=self.category, code="missing_identity_authority",
                            message=f"No authoritative identity allowlist available for {rtype or 'entity'}:{rid!r} at {loc}; cannot verify canonical identity",
                            details={"location": loc, "id": rid, "type": rtype}, entity_ref=rid
                        )
                    )
                else:
                    violations.append(
                        ValidationViolation(
                            validator=self.name, category=self.category, code="unknown_canonical_id",
                            message=f"Unknown canonical {rtype or 'entity'}:{rid!r} at {loc} not in authoritative context and not a declared new entity",
                            details={"location": loc, "id": rid, "type": rtype}, entity_ref=rid
                        )
                    )

        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class ProvenanceValidator:
    """Validate provenance/source-ref semantics."""

    name = "provenance_validator"
    category = "provenance"

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        # Build known source ids from packet (submission ids, event ids, evidence ids)
        known_sources: set[str] = set()
        if packet is not None:
            known_sources |= _known_ids_from_packet(packet)
            for lane in packet.lanes:
                for rec in lane.records:
                    known_sources.add(rec.record_id)
                    for src in rec.sources:
                        known_sources.add(src.source_id)
                        known_sources.add(f"{src.source_type}:{src.source_id}")
            # Current attempt's adjudication_input is authoritative even if not yet echoed in a lane
            if contract.adjudication_input and contract.adjudication_input.submission_ids:
                for sid in contract.adjudication_input.submission_ids:
                    s = str(sid)
                    known_sources.add(s)
                    known_sources.add(f"submission:{s}")
                    known_sources.add(f"player_submission:{s}")

        for bi, ci, claim in _all_claims(contract):
            # evidence_refs/trigger_refs must resolve to authoritative sources when a packet is present
            for ref in list(claim.evidence_refs) + list(claim.trigger_refs):
                if packet is not None and ref not in known_sources:
                    # Allow bare trigger_refs that point to the current attempt's own submission_ids
                    # (they are authoritative even if not yet in the packet's retrieval_dependencies)
                    # For all other refs, reject hallucinated source refs
                    violations.append(
                        ValidationViolation(
                            validator=self.name,
                            category=self.category,
                            code="unknown_source_ref",
                            message=f"Unknown source ref {ref!r} not in authoritative packet/evidence sources",
                            details={"beat": bi, "claim": ci, "ref": ref},
                            claim_index=(bi, ci),
                        )
                    )
            # Origin-specific provenance rules
            if claim.origin == "resolver_evidence" and not claim.evidence_refs:
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category, code="resolver_evidence_without_refs",
                        message="resolver_evidence origin requires evidence_refs",
                        details={"beat": bi, "claim": ci, "origin": claim.origin}, claim_index=(bi, ci)
                    )
                )
            if claim.origin == "established_state" and not claim.evidence_refs and not claim.trigger_refs:
                # established_state should have provenance (trigger or evidence) — soft require
                pass
            # player_declaration must have player_transcript origin (already contract-enforced) but also need trigger/evidence pointing to submission
            if claim.claim_kind == "player_declaration" and claim.origin == "player_transcript":
                if not claim.evidence_refs and not claim.trigger_refs:
                    # Require at least one ref to authoritative input
                    violations.append(
                        ValidationViolation(
                            validator=self.name, category=self.category, code="player_declaration_without_source_ref",
                            message="player_declaration requires at least one evidence_ref or trigger_ref to authoritative input",
                            details={"beat": bi, "claim": ci}, claim_index=(bi, ci)
                        )
                    )

        # New entities should have provenance if needed — check location_ref already validated
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class EpistemicValidator:
    """Prevent unsupported claims/beliefs/NPC statements from becoming objective truth."""

    name = "epistemic_validator"
    category = "epistemics"

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        # Collect player_declaration and npc_utterance texts
        declaration_texts = {c.text.strip().lower() for _, _, c in _all_claims(contract) if c.claim_kind == "player_declaration"}
        npc_texts = {c.text.strip().lower() for _, _, c in _all_claims(contract) if c.claim_kind == "npc_utterance"}

        for bi, ci, claim in _all_claims(contract):
            if claim.claim_kind in ("world_fact", "observation"):
                low = claim.text.strip().lower()
                # If world_fact duplicates a player declaration without evidence, it's promotion
                if low in declaration_texts and not claim.evidence_refs and not claim.trigger_refs:
                    violations.append(
                        ValidationViolation(
                            validator=self.name, category=self.category, code="player_claim_promoted_to_fact",
                            message="Player declaration promoted to world_fact without evidence/adjudication",
                            details={"beat": bi, "claim": ci, "text": claim.text}, claim_index=(bi, ci)
                        )
                    )
                if low in npc_texts and claim.claim_kind == "world_fact":
                    # NPC lie/mistake must not become world fact without adjudication evidence
                    # Check beat truth_status: if npc dialogue was deceptive/mistaken, world_fact cannot assert it truthfully
                    # Look up npc beats for truth_status
                    is_deceptive_source = False
                    for beat in contract.beats:
                        if beat.type == "npc_dialogue" and beat.truth_status in ("deceptive", "mistaken", "incomplete"):
                            for c in beat.claims:
                                if c.text.strip().lower() == low:
                                    is_deceptive_source = True
                    if is_deceptive_source and not claim.evidence_refs:
                        violations.append(
                            ValidationViolation(
                                validator=self.name, category=self.category, code="npc_utterance_promoted_to_fact",
                                message="NPC utterance (non-truthful) promoted to world_fact without evidence",
                                details={"beat": bi, "claim": ci, "text": claim.text}, claim_index=(bi, ci)
                            )
                        )
                # World fact with origin player_transcript is always wrong kind — should be player_declaration
                if claim.origin == "player_transcript":
                    violations.append(
                        ValidationViolation(
                            validator=self.name, category=self.category, code="world_fact_with_player_origin",
                            message="world_fact must not have origin=player_transcript; use player_declaration",
                            details={"beat": bi, "claim": ci, "origin": claim.origin}, claim_index=(bi, ci)
                        )
                    )

        # NPC beats with truthful but no supporting evidence are okay; deception already validated via contract

        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class VisibilityValidator:
    """Audience/visibility validation using current authorization metadata."""

    name = "visibility_validator"
    category = "visibility"

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        audience = packet.audience if packet is not None else None
        is_shared = audience is None or audience.audience == "campaign"

        for bi, ci, claim in _all_claims(contract):
            # Shared-audience output must not contain private-only facts
            if is_shared and claim.visibility in ("private", "dm_private"):
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category, code="private_fact_in_shared_audience",
                        message=f"Claim visibility {claim.visibility!r} not allowed in shared campaign audience",
                        details={"beat": bi, "claim": ci, "visibility": claim.visibility, "audience": audience.audience if audience else "campaign"},
                        claim_index=(bi, ci),
                    )
                )
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class AudienceContentValidator:
    """A narrated contract must leave its audience something to read (issue #514).

    ``public_projection`` strips every ``dm_private`` claim. A shared thread
    already refuses those claims (:class:`VisibilityValidator`), but a private
    thread allows them, so a respond turn whose every claim is DM-private
    validated, streamed zero chunks, and could never commit. Refuse it before
    narration so bounded regeneration fixes it in-process with feedback.
    """

    name = "audience_content_validator"
    category = "visibility"

    def validate(self, contract, packet) -> ValidatorResult:
        violations: list[ValidationViolation] = []
        if contract.mode not in NON_NARRATED_MODES and not has_audience_visible_content(contract):
            audience = packet.audience.audience if packet is not None else "campaign"
            violations.append(ValidationViolation(
                validator=self.name, category=self.category, code="no_audience_visible_content",
                message=(
                    "Nothing in this contract is visible to the players reading this thread: "
                    "every claim is dm_private, so they would get no reply. Claim visibility is "
                    "relative to the thread's audience: public means the players reading this "
                    "thread see it (in a private thread, only that player). A player acting "
                    "privately or secretly is what the private thread is for, not a reason for "
                    "dm_private, which is hidden truth no player sees. Mark what the player "
                    "should read as public, or use silent mode if there is genuinely nothing to say."
                ),
                details={"mode": contract.mode, "audience": audience},
            ))
        return ValidatorResult(validator=self.name, category=self.category, passed=not violations, violations=violations, latency_ms=0.0)


class KnowledgeValidator:
    """Deterministic unavailable-knowledge checks against the #251 lane (issue #251).

    Knowledge-bearing claims (NPC utterances, NPC-attributed observations
    and world facts) that reference concrete world entities must be backed
    by the speaking subject's fictional knowledge (code-supplied #211 lane
    entries) or by a matching in-turn ``transfer_knowledge`` staged effect
    for the same subject and target. Generic provenance (``evidence_refs`` /
    ``trigger_refs``) merely cites what prompted the reaction and never
    exempts on its own. No model call, no DB: the packet's
    ``knowledge_visibility`` lane plus the contract's staged effects are
    the authority.

    Fail-conservative: a contract-referenced NPC with no resolvable lane
    perspective is ambiguous state and fails closed. Non-NPC actors
    (player declarations) are out of scope — the system does not police
    player roleplay.
    """

    name = "knowledge_validator"
    category = "knowledge"

    def _perspectives(self, packet) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if packet is None:
            return out
        lane = next((lane for lane in packet.lanes if lane.name == LaneName.KNOWLEDGE_VISIBILITY), None)
        if lane is None:
            return out
        for rec in lane.records:
            value = rec.value or {}
            known: set[str] = set()
            denied: set[str] = set()
            for entry in value.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                tid = str(entry.get("target_id") or "").strip()
                if not tid:
                    continue
                if entry.get("knowledge_state") == "does_not_know":
                    denied.add(tid)
                else:
                    known.add(tid)
            perspective = {
                "known": known,
                "denied": denied,
                "resolved": bool(value.get("subject_resolved")),
            }
            character_id = str(value.get("character_id") or "").strip()
            subject_id = str(value.get("subject_entity_id") or "").strip()
            if character_id:
                out[character_id] = perspective
            if subject_id:
                out[subject_id] = perspective
        return out

    def _co_present_ids(self, packet) -> set[str]:
        """What any NPC in the scene perceives by being there.

        The current scene's location and the PCs acting this turn, the same
        co-presence baseline an NPC gets when introduced or entering the
        scene, applied at validation so NPCs from before that baseline are
        covered too. Explicit ``does_not_know`` entries still win.
        """
        out: set[str] = set()
        if packet is None:
            return out
        for lane in packet.lanes:
            if lane.name not in (LaneName.CURRENT_SCENE, LaneName.PLAYER_INPUTS):
                continue
            for rec in lane.records:
                value = rec.value if isinstance(rec.value, dict) else {}
                key = "location_entity_id" if lane.name == LaneName.CURRENT_SCENE else "character_id"
                token = _norm_id(value.get(key))
                if token:
                    out.add(token)
        return out

    # Claim kinds that can carry fictional knowledge for an NPC subject.
    _NPC_KNOWLEDGE_KINDS = frozenset({"npc_utterance", "observation", "world_fact"})

    # Staged transfer fields that can name a knowledge target.
    _TRANSFER_TARGET_FIELDS = (
        "target_fact_id", "target_relation_id", "target_entity_id", "target_id",
    )

    def _transfer_coverage(self, contract) -> dict[str, set[str]]:
        """Actor subject ID -> target IDs taught by in-turn transfers.

        Only a staged ``transfer_knowledge`` effect for the same subject and
        target counts as an explicit learning source. Generic provenance
        (``evidence_refs``/``trigger_refs``) merely cites what prompted the
        reaction — a player submission that prompts an NPC does not teach
        that NPC the referenced facts — so it never exempts on its own.
        """
        out: dict[str, set[str]] = {}
        for effect in contract.staged_effects or []:
            if getattr(effect, "effect_type", None) != "transfer_knowledge":
                continue
            args = getattr(effect, "arguments", None) or {}
            if not isinstance(args, dict):
                continue
            # A denial transfer ("this subject does not know X") is not a
            # learning source — it must never clear the claim. Omitted
            # state defaults to "knows" like the effect handler.
            if str(args.get("knowledge_state") or "knows").strip().lower() == "does_not_know":
                continue
            subject = str(args.get("subject_entity_id") or "").strip()
            targets = {
                str(args.get(field) or "").strip()
                for field in self._TRANSFER_TARGET_FIELDS
            } - {""}
            if subject and targets:
                out.setdefault(subject, set()).update(targets)
        return out

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        perspectives = self._perspectives(packet)
        transfers = self._transfer_coverage(contract)
        co_present = self._co_present_ids(packet)
        for bi, ci, claim in _all_claims(contract):
            if claim.claim_kind not in self._NPC_KNOWLEDGE_KINDS:
                continue
            if claim.actor_ref is None or getattr(claim.actor_ref, "type", None) != "npc":
                continue
            actor_id = _norm_id(claim.actor_ref.id)
            refs = [
                _norm_id(ref.id)
                for ref in (list(claim.target_refs or []) + list(claim.topic_refs or []))
                if _norm_id(ref.id)
            ]
            if claim.location_ref is not None and _norm_id(claim.location_ref.id):
                refs.append(_norm_id(claim.location_ref.id))
            if not refs:
                continue
            perspective = perspectives.get(actor_id)
            if perspective is None or not perspective["resolved"]:
                # Ambiguous state fails closed: a contract-referenced NPC
                # with no resolvable knowledge cannot be cleared.
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category,
                        code="npc_utterance_ambiguous_knowledge",
                        message="NPC-attributed claim from a subject with no resolvable knowledge",
                        details={"beat": bi, "claim": ci, "actor": actor_id, "targets": refs},
                        claim_index=(bi, ci),
                    )
                )
                continue
            taught = transfers.get(actor_id, set())
            denied_hit = next(
                (ref for ref in refs if ref in perspective["denied"] and ref not in taught),
                None,
            )
            if denied_hit is not None:
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category,
                        code="npc_utterance_denied_knowledge",
                        message="NPC utterance references a target the subject explicitly does not know",
                        details={"beat": bi, "claim": ci, "actor": actor_id, "target": denied_hit},
                        claim_index=(bi, ci),
                    )
                )
                continue
            # Every referenced target must be known or taught in-turn: one
            # uncovered target is enough for reliance on unavailable
            # knowledge. A matching staged transfer covers even a prior
            # denial — the turn itself is the learning source.
            unknown = [
                ref for ref in refs
                if ref not in perspective["known"] and ref not in taught and ref not in co_present
            ]
            if unknown:
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category,
                        code="npc_utterance_without_knowledge",
                        message="NPC utterance references targets outside the subject's knowledge with no learning source",
                        details={"beat": bi, "claim": ci, "actor": actor_id, "targets": refs, "unknown": unknown},
                        claim_index=(bi, ci),
                    )
                )
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class CanonValidator:
    """Basic current-canon contradiction checks against authoritative context.

    All records in RELEVANT_CANON / CURRENT_SCENE / RECENT_HISTORY are treated
    as authoritative. Contradiction is detected via shared subject + antonym pairs.
    """

    # Deterministic antonym pairs for packet-derived checks
    _ANTONYMS: list[tuple[str, str]] = [
        ("intact", "cracked"),
        ("intact", "broken"),
        ("alive", "dead"),
        ("open", "closed"),
        ("locked", "unlocked"),
        ("present", "missing"),
        ("standing", "prone"),
        ("awake", "asleep"),
        ("healthy", "injured"),
        ("full", "empty"),
    ]

    name = "canon_validator"
    category = "canon"

    def _extract_canon_entries(self, packet) -> list[tuple[str, list[str]]]:
        """Return list of (canonical_text, forbids_phrases)."""
        entries: list[tuple[str, list[str]]] = []
        if packet is not None:
            for lane_name in (LaneName.RELEVANT_CANON, LaneName.CURRENT_SCENE, LaneName.RECENT_HISTORY):
                lane = next((lane for lane in packet.lanes if lane.name == lane_name), None)
                if lane:
                    for rec in lane.records:
                        # value may be dict with payload/text/fact etc.
                        val = rec.value
                        texts: list[str] = []
                        if isinstance(val, dict):
                            for key in ("text", "fact", "value", "payload", "state", "summary"):
                                inner = val.get(key)
                                if isinstance(inner, str) and inner.strip():
                                    texts.append(inner.strip().lower())
                                elif isinstance(inner, dict):
                                    for sub in inner.values():
                                        if isinstance(sub, str) and sub.strip():
                                            texts.append(sub.strip().lower())
                            if not texts:
                                texts.append(str(val).lower())
                        elif isinstance(val, str):
                            texts.append(val.strip().lower())
                        for t in texts:
                            forbids: list[str] = []
                            for a, b in self._ANTONYMS:
                                if a in t:
                                    forbids.append(b)
                                if b in t:
                                    forbids.append(a)
                            entries.append((t, forbids))
        return entries

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        canon_entries = self._extract_canon_entries(packet)

        for bi, ci, claim in _all_claims(contract):
            if claim.claim_kind not in ("world_fact", "observation"):
                continue
            low = claim.text.strip().lower()
            # No evidence -> more likely to be hallucinated contradiction
            has_evidence = bool(claim.evidence_refs or claim.trigger_refs)
            for canon_text, forbids in canon_entries:
                if not canon_text:
                    continue
                # fast subject overlap: share a significant token (>3 chars) or exact entity name
                # extract tokens
                canon_tokens = {w for w in canon_text.split() if len(w) > 3}
                claim_tokens = {w for w in low.split() if len(w) > 3}
                shares_subject = bool(canon_tokens & claim_tokens) or any(tok in low for tok in canon_text.split() if len(tok) > 4)
                # check forbids — require shared subject for multi-word canon to avoid broad false positives;
                # for single-word typed constraints (e.g. value=intact) the forbid alone is sufficient
                is_single_word_canon = len(canon_text.split()) <= 1
                for forbid in forbids:
                    if forbid and forbid in low and (shares_subject or is_single_word_canon):
                        if not has_evidence:
                            violations.append(
                                ValidationViolation(
                                    validator=self.name, category=self.category, code="canon_contradiction",
                                    message=f"Claim contradicts authoritative canon {canon_text!r}: contains {forbid!r}",
                                    details={"beat": bi, "claim": ci, "canon_text": canon_text, "forbid": forbid, "text": claim.text}, claim_index=(bi, ci)
                                )
                            )

        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class RulesValidator:
    """Deterministic 2024 rules validation (#180; first enforced rule #226).

    Damage effects are code-built only: provider output must never author HP
    damage totals, because model arithmetic is not authoritative — damage is
    resolved server-side from authoritative mechanics plus supplied/runtime
    dice (see :mod:`app.rules.attacks`). Any ``apply_attack_damage`` effect
    in provider-validated output is rejected; server code stages damage via
    ``build_damage_effect`` with a server-resolved ``DamageResolution``.

    Rules-state effects (#227) are likewise code-built only: provider output
    must never author structural condition / resource / concentration /
    death-save transitions — those are resolved server-side from
    authoritative sheet/NPC state (see :mod:`app.rules.state`) and staged
    via the ``build_*_effect`` helpers.
    """

    name = "rules_validator"
    category = "mechanics"

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        for eff in getattr(contract, "staged_effects", None) or []:
            if getattr(eff, "effect_type", None) == "apply_attack_damage":
                violations.append(
                    ValidationViolation(
                        validator=self.name,
                        category=self.category,
                        code="provider_authored_damage",
                        message=(
                            f"staged effect {getattr(eff, 'id', '?')!r} authors HP damage: "
                            "damage effects are code-built only (server-resolved DamageResolution), never provider output"
                        ),
                        details={"effect_id": getattr(eff, "id", None)},
                    )
                )
            elif getattr(eff, "effect_type", None) in (
                "apply_healing",
                "apply_condition",
                "apply_resource",
                "apply_concentration",
                "apply_death_save",
            ):
                violations.append(
                    ValidationViolation(
                        validator=self.name,
                        category=self.category,
                        code="provider_authored_rules_state",
                        message=(
                            f"staged effect {getattr(eff, 'id', '?')!r} authors rules state: "
                            "condition/resource/concentration/death-save effects are code-built only (server-resolved rules state), never provider output"
                        ),
                        details={"effect_id": getattr(eff, "id", None), "effect_type": getattr(eff, "effect_type", None)},
                    )
                )
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class MechanicsValidator:
    """Mechanics intents must resolve legally against current state (#229).

    Bound to one attempt's session/campaign/turn (``attempt_pipeline``):
    legality reads authoritative sheet/NPC rows, which a packet-only
    validator cannot. A refused intent (no slot left, unknown condition,
    target without tracked HP) or stat-block pick (#478: unknown id, over
    the party's encounter budget, NPC already statted) or a supersede of
    code-owned canon (#468) becomes regeneration feedback; nothing is
    consumed before commit.
    """

    name = "mechanics_validator"
    category = "mechanics"

    def __init__(self, db, campaign, turn):
        self.db = db
        self.campaign = campaign
        self.turn = turn

    def validate(self, contract, packet) -> ValidatorResult:
        from app.dm.mechanics import (
            canon_supersede_issues,
            loot_issues,
            npc_turn_issues,
            resolve_mechanics,
            reveal_issues,
            stat_block_issues,
        )

        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        issues = (stat_block_issues(self.db, self.campaign, contract)
                  + reveal_issues(self.db, self.campaign, self.turn, contract)
                  + canon_supersede_issues(self.db, self.campaign, contract)
                  + npc_turn_issues(self.db, self.campaign, self.turn, contract)
                  + loot_issues(self.db, self.campaign, contract))
        if contract.mechanics:
            issues += resolve_mechanics(self.db, self.campaign, self.turn, contract).issues
        for issue in issues:
            violations.append(
                ValidationViolation(
                    validator=self.name,
                    category=self.category,
                    code=f"mechanic_{issue.code}",
                    message=f"{issue.intent_id!r} refused: {issue.message}",
                    details={"mechanic_id": issue.intent_id},
                )
            )
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class RollRequestValidator:
    """A turn's roll requests resolve its intent once.

    After a fulfilled roll the DM must resolve the original intent from the
    roll evidence. Requesting the same character's same ability or skill
    again in the same turn, or reusing a request key the turn already used,
    is refused as regeneration feedback instead of reaching the unique
    request-key constraint (a DB error that would retry forever).
    """

    name = "roll_request_validator"
    category = "rules"

    def __init__(self, db, turn):
        self.db = db
        self.turn = turn

    def validate(self, contract, packet) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        rr = contract.roll_request
        if contract.mode == "await_roll" and rr is not None and rr.roll_kind == "attack":
            # Damage needs a statted entity: an attack on something only
            # narrated could never land, so combat would never end.
            target = rr.target_ref
            known = _known_entities_map_from_packet(packet)
            target_id = str(target.id).strip().lower() if target is not None else ""
            if not target_id or target_id not in known:
                violations.append(ValidationViolation(
                    validator=self.name, category=self.category,
                    code="attack_target_unregistered",
                    message=(
                        "an attack roll_request needs target_ref naming a registered creature or NPC "
                        "(an entity id from the packet). If the foe is not registered yet, respond this "
                        "turn instead: decide who or what it is yourself, introduce it through "
                        "new_entities, and narrate the clash in-fiction without a roll; attacks against "
                        "it are rolled once it is registered. Never mention this to the players"
                    ),
                    details={"target": target_id or None},
                ))
            elif self.turn is not None:
                violations += self._attack_violations(contract, rr)
        if contract.mode == "await_roll" and rr is not None and rr.roll_kind == "damage" and self.turn is not None:
            violations += self._damage_violations(contract, rr)
        if contract.mode == "await_roll" and rr is not None and self.turn is not None:
            from sqlalchemy import select
            from app.dm.execution import _resolve_roll_participants
            from models.dm import PlayerRollRequest

            existing = self.db.scalars(
                select(PlayerRollRequest).where(PlayerRollRequest.turn_id == self.turn.id)
            ).all()
            if any(row.request_key == rr.request_id for row in existing):
                violations.append(ValidationViolation(
                    validator=self.name, category=self.category, code="roll_request_key_reused",
                    message=f"roll request_id {rr.request_id!r} was already used this turn; a new roll needs a new id",
                    details={"request_id": rr.request_id},
                ))
            _, character_id = _resolve_roll_participants(self.db, self.turn, contract)
            skill = " ".join(str(rr.ability_or_skill).split()).casefold()
            # Each damage roll follows its own hit (one per attack, enforced
            # by _damage_violations), and a second attack with the same weapon
            # is a new swing (Extra Attack), so neither is a re-roll.
            rolled = rr.roll_kind not in {"attack", "damage"} and next((
                row for row in existing
                if row.status == "fulfilled" and row.character_id == character_id
                and " ".join(str(row.ability_or_skill).split()).casefold() == skill
            ), None)
            if rolled:
                violations.append(ValidationViolation(
                    validator=self.name, category=self.category, code="roll_already_resolved",
                    message=(
                        f"{rr.ability_or_skill} was already rolled for this character this turn "
                        f"({rolled.label!r}); resolve the action with that roll evidence "
                        "instead of requesting another roll"
                    ),
                    details={"roll_request_id": str(rolled.id), "ability_or_skill": rolled.ability_or_skill},
                ))
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)

    def _violation(self, code: str, message: str, **details) -> ValidationViolation:
        return ValidationViolation(validator=self.name, category=self.category, code=code, message=message, details=details)

    def _attack_violations(self, contract, rr) -> list[ValidationViolation]:
        """Issue #234 — code checks the attack itself; AC is never model-authored."""
        from app.combat.attacks import AttackRollError, plan_attack
        from app.dm.execution import _resolve_roll_participants
        from models.campaigns import Campaign

        if rr.dc_private is not None:
            return [self._violation(
                "attack_dc_forbidden",
                "an attack roll_request never carries dc_private: code compares the roll against the "
                "target's armor class itself. Leave dc_private null",
            )]
        try:
            _, character_id = _resolve_roll_participants(self.db, self.turn, contract)
            plan_attack(
                self.db, self.db.get(Campaign, self.turn.campaign_id), character_id=character_id,
                target_ref=rr.target_ref, attack_name=rr.attack_name, advantage_state=rr.advantage_state,
            )
        except AttackRollError as exc:
            return [self._violation(f"attack_{exc.code}", f"attack refused: {exc}", attack_name=rr.attack_name)]
        return []

    def _damage_violations(self, contract, rr) -> list[ValidationViolation]:
        from app.combat.attacks import AttackRollError, plan_damage
        from app.dm.execution import _resolve_roll_participants

        if rr.dc_private is not None:
            return [self._violation("damage_dc_forbidden", "a damage roll_request never carries dc_private")]
        try:
            _, character_id = _resolve_roll_participants(self.db, self.turn, contract)
            plan_damage(self.db, turn_id=self.turn.id, character_id=character_id, attack_request_key=rr.attack_request_id)
        except AttackRollError as exc:
            return [self._violation(f"damage_{exc.code}", f"damage roll refused: {exc}",
                                    attack_request_id=rr.attack_request_id)]
        return []


class EvidenceResolvedValidator:
    """Once a turn's evidence loop has run, the retry must resolve the turn.

    Appended once the turn's evidence is gathered (after the single resume
    a retry may take, or when the bounded evidence loop hits its round
    limit): a further need_evidence is refused as regeneration feedback
    rather than looping, failing the turn, or committing a prelude-only turn.
    """

    name = "evidence_resolved_validator"
    category = "structure"

    def validate(self, contract, packet) -> ValidatorResult:
        violations = []
        if contract.mode == "need_evidence":
            violations.append(ValidationViolation(
                validator=self.name, category=self.category, code="evidence_already_resolved",
                message=("This turn's evidence is already resolved and in the packet; resolve the "
                         "turn now from it (respond, clarify, or a roll not yet made) instead of "
                         "requesting more evidence"),
            ))
        return ValidatorResult(validator=self.name, category=self.category, passed=not violations, violations=violations, latency_ms=0.0)


# ── Pipeline ─────────────────────────────────────────────────────────────────

DEFAULT_VALIDATORS: list[Validator] = [
    AgencyValidator(),
    OwnershipValidator(),
    EntityValidator(),
    ProvenanceValidator(),
    EpistemicValidator(),
    VisibilityValidator(),
    AudienceContentValidator(),
    KnowledgeValidator(),
    CanonValidator(),
    RulesValidator(),
]

# category ordering is the validator list order above

class ValidatorPipeline:
    def __init__(self, validators: list[Validator] | None = None):
        self.validators: list[Validator] = list(validators) if validators is not None else list(DEFAULT_VALIDATORS)

    def validate(
        self,
        contract: DmTurnContractV1,
        packet: ForwardDmContextPacket | None = None,
        *,
        correlation_id: str | None = None,
        regeneration_index: int = 0,
    ) -> ValidationReport:
        cid = correlation_id or uuid.uuid4().hex[:12]
        t0 = time.monotonic()
        results: list[ValidatorResult] = []
        all_violations: list[ValidationViolation] = []

        for validator in self.validators:
            v_t0 = time.monotonic()
            try:
                result = validator.validate(contract, packet)
                # ensure latency is set (validator may have already)
                if result.latency_ms == 0:
                    result.latency_ms = (time.monotonic() - v_t0) * 1000
            except Exception as exc:  # noqa: BLE001
                # fail closed
                latency = (time.monotonic() - v_t0) * 1000
                structured_log(
                    logger, logging.ERROR, "validator_execution_failed",
                    validator=validator.name, category=validator.category, error=str(exc), correlation_id=cid,
                )
                raise ValidatorError(f"Validator {validator.name!r} failed: {exc}") from exc
            results.append(result)
            all_violations.extend(result.violations)
            structured_log(
                logger, logging.INFO, "validator_result",
                validator=result.validator, category=result.category, passed=result.passed,
                violation_count=len(result.violations), latency_ms=round(result.latency_ms, 2),
                correlation_id=cid, regeneration_index=regeneration_index,
            )

        total = (time.monotonic() - t0) * 1000
        passed = len(all_violations) == 0
        report = ValidationReport(
            passed=passed,
            violations=all_violations,
            results=results,
            regeneration_index=regeneration_index,
            total_latency_ms=total,
            correlation_id=cid,
        )
        structured_log(
            logger, logging.INFO, "validation_pipeline_complete",
            passed=passed, violation_count=len(all_violations), total_latency_ms=round(total, 2),
            categories=[r.category for r in results], correlation_id=cid,
        )
        return report


# Singleton pipeline
default_pipeline = ValidatorPipeline()


def attempt_pipeline(db, campaign, turn) -> ValidatorPipeline:
    """Default validators plus the state-reading mechanics and roll checks for one attempt."""
    return ValidatorPipeline([
        *DEFAULT_VALIDATORS, MechanicsValidator(db, campaign, turn), RollRequestValidator(db, turn),
    ])


def validate_contract(
    contract: DmTurnContractV1,
    packet: ForwardDmContextPacket | None = None,
    *,
    correlation_id: str | None = None,
    regeneration_index: int = 0,
) -> ValidationReport:
    """Convenience: validate before first visible chunk using default pipeline."""
    return default_pipeline.validate(
        contract, packet,
        correlation_id=correlation_id, regeneration_index=regeneration_index,
    )


def _augment_packet_with_feedback(
    packet: ForwardDmContextPacket | None,
    feedback: str,
    correlation_id: str,
) -> ForwardDmContextPacket | None:
    if packet is None:
        return None
    try:
        from app.dm.context import AuthorizationScope, ContextRecord, LaneName, SourceRef

        rec = ContextRecord(
            record_id=f"repair:{correlation_id}",
            value={"directive": feedback, "correlation_id": correlation_id},
            sources=[SourceRef(source_type="validator_rejection", source_id=correlation_id, source_version="1", provenance={"feedback": True})],
            authorization=AuthorizationScope(campaign_id=packet.audience.campaign_id, thread_ids=[packet.audience.thread_id]),
            visibility="dm_only",
            use="adjudication_only",
            required=False,
            priority=100,
        )
        return packet.with_records(
            {LaneName.REPAIR_DIRECTIVES: [rec]},
            dependency="validator_repair",
            authoritative_lanes=[LaneName.REPAIR_DIRECTIVES],
        )
    except Exception:
        return packet


def run_with_bounded_regeneration(
    adjudicate: Callable[..., DmTurnContractV1 | dict[str, Any]],
    packet: ForwardDmContextPacket | None = None,
    *,
    max_regenerations: int = 3,
    packet_repair: Callable[[ValidationReport, ForwardDmContextPacket | None], ForwardDmContextPacket | None] | None = None,
    pipeline: ValidatorPipeline | None = None,
    initial_contract: DmTurnContractV1 | None = None,
) -> tuple[DmTurnContractV1, ValidationReport]:
    """Bounded retry: adjudicate → validate → on rejection, adjudicate again with feedback.

    ``adjudicate`` is called as ``adjudicate(packet, feedback)``. On retry,
    ``feedback`` is the structured string from ``format_rejection_for_retry`` and
    ``packet`` is augmented with a ``repair_directives`` record so packet-only
    adjudicators still see the rejection. Failures after the bound surface a
    truncated structured error.

    ``packet_repair`` is an optional deterministic hook for missing knowledge
    perspectives (issue #455): called once with the failing report and current
    packet, it may return a packet with the absent lane entries resolved (or
    None). The same contract is first re-validated against the repaired
    packet: an NPC the DM brought into the turn whose stored perspective
    covers its claims passes with no model call. Otherwise the repaired packet
    costs exactly one model retry inside the normal budget — never an extra
    call.

    ``initial_contract`` validates an already-produced contract as attempt 0
    instead of calling ``adjudicate``, so its rejection (and any perspective
    repair) feeds the first retry.

    Deterministic fast-path (issue #455): when every violation is a missing
    knowledge perspective, rewording cannot help — the lane entry is absent
    from the frozen packet — so no further model call is spent. The offending
    claims are stripped deterministically (scope-narrowing). When nothing
    salvageable remains, a dialogue-only turn degrades to a silent contract;
    a turn carrying effects, a roll request, new entities, or evidence
    requests is never silenced — it spends a normal model retry, and fails
    visibly once the budget is exhausted.
    """
    from app.dm.contract import normalize_contract

    pipe = pipeline or default_pipeline
    last_report: ValidationReport | None = None
    current_packet = packet
    repair_attempted = False

    for attempt in range(max_regenerations + 1):
        feedback: str | None = format_rejection_for_retry(last_report) if last_report else None
        try:
            # Augment packet with repair feedback for packet-aware adjudicators.
            # Augmentation applies to the CURRENT packet (never the original):
            # repair records accumulate across retries and a repaired
            # perspective lane survives re-augmentation.
            if attempt > 0 and feedback is not None:
                current_packet = _augment_packet_with_feedback(current_packet, feedback, last_report.correlation_id if last_report else "retry")  # type: ignore[union-attr]
            if attempt == 0 and initial_contract is not None:
                raw = initial_contract
            else:
                raw = adjudicate(current_packet, feedback)
            if isinstance(raw, dict):
                contract = normalize_contract(raw)
            elif isinstance(raw, DmTurnContractV1):
                contract = raw
            else:
                raise ValidatorError(f"adjudicate must return contract dict or DmTurnContractV1, got {type(raw)}")
        except ContractValidationError as exc:
            # Structurally invalid output never reaches validators — convert to
            # a synthetic rejection so it retries with explicit feedback
            # instead of failing on the first shot. Keep the real code
            # (unknown_field, invalid_mode, ...) so rejection telemetry
            # attributes structural waste precisely.
            last_report = ValidationReport(
                passed=False,
                violations=[ValidationViolation(
                    validator="contract",
                    category="structure",
                    code=exc.code,
                    message=str(exc)[:500],
                )],
                results=[],
                regeneration_index=attempt,
                total_latency_ms=0,
                correlation_id=str(uuid.uuid4()),
            )
            structured_log(
                logger, logging.WARNING, "validator_regeneration",
                attempt=attempt, violations=[f"contract/{exc.code}"],
                detail=_structural_error_shape(exc),
                correlation_id=last_report.correlation_id,
            )
            if attempt >= max_regenerations:
                break
            continue

        report = pipe.validate(
            contract, current_packet,
            regeneration_index=attempt,
        )
        if report.passed:
            return contract, report
        last_report = report
        structured_log(
            logger, logging.WARNING, "validator_regeneration",
            attempt=attempt, violations=[f"{v.validator}/{v.code}" for v in report.violations], correlation_id=report.correlation_id,
        )
        locations = _missing_perspective_locations(report)
        if locations is not None:
            if packet_repair is not None and not repair_attempted and current_packet is not None:
                # Resolve-then-retry (issue #455): one model retry against a
                # deterministically repaired packet, inside the normal budget.
                repair_attempted = True
                try:
                    repaired_packet = packet_repair(report, current_packet)
                except Exception as exc:
                    structured_log(
                        logger, logging.WARNING, "validator_perspective_repair_failed",
                        attempt=attempt, error=str(exc)[:200],
                        correlation_id=report.correlation_id,
                    )
                    repaired_packet = None
                if repaired_packet is not None and repaired_packet is not current_packet:
                    current_packet = repaired_packet
                    structured_log(
                        logger, logging.INFO, "validator_perspective_repaired",
                        attempt=attempt, subjects=missing_perspective_subjects(report),
                        correlation_id=report.correlation_id,
                    )
                    # The contract only lacked the perspective: with it in
                    # the packet, the same claims may already be covered.
                    resolved_report = pipe.validate(
                        contract, current_packet, regeneration_index=attempt,
                    )
                    if resolved_report.passed:
                        return contract, resolved_report
                    if attempt < max_regenerations:
                        last_report = resolved_report
                        continue
            # Repair unavailable or exhausted: narrow deterministically with
            # zero additional model calls. Silence is only a fallback when it
            # drops nothing but the dialogue; a contract carrying effects, a
            # roll request, new entities, or evidence requests spends a model
            # retry instead, and fails visibly once the budget is gone.
            narrowed = _narrow_contract_for_missing_perspective(contract, locations)
            candidates = [narrowed]
            if not _carries_turn_consequences(contract):
                candidates.append(_silent_contract_for_missing_perspective(contract))
            for candidate in [c for c in candidates if c is not None]:
                repair_report = pipe.validate(
                    candidate, current_packet,
                    regeneration_index=attempt + 1,
                )
                if repair_report.passed:
                    structured_log(
                        logger, logging.WARNING, "validator_deterministic_repair",
                        attempt=attempt, mode=candidate.mode,
                        removed_claims=sorted(locations),
                        correlation_id=repair_report.correlation_id,
                    )
                    return candidate, repair_report
                last_report = repair_report
            if _carries_turn_consequences(contract) and attempt < max_regenerations:
                last_report = report
                continue
            raise ValidatorRejectionError(
                f"Validation failed after {attempt + 1} attempt(s); deterministic repair exhausted; "
                f"last violations: {[v.code for v in last_report.violations]}",
                last_report,
            )
        if attempt >= max_regenerations:
            break

    assert last_report is not None
    raise ValidatorRejectionError(
        f"Validation failed after {max_regenerations + 1} attempts; last violations: {[v.code for v in last_report.violations]}",
        last_report,
    )


def _structural_error_shape(exc: ContractValidationError) -> str:
    """Compact pydantic error shape for rejection taxonomy (observability only).

    Returns up to two ``loc:type`` pairs from the preserved pydantic error
    list (e.g. ``roll_request.dc_private:int_parsing``), so rejection logs
    identify which field breaks without dumping model output. Never raises:
    unknown detail shapes yield ``"unavailable"``.
    """
    try:
        errors = (exc.details or {}).get("errors") or []
        parts = []
        for item in errors[:2]:
            if not isinstance(item, dict):
                continue
            loc = ".".join(str(p) for p in (item.get("loc") or ()))
            message = str(item.get("msg") or "")[:120]
            parts.append(f"{loc}:{item.get('type', '?')}" + (f" ({message})" if message else ""))
        return "; ".join(parts) if parts else "unavailable"
    except Exception:
        return "unavailable"


# Violation code for NPC claims with no resolvable knowledge perspective
# (KnowledgeValidator, fail-closed on ambiguous state). Rewording the
# utterance can never clear it — the lane entry is absent from the frozen
# packet — so it gets a deterministic fast-path in bounded regeneration
# (issue #455) instead of burning model retries.
_MISSING_PERSPECTIVE_CODE = "npc_utterance_ambiguous_knowledge"


def format_rejection_for_retry(report: ValidationReport) -> str:
    """Human/model-facing structured feedback for bounded regeneration."""
    lines = [f"Validation failed (correlation {report.correlation_id}):"]
    for v in report.violations:
        loc = f" beat {v.claim_index[0]} claim {v.claim_index[1]}" if v.claim_index else ""
        lines.append(f"- [{v.validator}/{v.code}]{loc}: {v.message}")
    if any(v.code == _MISSING_PERSPECTIVE_CODE for v in report.violations):
        lines.append(
            "NPCs listed with no resolvable knowledge perspective must not carry "
            "knowledge-bearing claims: omit their dialogue or leave them silent, "
            "unless the context now provides their perspective."
        )
    lines.append("Fix the contract and retry without inventing facts. Remove unauthorized PC actions, unknown entity refs, private leaks, and unsupported promotions to fact.")
    return "\n".join(lines)


def missing_perspective_subjects(report: ValidationReport) -> list[str]:
    """Actor IDs behind missing-perspective violations, in first-seen order."""
    subjects: list[str] = []
    for v in report.violations or []:
        if v.code != _MISSING_PERSPECTIVE_CODE:
            continue
        actor = ((v.details or {}).get("actor"))
        sid = str(actor or "").strip()
        if sid and sid not in subjects:
            subjects.append(sid)
    return subjects


def _missing_perspective_locations(report: ValidationReport) -> set[tuple[int, int]] | None:
    """Claim locations failing only on a missing knowledge perspective.

    Returns the ``(beat, claim)`` set when EVERY violation is
    ``npc_utterance_ambiguous_knowledge`` with a location — i.e. rewording
    cannot help because the knowledge lane entry is absent from the frozen
    packet (issue #455). Returns None for mixed, unlocatable, or empty
    reports, where normal regeneration still applies.
    """
    if not report.violations:
        return None
    locations: set[tuple[int, int]] = set()
    for v in report.violations:
        if v.code != _MISSING_PERSPECTIVE_CODE or v.claim_index is None:
            return None
        locations.add((int(v.claim_index[0]), int(v.claim_index[1])))
    return locations


def _narrow_contract_for_missing_perspective(
    contract: DmTurnContractV1, locations: set[tuple[int, int]]
) -> DmTurnContractV1 | None:
    """Drop perspective-missing claims (and beats left empty), or None."""
    try:
        data = contract.model_dump(mode="json")
    except Exception:
        return None
    narrowed_beats = []
    for bi, beat in enumerate(data.get("beats") or []):
        claims = beat.get("claims") or []
        kept = [c for ci, c in enumerate(claims) if (bi, ci) not in locations]
        if not kept:
            continue
        if len(kept) != len(claims):
            beat = dict(beat)
            beat["claims"] = kept
        narrowed_beats.append(beat)
    data["beats"] = narrowed_beats
    try:
        from app.dm.contract import normalize_contract as _normalize

        return _normalize(data)
    except Exception:
        return None


def _carries_turn_consequences(contract: DmTurnContractV1) -> bool:
    """True when silencing the contract would discard more than dialogue."""
    return bool(
        contract.staged_effects
        or contract.roll_request is not None
        or contract.new_entities
        or contract.evidence_requests
    )


def _silent_contract_for_missing_perspective(contract: DmTurnContractV1) -> DmTurnContractV1 | None:
    """Degrade to a no-op silent contract, or None when not constructible."""
    try:
        data = contract.model_dump(mode="json")
    except Exception:
        return None
    reason = str(data.get("reason") or "").strip() or "NPC dialogue withheld: no resolvable knowledge perspective"
    data["mode"] = "silent"
    data["reason"] = reason[:400]
    data["beats"] = []
    data["staged_effects"] = []
    data["new_entities"] = []
    data["evidence_requests"] = []
    data["roll_request"] = None
    data["table_chat_intent"] = None
    data["safe_prelude"] = None
    data["clarify_question"] = None
    try:
        from app.dm.contract import normalize_contract as _normalize

        return _normalize(data)
    except Exception:
        return None
