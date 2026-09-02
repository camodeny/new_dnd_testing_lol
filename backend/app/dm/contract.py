"""Versioned structured DM turn contract — issue #201.

Contract: ``dm_turn_contract_v1``
One validated output from the forward DM.  The model proposes; the runtime verifies.
Narration is a deterministic projection of this contract, not an independent truth source.

Key invariants (checked by Pydantic strict validation):
- Unknown / extra fields are rejected (additionalProperties: false / extra="forbid").
- All runtime states are explicit modes, never inferred from prose.
- NPC utterance beats store truth_status + dm_private_context independently of
  the player-visible utterance text, so deception can be projected without leaking.
- Player declarations (claim_kind=player_declaration) are claim-typed and
  provenance-typed, never conflated with world_fact/observation.
- Existing entity references (EntityRef) and new-entity proposals (NewEntityProposal
  with temp_id) are structurally distinct types.
- No generic SQL / arbitrary mutation tools exist in the contract; only the
  typed staged effects in STAGED_EFFECT_TYPES can be expressed.
- Mixed IC/OOC input identity is preserved in AdjudicationInput segments.
- Contract is versioned; normalize_contract() upgrades/dispatches by contract_version
  and is fixture-testable independently of any LLM provider.

Security / privacy:
- Public vs private material is structurally separated; public_projection()
  removes dm_private_context, private roll thresholds etc. deterministically.

Observability:
- CONTRACT_VERSION, mode, validation failure codes (ContractValidationError.code),
  and output-size metrics (serialized size, claim/beat counts) are emitted per attempt.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

CONTRACT_VERSION = "dm_turn_contract_v1"
SUPPORTED_VERSIONS = {CONTRACT_VERSION}

# ── Strict base ───────────────────────────────────────────────────────────────

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


# ── Input identity (preserves IC/OOC) ───────────────────────────────────────

class InputSegment(StrictModel):
    """One ordered IC/OOC part of a player submission, preserving mixed identity."""
    position: int = Field(ge=0, description="Order within the submission")
    segment_type: Literal["ic", "ooc"] = Field(description="In-character vs out-of-character")
    text: str = Field(min_length=1, max_length=4000)


class AdjudicationInput(StrictModel):
    """Machine representation of the adjudication input set for this turn.

    This contract preserves per-segment IC/OOC identity, per-submission
    ordering, and source submission IDs so validators can reason about agency
    without re-parsing rendered text. Segments are authoritative.
    """
    submission_ids: list[str] = Field(default_factory=list, max_length=32, description="Ordered submission UUID strings included")
    segments: list[InputSegment] = Field(default_factory=list, max_length=64)

    @field_validator("submission_ids")
    @classmethod
    def _nonempty_ids(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item or not item.strip():
                raise ValueError("submission_ids entries must be non-empty")
        return v


# ── Entity refs: existing vs temporary are structurally distinct ─────────────

EntityType = Literal["character", "npc", "location", "object", "entity"]
_TEMP_PREFIXES = ("tmp_", "temp_")

class EntityRef(StrictModel):
    """Reference to an *existing* canonical entity that already exists in context.

    ``id`` is a durable canonical ID supplied in context (graph or DB).
    Structural distinction: NewEntityProposal uses ``temp_id`` instead of ``id``,
    so a generic handler cannot confuse the two lanes.
    """
    type: EntityType
    id: str | int = Field(description="Durable canonical entity id; never a temp id")

    @field_validator("id")
    @classmethod
    def _id_not_temp(cls, v: str | int) -> str | int:
        s = str(v).strip()
        if not s:
            raise ValueError("EntityRef.id must be non-empty")
        low = s.lower()
        for pref in _TEMP_PREFIXES:
            if low.startswith(pref):
                raise ValueError(f"EntityRef.id must not be a temporary id (got {s!r}); use NewEntityProposal.temp_id")
        if len(s) > 160:
            raise ValueError("EntityRef.id too long")
        return v

    def normalized_id(self) -> str:
        return str(self.id)


class NewEntityProposal(StrictModel):
    """Proposal to introduce a previously unknown entity within this turn.

    The ``temp_id`` is an ephemeral handle valid only within this contract;
    persistence assigns the durable literal ID atomically after validation.
    Structural distinction from EntityRef (``id``) is intentionally field-name
    based so ``extra=forbid`` rejects cross-lane confusion.
    """
    temp_id: str = Field(description="Ephemeral handle, e.g. tmp_npc_1")
    kind: Literal["npc"] = Field(description="Only NPC introduction is supported in v1")
    public_name: str = Field(min_length=1, max_length=160)
    role: str | None = Field(default=None, max_length=160)
    public_summary: str | None = Field(default=None, max_length=500)
    location_ref: EntityRef | None = None

    @field_validator("temp_id")
    @classmethod
    def _valid_temp_id(cls, v: str) -> str:
        s = v.strip()
        if not re.fullmatch(r"(tmp_npc_[A-Za-z0-9_-]+|tmp_[a-z]+_[A-Za-z0-9_-]+)", s):
            raise ValueError("temp_id must be like tmp_npc_1 or tmp_<kind>_<id>")
        return s

    @field_validator("location_ref")
    @classmethod
    def _location_only(cls, v: EntityRef | None) -> EntityRef | None:
        if v is not None and v.type != "location":
            raise ValueError("NewEntityProposal.location_ref must be type=location")
        return v


# ── Claims and beats ─────────────────────────────────────────────────────────

ClaimKind = Literal[
    "observation",
    "world_fact",
    "npc_utterance",
    "player_declaration",
    "roll_instruction",
    "roll_outcome",
]

Origin = Literal[
    "player_transcript",
    "established_state",
    "resolver_evidence",
    "dm_adjudication",
    "roll_adjudication",
]

ClaimVisibility = Literal["public", "dm_private"]
# v1 keeps beat/claim visibility simple: public material survives projection,
# dm_private material is stripped deterministically. Future versions may add
# party_known / thread-scoped visibility.

class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=800, description="Atomic player-visible fact or utterance")
    claim_kind: ClaimKind = Field(description="Distinguishes player declarations from world facts")
    actor_ref: EntityRef | None = Field(default=None, description="Who performed / spoke; null for world observations")
    target_refs: list[EntityRef] = Field(default_factory=list, max_length=12)
    topic_refs: list[EntityRef] = Field(default_factory=list, max_length=12)
    location_ref: EntityRef | None = None
    # Provenance: which sources authorize this claim
    evidence_refs: list[str] = Field(default_factory=list, max_length=12, description="Evidence ids proving an established claim")
    trigger_refs: list[str] = Field(default_factory=list, max_length=12, description="Source ids that prompted a newly adjudicated reaction")
    origin: Origin
    roll_request_id: str | None = Field(default=None, max_length=48)
    visibility: ClaimVisibility = Field(default="public")

    @field_validator("evidence_refs", "trigger_refs")
    @classmethod
    def _bounded_refs(cls, v: list[str]) -> list[str]:
        for item in v:
            s = str(item).strip()
            if not s or len(s) > 160:
                raise ValueError("source refs must be 1-160 chars")
        # deduplicate preserving order
        seen: list[str] = []
        for item in v:
            if item not in seen:
                seen.append(item)
        if len(seen) != len(v):
            # allow deduplication transparently, but keep canonical order
            return seen
        return v

    @model_validator(mode="after")
    def _claim_invariants(self) -> "Claim":
        if self.claim_kind == "npc_utterance":
            if self.actor_ref is None or self.actor_ref.type != "npc":
                raise ValueError("npc_utterance requires actor_ref type=npc")
        if self.claim_kind == "player_declaration":
            if self.actor_ref is None or self.actor_ref.type != "character":
                raise ValueError("player_declaration requires actor_ref type=character")
            if self.origin != "player_transcript":
                raise ValueError("player_declaration must have origin=player_transcript")
        if self.claim_kind == "roll_outcome" and not self.roll_request_id:
            raise ValueError("roll_outcome requires roll_request_id")
        if self.claim_kind != "roll_outcome" and self.roll_request_id is not None:
            # allow null, but non-null on non-outcome is suspect; enforce strict
            raise ValueError("roll_request_id is only valid on roll_outcome claims")
        # Objects cannot be actors; actor must be character or npc when present
        if self.actor_ref is not None and self.actor_ref.type not in ("character", "npc"):
            raise ValueError("actor_ref must be character or npc when present; put objects in topic_refs and locations in location_ref")
        if self.location_ref is not None and self.location_ref.type != "location":
            raise ValueError("location_ref must be type=location")
        return self


BeatType = Literal["narration", "npc_dialogue"]
TruthStatus = Literal["truthful", "mistaken", "deceptive", "incomplete", "unknown"]

class Beat(StrictModel):
    id: str = Field(min_length=1, max_length=48, description="Stable beat id, e.g. beat_1")
    type: BeatType
    speaker_ref: EntityRef | None = Field(default=None, description="Canonical speaker for npc_dialogue; null for narration")
    speaker_public_name: str | None = Field(default=None, max_length=160)
    claims: list[Claim] = Field(min_length=1, max_length=5, description="Ordered atomic claims for this beat")
    delivery: str | None = Field(default=None, max_length=400, description="Style/narration hint; contains no world facts")
    truth_status: TruthStatus | None = None
    dm_private_context: str | None = Field(default=None, max_length=1000, description="Authoritative interpretation when truth_status != truthful; stripped in public projection")

    @field_validator("id")
    @classmethod
    def _valid_beat_id(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError("beat id must match [A-Za-z0-9_-]+")
        return v

    @model_validator(mode="after")
    def _beat_invariants(self) -> "Beat":
        if self.type == "npc_dialogue":
            if self.speaker_ref is None:
                raise ValueError("npc_dialogue requires speaker_ref")
            if self.speaker_ref.type != "npc":
                raise ValueError("npc_dialogue speaker_ref must be type=npc")
            if not self.speaker_public_name or not self.speaker_public_name.strip():
                raise ValueError("npc_dialogue requires speaker_public_name")
            for c in self.claims:
                if c.claim_kind != "npc_utterance":
                    raise ValueError("npc_dialogue beats may only contain npc_utterance claims")
                # actor must match speaker
                if c.actor_ref is None or str(c.actor_ref.id) != str(self.speaker_ref.id):
                    raise ValueError("npc_dialogue claim actor_ref must equal beat speaker_ref")
            # truth_status + private context lane for deception
            if self.truth_status is None:
                raise ValueError("npc_dialogue requires truth_status")
            if self.truth_status != "truthful" and not (self.dm_private_context and self.dm_private_context.strip()):
                raise ValueError("non-truthful npc_dialogue requires dm_private_context")
            if self.truth_status == "unknown" and self.dm_private_context is not None and not self.dm_private_context.strip():
                # unknown gets safe default; allow validator to fill below
                pass
        else:  # narration
            if self.speaker_ref is not None or self.speaker_public_name is not None:
                raise ValueError("narration beats must not have speaker_ref / speaker_public_name")
            if self.truth_status is not None or self.dm_private_context is not None:
                raise ValueError("truth_status / dm_private_context only valid on npc_dialogue beats")
            for c in self.claims:
                if c.claim_kind == "npc_utterance":
                    raise ValueError("narration beats must not contain npc_utterance claims")
        return self


# ── Typed staged effects (no generic SQL / arbitrary mutation) ───────────────

STAGED_EFFECT_TYPES = (
    "record_world_event",
    "update_scene",
    "reveal_fact",
    "propose_sheet_update",
)

class RecordWorldEventArgs(StrictModel):
    event_type: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=800)
    visibility: Literal["public", "party_known", "dm_private"] = "dm_private"
    payload: dict[str, Any] | None = None
    source_facet_ids: list[str] | None = None

class UpdateSceneArgs(StrictModel):
    scene_patch: dict[str, Any] = Field(description="Bounded scene patch; no arbitrary SQL")
    reason: str = Field(min_length=1, max_length=400)

class RevealFactArgs(StrictModel):
    item_type: Literal["entity", "relation", "fact"]
    item_id: str = Field(min_length=1, max_length=160)
    visibility: Literal["public", "party_known", "dm_private"]
    reason: str = Field(min_length=1, max_length=400)

class ProposeSheetUpdateArgs(StrictModel):
    character_id: str | int = Field(description="Durable character id; proposal remains pending")
    reason: str = Field(min_length=1, max_length=400)
    changes: list[dict[str, Any]] = Field(min_length=1, max_length=8)

    @field_validator("changes")
    @classmethod
    def _validate_changes(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for ch in v:
            if not isinstance(ch, dict):
                raise ValueError("changes entries must be objects")
            if "field" not in ch or "operation" not in ch or "value" not in ch:
                raise ValueError("each change requires field, operation, value")
            if ch["operation"] not in ("add", "subtract", "set"):
                raise ValueError("operation must be add|subtract|set")
        return v

StagedEffectArgs = RecordWorldEventArgs | UpdateSceneArgs | RevealFactArgs | ProposeSheetUpdateArgs

class StagedEffect(StrictModel):
    """One typed, non-generic staged effect.  Must not encode arbitrary SQL."""
    id: str = Field(min_length=1, max_length=48)
    effect_type: Literal["record_world_event", "update_scene", "reveal_fact", "propose_sheet_update"] = Field(description="Typed effect; no generic SQL capability")
    arguments: dict[str, Any] = Field(description="Effect-specific payload validated by effect_type")

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError("staged effect id must match [A-Za-z0-9_-]+")
        return v

    @model_validator(mode="after")
    def _validate_args(self) -> "StagedEffect":
        t = self.effect_type
        args = self.arguments
        # Dispatch validation: coerce through the typed model for stricter checks
        try:
            if t == "record_world_event":
                RecordWorldEventArgs.model_validate(args)
            elif t == "update_scene":
                UpdateSceneArgs.model_validate(args)
            elif t == "reveal_fact":
                RevealFactArgs.model_validate(args)
            elif t == "propose_sheet_update":
                ProposeSheetUpdateArgs.model_validate(args)
        except Exception as e:
            raise ValueError(f"arguments invalid for effect_type={t}: {e}") from e
        # Generic SQL guard: reject any argument that looks like raw SQL / db mutation
        blob = json.dumps(args, default=str).lower()
        for needle in ("drop table", "delete from", "insert into", "update campaign", "select *", "alter table", ";--"):
            if needle in blob:
                raise ValueError(f"staged effect arguments must not contain generic SQL ({needle!r})")
        return self


# ── Evidence requests and roll requests ─────────────────────────────────────

class EvidenceRequest(StrictModel):
    id: str = Field(min_length=1, max_length=48)
    tool: Literal["ask_character_sheet", "get_current_scene", "search_campaign_memory", "lookup_rule", "search_rules"] = Field(description="Read-only evidence tool")
    question: str | None = Field(default=None, max_length=600)
    scope: Literal["current_player", "party", "character_id"] | None = None
    character_id: str | int | None = None
    query: str | None = Field(default=None, max_length=240)
    limit: int | None = Field(default=None, ge=1, le=20)
    include_private: bool | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError("evidence request id must match [A-Za-z0-9_-]+")
        return v

    @model_validator(mode="after")
    def _per_tool(self) -> "EvidenceRequest":
        if self.tool == "ask_character_sheet":
            if not self.question or not self.question.strip():
                raise ValueError("ask_character_sheet requires question")
            if self.scope not in ("current_player", "party", "character_id"):
                raise ValueError("ask_character_sheet requires scope=current_player|party|character_id")
            if self.scope == "character_id" and self.character_id is None:
                raise ValueError("character_id scope requires character_id")
        elif self.tool == "search_campaign_memory":
            if not self.query or not self.query.strip():
                raise ValueError("search_campaign_memory requires query")
        elif self.tool == "lookup_rule":
            if not (self.query and self.query.strip()) and not (self.question and self.question.strip()):
                raise ValueError("lookup_rule requires query (rule_id)")
        elif self.tool == "search_rules":
            if not (self.query and self.query.strip()) and not (self.question and self.question.strip()):
                raise ValueError("search_rules requires query")
        return self


class RollRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=48)
    character_id: str | int | None = None
    roll_kind: Literal["check", "save", "attack", "ability", "initiative", "other"]
    ability_or_skill: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    advantage_state: Literal["normal", "advantage", "disadvantage"] = "normal"
    reason_public: str = Field(min_length=1, max_length=600)
    dc_private: int | None = Field(default=None, ge=1, le=40, description="Hidden difficulty; stripped in public projection")

    @field_validator("request_id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError("roll request_id must match [A-Za-z0-9_-]+")
        return v


# ── Narration / choice hints ─────────────────────────────────────────────────

class NarrationHints(StrictModel):
    max_words: int = Field(default=80, ge=30, le=300)
    style_guidance: str | None = Field(default=None, max_length=600, description="Tone/style hint; no world facts")
    # Beat-level delivery remains the authoritative per-beat style; this is global guidance.


# ── Modes ────────────────────────────────────────────────────────────────────

ContractMode = Literal[
    "respond",        # normal narration / NPC response
    "await_roll",     # ask for a typed roll
    "need_evidence",  # read-only evidence resolution pass
    "clarify",        # ask players for clarification
    "table_chat",     # out-of-character chat, no campaign effects
    "silent",         # intentionally no DM utterance
    "unsupported",    # unsupported mechanic; safe fallback
]

# ── Top-level contract ───────────────────────────────────────────────────────

class DmTurnContractV1(StrictModel):
    """dm_turn_contract_v1 — strict TDD-style adjudication contract.

    Serialize with model_dump(mode='json'); deserialize via normalize_contract().
    """
    contract_version: Literal["dm_turn_contract_v1"] = Field(default=CONTRACT_VERSION)
    mode: ContractMode
    reason: str = Field(min_length=1, max_length=400, description="Short internal reason for this mode")
    beats: list[Beat] = Field(default_factory=list, max_length=8)
    # Open choice / narration hints are separate from atomic facts
    open_player_choice: str | None = Field(default=None, max_length=400)
    narration_hints: NarrationHints | None = None
    # Input preservation
    adjudication_input: AdjudicationInput | None = None
    # New entities — structurally distinct from EntityRef
    new_entities: list[NewEntityProposal] = Field(default_factory=list, max_length=2)
    # Effects — typed, no generic mutation
    staged_effects: list[StagedEffect] = Field(default_factory=list, max_length=4)
    # Evidence / rolls
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list, max_length=3)
    roll_request: RollRequest | None = None
    # Mode-specific free-form intent fields (strict: must be null outside owning mode)
    table_chat_intent: str | None = Field(default=None, max_length=400)
    safe_prelude: str | None = Field(default=None, max_length=240, description="Progress update for need_evidence only")
    clarify_question: str | None = Field(default=None, max_length=400, description="Explicit question for clarify mode")

    @model_validator(mode="after")
    def _mode_invariants(self) -> "DmTurnContractV1":
        m = self.mode
        beats = self.beats or []
        has_beats = len(beats) > 0

        # Per-mode structural expectations
        if m == "respond":
            if not has_beats:
                raise ValueError("respond requires 1-8 beats")
            if self.roll_request is not None:
                raise ValueError("respond must not have roll_request")
            if self.evidence_requests:
                raise ValueError("respond must not have evidence_requests")
            if self.table_chat_intent is not None:
                raise ValueError("table_chat_intent only valid in table_chat mode")
            if self.safe_prelude is not None:
                raise ValueError("safe_prelude only valid in need_evidence mode")
            if self.clarify_question is not None:
                raise ValueError("clarify_question only valid in clarify mode")
        elif m == "await_roll":
            if not has_beats:
                raise ValueError("await_roll requires 1-8 beats")
            if self.roll_request is None:
                raise ValueError("await_roll requires roll_request")
            if self.evidence_requests:
                raise ValueError("await_roll must not have evidence_requests")
            # staged effects are not resolved while awaiting a die roll
            if self.staged_effects:
                raise ValueError("await_roll must not stage effects before the roll resolves")
            if self.table_chat_intent is not None or self.safe_prelude is not None or self.clarify_question is not None:
                raise ValueError("await_roll must not have table_chat_intent/safe_prelude/clarify_question")
        elif m == "need_evidence":
            if has_beats:
                raise ValueError("need_evidence must not have beats")
            if not self.evidence_requests:
                raise ValueError("need_evidence requires 1-3 evidence_requests")
            if not self.safe_prelude or not self.safe_prelude.strip():
                raise ValueError("need_evidence requires safe_prelude")
            if self.roll_request is not None:
                raise ValueError("need_evidence must not have roll_request")
            if self.staged_effects:
                raise ValueError("need_evidence must not stage effects before evidence resolves")
            if self.new_entities:
                raise ValueError("need_evidence must not propose new entities")
            if self.table_chat_intent is not None or self.clarify_question is not None:
                raise ValueError("need_evidence must not have table_chat_intent/clarify_question")
        elif m == "clarify":
            if self.evidence_requests:
                raise ValueError("clarify must not have evidence_requests")
            if self.roll_request is not None:
                raise ValueError("clarify must not have roll_request")
            if self.table_chat_intent is not None or self.safe_prelude is not None:
                raise ValueError("clarify must not have table_chat_intent/safe_prelude")
            # clarify needs an explicit question via clarify_question or open_player_choice
            if not (self.clarify_question or self.open_player_choice):
                raise ValueError("clarify requires clarify_question or open_player_choice")
            # beats are optional for clarify (may be empty or a short narrated setup)
            if len(beats) > 2:
                raise ValueError("clarify may have at most 2 beats")
        elif m == "table_chat":
            if has_beats:
                raise ValueError("table_chat must not have beats")
            if not self.table_chat_intent or not self.table_chat_intent.strip():
                raise ValueError("table_chat requires table_chat_intent")
            if self.evidence_requests or self.roll_request is not None or self.staged_effects or self.new_entities:
                raise ValueError("table_chat must not have evidence_requests, roll_request, staged_effects, or new_entities")
            if self.safe_prelude is not None or self.clarify_question is not None:
                raise ValueError("table_chat must not have safe_prelude/clarify_question")
        elif m == "silent":
            if has_beats:
                raise ValueError("silent must not have beats")
            if self.evidence_requests or self.roll_request is not None or self.staged_effects or self.new_entities:
                raise ValueError("silent must not have evidence_requests, roll_request, staged_effects, or new_entities")
            if self.table_chat_intent is not None or self.safe_prelude is not None or self.clarify_question is not None:
                raise ValueError("silent must not have table_chat_intent/safe_prelude/clarify_question")
        elif m == "unsupported":
            if has_beats:
                raise ValueError("unsupported must not have beats")
            if self.evidence_requests or self.roll_request is not None or self.staged_effects or self.new_entities:
                raise ValueError("unsupported must not have evidence_requests, roll_request, staged_effects, or new_entities")
            if self.table_chat_intent is not None or self.safe_prelude is not None or self.clarify_question is not None:
                raise ValueError("unsupported must not have table_chat_intent/safe_prelude/clarify_question")

        # Global cross-field: new_entities only in respond
        if self.new_entities and m != "respond":
            raise ValueError("new_entities only valid in respond mode")
        # staged_effects only in respond (and arguably silent? no — strictly respond)
        if self.staged_effects and m not in ("respond",):
            # await_roll / need_evidence etc. already block above, but keep global guard
            if m != "respond":
                raise ValueError("staged_effects only valid in respond mode")

        return self

    def output_size_metrics(self) -> dict[str, int]:
        """Cheap size/observability metrics without re-serializing twice when possible."""
        try:
            blob = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
            bytes_len = len(blob.encode("utf-8"))
        except Exception:
            bytes_len = 0
        claim_count = sum(len(b.claims) for b in self.beats)
        return {
            "bytes": bytes_len,
            "beats": len(self.beats),
            "claims": claim_count,
            "staged_effects": len(self.staged_effects),
            "new_entities": len(self.new_entities),
            "evidence_requests": len(self.evidence_requests),
        }


# ── Errors and helpers ───────────────────────────────────────────────────────

class ContractValidationError(ValueError):
    """Raised by normalize_contract(); carries a short machine code for observability."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _validation_code_from_pydantic(exc: Exception) -> str:
    msg = str(exc).lower()
    if "extra" in msg or "additional" in msg or "unknown" in msg:
        return "unknown_field"
    if "mode" in msg:
        return "invalid_mode"
    if "contract_version" in msg:
        return "invalid_contract_version"
    return "contract_validation_failed"


def normalize_contract(raw: Any) -> DmTurnContractV1:
    """Parse and normalize a raw contract dict.

    - Enforces ``contract_version == dm_turn_contract_v1`` (single canonical field).
    - Strictly rejects unknown fields (fail-closed).
    - Applies per-mode structural validation.
    - Fills normalization defaults (e.g., unknown truth → safe private_context).
    - Version dispatch hook for future ``dm_turn_contract_v2`` etc.
    """
    if not isinstance(raw, dict):
        raise ContractValidationError("not_an_object", "Contract must be a JSON object")

    version = raw.get("contract_version")
    if version is None:
        raise ContractValidationError("missing_contract_version", "contract_version is required (dm_turn_contract_v1)")
    if version != CONTRACT_VERSION:
        raise ContractValidationError(
            "invalid_contract_version",
            f"Unsupported contract_version {version!r}; expected {CONTRACT_VERSION}",
            details={"got": version, "expected": CONTRACT_VERSION},
        )

    try:
        contract = DmTurnContractV1.model_validate(raw)
    except Exception as exc:
        code = _validation_code_from_pydantic(exc)
        # Preserve pydantic error shape for debugging but surface a stable code
        details: dict[str, Any] = {"pydantic_error": str(exc)}
        # Try to extract pydantic error list
        try:
            from pydantic import ValidationError as PydanticValidationError
            if isinstance(exc, PydanticValidationError):
                details["errors"] = exc.errors()
        except Exception:
            pass
        raise ContractValidationError(code, str(exc), details=details) from exc

    # Normalization: fill safe dm_private_context for unknown truth if missing
    for beat in contract.beats:
        if beat.type == "npc_dialogue" and beat.truth_status == "unknown":
            if not beat.dm_private_context or not beat.dm_private_context.strip():
                beat.dm_private_context = "Not established in private canon; do not treat the utterance as authoritative."

    return contract


def parse_contract(raw: Any) -> DmTurnContractV1:
    """Alias for normalize_contract for callers that prefer parse_* naming."""
    return normalize_contract(raw)


def public_projection(contract: DmTurnContractV1) -> dict[str, Any]:
    """Deterministic allowlisted public / audience-safe projection.

    Only fields explicitly intended for the player-facing narration/expander
    lane are included. Everything else — adjudication input, staged effects,
    evidence requests, new-entity proposals, private claims, private
    beat context, provenance lanes, and private roll thresholds — is removed
    wholesale so hidden canon cannot leak.

    Invariant: any ``Claim.visibility == \"dm_private\"`` or any internal
    payload containing a sentinel secret will be absent from ``json.dumps``
    of the returned dict (see sentinel-secret test).
    """
    result: dict[str, Any] = {
        "contract_version": contract.contract_version,
        "mode": contract.mode,
    }

    # Audience-visible intent / hints (mode-specific)
    if contract.mode == "table_chat" and contract.table_chat_intent is not None:
        result["table_chat_intent"] = contract.table_chat_intent
    if contract.mode == "need_evidence" and contract.safe_prelude is not None:
        result["safe_prelude"] = contract.safe_prelude
    if contract.mode == "clarify":
        if contract.clarify_question is not None:
            result["clarify_question"] = contract.clarify_question
        # open_player_choice is audience-visible in clarify as well
    if contract.open_player_choice is not None:
        result["open_player_choice"] = contract.open_player_choice
    if contract.narration_hints is not None:
        result["narration_hints"] = contract.narration_hints.model_dump(mode="json")

    # Beats — filtered to public claims only, with private beat fields removed
    public_beats: list[dict[str, Any]] = []
    for beat in contract.beats:
        public_claims: list[dict[str, Any]] = []
        for claim in beat.claims:
            if claim.visibility == "dm_private":
                continue
            pub_claim: dict[str, Any] = {
                "text": claim.text,
                "claim_kind": claim.claim_kind,
            }
            if claim.actor_ref is not None:
                pub_claim["actor_ref"] = claim.actor_ref.model_dump(mode="json")
            if claim.target_refs:
                pub_claim["target_refs"] = [r.model_dump(mode="json") for r in claim.target_refs]
            if claim.topic_refs:
                pub_claim["topic_refs"] = [r.model_dump(mode="json") for r in claim.topic_refs]
            if claim.location_ref is not None:
                pub_claim["location_ref"] = claim.location_ref.model_dump(mode="json")
            # roll_outcome linking id is player-visible; origin/provenance/evidence are internal
            if claim.claim_kind == "roll_outcome" and claim.roll_request_id:
                pub_claim["roll_request_id"] = claim.roll_request_id
            public_claims.append(pub_claim)
        if not public_claims:
            # Entire beat was private — drop deterministically
            continue
        pub_beat: dict[str, Any] = {
            "id": beat.id,
            "type": beat.type,
            "claims": public_claims,
        }
        if beat.speaker_ref is not None:
            pub_beat["speaker_ref"] = beat.speaker_ref.model_dump(mode="json")
        if beat.speaker_public_name is not None:
            pub_beat["speaker_public_name"] = beat.speaker_public_name
        if beat.delivery is not None:
            pub_beat["delivery"] = beat.delivery
        # truth_status / dm_private_context deliberately omitted
        public_beats.append(pub_beat)
    result["beats"] = public_beats

    # Roll request — allowlisted public fields only
    if contract.roll_request is not None:
        rr = contract.roll_request
        result["roll_request"] = {
            "request_id": rr.request_id,
            "roll_kind": rr.roll_kind,
            "ability_or_skill": rr.ability_or_skill,
            "label": rr.label,
            "advantage_state": rr.advantage_state,
            "reason_public": rr.reason_public,
        }
        # dc_private, character_id deliberately omitted

    # Explicitly omitted (internal, never audience-visible):
    # - reason, adjudication_input, new_entities, staged_effects,
    #   evidence_requests, and any dm_private payloads
    return result


def contract_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for dm_turn_contract_v1 (strict, no additionalProperties)."""
    return DmTurnContractV1.model_json_schema()


# For fixture/test helpers: alias version constant
DM_TURN_CONTRACT_V1 = CONTRACT_VERSION
