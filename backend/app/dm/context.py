"""Audience-aware forward-DM context assembly -- issue #202.

The forward model receives one versioned packet made from named authority lanes.
The packet is deliberately not a free-form prompt: every record has provenance,
an authorization scope, a use boundary, and deterministic budget behavior.

Current production tables provide turn identity, exact IC/OOC inputs, protected
PC ownership/state, ruleset identity, and recent committed domain events.  Other
authoritative readers (scene, combat, canon, clocks, repair, policy) can add
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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LaneName(str, Enum):
    TURN_IDENTITY = "turn_identity"
    PLAYER_INPUTS = "player_inputs"
    PROTECTED_PCS = "protected_pcs"
    CURRENT_SCENE = "current_scene"
    CHARACTER_STATE = "character_state"
    COMBAT_HOOKS = "combat_hooks"
    RELEVANT_CANON = "relevant_canon_relations"
    KNOWLEDGE_VISIBILITY = "knowledge_visibility"
    CLOCKS_PRESSURES = "clocks_pressures"
    RECENT_HISTORY = "recent_unprocessed_history"
    REPAIR_DIRECTIVES = "repair_directives"
    CONTENT_BOUNDARIES = "content_boundaries"
    DIFFICULTY = "difficulty"
    RULESET_IDENTITY = "ruleset_identity"
    EVIDENCE_RESULTS = "evidence_results"


LANE_ORDER = tuple(LaneName)
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
        from app.rules.mechanics import get_character_mechanics_for_sheet

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


def is_missing_current_scene_table_error(exc: BaseException) -> bool:
    """Strict rollout predicate: the ``campaign_current_scenes`` relation itself is absent.

    Only two exact cases qualify:
    - SQLite: ``no such table: campaign_current_scenes``.
    - PostgreSQL: SQLSTATE 42P01 (UndefinedTable) mentioning the relation.
    Every other DB error (permission denied, missing column, malformed data,
    etc.) must stay fail-closed even when the statement text names the table.
    """
    from sqlalchemy.exc import SQLAlchemyError

    if not isinstance(exc, SQLAlchemyError):
        return False
    msg = str(exc).lower()
    if "campaign_current_scenes" not in msg:
        return False
    if "no such table: campaign_current_scenes" in msg:
        return True
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        cause = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if isinstance(cause, BaseException):
            current = cause
        else:
            current = None
    orig = getattr(exc, "orig", None)
    if isinstance(orig, BaseException) and id(orig) not in seen:
        chain.append(orig)
    for err in chain:
        sqlstate = getattr(err, "sqlstate", None) or getattr(err, "pgcode", None)
        if sqlstate == "42P01":
            return True
        if type(err).__name__ == "UndefinedTable":
            return True
    return False


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
            )
        )
    timings[LaneName.PLAYER_INPUTS] = (time.monotonic() - lane_started) * 1000

    lane_started = time.monotonic()
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
        sheet = db.scalar(
            select(Dnd5eCharacterSheet).where(
                Dnd5eCharacterSheet.character_id == character.id
            )
        )
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
        for event in reversed(recent_events):
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
                continue
            records[LaneName.RECENT_HISTORY].append(
                ContextRecord(
                    record_id=f"domain-event:{event.id}",
                    priority=80,
                    sort_key=f"{event.sequence:020d}",
                    value={
                        "event_id": str(event.id),
                        "sequence": event.sequence,
                        "event_type": event.event_type,
                        "payload": event.payload,
                        "targets": event.targets,
                    },
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
                        campaign.id, thread_ids=[event_thread] if event_thread else []
                    ),
                    visibility=visibility,
                    use="adjudication_only"
                    if visibility == "dm_only"
                    else "narration_eligible",
                )
            )
    timings[LaneName.RECENT_HISTORY] = (time.monotonic() - lane_started) * 1000

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
    try:
        from app.world.service import build_current_scene_context_record

        scene_value = build_current_scene_context_record(db, campaign)
    except (ImportError, AttributeError) as exc:
        # Missing scene reader/wiring: lane stays empty so the caller can
        # decide (explicit not_applicable vs fail-closed). Never fabricate.
        logger.warning("current_scene reader unavailable: %s", exc)
        scene_value = None
    except Exception as exc:
        # Source failure (malformed row, reader regression, DB error):
        # fail closed — never silently convert to "no scene established".
        # The only tolerated case is a not-yet-migrated deployment where
        # the scene table itself does not exist.
        if is_missing_current_scene_table_error(exc):
            logger.warning("current_scene table missing, treating lane as empty: %s", exc)
            scene_value = None
        else:
            raise
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
    timings[LaneName.CURRENT_SCENE] = (time.monotonic() - lane_started) * 1000

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
            "campaign_current_scenes",
            "world_entities",
        ],
    )
    # Include DB collection in total duration without contaminating deterministic payload.
    packet.observability.assembly_ms = (time.monotonic() - started) * 1000
    return packet
