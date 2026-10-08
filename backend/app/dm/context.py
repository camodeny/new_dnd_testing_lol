"""Audience-aware forward-DM context assembly -- issue #202.

The forward model receives one versioned packet made from named authority lanes.
The packet is deliberately not a free-form prompt: every record has provenance,
an authorization scope, a use boundary, and deterministic budget behavior.

Current production tables provide turn identity, exact IC/OOC inputs, protected
PC ownership/state, ruleset identity, and recent committed domain events.  Other
authoritative readers (scene, canon, knowledge, evidence) can add
``ContextRecord`` objects without changing this contract.
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adventures.service import get_current_adventure
from app.dm.turns import DM_TURN_RESOLVED
from app.characters.service import latest_sheet
from app.combat.npc_turns import active_encounter_context, encounter_character_ids
from app.loot.service import loot_context
from app.submissions.service import DM_ONLY_SUBMISSION_SOURCES
from app.observability.tracing import structured_log
from app.rules.mechanics import get_character_mechanics_for_sheet
from app.schema import StrictModel
from app.world.clocks import dm_pressure_view
from app.world.identity import exact_identity
from app.world.knowledge import build_knowledge_visibility_values
from app.world.service import build_current_scene_context_record
from models.campaigns import Campaign
from models.campaigns import CampaignDomainEvent
from models.campaigns import CampaignMember
from models.characters import Character
from models.characters import Dnd5eCharacterSheet
from models.dm import DmTurn
from models.dm import DmTurnAttempt
from models.threads import CampaignThread
from models.threads import CampaignThreadMember
from models.threads import PlayerSubmission
from models.threads import PlayerSubmissionSegment

logger = logging.getLogger(__name__)

CONTEXT_VERSION = "forward_dm_context_v1"
DEFAULT_MAX_BYTES = 64_000
DEFAULT_MAX_TOKENS = 16_000


class LaneName(str, Enum):
    TURN_IDENTITY = "turn_identity"
    PLAYER_INPUTS = "player_inputs"
    PROTECTED_PCS = "protected_pcs"
    CURRENT_SCENE = "current_scene"
    ACTIVE_ADVENTURE = "active_adventure"
    PRESSURES = "pressures"
    CHARACTER_STATE = "character_state"
    RELEVANT_CANON = "relevant_canon_relations"
    KNOWLEDGE_VISIBILITY = "knowledge_visibility"
    RECENT_HISTORY = "recent_unprocessed_history"
    RECENT_CONVERSATION = "recent_conversation"
    REPAIR_DIRECTIVES = "repair_directives"
    CONTENT_BOUNDARIES = "content_boundaries"
    DIFFICULTY = "difficulty"
    RULESET_IDENTITY = "ruleset_identity"
    EVIDENCE_RESULTS = "evidence_results"


LANE_ORDER = tuple(LaneName)

#: Clock visibility -> context-record visibility; anything else stays DM-only.
_PRESSURE_VISIBILITY = {"public": "public", "campaign": "campaign"}
REQUIRED_LANES = {
    LaneName.TURN_IDENTITY,
    LaneName.PLAYER_INPUTS,
    LaneName.PROTECTED_PCS,
    LaneName.CURRENT_SCENE,
    LaneName.CHARACTER_STATE,
    LaneName.KNOWLEDGE_VISIBILITY,
    LaneName.CONTENT_BOUNDARIES,
    LaneName.DIFFICULTY,
    LaneName.RULESET_IDENTITY,
}


class SourceRef(StrictModel):
    """Stable source identity used by validators and audit tooling."""

    source_type: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=256)
    source_version: str = Field(min_length=1, max_length=128)
    campaign_revision: int | None = Field(default=None, ge=0)
    provenance: dict[str, Any] = Field(default_factory=dict)


class AuthorizationScope(StrictModel):
    """Where a source is authorized; absence of restrictions means campaign-wide."""

    campaign_id: str = Field(min_length=1, max_length=64)
    thread_ids: list[str] = Field(default_factory=list, max_length=32)
    user_ids: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("thread_ids", "user_ids")
    @classmethod
    def _canonical_ids(cls, values: list[str]) -> list[str]:
        if any(not str(value).strip() for value in values):
            raise ValueError("authorization ids must be non-empty")
        return sorted(set(str(value) for value in values))


class ContextRecord(StrictModel):
    record_id: str = Field(min_length=1, max_length=256)
    value: dict[str, Any]
    sources: list[SourceRef] = Field(min_length=1, max_length=32)
    authorization: AuthorizationScope
    visibility: Literal["public", "campaign", "private", "dm_only"] = "campaign"
    use: Literal["narration_eligible", "adjudication_only"] = "narration_eligible"
    required: bool = False
    priority: int = Field(default=50, ge=0, le=100)
    sort_key: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def _private_scope_is_explicit(self) -> "ContextRecord":
        if self.visibility == "private" and not (
            self.authorization.thread_ids or self.authorization.user_ids
        ):
            raise ValueError(
                "private records require thread_ids or user_ids authorization"
            )
        return self


class ContextLane(StrictModel):
    name: LaneName
    authority_status: Literal["authoritative", "not_applicable", "unavailable"]
    required: bool
    records: list[ContextRecord] = Field(default_factory=list)
    source_errors: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _record_ids_unique(self) -> "ContextLane":
        ids = [record.record_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError(f"record ids must be unique within lane {self.name.value}")
        return self


class ContextAudience(StrictModel):
    campaign_id: str
    thread_id: str
    audience: Literal["campaign", "private"]
    user_ids: list[str]

    @field_validator("user_ids")
    @classmethod
    def _sort_users(cls, values: list[str]) -> list[str]:
        return sorted(set(values))


class BudgetDecision(StrictModel):
    lane: LaneName
    record_id: str | None = None
    action: Literal["included", "omitted", "failed"]
    reason: str
    estimated_bytes: int = Field(ge=0)
    required: bool = False


class LaneMetric(StrictModel):
    lane: LaneName
    assembly_ms: float = Field(ge=0)
    bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    included_records: int = Field(ge=0)
    omitted_records: int = Field(ge=0)
    source_versions: list[str]


class ContextObservability(StrictModel):
    assembly_ms: float = Field(ge=0)
    serialized_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    lanes: list[LaneMetric]
    budget_decisions: list[BudgetDecision]
    retrieval_dependencies: list[str]


class ForwardDmContextPacket(StrictModel):
    context_version: Literal["forward_dm_context_v1"] = CONTEXT_VERSION
    audience: ContextAudience
    lanes: list[ContextLane]
    observability: ContextObservability

    @model_validator(mode="after")
    def _all_lanes_once_in_order(self) -> "ForwardDmContextPacket":
        names = [lane.name for lane in self.lanes]
        if names != list(LANE_ORDER):
            raise ValueError(
                "context packet must contain every named lane exactly once in canonical order"
            )
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """Model input without nondeterministic timing telemetry."""
        return {
            "context_version": self.context_version,
            "audience": self.audience.model_dump(mode="json"),
            "lanes": [lane.model_dump(mode="json") for lane in self.lanes],
        }

    def serialize_for_adjudication(self) -> str:
        """Deterministic compact JSON for the forward-DM adjudication call."""
        return _canonical_json(self.canonical_payload())

    def narration_projection(self) -> dict[str, Any]:
        """Audience-safe input for later narration; hidden/private truth is absent.

        Issue #207 will narrate from the validated structured turn projection.
        This method exists as a hard boundary so no caller can accidentally pass
        adjudication-only material into a public narration prompt.
        """
        lanes: list[dict[str, Any]] = []
        for lane in self.lanes:
            records = []
            for record in lane.records:
                if record.use != "narration_eligible" or record.visibility == "dm_only":
                    continue
                if (
                    record.visibility == "private"
                    and self.audience.audience != "private"
                ):
                    continue
                records.append(record.model_dump(mode="json"))
            lanes.append(
                {
                    "name": lane.name.value,
                    "authority_status": lane.authority_status,
                    "required": lane.required,
                    "records": records,
                    "source_errors": lane.source_errors,
                }
            )
        return {
            "context_version": self.context_version,
            "audience": self.audience.model_dump(mode="json"),
            "lanes": lanes,
        }

    def serialize_for_narration(self) -> str:
        return _canonical_json(self.narration_projection())

    def with_records(
        self,
        additions: Mapping[LaneName, Iterable[ContextRecord]],
        *,
        dependency: str,
        budget: ContextBudget | None = None,
        authoritative_lanes: Iterable[LaneName] = (),
    ) -> "ForwardDmContextPacket":
        """Rebuild this packet with ``additions`` appended to their lanes.

        The rebuild goes back through :func:`assemble_context_packet`, so
        appended records get the same authorization and budget enforcement
        as assembled ones. ``dependency`` is recorded in the retrieval
        dependencies; ``authoritative_lanes`` force those lanes' status.
        """
        records = {lane.name: list(lane.records) for lane in self.lanes}
        for name, extra in additions.items():
            records[name] = [*records.get(name, []), *extra]
        lane_status = {lane.name: lane.authority_status for lane in self.lanes}
        for name in authoritative_lanes:
            lane_status[name] = "authoritative"
        return assemble_context_packet(
            audience=self.audience,
            records=records,
            lane_status=lane_status,
            source_errors={lane.name: lane.source_errors for lane in self.lanes},
            budget=budget,
            retrieval_dependencies=[*self.observability.retrieval_dependencies, dependency],
        )

    def headroom_budget(self, extra_bytes: int, extra_tokens: int) -> ContextBudget:
        """Default budget, raised so this packet plus the headroom still fits."""
        return ContextBudget(
            max_bytes=max(DEFAULT_MAX_BYTES, self.observability.serialized_bytes + extra_bytes),
            max_tokens=max(DEFAULT_MAX_TOKENS, self.observability.estimated_tokens + extra_tokens),
        )


class ContextAssemblyError(RuntimeError):
    code = "context_assembly_failed"

    def __init__(self, message: str, *, decisions: list[BudgetDecision] | None = None):
        self.decisions = decisions or []
        super().__init__(message)


class MissingAuthoritativeContextError(ContextAssemblyError):
    code = "missing_authoritative_context"


class ContextAuthorizationError(ContextAssemblyError):
    code = "context_authorization_failed"


class ContextBudgetError(ContextAssemblyError):
    code = "required_context_exceeds_budget"


class ContextBudget(StrictModel):
    max_bytes: int = Field(default=DEFAULT_MAX_BYTES, ge=1024)
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=256)
    lane_max_bytes: dict[LaneName, int] = Field(default_factory=dict)

    @field_validator("lane_max_bytes")
    @classmethod
    def _positive_lane_limits(cls, values: dict[LaneName, int]) -> dict[LaneName, int]:
        if any(value < 256 for value in values.values()):
            raise ValueError("lane budgets must be at least 256 bytes")
        return values

    @property
    def effective_max_bytes(self) -> int:
        return min(self.max_bytes, self.max_tokens * 4)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _size(value: Any) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def _tokens(byte_count: int) -> int:
    return math.ceil(byte_count / 4)


def _record_order(record: ContextRecord) -> tuple[int, str, str]:
    return (-record.priority, record.sort_key, record.record_id)


def _authorized(record: ContextRecord, audience: ContextAudience) -> bool:
    scope = record.authorization
    if scope.campaign_id != audience.campaign_id:
        return False
    if scope.thread_ids and audience.thread_id not in scope.thread_ids:
        return False
    if scope.user_ids and not set(audience.user_ids).issubset(set(scope.user_ids)):
        return False
    if record.visibility == "private" and audience.audience != "private":
        return False
    return True


def _source_versions(records: Iterable[ContextRecord]) -> list[str]:
    return sorted(
        {
            f"{source.source_type}:{source.source_id}@{source.source_version}"
            for record in records
            for source in record.sources
        }
    )


def _lane_size(lane: ContextLane) -> int:
    return _size(lane.model_dump(mode="json"))


def _payload_size(audience: ContextAudience, lanes: list[ContextLane]) -> int:
    return _size(
        {
            "context_version": CONTEXT_VERSION,
            "audience": audience.model_dump(mode="json"),
            "lanes": [lane.model_dump(mode="json") for lane in lanes],
        }
    )


def assemble_context_packet(
    *,
    audience: ContextAudience,
    records: Mapping[LaneName | str, Iterable[ContextRecord]],
    lane_status: Mapping[
        LaneName | str, Literal["authoritative", "not_applicable", "unavailable"]
    ]
    | None = None,
    source_errors: Mapping[LaneName | str, Iterable[str]] | None = None,
    budget: ContextBudget | None = None,
    lane_assembly_ms: Mapping[LaneName | str, float] | None = None,
    retrieval_dependencies: Iterable[str] = (),
) -> ForwardDmContextPacket:
    """Validate authorization, apply deterministic budgets, and build a packet."""
    started = time.monotonic()
    budget = budget or ContextBudget()
    lane_status = lane_status or {}
    source_errors = source_errors or {}
    lane_assembly_ms = lane_assembly_ms or {}
    decisions: list[BudgetDecision] = []
    lanes: list[ContextLane] = []

    def lookup(mapping: Mapping, name: LaneName, default):
        return mapping.get(name, mapping.get(name.value, default))

    for name in LANE_ORDER:
        required_lane = name in REQUIRED_LANES
        status = lookup(lane_status, name, "authoritative")
        errors = sorted(set(str(error) for error in lookup(source_errors, name, ())))
        supplied = sorted(list(lookup(records, name, ())), key=_record_order)
        accepted: list[ContextRecord] = []
        for record in supplied:
            if _authorized(record, audience):
                accepted.append(record)
            elif record.required:
                decision = BudgetDecision(
                    lane=name,
                    record_id=record.record_id,
                    action="failed",
                    reason="required_record_not_authorized_for_attempt_audience",
                    estimated_bytes=_size(record.model_dump(mode="json")),
                    required=True,
                )
                raise ContextAuthorizationError(
                    f"Required {name.value} record {record.record_id!r} is outside the attempt audience",
                    decisions=[*decisions, decision],
                )
            else:
                decisions.append(
                    BudgetDecision(
                        lane=name,
                        record_id=record.record_id,
                        action="omitted",
                        reason="not_authorized_for_attempt_audience",
                        estimated_bytes=_size(record.model_dump(mode="json")),
                        required=False,
                    )
                )

        if required_lane and status == "unavailable":
            raise MissingAuthoritativeContextError(
                f"Required context lane {name.value!r} is unavailable: {', '.join(errors) or 'no source result'}",
                decisions=decisions,
            )
        lane = ContextLane(
            name=name,
            authority_status=status,
            required=required_lane,
            records=accepted,
            source_errors=errors,
        )

        lane_limit = budget.lane_max_bytes.get(name)
        if lane_limit is not None:
            while _lane_size(lane) > lane_limit:
                removable = [record for record in lane.records if not record.required]
                if not removable:
                    decision = BudgetDecision(
                        lane=name,
                        action="failed",
                        reason="required_lane_exceeds_lane_budget",
                        estimated_bytes=_lane_size(lane),
                        required=True,
                    )
                    raise ContextBudgetError(
                        f"Required authority in lane {name.value!r} exceeds its {lane_limit}-byte budget",
                        decisions=[*decisions, decision],
                    )
                victim = sorted(removable, key=_record_order, reverse=True)[0]
                lane.records.remove(victim)
                decisions.append(
                    BudgetDecision(
                        lane=name,
                        record_id=victim.record_id,
                        action="omitted",
                        reason="lane_budget_pressure",
                        estimated_bytes=_size(victim.model_dump(mode="json")),
                        required=False,
                    )
                )
        lanes.append(lane)

    # Remove lowest-priority optional records until the exact canonical payload fits.
    while _payload_size(audience, lanes) > budget.effective_max_bytes:
        candidates = [
            (lane, record)
            for lane in lanes
            for record in lane.records
            if not record.required
        ]
        if not candidates:
            actual = _payload_size(audience, lanes)
            decision = BudgetDecision(
                lane=LaneName.TURN_IDENTITY,
                action="failed",
                reason="required_packet_exceeds_total_budget",
                estimated_bytes=actual,
                required=True,
            )
            raise ContextBudgetError(
                f"Required authoritative context is {actual} bytes, above {budget.effective_max_bytes}-byte budget",
                decisions=[*decisions, decision],
            )
        lane, victim = min(
            candidates,
            key=lambda pair: (
                pair[1].priority,
                -LANE_ORDER.index(pair[0].name),
                pair[1].sort_key,
                pair[1].record_id,
            ),
        )
        lane.records.remove(victim)
        decisions.append(
            BudgetDecision(
                lane=lane.name,
                record_id=victim.record_id,
                action="omitted",
                reason="total_budget_pressure",
                estimated_bytes=_size(victim.model_dump(mode="json")),
                required=False,
            )
        )

    for lane in lanes:
        for record in lane.records:
            decisions.append(
                BudgetDecision(
                    lane=lane.name,
                    record_id=record.record_id,
                    action="included",
                    reason="required_authority" if record.required else "within_budget",
                    estimated_bytes=_size(record.model_dump(mode="json")),
                    required=record.required,
                )
            )

    serialized_bytes = _payload_size(audience, lanes)
    metrics = [
        LaneMetric(
            lane=lane.name,
            assembly_ms=max(0.0, float(lookup(lane_assembly_ms, lane.name, 0.0))),
            bytes=_lane_size(lane),
            estimated_tokens=_tokens(_lane_size(lane)),
            included_records=len(lane.records),
            omitted_records=sum(
                1 for d in decisions if d.lane == lane.name and d.action == "omitted"
            ),
            source_versions=_source_versions(lane.records),
        )
        for lane in lanes
    ]
    packet = ForwardDmContextPacket(
        audience=audience,
        lanes=lanes,
        observability=ContextObservability(
            assembly_ms=(time.monotonic() - started) * 1000,
            serialized_bytes=serialized_bytes,
            estimated_tokens=_tokens(serialized_bytes),
            lanes=metrics,
            budget_decisions=decisions,
            retrieval_dependencies=sorted(set(retrieval_dependencies)),
        ),
    )
    structured_log(
        logger,
        logging.INFO,
        "forward_dm_context_assembled",
        campaign_id=audience.campaign_id,
        thread_id=audience.thread_id,
        audience=audience.audience,
        assembly_ms=round(packet.observability.assembly_ms, 3),
        serialized_bytes=serialized_bytes,
        estimated_tokens=packet.observability.estimated_tokens,
        lane_bytes={metric.lane.value: metric.bytes for metric in metrics},
        omitted=[
            f"{d.lane.value}:{d.record_id}" for d in decisions if d.action == "omitted"
        ],
        source_versions=sorted(
            {version for metric in metrics for version in metric.source_versions}
        ),
        retrieval_dependencies=packet.observability.retrieval_dependencies,
    )
    return packet


def _scope(
    campaign_id: uuid.UUID,
    *,
    thread_ids: Iterable[str] = (),
    user_ids: Iterable[str] = (),
) -> AuthorizationScope:
    return AuthorizationScope(
        campaign_id=str(campaign_id),
        thread_ids=list(thread_ids),
        user_ids=list(user_ids),
    )


def _source(
    source_type: str, source_id: Any, version: Any, revision: int | None, **provenance
) -> SourceRef:
    return SourceRef(
        source_type=source_type,
        source_id=str(source_id),
        source_version=str(version),
        campaign_revision=revision,
        provenance=provenance,
    )


def _history_record(
    campaign_id: uuid.UUID,
    event: CampaignDomainEvent,
    *,
    required: bool = False,
    priority: int = 80,
    post_turn_processed_through: int | None = None,
) -> ContextRecord | None:
    """Build one RECENT_HISTORY record for a committed domain event.

    Returns None when the event cannot be safely scoped (private without an
    explicit thread scope) — it is never widened. When
    ``post_turn_processed_through`` is given and the event sequence trails
    past it, the record carries an additive ``post_turn`` marker exposing
    the outstanding range (issue #222) so the forward DM reasons from
    accumulated unprocessed history instead of claiming an unaware state.
    The marker carries only sequence numbers — never private payloads —
    and per-event visibility/authorization still governs who may see it.
    """
    visibility = (
        event.visibility
        if event.visibility in {"public", "campaign", "private", "dm_only"}
        else "dm_only"
    )
    event_thread = None
    for container in (event.payload, event.provenance):
        if isinstance(container, dict) and container.get("thread_id"):
            event_thread = str(container["thread_id"])
            break
    # A private event without an explicit scope cannot be safely widened.
    if visibility == "private" and event_thread is None:
        return None
    value: dict[str, Any] = {
        "event_id": str(event.id),
        "sequence": event.sequence,
        "event_type": event.event_type,
        "payload": event.payload,
        "targets": event.targets,
    }
    if (
        post_turn_processed_through is not None
        and int(event.sequence or 0) > int(post_turn_processed_through)
    ):
        value["post_turn"] = {
            "processed": False,
            "processed_through": int(post_turn_processed_through),
        }
    return ContextRecord(
        record_id=f"domain-event:{event.id}",
        required=required,
        priority=priority,
        sort_key=f"{event.sequence:020d}",
        value=value,
        sources=[
            _source(
                "campaign_domain_event",
                event.id,
                event.sequence,
                event.sequence,
                operation_id=event.operation_id,
                trace_id=event.trace_id,
                upstream_provenance=event.provenance or {},
            )
        ],
        authorization=_scope(
            campaign_id, thread_ids=[event_thread] if event_thread else []
        ),
        visibility=visibility,  # type: ignore[arg-type]
        use="adjudication_only"
        if visibility == "dm_only"
        else "narration_eligible",
    )


def _history_record_for_audience(
    campaign_id: uuid.UUID,
    event: CampaignDomainEvent,
    audience: ContextAudience,
    *,
    required: bool = False,
    priority: int = 80,
    post_turn_processed_through: int | None = None,
) -> ContextRecord | None:
    """Return history only when its event scope matches this attempt.

    The campaign event stream contains events from every thread. Events that
    belong to another private thread are expected to be omitted from this
    attempt's history; passing them onward as required records would turn
    normal audience filtering into an authorization failure.
    """
    record = _history_record(
        campaign_id,
        event,
        required=required,
        priority=priority,
        post_turn_processed_through=post_turn_processed_through,
    )
    if record is None:
        return None
    # Cross-thread private events are ordinary audience filtering. Leave
    # other required records untouched so packet validation still fails
    # closed when authoritative history cannot be authorized for the attempt.
    if record.visibility == "private" and not _authorized(record, audience):
        return None
    return record


def _processed_through_sequence(db: Session, campaign_id: uuid.UUID) -> int:
    """Read-only post-turn checkpoint position (issue #222).

    Never creates a row: assembly is a read path, and a missing checkpoint
    simply means nothing has been processed yet.
    """
    try:
        from models.post_turn import PostTurnCheckpoint
    except (ImportError, AttributeError):
        return 0
    try:
        row = db.get(PostTurnCheckpoint, campaign_id)
    except Exception:
        return 0
    if row is None:
        return 0
    try:
        return int(row.processed_through_sequence or 0)
    except (TypeError, ValueError):
        return 0


def _roll_outcome(roll_kind, dc, fulfillment) -> dict | None:
    """Code-owned result of a fulfilled roll against its DC (meet or beat wins).

    Stated in the evidence so the DM resolves the intent from it instead of
    re-deriving (or re-requesting) the roll. None when there is nothing to
    compare (no DC, no numeric total, or an uncompared kind like initiative).
    """
    if not isinstance(fulfillment, dict):
        return None
    resolution = fulfillment.get("resolution") if isinstance(fulfillment.get("resolution"), dict) else None
    if roll_kind == "attack" and resolution:
        # Issue #234 — code compared the roll against the target's AC; the
        # AC itself is not evidence the DM needs.
        hit = resolution.get("outcome") in ("hit", "critical")
        return {
            "result": "hit" if hit else "miss",
            "critical": bool(resolution.get("is_critical")),
            "next": (
                "request a damage roll (roll_kind damage, attack_request_id = this request_key); "
                "code supplies the dice and applies the damage"
                if hit else "the attack misses: narrate it, no damage"
            ),
        }
    if roll_kind == "damage" and resolution:
        damage = resolution.get("damage") or {}
        return {
            "result": "damage",
            "damage_total": damage.get("final_total"),
            "damage_type": damage.get("damage_type"),
            "next": "code applies this damage when the turn commits and states it in your outcome beat; narrate it",
        }
    if roll_kind not in ("check", "save", "ability"):
        return None
    total = fulfillment.get("total")
    if isinstance(dc, bool) or isinstance(total, bool) or not isinstance(dc, int) or not isinstance(total, int):
        return None
    return {"result": "success" if total >= dc else "failure", "margin": total - dc}


#: Recently active NPCs preloaded into the knowledge lane per attempt.
RECENTLY_ACTIVE_NPC_LIMIT = 8


def _recently_active_npc_ids(
    db: Session,
    campaign_id: uuid.UUID,
    turn_attempt_ids: list[str],
    *,
    limit: int = RECENTLY_ACTIVE_NPC_LIMIT,
) -> list[str]:
    """Live NPC entity IDs active in the given resolved turns, most recent first.

    Active means introduced by the turn (``source_attempt_id``) or referenced
    as speaker, actor, target, or topic in its committed contract. Only
    current (non-superseded) NPC entities of this campaign count.
    """
    from models.world import WorldEntity

    attempt_ids: list[uuid.UUID] = []
    for value in turn_attempt_ids:
        try:
            attempt_ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    if not attempt_ids or limit <= 0:
        return []
    snapshots = dict(
        db.execute(
            select(DmTurnAttempt.id, DmTurnAttempt.contract_snapshot).where(
                DmTurnAttempt.id.in_(attempt_ids),
                DmTurnAttempt.campaign_id == campaign_id,
            )
        ).all()
    )
    introduced: dict[uuid.UUID, list[str]] = {}
    for entity_id, source_attempt_id in db.execute(
        select(WorldEntity.id, WorldEntity.source_attempt_id)
        .where(
            WorldEntity.campaign_id == campaign_id,
            WorldEntity.source_attempt_id.in_(attempt_ids),
            WorldEntity.entity_type == "npc",
        )
        .order_by(WorldEntity.created_at.asc())
    ).all():
        introduced.setdefault(source_attempt_id, []).append(str(entity_id))

    candidates: list[str] = []
    for attempt_id in attempt_ids:
        refs = list(introduced.get(attempt_id, []))
        for beat in (snapshots.get(attempt_id) or {}).get("beats") or []:
            if not isinstance(beat, dict):
                continue
            refs.append(beat.get("speaker_ref"))
            for claim in beat.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                refs.append(claim.get("actor_ref"))
                refs.extend(claim.get("target_refs") or [])
                refs.extend(claim.get("topic_refs") or [])
        for ref in refs:
            if isinstance(ref, dict):
                if ref.get("type") != "npc":
                    continue
                ref = ref.get("id")
            token = str(ref or "").strip()
            if token and token not in candidates:
                candidates.append(token)

    candidate_ids: list[uuid.UUID] = []
    for token in candidates:
        try:
            candidate_ids.append(uuid.UUID(token))
        except ValueError:
            continue
    if not candidate_ids:
        return []
    live = {
        str(entity_id)
        for entity_id in db.scalars(
            select(WorldEntity.id).where(
                WorldEntity.id.in_(candidate_ids),
                WorldEntity.campaign_id == campaign_id,
                WorldEntity.entity_type == "npc",
                WorldEntity.superseded_by_id.is_(None),
            )
        ).all()
    }
    return [token for token in candidates if token in live][:limit]


def _audience_for_attempt(
    db: Session, campaign: Campaign, turn: DmTurnAttempt
) -> ContextAudience:
    thread = db.get(CampaignThread, uuid.UUID(turn.thread_id))
    if thread is None or thread.campaign_id != campaign.id:
        raise ContextAuthorizationError(
            "Attempt thread is missing or belongs to another campaign"
        )
    if turn.audience != thread.thread_type:
        raise ContextAuthorizationError(
            "Attempt audience does not match its authoritative thread type"
        )
    if thread.thread_type == "private":
        user_ids = list(
            db.scalars(
                select(CampaignThreadMember.user_id).where(
                    CampaignThreadMember.thread_id == thread.id
                )
            ).all()
        )
    else:
        user_ids = list(
            db.scalars(
                select(CampaignMember.user_id).where(
                    CampaignMember.campaign_id == campaign.id
                )
            ).all()
        )
        user_ids.append(campaign.owner_id)
    return ContextAudience(
        campaign_id=str(campaign.id),
        thread_id=str(thread.id),
        audience=thread.thread_type,
        user_ids=[str(user_id) for user_id in user_ids],
    )


def _sheet_value(sheet: Dnd5eCharacterSheet) -> dict[str, Any]:
    """Bounded rules-relevant state, excluding biography/notes and other prompt bloat.

    Additive enrichment (issue #224): includes deterministic mechanics derived
    from the sheet via app.rules.mechanics — always backward-compatible (existing
    keys unchanged). If mechanics derivation fails, the error is surfaced in
    ``mechanics_error`` rather than inventing fallback stats.
    """
    base: dict[str, Any] = {
        "armor_class": sheet.armor_class,
        "abilities": {
            name: getattr(sheet, name)
            for name in (
                "strength",
                "dexterity",
                "constitution",
                "intelligence",
                "wisdom",
                "charisma",
            )
        },
        "conditions": sheet.conditions or [],
        "death_saves": {
            "successes": sheet.death_save_successes,
            "failures": sheet.death_save_failures,
        },
        "exhaustion_level": sheet.exhaustion_level,
        "hit_points": {
            "current": sheet.hit_points_current,
            "maximum": sheet.hit_points_max,
            "temporary": sheet.hit_points_temp,
        },
        "initiative_bonus": sheet.initiative_bonus,
        "level": sheet.level,
        "passive_perception": sheet.passive_perception,
        "proficiency_bonus": sheet.proficiency_bonus,
        "resources": sheet.resources or [],
        "saving_throws": sheet.saving_throws or [],
        "skills": sheet.skills or [],
        "speed": sheet.speed,
        "spell_slots": sheet.spell_slots or {},
    }
    # Additive #224 mechanics — try pure derivation, surface error without blocking lane
    try:
        m = get_character_mechanics_for_sheet(sheet)
        # Keep payload bounded: include deterministic derived views gameplay needs,
        # plus provenance/version for evidence. Full DTO available via evidence tool.
        base["mechanics"] = {
            "model_version": m.meta.model_version,
            "rules_revision": m.meta.rules_revision,
            "ability_modifiers": {k: v.modifier for k, v in m.abilities.items()},
            "proficiency": m.proficiency.model_dump(mode="json"),
            "saves": {k: v.model_dump(mode="json") for k, v in m.saves.items()},
            "skills": {k: v.model_dump(mode="json") for k, v in m.skills.items()},
            "passive": {k: v.model_dump(mode="json") for k, v in m.passive.items()},
            "initiative": m.combat.get("initiative"),
            "spellcasting": m.spellcasting.model_dump(mode="json"),
            "validation": m.validation.model_dump(mode="json"),
            "provenance": m.provenance,
        }
        # Expose explicit derived passive for direct lane consumers (no alias knowledge needed)
        base["passive_perception_derived"] = m.passive["perception"].derived
        base["passive_perception_effective"] = m.passive["perception"].effective
    except Exception as exc:  # noqa: BLE001
        # Fail-closed: surface error, do not invent stats
        code = getattr(exc, "code", "mechanics_error")
        base["mechanics_error"] = {"code": code, "message": str(exc)[:400], "field": getattr(exc, "field", None)}
    return base


def assemble_attempt_context(
    db: Session,
    attempt_id: uuid.UUID,
    *,
    supplemental_records: Mapping[LaneName | str, Iterable[ContextRecord]]
    | None = None,
    supplemental_status: Mapping[
        LaneName | str, Literal["authoritative", "not_applicable", "unavailable"]
    ]
    | None = None,
    supplemental_errors: Mapping[LaneName | str, Iterable[str]] | None = None,
    budget: ContextBudget | None = None,
    recent_event_limit: int = 24,
) -> ForwardDmContextPacket:
    """Assemble an attempt from authoritative DB rows plus typed source adapters.

    This is a read-only operation. It requires the exact current attempt/input set
    from #200 and never accepts a caller-provided audience or campaign snapshot.
    """
    if recent_event_limit < 0 or recent_event_limit > 100:
        raise ValueError("recent_event_limit must be between 0 and 100")
    started = time.monotonic()
    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is None:
        raise MissingAuthoritativeContextError(f"DM attempt {attempt_id} not found")
    turn = db.get(DmTurn, attempt.turn_id)
    campaign = db.get(Campaign, attempt.campaign_id)
    if turn is None or campaign is None:
        raise MissingAuthoritativeContextError(
            "Attempt's turn or campaign authority is missing"
        )
    if (
        turn.campaign_id != attempt.campaign_id
        or turn.thread_id != attempt.thread_id
        or turn.audience != attempt.audience
        or turn.input_set_revision != attempt.input_set_revision
        or list(turn.submission_ids or []) != list(attempt.submission_ids or [])
        or turn.current_attempt_id != attempt.id
    ):
        raise MissingAuthoritativeContextError(
            "Attempt is stale or inconsistent with the current logical turn"
        )
    if campaign.revision != attempt.source_revision:
        raise MissingAuthoritativeContextError(
            f"Attempt source revision {attempt.source_revision} is stale; campaign is at {campaign.revision}"
        )
    audience = _audience_for_attempt(db, campaign, attempt)
    records: dict[LaneName, list[ContextRecord]] = {name: [] for name in LANE_ORDER}
    timings: dict[LaneName, float] = {}
    scope = _scope(campaign.id)

    lane_started = time.monotonic()
    records[LaneName.TURN_IDENTITY].append(
        ContextRecord(
            record_id=f"attempt:{attempt.id}",
            required=True,
            priority=100,
            value={
                "attempt_id": str(attempt.id),
                "turn_id": str(turn.id),
                "attempt_number": attempt.attempt_number,
                "input_set_revision": attempt.input_set_revision,
                "source_campaign_revision": attempt.source_revision,
                "submission_ids": list(attempt.submission_ids or []),
            },
            sources=[
                _source(
                    "dm_turn_attempt",
                    attempt.id,
                    attempt.input_set_revision,
                    attempt.source_revision,
                    turn_id=str(turn.id),
                    attempt_number=attempt.attempt_number,
                )
            ],
            authorization=scope,
            use="adjudication_only",
        )
    )
    timings[LaneName.TURN_IDENTITY] = (time.monotonic() - lane_started) * 1000

    lane_started = time.monotonic()
    submission_ids = [uuid.UUID(value) for value in attempt.submission_ids or []]
    submissions = (
        list(
            db.scalars(
                select(PlayerSubmission)
                .where(PlayerSubmission.id.in_(submission_ids))
                .order_by(PlayerSubmission.sequence)
            ).all()
        )
        if submission_ids
        else []
    )
    if [submission.id for submission in submissions] != submission_ids:
        raise MissingAuthoritativeContextError(
            "One or more exact attempt submissions are missing or out of order"
        )
    character_ids: set[uuid.UUID] = set()
    for submission in submissions:
        if (
            submission.campaign_id != campaign.id
            or submission.thread_id != attempt.thread_id
            or submission.audience != attempt.audience
        ):
            raise ContextAuthorizationError(
                f"Submission {submission.id} is outside the attempt audience"
            )
        segments = list(
            db.scalars(
                select(PlayerSubmissionSegment)
                .where(PlayerSubmissionSegment.submission_id == submission.id)
                .order_by(PlayerSubmissionSegment.position)
            ).all()
        )
        if not segments:
            raise MissingAuthoritativeContextError(
                f"Submission {submission.id} has no typed IC/OOC segments"
            )
        if [segment.position for segment in segments] != list(range(len(segments))):
            raise MissingAuthoritativeContextError(
                f"Submission {submission.id} segments are not contiguous"
            )
        if submission.character_id:
            character_ids.add(submission.character_id)
        records[LaneName.PLAYER_INPUTS].append(
            ContextRecord(
                record_id=f"submission:{submission.id}",
                required=True,
                priority=100,
                sort_key=f"{submission.sequence:020d}",
                value={
                    "submission_id": str(submission.id),
                    "sequence": submission.sequence,
                    "user_id": str(submission.user_id),
                    "character_id": str(submission.character_id)
                    if submission.character_id
                    else None,
                    "segments": [
                        {
                            "position": segment.position,
                            "segment_type": segment.segment_type,
                            "text": segment.text,
                        }
                        for segment in segments
                    ],
                },
                sources=[
                    _source(
                        "player_submission",
                        submission.id,
                        submission.sequence,
                        attempt.source_revision,
                        accepted_at=submission.accepted_at.isoformat()
                        if submission.accepted_at
                        else None,
                    )
                ],
                authorization=_scope(campaign.id, thread_ids=[attempt.thread_id]),
                visibility="private" if attempt.audience == "private" else "campaign",
                # System cues for the DM alone (#236 NPC turns) carry
                # internal ids and instructions, never narration material.
                use="adjudication_only" if submission.source in DM_ONLY_SUBMISSION_SOURCES else "narration_eligible",
            )
        )
    timings[LaneName.PLAYER_INPUTS] = (time.monotonic() - lane_started) * 1000

    lane_started = time.monotonic()
    # NPC turns (#236): the DM acts against the encounter's PCs even when no
    # player spoke this turn, so their sheets join the PC lanes (within the
    # attempt's audience).
    for character_id in encounter_character_ids(db, campaign.id, attempt.thread_id):
        character = db.get(Character, character_id)
        if character is not None and str(character.owner_id) in audience.user_ids:
            character_ids.add(character_id)
    for character_id in sorted(character_ids, key=str):
        character = db.get(Character, character_id)
        if character is None:
            raise MissingAuthoritativeContextError(
                f"Protected player character {character_id} is missing"
            )
        if str(character.owner_id) not in audience.user_ids:
            raise ContextAuthorizationError(
                f"Protected character {character_id} owner is outside the attempt audience"
            )
        records[LaneName.PROTECTED_PCS].append(
            ContextRecord(
                record_id=f"pc-control:{character.id}",
                required=True,
                priority=100,
                value={
                    "character_id": str(character.id),
                    "name": character.name,
                    "owner_user_id": str(character.owner_id),
                    "control_policy": "player_only",
                    "dm_may_not_choose_actions": True,
                },
                sources=[
                    _source(
                        "character",
                        character.id,
                        character.updated_at.isoformat(),
                        attempt.source_revision,
                        owner_id=str(character.owner_id),
                    )
                ],
                authorization=scope,
                use="adjudication_only",
            )
        )
        sheet = latest_sheet(db, character.id)
        if character.system == "dnd5e" and sheet is None:
            raise MissingAuthoritativeContextError(
                f"Relevant PC {character.id} has no authoritative D&D 5e sheet"
            )
        if sheet is not None:
            records[LaneName.CHARACTER_STATE].append(
                ContextRecord(
                    record_id=f"character-state:{character.id}",
                    required=True,
                    priority=100,
                    value={
                        "character_id": str(character.id),
                        "system": character.system,
                        "state": _sheet_value(sheet),
                    },
                    sources=[
                        _source(
                            "dnd5e_character_sheet",
                            sheet.id,
                            sheet.updated_at.isoformat(),
                            attempt.source_revision,
                            character_id=str(character.id),
                        )
                    ],
                    authorization=scope,
                    use="adjudication_only",
                )
            )
        ruleset_id = f"ruleset:{character.system}"
        existing_ruleset = next(
            (
                record
                for record in records[LaneName.RULESET_IDENTITY]
                if record.record_id == ruleset_id
            ),
            None,
        )
        ruleset_source = _source(
            "character",
            character.id,
            character.updated_at.isoformat(),
            attempt.source_revision,
        )
        if existing_ruleset is None:
            records[LaneName.RULESET_IDENTITY].append(
                ContextRecord(
                    record_id=ruleset_id,
                    required=True,
                    priority=100,
                    value={"system": character.system},
                    sources=[ruleset_source],
                    authorization=scope,
                    use="adjudication_only",
                )
            )
        else:
            existing_ruleset.sources.append(ruleset_source)
    timings[LaneName.PROTECTED_PCS] = (time.monotonic() - lane_started) * 1000
    timings[LaneName.CHARACTER_STATE] = timings[LaneName.PROTECTED_PCS]
    timings[LaneName.RULESET_IDENTITY] = timings[LaneName.PROTECTED_PCS]

    lane_started = time.monotonic()
    # Issue #222 — the forward DM must reason from ALL completed but
    # post-turn-unprocessed gameplay while lag is safe, not just the recent
    # window. Events already covered by the window below are never
    # duplicated; unprocessed events beyond the window are appended as
    # required gap-fill records so budget pressure fails closed
    # (ContextBudgetError) instead of silently dropping required history.
    # The execution gate (pause_if_backpressured) blocks before that in
    # production; private history keeps its scoped visibility either way.
    processed_through = _processed_through_sequence(db, campaign.id)
    # Resolved turns this audience can see, newest first: the source of
    # recently active NPCs for the knowledge lane below.
    visible_turn_attempt_ids: list[str] = []
    if recent_event_limit:
        recent_events = list(
            db.scalars(
                select(CampaignDomainEvent)
                .where(
                    CampaignDomainEvent.campaign_id == campaign.id,
                    CampaignDomainEvent.sequence <= attempt.source_revision,
                )
                .order_by(CampaignDomainEvent.sequence.desc())
                .limit(recent_event_limit)
            ).all()
        )
        covered_ids: set[str] = set()
        for event in reversed(recent_events):
            # Events from other threads are still covered by this window,
            # even though their records are intentionally omitted below.
            covered_ids.add(str(event.id))
            # Issue #222 — unprocessed history is required wherever it is
            # found: a recent-window event past processed_through must fail
            # closed under budget pressure like gap-fill records, never be
            # silently dropped while the gate reports within_budget.
            record = _history_record_for_audience(
                campaign.id, event, audience,
                required=int(event.sequence or 0) > int(processed_through),
                post_turn_processed_through=processed_through,
            )
            if record is None:
                continue
            records[LaneName.RECENT_HISTORY].append(record)
            if event.event_type == DM_TURN_RESOLVED:
                turn_attempt_id = str((event.payload or {}).get("attempt_id") or "").strip()
                if turn_attempt_id:
                    visible_turn_attempt_ids.insert(0, turn_attempt_id)
        if processed_through < attempt.source_revision:
            gap_events = list(
                db.scalars(
                    select(CampaignDomainEvent)
                    .where(
                        CampaignDomainEvent.campaign_id == campaign.id,
                        CampaignDomainEvent.sequence > processed_through,
                        CampaignDomainEvent.sequence <= attempt.source_revision,
                    )
                    .order_by(CampaignDomainEvent.sequence.asc())
                ).all()
            )
            for event in gap_events:
                if str(event.id) in covered_ids:
                    continue
                record = _history_record_for_audience(
                    campaign.id, event, audience,
                    required=True,
                    priority=95,
                    post_turn_processed_through=processed_through,
                )
                if record is None:
                    continue
                records[LaneName.RECENT_HISTORY].append(record)
    timings[LaneName.RECENT_HISTORY] = (time.monotonic() - lane_started) * 1000

    lane_started = time.monotonic()
    # The visible chat the narrator already gets: domain events carry ids,
    # not words, so without it the DM adjudicates blind to what it just
    # narrated and what the player said a moment ago. Optional records, so
    # budget pressure drops the oldest messages first.
    from app.dm.narration import build_recent_conversation

    conversation = build_recent_conversation(
        db, campaign_id=campaign.id, thread_id=attempt.thread_id, audience=attempt.audience,
    )
    for position, message in enumerate(conversation):
        records[LaneName.RECENT_CONVERSATION].append(
            ContextRecord(
                record_id=f"conversation:{message['source_id']}",
                priority=70,
                sort_key=f"{position:04d}",
                value={
                    "speaker": message["speaker"],
                    "role": message["role"],
                    "text": message["text"],
                },
                sources=[
                    _source(
                        message["source_type"],
                        message["source_id"],
                        message["source_version"],
                        attempt.source_revision,
                    )
                ],
                authorization=_scope(campaign.id, thread_ids=[attempt.thread_id]),
                visibility="private" if attempt.audience == "private" else "campaign",
            )
        )
    timings[LaneName.RECENT_CONVERSATION] = (time.monotonic() - lane_started) * 1000

    # Populate authoritative campaign-level lanes directly from the campaign row.
    # These are read-only authoritative sources for difficulty / content boundaries.
    lane_started = time.monotonic()
    difficulty = campaign.difficulty
    if difficulty not in (None, ""):
        records[LaneName.DIFFICULTY].append(
            ContextRecord(
                record_id=f"campaign-difficulty:{campaign.id}",
                required=True,
                priority=90,
                value={"difficulty": str(difficulty)},
                sources=[
                    _source(
                        "campaign",
                        campaign.id,
                        campaign.revision,
                        campaign.revision,
                        field="difficulty",
                    )
                ],
                authorization=scope,
            )
        )
    # Loot (#463): loot mode, each PC's rarity ceiling, and ended encounters
    # whose loot the DM has yet to award or decline. DM-only.
    records[LaneName.DIFFICULTY].append(
        ContextRecord(
            record_id=f"loot-status:{campaign.id}",
            priority=60,
            value={"loot": loot_context(db, campaign, attempt.thread_id)},
            sources=[_source("campaign", campaign.id, campaign.revision, campaign.revision, field="loot_mode")],
            authorization=scope,
            visibility="dm_only",
            use="adjudication_only",
        )
    )
    timings[LaneName.DIFFICULTY] = (time.monotonic() - lane_started) * 1000

    lane_started = time.monotonic()
    content_boundaries: Any | None = campaign.content_boundaries
    if content_boundaries is not None:
        # Structural JSON already validated by campaign lifecycle (#240).
        records[LaneName.CONTENT_BOUNDARIES].append(
            ContextRecord(
                record_id=f"campaign-content-boundaries:{campaign.id}",
                required=True,
                priority=90,
                value={"content_boundaries": content_boundaries},
                sources=[
                    _source(
                        "campaign",
                        campaign.id,
                        campaign.revision,
                        campaign.revision,
                        field="content_boundaries",
                    )
                ],
                authorization=scope,
            )
        )
    timings[LaneName.CONTENT_BOUNDARIES] = (time.monotonic() - lane_started) * 1000

    # Authoritative current-scene lane (issue #209): answers current
    # location/time/present actors without parsing chat history. Absent
    # scene rows leave the lane empty so #202 fail-closed rules apply.
    lane_started = time.monotonic()
    # Source failure (malformed row, reader regression, DB error) raises:
    # fail closed — never silently convert to "no scene established".
    scene_value = build_current_scene_context_record(db, campaign)
    if scene_value is not None:
        scene_visibility = scene_value.get("visibility") or "campaign"
        if scene_visibility not in {"public", "campaign", "private", "dm_only"}:
            scene_visibility = "campaign"
        if scene_visibility == "private":
            # Campaign-level scene state has no thread/user recipient scope,
            # and ContextRecord rejects scope-less private records — so a
            # private current scene would make assembly raise. Project it as
            # dm_only/adjudication-only instead (consistent with the dm_only
            # handling below and the RECENT_HISTORY lane): assembly succeeds
            # and narration_projection() can never carry it to a player
            # audience.
            scene_visibility = "dm_only"
        records[LaneName.CURRENT_SCENE].append(
            ContextRecord(
                record_id=f"current-scene:{campaign.id}",
                required=True,
                priority=90,
                value=scene_value,
                sources=[
                    _source(
                        "campaign_current_scene",
                        campaign.id,
                        scene_value.get("revision", campaign.revision),
                        campaign.revision,
                        source_turn_id=scene_value.get("source_turn_id"),
                        source_attempt_id=scene_value.get("source_attempt_id"),
                    )
                ],
                authorization=scope,
                visibility=scene_visibility,  # type: ignore[arg-type]
                # Viewer-aware projection: dm_only scene truth is
                # adjudication-only so narration_projection() can never carry
                # it to a player audience (mirrors RECENT_HISTORY lane).
                use="adjudication_only" if scene_visibility == "dm_only" else "narration_eligible",
            )
        )
    # Active encounter (#236): whose turn it is, what they have left, and the
    # NPCs' stat-block attacks, so the DM can run NPC turns. DM-only and
    # adjudication-only: it carries hidden combatants and NPC stats.
    encounter_value = active_encounter_context(db, campaign.id, attempt.thread_id)
    if encounter_value is not None:
        records[LaneName.CURRENT_SCENE].append(
            ContextRecord(
                record_id=f"active-encounter:{encounter_value['encounter_id']}",
                priority=90,
                value={"active_encounter": encounter_value},
                sources=[
                    _source(
                        "encounter",
                        encounter_value["encounter_id"],
                        encounter_value["turn_sequence"],
                        campaign.revision,
                    )
                ],
                authorization=scope,
                visibility="dm_only",
                use="adjudication_only",
            )
        )
    timings[LaneName.CURRENT_SCENE] = (time.monotonic() - lane_started) * 1000

    # Active adventure (issue #458): lets the model see the arc it may close
    # via ``complete_adventure``. Optional lane -- a campaign between
    # adventures (or in an epilogue) legitimately has none. Adjudication-only:
    # the premise comes from owner-supplied adventure metadata, which is not
    # a player-safe projection.
    lane_started = time.monotonic()
    adventure = get_current_adventure(db, campaign.id)
    if adventure is not None:
        premise = (adventure.adventure_metadata or {}).get("premise")
        records[LaneName.ACTIVE_ADVENTURE].append(
            ContextRecord(
                record_id=f"active-adventure:{adventure.id}",
                required=False,
                priority=80,
                value={
                    "adventure_id": str(adventure.id),
                    "title": adventure.title,
                    "premise": premise[:1000] if isinstance(premise, str) and premise.strip() else None,
                    "status": adventure.status,
                },
                sources=[
                    _source(
                        "adventure",
                        adventure.id,
                        adventure.updated_at.isoformat() if adventure.updated_at else adventure.status,
                        attempt.source_revision,
                    )
                ],
                authorization=scope,
                use="adjudication_only",
            )
        )
    timings[LaneName.ACTIVE_ADVENTURE] = (time.monotonic() - lane_started) * 1000

    # Pressures: campaign clocks the forward DM plays to. Post-turn
    # evaluation advances them; this lane closes the loop by showing their
    # state and, when a stage was crossed or a clock finished since the last
    # DM turn, a directive to show it in the world. Directive records are
    # required (never budget-trimmed); state-only records are optional.
    lane_started = time.monotonic()
    for pressure in dm_pressure_view(
        db, campaign.id, through_sequence=attempt.source_revision, turn_event_type=DM_TURN_RESOLVED,
    ):
        owed = pressure["directive"] is not None
        records[LaneName.PRESSURES].append(
            ContextRecord(
                record_id=f"pressure:{pressure['clock_id']}",
                required=owed,
                priority=90 if owed else 70,
                value={k: v for k, v in pressure.items() if k != "visibility"},
                sources=[
                    _source(
                        "campaign_clock",
                        pressure["clock_id"],
                        str(pressure["evaluated_through_sequence"]),
                        attempt.source_revision,
                    )
                ],
                authorization=scope,
                visibility=_PRESSURE_VISIBILITY.get(pressure["visibility"], "dm_only"),  # type: ignore[arg-type]
                use="adjudication_only",
            )
        )
    timings[LaneName.PRESSURES] = (time.monotonic() - lane_started) * 1000

    # Complete entity registry (experiment): every live NPC/location-style
    # entity with id + name + one-line summary, so the adjudicator can
    # reference exact canonical IDs instead of proposing near-duplicate
    # new_entities that later DEFER in identity resolution. dm_only +
    # adjudication_only: never narrated, never player-visible. Low
    # priority so budget trimming drops it before authoritative lanes;
    # capped so large campaigns stay bounded.
    lane_started = time.monotonic()
    try:
        from models.world import WorldEntity as _RegistryEntity

        _registry_rows = list(db.execute(
            select(_RegistryEntity)
            .where(
                _RegistryEntity.campaign_id == campaign.id,
                _RegistryEntity.superseded_by_id.is_(None),
            )
            .order_by(_RegistryEntity.created_at.asc())
            .limit(200)
        ).scalars().all())
        _registry = [
            {
                "id": str(row.id),
                "name": row.name,
                "kind": row.entity_type,
                "summary": (str(row.summary or "")[:200] or None),
            }
            for row in _registry_rows
        ]
        records[LaneName.RELEVANT_CANON].append(
            ContextRecord(
                record_id=f"entity-registry:{campaign.id}",
                required=False,
                priority=10,
                value={"entities": _registry},
                sources=[
                    _source(
                        "world_entity",
                        campaign.id,
                        campaign.revision,
                        campaign.revision,
                        lane="relevant_canon_relations",
                    )
                ],
                authorization=scope,
                visibility="dm_only",  # type: ignore[arg-type]
                use="adjudication_only",
            )
        )
    except Exception as exc:
        logger.warning("entity registry record failed: %s", exc)
    timings[LaneName.RELEVANT_CANON] = (time.monotonic() - lane_started) * 1000

    # Per-subject fictional-knowledge lane (issue #251): DM-internal
    # perspective snapshots built from #211 WorldKnowledge, distinct from
    # objective truth and human disclosure. Covers acting PCs plus
    # scene-relevant non-player subjects (present actors carrying entity
    # IDs) so NPC perspectives are knowledge-checked too. dm_only +
    # adjudication_only so narration_projection() can never carry it to a
    # player audience. Source failure fails closed; unresolved subjects
    # yield explicit empty perspectives (never fabricated knowledge).
    lane_started = time.monotonic()
    scene_npc_ids: list[str] = []
    for scene_rec in records[LaneName.CURRENT_SCENE]:
        present = (scene_rec.value or {}).get("present_actors") or []
        for actor in present:
            if isinstance(actor, dict):
                eid = str(actor.get("entity_id") or "").strip()
                if eid and eid not in scene_npc_ids:
                    scene_npc_ids.append(eid)
            if len(scene_npc_ids) >= 32:
                break
    # Recently active NPCs carry perspectives up front too: an NPC who spoke,
    # acted, or was introduced in a recent visible turn is likely to act
    # again, and without a perspective its first claim fails closed and
    # costs a model retry. present_actors alone misses NPCs that were never
    # (re)registered in the scene.
    for eid in _recently_active_npc_ids(db, campaign.id, visible_turn_attempt_ids):
        if len(scene_npc_ids) >= 32:
            break
        if eid not in scene_npc_ids:
            scene_npc_ids.append(eid)
    knowledge_values = build_knowledge_visibility_values(
        db, campaign, sorted(character_ids, key=str),
        npc_entity_ids=scene_npc_ids,
    )
    for index, value in enumerate(knowledge_values):
        subject_ref = (
            value.get("character_id")
            or value.get("subject_entity_id")
            or f"no-subject-{index}"
        )
        sources = [
            _source(
                "character" if value.get("character_id") else "dm_turn_attempt",
                value.get("character_id") or attempt.id,
                attempt.source_revision,
                attempt.source_revision,
                lane="knowledge_visibility",
            )
        ]
        if value.get("subject_entity_id"):
            sources.append(
                _source(
                    "world_entity",
                    value["subject_entity_id"],
                    attempt.source_revision,
                    attempt.source_revision,
                    lane="knowledge_visibility",
                )
            )
        records[LaneName.KNOWLEDGE_VISIBILITY].append(
            ContextRecord(
                record_id=f"knowledge:{subject_ref}",
                required=False,
                priority=90,
                value=value,
                sources=sources,
                authorization=scope,
                visibility="dm_only",
                use="adjudication_only",
            )
        )
    timings[LaneName.KNOWLEDGE_VISIBILITY] = (time.monotonic() - lane_started) * 1000

    # Fulfilled player-roll evidence (issue #354): when this attempt resumes
    # the same logical turn after a roll fulfillment, project the authoritative
    # die result into the adjudication-only evidence lane. dm_only +
    # adjudication_only so narration_projection() can never leak totals, DCs,
    # or private fulfillments to a player audience.
    for index, item in enumerate(list(getattr(attempt, "roll_evidence", None) or [])):
        if not isinstance(item, dict):
            continue
        request_id = str(item.get("request_key") or item.get("id") or f"roll_{index}")
        fulfillment = item.get("fulfillment") if isinstance(item.get("fulfillment"), dict) else None
        version = (
            fulfillment.get("submitted_at") if fulfillment and fulfillment.get("submitted_at")
            else item.get("fulfilled_at") or item.get("requested_at") or "unknown"
        )
        records[LaneName.EVIDENCE_RESULTS].append(
            ContextRecord(
                record_id=f"roll_evidence:{request_id}",
                required=False,
                priority=85,
                value={
                    "request_key": request_id,
                    "roll_kind": item.get("roll_kind"),
                    "ability_or_skill": item.get("ability_or_skill"),
                    "label": item.get("label"),
                    "reason_public": item.get("reason_public"),
                    "dc_private": item.get("dc_private"),
                    "fulfillment": fulfillment,
                    "outcome": _roll_outcome(item.get("roll_kind"), item.get("dc_private"), fulfillment),
                },
                sources=[
                    _source(
                        "player_roll",
                        request_id,
                        version,
                        campaign.revision,
                    )
                ],
                authorization=scope,
                visibility="dm_only",  # type: ignore[arg-type]
                use="adjudication_only",
            )
        )

    supplemental_records = supplemental_records or {}
    for key, values in supplemental_records.items():
        name = key if isinstance(key, LaneName) else LaneName(key)
        records[name].extend(list(values))

    # Required lanes must fail closed when no authoritative source produced a
    # record.  A genuinely inapplicable concept must be explicitly declared via
    # supplemental_status (adapter evidence), not silently defaulted.
    statuses: dict[
        LaneName, Literal["authoritative", "not_applicable", "unavailable"]
    ] = {name: "authoritative" for name in LANE_ORDER}
    for name in REQUIRED_LANES:
        if not records[name]:
            statuses[name] = "unavailable"
    # Protected-PC lanes are only required when a submission actually references
    # a character. When no PC is relevant the lane is explicitly not applicable,
    # not missing authority -- this matches the "when relevant" wording of #202.
    if not character_ids:
        for pc_lane in (
            LaneName.PROTECTED_PCS,
            LaneName.CHARACTER_STATE,
            LaneName.RULESET_IDENTITY,
        ):
            if not records[pc_lane] and pc_lane not in (supplemental_status or {}):
                statuses[pc_lane] = "not_applicable"
    for key, value in (supplemental_status or {}).items():
        statuses[key if isinstance(key, LaneName) else LaneName(key)] = value

    packet = assemble_context_packet(
        audience=audience,
        records=records,
        lane_status=statuses,
        source_errors=supplemental_errors,
        budget=budget,
        lane_assembly_ms=timings,
        retrieval_dependencies=[
            "campaign",
            "campaign_thread_membership",
            "dm_turn",
            "dm_turn_attempt",
            "player_submissions",
            "player_submission_segments",
            "characters",
            "dnd5e_character_sheets",
            "campaign_domain_events",
            "post_turn_checkpoints",
            "campaign_current_scenes",
            "world_entities",
            "world_knowledge",
            "world_visibility_grants",
            "player_roll_requests",
            "player_roll_fulfillments",
        ],
    )
    # Include DB collection in total duration without contaminating deterministic payload.
    packet.observability.assembly_ms = (time.monotonic() - started) * 1000
    return packet


def repair_packet_missing_perspectives(
    packet: ForwardDmContextPacket,
    db: Session,
    campaign: Campaign,
    subject_ids: list[str],
) -> ForwardDmContextPacket | None:
    """Append knowledge-perspective records for deterministically known subjects.

    Issue #455 deeper fix: the ``knowledge_visibility`` lane is built from
    scene present-actors, so a recently introduced (or otherwise omitted) NPC
    has no perspective and their dialogue fails closed every regeneration.
    Each requested subject is resolved through deterministic identity only —
    stable UUID, exact alias, or unique exact name
    (``app.world.identity.exact_identity``); ``tmp_*`` references stay owned
    by identity deferral (#454) and unresolvable subjects are skipped. One
    record is added per resolved entity, keyed only by its canonical ID: a
    subject referenced by name stays unresolvable to the validator, and the
    directive tells the retrying model which exact ID to use instead.
    Returns None when nothing resolved (callers fall back to deterministic
    scope-narrowing). Never raises: repair is best-effort.
    """
    try:
        ordered = [str(s or "").strip() for s in (subject_ids or [])]
        ordered = [s for s in dict.fromkeys(ordered) if s][:8]
        if not ordered or packet is None:
            return None
        have: set[str] = set()
        knowledge_records: list[ContextRecord] = []
        for lane in packet.lanes or []:
            if lane.name == LaneName.KNOWLEDGE_VISIBILITY:
                knowledge_records = list(lane.records)
                for rec in knowledge_records:
                    value = rec.value or {}
                    for key in (value.get("character_id"), value.get("subject_entity_id")):
                        token = str(key or "").strip()
                        if token:
                            have.add(token)
        # reference the model used -> canonical entity ID
        canonical_by_actor: dict[str, str] = {}
        # entities that still need a perspective record, one per canonical ID
        missing: dict[str, Any] = {}
        for sid in ordered:
            if sid in have or sid.startswith("tmp_"):
                continue
            entity = exact_identity(db, campaign.id, sid)
            if entity is None or entity.campaign_id != campaign.id:
                continue
            canonical = str(entity.id)
            canonical_by_actor[sid] = canonical
            if canonical not in have:
                missing.setdefault(canonical, entity)
        if not canonical_by_actor:
            return None
        values = build_knowledge_visibility_values(
            db, campaign, [],
            npc_entity_ids=[entity.id for entity in missing.values()],
        ) if missing else []
        by_entity = {str(v.get("subject_entity_id")): v for v in (values or []) if isinstance(v, dict)}
        template_auth = (
            knowledge_records[0].authorization if knowledge_records
            else _scope(campaign.id, thread_ids=[packet.audience.thread_id])
        )
        revision = campaign.revision
        for rec in knowledge_records:
            for source in rec.sources or []:
                if source.campaign_revision is not None:
                    revision = source.campaign_revision
                    break
            if revision != campaign.revision:
                break
        new_records: list[ContextRecord] = []
        for canonical, entity in missing.items():
            value = by_entity.get(canonical)
            if not isinstance(value, dict) or not value.get("subject_resolved"):
                canonical_by_actor = {
                    actor: cid for actor, cid in canonical_by_actor.items() if cid != canonical
                }
                continue
            new_records.append(
                ContextRecord(
                    record_id=f"knowledge-repair:{entity.id}",
                    required=False,
                    priority=90,
                    value=dict(value),
                    sources=[
                        *_template_sources(knowledge_records),
                        _source(
                            "world_entity",
                            entity.id,
                            entity.revision,
                            revision,
                            lane="knowledge_visibility",
                        ),
                    ],
                    authorization=template_auth,
                    visibility="dm_only",  # type: ignore[arg-type]
                    use="adjudication_only",
                )
            )
        if not canonical_by_actor:
            return None
        mapping = ", ".join(f"{actor} -> {canonical}" for actor, canonical in canonical_by_actor.items())
        directive = ContextRecord(
            record_id=f"repair:perspective-{uuid.uuid4().hex[:8]}",
            value={
                "directive": (
                    "Knowledge perspectives resolved in the knowledge_visibility "
                    f"lane: {mapping}. Reference these NPCs by the exact ID on the "
                    "right in actor_ref, speaker_ref, and other refs, never by name. "
                    "They may speak only within their listed knowledge."
                ),
            },
            sources=[
                _source(
                    "knowledge_repair",
                    mapping,
                    "1",
                    revision,
                )
            ],
            authorization=template_auth,
            visibility="dm_only",  # type: ignore[arg-type]
            use="adjudication_only",
            required=False,
            priority=100,
        )
        return packet.with_records(
            {
                LaneName.KNOWLEDGE_VISIBILITY: new_records,
                LaneName.REPAIR_DIRECTIVES: [directive],
            },
            dependency="knowledge_perspective_repair",
            authoritative_lanes=[LaneName.REPAIR_DIRECTIVES],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge perspective repair unavailable: %s", exc)
        return None


def _template_sources(knowledge_records: list[ContextRecord]) -> list[SourceRef]:
    """Reuse the attempt-attribution source of a sibling knowledge record.

    Only the ``dm_turn_attempt`` source carries over: a sibling's
    ``character``/``world_entity`` sources describe that sibling's subject,
    not the repaired NPC.
    """
    for rec in knowledge_records or []:
        for source in rec.sources or []:
            if source.source_type == "dm_turn_attempt":
                return [source]
    return []


#: Bounds for the identity-deferral retry advisory: explicit-retry ancestry
#: walked, and deferral memos surfaced in one advisory.
_DEFERRAL_MAX_LEVELS = 5
_DEFERRAL_MAX_RECORDS = 3
IDENTITY_DEFERRAL_RECORD_ID = "identity-deferral:advisory"


def build_retry_deferral_advisory(db: Session, attempt: Any) -> str | None:
    """Advisory note from the attempt's own and abandoned-parent identity deferrals, or ``None``.

    Walks the explicit-retry parent chain (abandoned ``explicit_retry``
    attempts) collecting ``via == "deferred"`` identity-resolution memos left
    by :func:`app.world.identity.resolve_new_entity_identities_pre_narration`.
    Without this, deterministic adjudication would replay the identical frame
    into the same DEFER. Never raises: unreadable ancestry means no advisory,
    never a blocked turn. Memo content is DM-prompt-safe by construction
    (public proposal fields + canonical candidate labels only).
    """
    try:
        memos: list[dict[str, Any]] = []
        current = attempt
        # The attempt's own memo comes first: an in-attempt re-adjudication
        # after a deferral reads the memo the resolver just persisted.
        for item in getattr(attempt, "identity_resolutions", None) or []:
            if (
                isinstance(item, dict)
                and item.get("outcome") == "DEFER"
                and item.get("via") == "deferred"
            ):
                memos.append(item)
        for _ in range(_DEFERRAL_MAX_LEVELS):
            parent_id = getattr(current, "parent_attempt_id", None)
            if not parent_id:
                break
            parent = db.get(DmTurnAttempt, parent_id)
            if parent is None:
                break
            for item in getattr(parent, "identity_resolutions", None) or []:
                if (
                    isinstance(item, dict)
                    and item.get("outcome") == "DEFER"
                    and item.get("via") == "deferred"
                ):
                    memos.append(item)
            if not (
                getattr(parent, "status", None) == "abandoned"
                and (getattr(parent, "abandonment_reason", None) or "") == "explicit_retry"
            ):
                break
            current = parent
    except Exception:
        return None
    # Dedupe repeat deferrals of the same proposal across the chain.
    unique: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for memo in memos:
        proposal = memo.get("proposal") if isinstance(memo.get("proposal"), dict) else {}
        key = f"{memo.get('temp_id')}|{proposal.get('public_name')}|{proposal.get('kind')}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(memo)
        if len(unique) >= _DEFERRAL_MAX_RECORDS:
            break
    if not unique:
        return None
    parts: list[str] = []
    for memo in unique:
        proposal = memo.get("proposal") if isinstance(memo.get("proposal"), dict) else {}
        name = str(proposal.get("public_name") or memo.get("temp_id") or "the figure")[:120]
        kind = str(proposal.get("kind") or "entity")[:40]
        labels = [str(label)[:120] for label in (memo.get("candidate_labels") or [])][:5]
        parts.append(
            f"'{name}' ({kind})"
            + (f" resembled existing: {', '.join(labels)}" if labels else "")
        )
    note = (
        "A previous attempt could not determine whether "
        + "; ".join(parts)
        + " is an already-established entity or someone new, so the turn could not "
        "proceed. If it is an established canonical entity, reference it by exact "
        "canonical name or alias in entity references instead of proposing a new "
        "entity. If it is genuinely new, describe it with distinguishing detail "
        "(appearance, role, location, group affiliation) so it cannot be confused "
        "with an existing entity. If its true identity is still hidden (masked, "
        "hooded, unseen), keep proposing it as a new entity named for what the "
        "party perceives; do not drop it from the turn. This note is private "
        "adjudication context: never narrate it or what it implies about who "
        "the figure is."
    )
    return note[:2000]


def attach_retry_deferral_advisory(
    packet: ForwardDmContextPacket, note: str
) -> ForwardDmContextPacket:
    """Return a copy of ``packet`` with the identity-deferral advisory attached.

    One ``adjudication_only`` record in the player-inputs lane (idempotent by
    record ID), so it never reaches narration. It inherits the packet
    audience: on a private turn it stays thread-scoped and private.
    """
    audience = packet.audience
    inputs = next(lane for lane in packet.lanes if lane.name == LaneName.PLAYER_INPUTS)
    if any(r.record_id == IDENTITY_DEFERRAL_RECORD_ID for r in inputs.records):
        return packet
    private = str(audience.audience or "campaign") == "private"
    record = ContextRecord(
        record_id=IDENTITY_DEFERRAL_RECORD_ID,
        value={
            "label": "A previous attempt deferred an entity identity — disambiguate",
            "note": note,
            "authority": (
                "advisory only: the adjudicator weighs this prior against "
                "the full authoritative context and is not bound by it"
            ),
        },
        sources=[
            SourceRef(
                source_type="identity_deferral",
                source_id="identity-deferral",
                source_version="1",
                campaign_revision=None,
            )
        ],
        authorization=AuthorizationScope(
            campaign_id=str(audience.campaign_id),
            thread_ids=[str(audience.thread_id)],
            user_ids=[],
        ),
        visibility="private" if private else "campaign",  # type: ignore[arg-type]
        use="adjudication_only",
        required=False,
        priority=5,
    )
    return packet.with_records(
        {LaneName.PLAYER_INPUTS: [record]},
        dependency="identity_deferral",
        budget=packet.headroom_budget(4096, 1024),
    )
