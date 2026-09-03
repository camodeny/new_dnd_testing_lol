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
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.dm.context import ContextAudience, ForwardDmContextPacket, LaneName
from app.dm.contract import Claim, DmTurnContractV1
from app.observability.tracing import structured_log

logger = logging.getLogger(__name__)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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
        *,
        known_entity_ids: set[str] | None = None,
        canon_facts: dict[str, Any] | None = None,
        private_fact_texts: set[str] | None = None,
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


def _extract_player_input_texts(packet: ForwardDmContextPacket | None) -> list[str]:
    if packet is None:
        return []
    lane = next((lane for lane in packet.lanes if lane.name == LaneName.PLAYER_INPUTS), None)
    if lane is None:
        return []
    texts: list[str] = []
    for rec in lane.records:
        for seg in rec.value.get("segments") or []:
            t = seg.get("text") if isinstance(seg, dict) else None
            if t:
                texts.append(str(t))
    return texts


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
        elif lane.name in (LaneName.RELEVANT_CANON, LaneName.CURRENT_SCENE, LaneName.COMBAT_HOOKS):
            for rec in lane.records:
                v = rec.value
                if isinstance(v, dict):
                    for k in ("entity_type", "type"):
                        t = v.get(k)
                        eid = v.get("entity_id") or v.get("id")
                        if t and eid:
                            full = str(eid).strip().lower()
                            out[full] = str(t).strip().lower()
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

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
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

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
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

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []

        # Typed allowlist: full_id_lower -> expected_type (None for bare)
        # Only genuinely bare caller ids become wildcard; typed ids stay typed.
        known_map: dict[str, str | None] = {}
        if known_entity_ids is not None:
            for entry in set(known_entity_ids):
                e = str(entry).strip()
                if not e:
                    continue
                low = e.strip().lower()
                norm_low = self._normalize_full_id(low)
                if ":" in low:
                    prefix = low.split(":", 1)[0].strip().lower()
                    if prefix in ("character", "char", "npc", "location", "object", "entity"):
                        known_map[norm_low] = self._normalize_type(prefix)
                    else:
                        known_map[norm_low] = None
                else:
                    known_map[norm_low] = None
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
        for eff in contract.staged_effects:
            args = eff.arguments or {}
            # reveal_fact item_id is entity-like
            if eff.effect_type == "reveal_fact" and args.get("item_id"):
                # only check if it looks like an entity ref
                pass
            if eff.effect_type == "propose_sheet_update" and args.get("character_id"):
                refs.append((f"effect[{eff.id}].character_id", _fake_ref(args.get("character_id"))))
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

        # Check that new entity public names don't duplicate existing entity names (hook)
        # naive: if known_entity_ids contains a name that matches new_entity public_name, flag
        # caller can supply names via canon_facts mapping
        if canon_facts:
            known_names = {str(v).lower() for v in canon_facts.values() if isinstance(v, str)}
            for ne in contract.new_entities:
                if ne.public_name.lower() in known_names:
                    violations.append(
                        ValidationViolation(
                            validator=self.name, category=self.category, code="duplicate_identity_name",
                            message=f"New entity {ne.temp_id!r} name {ne.public_name!r} duplicates known canonical name",
                            details={"temp_id": ne.temp_id, "name": ne.public_name}, entity_ref=ne.temp_id
                        )
                    )

        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


def _fake_ref(char_id: Any):
    """Wrap a bare id into a minimal EntityRef-like dict for uniform handling."""
    class _R:
        def __init__(self, id_val):
            self.id = id_val
            self.type = "character"
    return _R(char_id)


class ProvenanceValidator:
    """Validate provenance/source-ref semantics."""

    name = "provenance_validator"
    category = "provenance"

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
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

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
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
                # Observation without evidence but claiming knowledge of hidden truth
                # For now, require that observations about hidden canon have evidence_refs
                if claim.claim_kind == "observation" and "hidden" in low and not claim.evidence_refs:
                    # heuristic — only if canon_facts supplied and claim contradicts? skip heavy
                    pass

        # NPC beats with truthful but no supporting evidence are okay; deception already validated via contract

        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class VisibilityValidator:
    """Audience/visibility validation using current authorization metadata."""

    name = "visibility_validator"
    category = "visibility"

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
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
            # Also check semantic private leak: claim text matches known private fact texts
            if is_shared and private_fact_texts and claim.text.strip() in private_fact_texts:
                violations.append(
                    ValidationViolation(
                        validator=self.name, category=self.category, code="private_semantic_leak",
                        message="Shared-audience output contains private-only fact text",
                        details={"beat": bi, "claim": ci, "text": claim.text}, claim_index=(bi, ci)
                    )
                )

        # Check staged effects visibility: shared audience must not have dm_private events leaked via narration? But effects are internal — skip
        # Check packet-aware: private records must not be in campaign audience packet — already enforced by context assembly, but validator double-checks
        if is_shared and private_fact_texts:
            for beat in contract.beats:
                for claim in beat.claims:
                    if claim.text.strip() in private_fact_texts:
                        # already handled above; dedup
                        pass

        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=len(violations) == 0, violations=violations, latency_ms=latency)


class CanonValidator:
    """Basic current-canon contradiction checks against authoritative context.

    Supports two typed inputs:
    * packet-derived canon: all records in RELEVANT_CANON / CURRENT_SCENE / RECENT_HISTORY
      are treated as authoritative. Contradiction is detected via shared subject + antonym pairs.
    * caller-supplied canon_facts: dict where each value is either a string, or a dict
      {\"value\": str, \"forbids\": [str, ...]} . A claim that contains a forbids phrase
      (or an antonym of the value) without evidence_refs is a contradiction.
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

    def _extract_canon_entries(self, packet, canon_facts) -> list[tuple[str, list[str]]]:
        """Return list of (canonical_text, forbids_phrases)."""
        entries: list[tuple[str, list[str]]] = []
        if canon_facts:
            for k, v in canon_facts.items():
                if k.startswith("contradicts:") and isinstance(v, str):
                    # legacy explicit forbid
                    entries.append((k, [v.strip().lower()]))
                elif isinstance(v, str):
                    # derive forbids via antonym lookup
                    low = v.strip().lower()
                    forbids: list[str] = []
                    for a, b in self._ANTONYMS:
                        if a in low:
                            forbids.append(b)
                        if b in low:
                            forbids.append(a)
                    entries.append((low, forbids))
                elif isinstance(v, dict):
                    canon_val = str(v.get("value") or v.get("canonical") or "").strip().lower()
                    forbids = [str(x).strip().lower() for x in v.get("forbids") or v.get("forbids_contains") or [] if str(x).strip()]
                    # also augment with antonym expansion
                    for a, b in self._ANTONYMS:
                        if canon_val and a in canon_val and b not in forbids:
                            forbids.append(b)
                        if canon_val and b in canon_val and a not in forbids:
                            forbids.append(a)
                    entries.append((canon_val, forbids))
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

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
        t0 = time.monotonic()
        violations: list[ValidationViolation] = []
        canon_entries = self._extract_canon_entries(packet, canon_facts)

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


# ── Extension hook ───────────────────────────────────────────────────────────

class ContentBoundaryValidator:
    """Hook for content-boundary policy — supplied via lane, fails closed on disallowed content."""
    name = "content_boundary_validator"
    category = "content"

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
        # No-op stub: real policy adapter will supply disallowed patterns via supplemental records
        t0 = time.monotonic()
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=True, violations=[], latency_ms=latency)


class RulesValidator:
    """Hook for deterministic 2024 rules validation (#180)."""
    name = "rules_validator"
    category = "mechanics"

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
        t0 = time.monotonic()
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=True, violations=[], latency_ms=latency)


class RepairValidator:
    """Hook for post-visible repair directives (#179) — in pre-narration pipeline it's a no-op."""
    name = "repair_validator"
    category = "repair"

    def validate(self, contract, packet, *, known_entity_ids=None, canon_facts=None, private_fact_texts=None) -> ValidatorResult:
        t0 = time.monotonic()
        latency = (time.monotonic() - t0) * 1000
        return ValidatorResult(validator=self.name, category=self.category, passed=True, violations=[], latency_ms=latency)


# ── Pipeline ─────────────────────────────────────────────────────────────────

DEFAULT_VALIDATORS: list[Validator] = [
    AgencyValidator(),
    OwnershipValidator(),
    EntityValidator(),
    ProvenanceValidator(),
    EpistemicValidator(),
    VisibilityValidator(),
    CanonValidator(),
    ContentBoundaryValidator(),
    RulesValidator(),
    RepairValidator(),
]

# category ordering is the validator list order above

class ValidatorPipeline:
    def __init__(self, validators: list[Validator] | None = None):
        self.validators: list[Validator] = list(validators) if validators is not None else list(DEFAULT_VALIDATORS)

    def add_validator(self, validator: Validator, *, before: str | None = None, after: str | None = None) -> None:
        """Extension point: add later validators without rewriting orchestration."""
        if before:
            for idx, v in enumerate(self.validators):
                if v.name == before:
                    self.validators.insert(idx, validator)
                    return
            raise ValueError(f"before target {before!r} not found")
        if after:
            for idx, v in enumerate(self.validators):
                if v.name == after:
                    self.validators.insert(idx + 1, validator)
                    return
            raise ValueError(f"after target {after!r} not found")
        self.validators.append(validator)

    def validate(
        self,
        contract: DmTurnContractV1,
        packet: ForwardDmContextPacket | None = None,
        *,
        known_entity_ids: set[str] | None = None,
        canon_facts: dict[str, Any] | None = None,
        private_fact_texts: set[str] | None = None,
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
                result = validator.validate(
                    contract,
                    packet,
                    known_entity_ids=known_entity_ids,
                    canon_facts=canon_facts,
                    private_fact_texts=private_fact_texts,
                )
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

    def validate_or_raise(
        self,
        contract: DmTurnContractV1,
        packet: ForwardDmContextPacket | None = None,
        *,
        known_entity_ids: set[str] | None = None,
        canon_facts: dict[str, Any] | None = None,
        private_fact_texts: set[str] | None = None,
        correlation_id: str | None = None,
        regeneration_index: int = 0,
    ) -> ValidationReport:
        report = self.validate(
            contract, packet,
            known_entity_ids=known_entity_ids, canon_facts=canon_facts,
            private_fact_texts=private_fact_texts,
            correlation_id=correlation_id, regeneration_index=regeneration_index,
        )
        if not report.passed:
            raise ValidatorRejectionError("Contract failed pre-narration validation", report)
        return report


# Singleton pipeline
default_pipeline = ValidatorPipeline()


def validate_contract(
    contract: DmTurnContractV1,
    packet: ForwardDmContextPacket | None = None,
    *,
    known_entity_ids: set[str] | None = None,
    canon_facts: dict[str, Any] | None = None,
    private_fact_texts: set[str] | None = None,
    correlation_id: str | None = None,
    regeneration_index: int = 0,
    pipeline: ValidatorPipeline | None = None,
) -> ValidationReport:
    """Convenience: validate before first visible chunk using default pipeline."""
    pipe = pipeline or default_pipeline
    return pipe.validate(
        contract, packet,
        known_entity_ids=known_entity_ids, canon_facts=canon_facts,
        private_fact_texts=private_fact_texts,
        correlation_id=correlation_id, regeneration_index=regeneration_index,
    )


def validate_contract_or_raise(
    contract: DmTurnContractV1,
    packet: ForwardDmContextPacket | None = None,
    *,
    known_entity_ids: set[str] | None = None,
    canon_facts: dict[str, Any] | None = None,
    private_fact_texts: set[str] | None = None,
    correlation_id: str | None = None,
    regeneration_index: int = 0,
    pipeline: ValidatorPipeline | None = None,
) -> ValidationReport:
    pipe = pipeline or default_pipeline
    return pipe.validate_or_raise(
        contract, packet,
        known_entity_ids=known_entity_ids, canon_facts=canon_facts,
        private_fact_texts=private_fact_texts,
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
        from app.dm.context import AuthorizationScope, ContextRecord, LaneName as _LN, SourceRef as _SR, assemble_context_packet

        rec = ContextRecord(
            record_id=f"repair:{correlation_id}",
            value={"directive": feedback, "correlation_id": correlation_id},
            sources=[_SR(source_type="validator_rejection", source_id=correlation_id, source_version="1", provenance={"feedback": True})],
            authorization=AuthorizationScope(campaign_id=packet.audience.campaign_id, thread_ids=[packet.audience.thread_id]),
            visibility="dm_only",
            use="adjudication_only",
            required=False,
            priority=100,
        )
        # Reassemble packet with additional repair record
        from collections import defaultdict

        records: dict = {lane.name: list(lane.records) for lane in packet.lanes}
        # ensure repair lane exists
        records[_LN.REPAIR_DIRECTIVES] = list(records.get(_LN.REPAIR_DIRECTIVES, [])) + [rec]
        lane_status = {lane.name: lane.authority_status for lane in packet.lanes}
        source_errors = {lane.name: lane.source_errors for lane in packet.lanes}
        # keep repair lane authoritative
        lane_status[_LN.REPAIR_DIRECTIVES] = "authoritative"
        return assemble_context_packet(
            audience=packet.audience,
            records=records,
            lane_status=lane_status,
            source_errors=source_errors,
            retrieval_dependencies=list(packet.observability.retrieval_dependencies) + ["validator_repair"],
        )
    except Exception:
        return packet


def _call_adjudicate(adjudicate: Callable[..., Any], packet: ForwardDmContextPacket | None, feedback: str | None):
    """Explicit signature dispatch — does not use trial invocation.

    Supported adjudicate forms:
    * ``() -> contract``
    * ``(packet) -> contract``
    * ``(feedback) -> contract`` (name contains feedback/rejection)
    * ``(packet, feedback) -> contract``
    Any TypeError raised *inside* the adjudicate is not treated as a
    signature mismatch and propagates.
    """
    import inspect

    sig = inspect.signature(adjudicate)
    params = [p for p in sig.parameters.values() if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    has_var = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())

    if has_var:
        return adjudicate(packet, feedback)  # type: ignore[call-arg]

    if len(params) == 0:
        return adjudicate()  # type: ignore[call-arg]

    if len(params) == 1:
        name = params[0].name.lower()
        if "packet" in name:
            return adjudicate(packet)  # type: ignore[call-arg]
        if "feedback" in name or "rejection" in name:
            return adjudicate(feedback)  # type: ignore[call-arg]
        # ambiguous single-arg: treat as packet-only (explicit contract prefers
        # 2-arg (packet, feedback)); packet will already be augmented with
        # repair_directives on retry so feedback is still visible.
        return adjudicate(packet)  # type: ignore[call-arg]

    # 2+ positional args — assume (packet, feedback) order, but respect names
    names = [p.name.lower() for p in params[:2]]
    if "packet" in names[0] and ("feedback" in names[1] or "rejection" in names[1]):
        return adjudicate(packet, feedback)  # type: ignore[call-arg]
    if ("feedback" in names[0] or "rejection" in names[0]) and "packet" in names[1]:
        return adjudicate(feedback, packet)  # type: ignore[call-arg]
    # default: (packet, feedback)
    return adjudicate(packet, feedback)  # type: ignore[call-arg]


def run_with_bounded_regeneration(
    adjudicate: Callable[..., DmTurnContractV1 | dict[str, Any]],
    packet: ForwardDmContextPacket | None = None,
    *,
    known_entity_ids: set[str] | None = None,
    canon_facts: dict[str, Any] | None = None,
    private_fact_texts: set[str] | None = None,
    max_regenerations: int = 3,
    pipeline: ValidatorPipeline | None = None,
    normalize_fn: Callable[[Any], DmTurnContractV1] | None = None,
) -> tuple[DmTurnContractV1, ValidationReport]:
    """Bounded retry: adjudicate → validate → on rejection, adjudicate again with feedback.

    ``adjudicate`` may be ``() -> contract``, ``(feedback: str|None) -> contract``,
    ``(packet) -> contract`` or ``(packet, feedback) -> contract``.  On retry,
    ``feedback`` is the structured string from ``format_rejection_for_retry`` and
    ``packet`` is augmented with a ``repair_directives`` record so packet-only
    adjudicators still see the rejection. Failures after the bound surface a
    truncated structured error.
    """
    from app.dm.contract import normalize_contract as _normalize

    norm = normalize_fn or _normalize
    pipe = pipeline or default_pipeline
    last_report: ValidationReport | None = None
    current_packet = packet

    for attempt in range(max_regenerations + 1):
        feedback: str | None = format_rejection_for_retry(last_report) if last_report else None
        # Augment packet with repair feedback for packet-aware adjudicators
        if attempt > 0 and feedback is not None:
            current_packet = _augment_packet_with_feedback(packet, feedback, last_report.correlation_id if last_report else "retry")  # type: ignore[union-attr]
            raw = _call_adjudicate(adjudicate, current_packet, feedback)
        else:
            raw = _call_adjudicate(adjudicate, current_packet, feedback)
        if isinstance(raw, dict):
            contract = norm(raw)
        elif isinstance(raw, DmTurnContractV1):
            contract = raw
        else:
            raise ValidatorError(f"adjudicate must return contract dict or DmTurnContractV1, got {type(raw)}")

        report = pipe.validate(
            contract, current_packet,
            known_entity_ids=known_entity_ids, canon_facts=canon_facts,
            private_fact_texts=private_fact_texts,
            regeneration_index=attempt,
        )
        if report.passed:
            return contract, report
        last_report = report
        structured_log(
            logger, logging.WARNING, "validator_regeneration",
            attempt=attempt, violations=[v.code for v in report.violations], correlation_id=report.correlation_id,
        )
        if attempt >= max_regenerations:
            break

    assert last_report is not None
    raise ValidatorRejectionError(
        f"Validation failed after {max_regenerations + 1} attempts; last violations: {[v.code for v in last_report.violations]}",
        last_report,
    )


def format_rejection_for_retry(report: ValidationReport) -> str:
    """Human/model-facing structured feedback for bounded regeneration."""
    lines = [f"Validation failed (correlation {report.correlation_id}):"]
    for v in report.violations:
        loc = f" beat {v.claim_index[0]} claim {v.claim_index[1]}" if v.claim_index else ""
        lines.append(f"- [{v.validator}/{v.code}]{loc}: {v.message}")
    lines.append("Fix the contract and retry without inventing facts. Remove unauthorized PC actions, unknown entity refs, private leaks, and unsupported promotions to fact.")
    return "\n".join(lines)
