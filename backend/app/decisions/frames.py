"""Decision frames — issue #381.

Code-owned candidate enumeration and decision-frame construction on top of
the provider-neutral bounded runtime (#380).

Responsibility split (never inverted):

- Deterministic code enumerates every legal candidate, owns stable IDs,
  display/debug metadata, provenance refs, and domain payload refs.
- The decision model selects *only* among the supplied candidate IDs.
  Confidence/probability output is evidence for the execution policy —
  never authorization to mutate state.
- Callers revalidate the selected candidate against the current
  authoritative state revision immediately before execution.

The AI Dungeon Master is the only DM in this product; no frame, candidate,
or policy copy may imply a separate human DM or moderator.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from app.decisions.contracts import (
    ChoiceQuestion,
    DecisionCandidate,
    DecisionRequest,
)
from app.decisions.errors import DecisionError

CANDIDATE_SCHEMA_VERSION = 1
FRAME_SCHEMA_VERSION = 1

# Standard escape/defer candidates. Every frame carries these so the model
# can always decline to force a bounded answer and instead defer to the
# open-ended AI DM, ask for clarification, or defer the decision.
OPEN_ENDED_DM_CANDIDATE_ID = "OPEN_ENDED_DM"
CLARIFY_CANDIDATE_ID = "CLARIFY"
DEFER_CANDIDATE_ID = "DEFER"
ESCAPE_CANDIDATE_IDS = frozenset(
    {OPEN_ENDED_DM_CANDIDATE_ID, CLARIFY_CANDIDATE_ID, DEFER_CANDIDATE_ID}
)

# Consequence/risk classes, ordered low -> critical. Execution policy
# thresholds are configured per decision class; there is no single global
# threshold shared across classes.
RISK_LOW = "low"
RISK_STANDARD = "standard"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"
RISK_ORDER = (RISK_LOW, RISK_STANDARD, RISK_HIGH, RISK_CRITICAL)


def _check_risk(risk: str) -> str:
    if risk not in RISK_ORDER:
        raise DecisionError(
            f"unknown risk class {risk!r}; expected one of {list(RISK_ORDER)}",
            kind="malformed",
        )
    return risk


@dataclass(frozen=True)
class CandidateRecord:
    """One code-enumerated legal candidate (schema v1).

    - ``id`` is the stable ID the model selects. Unique within a frame.
    - ``label``/``debug_hint`` are display/debug metadata for logs and UX.
    - ``source``/``source_ref`` are provenance refs naming the deterministic
      code path that enumerated this candidate (e.g. ``"rules:attack"``).
    - ``payload_ref`` is an opaque domain payload ref (entity key, index, or
      action handle) into authoritative state. The referenced object itself
      is never serialized to the model — only ``id`` + ``label`` cross the
      adapter boundary.
    - ``risk`` is the consequence class; ``reversible`` records whether the
      underlying domain action is reversible.
    """

    id: str
    label: str
    source: str
    source_ref: str | None = None
    payload_ref: str | None = None
    debug_hint: str | None = None
    risk: str = RISK_STANDARD
    reversible: bool = True
    schema_version: int = CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise DecisionError("candidate is missing a stable id", kind="malformed")
        if not isinstance(self.label, str) or not self.label.strip():
            raise DecisionError(
                f"candidate {self.id!r} is missing display metadata", kind="malformed"
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise DecisionError(
                f"candidate {self.id!r} is missing a provenance source",
                kind="malformed",
            )
        if self.debug_hint is not None and not isinstance(self.debug_hint, str):
            raise DecisionError(
                f"candidate {self.id!r} has a non-string debug hint",
                kind="malformed",
            )
        for attr in ("source_ref", "payload_ref"):
            value = getattr(self, attr)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise DecisionError(
                    f"candidate {self.id!r} has an empty {attr}", kind="malformed"
                )
        if not isinstance(self.reversible, bool):
            raise DecisionError(
                f"candidate {self.id!r} has a non-boolean reversible flag",
                kind="malformed",
            )
        _check_risk(self.risk)
        if self.schema_version != CANDIDATE_SCHEMA_VERSION:
            raise DecisionError(
                f"candidate {self.id!r} has unsupported schema version "
                f"{self.schema_version!r}",
                kind="unsupported_feature",
            )


def escape_candidates() -> tuple[CandidateRecord, ...]:
    """Standard escape/defer candidates appended to every frame."""
    return (
        CandidateRecord(
            id=OPEN_ENDED_DM_CANDIDATE_ID,
            label="Defer to open-ended AI DM narration",
            source="frame:escape",
            debug_hint="Model declines the bounded set; AI DM handles openly",
        ),
        CandidateRecord(
            id=CLARIFY_CANDIDATE_ID,
            label="Ask for clarification before deciding",
            source="frame:escape",
            debug_hint="Authorized state underdetermines the choice",
        ),
        CandidateRecord(
            id=DEFER_CANDIDATE_ID,
            label="Defer the decision to a later revision",
            source="frame:escape",
            debug_hint="No candidate is safe to execute at this revision",
        ),
    )


def is_escape_id(candidate_id: str) -> bool:
    """Whether a selected ID is a standard escape/defer candidate."""
    return candidate_id in ESCAPE_CANDIDATE_IDS


def enumerate_candidates(
    items: Iterable[Mapping[str, Any] | CandidateRecord],
    *,
    id_fn: Callable[[Any], str] | None = None,
    label_fn: Callable[[Any], str] | None = None,
    source: str = "code:enumeration",
) -> tuple[CandidateRecord, ...]:
    """Build a candidate tuple from code-owned items.

    Accepts ready-made :class:`CandidateRecord` entries or raw mappings with
    at least ``id``/``label`` keys (plus optional ``source``, ``source_ref``,
    ``payload_ref``, ``debug_hint``, ``risk``, ``reversible``). Raw items may
    alternatively be projected through ``id_fn``/``label_fn`` callables owned
    by caller code. The model never participates in enumeration.
    """
    records: list[CandidateRecord] = []
    for item in items:
        if isinstance(item, CandidateRecord):
            records.append(item)
            continue
        if isinstance(item, Mapping):
            # Raw values pass straight into CandidateRecord validation with
            # no coercion: bool("false") is True and str(None) is "None",
            # so coercing here could bless an irreversible candidate as
            # reversible or an absent ID as the literal "None".
            try:
                records.append(
                    CandidateRecord(
                        id=item["id"],
                        label=item["label"],
                        source=item.get("source", source),
                        source_ref=item.get("source_ref"),
                        payload_ref=item.get("payload_ref"),
                        debug_hint=item.get("debug_hint"),
                        risk=item.get("risk", RISK_STANDARD),
                        reversible=item.get("reversible", True),
                    )
                )
            except KeyError as error:
                raise DecisionError(
                    f"candidate mapping is missing required key {error}",
                    kind="malformed",
                ) from error
            except DecisionError:
                raise
            except Exception as error:
                raise DecisionError(
                    f"candidate mapping is malformed: {error}",
                    kind="malformed",
                ) from error
            continue
        if id_fn is None or label_fn is None:
            raise DecisionError(
                "enumerate_candidates needs CandidateRecord/mapping items "
                "or id_fn + label_fn projections",
                kind="malformed",
            )
        records.append(
            CandidateRecord(id=id_fn(item), label=label_fn(item), source=source)
        )
    return tuple(records)


@dataclass(frozen=True)
class DecisionFrame:
    """An authorized decision frame: state revision + candidate set + question.

    ``state`` must already be audience/visibility-authorized by the caller;
    the frame passes it through verbatim and never expands it. ``state_revision``
    is the authoritative revision the candidates were enumerated against.
    ``candidates`` reference only that authorized visible state via
    :attr:`CandidateRecord.payload_ref` refs.
    """

    decision_class: str
    question_id: str
    instructions: str
    state: Any
    state_revision: str | int
    candidates: tuple[CandidateRecord, ...] = field(default_factory=tuple)
    frame_id: str = ""
    schema_version: int = FRAME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.decision_class, str) or not self.decision_class.strip():
            raise DecisionError("decision frame is missing a decision class", kind="malformed")
        if not isinstance(self.question_id, str) or not self.question_id.strip():
            raise DecisionError("decision frame is missing a question_id", kind="malformed")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise DecisionError(
                f"frame question {self.question_id!r} is missing instructions",
                kind="malformed",
            )
        if isinstance(self.state_revision, bool) or not isinstance(
            self.state_revision, (str, int)
        ):
            raise DecisionError("decision frame needs a state revision", kind="malformed")
        if isinstance(self.state_revision, str) and not self.state_revision.strip():
            raise DecisionError("decision frame needs a state revision", kind="malformed")
        if len(self.candidates) < 2:
            raise DecisionError("decision frame needs at least 2 candidates", kind="malformed")
        ids = [c.id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise DecisionError("decision frame has duplicate candidate ids", kind="malformed")
        if self.schema_version != FRAME_SCHEMA_VERSION:
            raise DecisionError(
                f"unsupported frame schema version {self.schema_version!r}",
                kind="unsupported_feature",
            )


def build_frame(
    *,
    decision_class: str,
    question_id: str,
    instructions: str,
    state: Any,
    state_revision: str | int,
    candidates: Iterable[Mapping[str, Any] | CandidateRecord],
    frame_id: str | None = None,
    include_escapes: bool = True,
    id_fn: Callable[[Any], str] | None = None,
    label_fn: Callable[[Any], str] | None = None,
    source: str = "code:enumeration",
) -> DecisionFrame:
    """Construct a validated frame, appending escape candidates by default.

    Enumeration stays in caller code: pass already-authorized
    :class:`CandidateRecord` entries (or raw mappings projected by code-owned
    ``id_fn``/``label_fn``). Escape candidates are appended unless their IDs
    are already present.
    """
    enumerated = list(enumerate_candidates(candidates, id_fn=id_fn, label_fn=label_fn, source=source))
    if include_escapes:
        present = {c.id for c in enumerated}
        for escape in escape_candidates():
            if escape.id not in present:
                enumerated.append(escape)
    return DecisionFrame(
        decision_class=decision_class,
        question_id=question_id,
        instructions=instructions,
        state=state,
        state_revision=state_revision,
        candidates=tuple(enumerated),
        frame_id=frame_id or uuid.uuid4().hex,
    )


def to_decision_request(frame: DecisionFrame) -> DecisionRequest:
    """Convert a frame to a runtime request.

    Only stable IDs + display labels cross the adapter boundary. Provenance
    refs and domain payload refs stay in code and never reach the model.
    """
    return DecisionRequest(
        questions=(
            ChoiceQuestion(
                question_id=frame.question_id,
                instructions=frame.instructions,
                candidates=tuple(
                    DecisionCandidate(id=c.id, description=c.label)
                    for c in frame.candidates
                ),
            ),
        ),
        state=frame.state,
    )


def resolve_candidate(frame: DecisionFrame, selected_id: str) -> CandidateRecord:
    """Resolve a model-selected ID against the frame's candidate set.

    Raises :exc:`DecisionError` (malformed, not retryable) for unknown IDs —
    the model may select only supplied candidates.
    """
    for candidate in frame.candidates:
        if candidate.id == selected_id:
            return candidate
    raise DecisionError(
        f"unknown candidate {selected_id!r} for question {frame.question_id!r}",
        kind="malformed",
    )


def is_stale(frame: DecisionFrame, current_revision: str | int) -> bool:
    """Whether the frame was enumerated against an older revision."""
    return frame.state_revision != current_revision


def assert_fresh(frame: DecisionFrame, current_revision: str | int) -> None:
    """Raise :exc:`DecisionError` (stale) when the frame revision moved on."""
    if is_stale(frame, current_revision):
        raise DecisionError(
            f"decision frame {frame.frame_id!r} is stale: enumerated at "
            f"revision {frame.state_revision!r}, current is {current_revision!r}",
            kind="stale",
        )


def rebuild_frame(
    frame: DecisionFrame,
    *,
    state: Any,
    state_revision: str | int,
    candidates: Iterable[Mapping[str, Any] | CandidateRecord],
    instructions: str | None = None,
) -> DecisionFrame:
    """Rebuild a frame after the authoritative revision changed.

    The fresh candidate set is required: caller code re-enumerates against
    the newly authorized state and passes the result here. Old domain
    candidates are never carried into the new revision — carrying them would
    bless potentially-stale candidates (e.g. targeting entities removed by
    the new state) as freshly enumerated. Escape candidates are re-appended
    unless already present. Returns a new frame with a new ``frame_id``.
    """
    base = list(enumerate_candidates(candidates))
    present = {c.id for c in base}
    for escape in escape_candidates():
        if escape.id not in present:
            base.append(escape)
    return DecisionFrame(
        decision_class=frame.decision_class,
        question_id=frame.question_id,
        instructions=instructions or frame.instructions,
        state=state,
        state_revision=state_revision,
        candidates=tuple(base),
        frame_id=uuid.uuid4().hex,
    )


def revalidate_for_execution(
    frame: DecisionFrame,
    selected_id: str,
    current_revision: str | int,
    *,
    still_legal: Callable[[CandidateRecord], bool] | None = None,
    legal_ids: Iterable[str] | None = None,
) -> CandidateRecord:
    """Deterministically revalidate a selection immediately before execution.

    Checks, in order: the frame revision is current, the ID belongs to the
    frame, the candidate is still legal against current authoritative state
    (via ``still_legal`` or an explicit ``legal_ids`` set — at least one is
    required for non-escape candidates), and — for escape IDs — returns the
    escape record so the caller defers instead of executing. Escape/defer
    candidates are exempt from the legality-source requirement because they
    are never executed.
    Raises :exc:`DecisionError` with kind ``stale`` on revision drift and
    kind ``malformed`` on unknown, no-longer-legal, or validator-less
    candidates.
    """
    assert_fresh(frame, current_revision)
    candidate = resolve_candidate(frame, selected_id)
    if is_escape_id(candidate.id):
        return candidate
    if still_legal is None and legal_ids is None:
        raise DecisionError(
            f"candidate {candidate.id!r} has no authoritative legality source "
            f"for revalidation at revision {current_revision!r}; pass "
            "still_legal or legal_ids",
            kind="malformed",
        )
    if legal_ids is not None and candidate.id not in set(legal_ids):
        raise DecisionError(
            f"candidate {candidate.id!r} is no longer legal at revision "
            f"{current_revision!r}",
            kind="malformed",
        )
    if still_legal is not None and not still_legal(candidate):
        raise DecisionError(
            f"candidate {candidate.id!r} failed deterministic revalidation at "
            f"revision {current_revision!r}",
            kind="malformed",
        )
    return candidate


def frame_trace(frame: DecisionFrame, policy_version: int | None = None) -> dict[str, Any]:
    """Trace metadata stamping candidate + frame (+ policy) schema versions."""
    trace: dict[str, Any] = {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "frame_schema_version": FRAME_SCHEMA_VERSION,
        "decision_class": frame.decision_class,
        "frame_id": frame.frame_id,
        "question_id": frame.question_id,
        "state_revision": frame.state_revision,
        "candidate_ids": [c.id for c in frame.candidates],
    }
    if policy_version is not None:
        trace["policy_schema_version"] = policy_version
    return trace
