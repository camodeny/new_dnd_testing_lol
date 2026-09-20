"""Provider-neutral semantic judge layer — issue #384.

Bounded decision-model judgments for genuinely semantic trust checks that
are brittle under regex/keyword heuristics:

- invented voluntary PC behavior (action/speech/thought),
- unsupported narration additions beyond the structured result,
- semantic contradiction with the structured result,
- ambiguous canon conflicts literal checks cannot resolve,
- semantic secrecy / epistemic risk (paraphrased leaks, promoted belief).

Responsibility split (never inverted):

- Deterministic code owns authorization, ownership, visibility scope, rule
  legality, provenance/source existence, exact IDs, literal hidden-DC and
  internal-secret checks, and typed contract invariants. Judges only advise
  among code-supplied questions over code-supplied evidence.
- A semantic judge supplements deterministic validation; it never weakens
  or bypasses a deterministic failure. A positive judge result cannot
  overturn a deterministic rejection — the verdict stays ``escalate`` with
  ``deterministic_final=True``.
- Judge state is DM-authorized comparison material evaluated server-side
  only (candidate text plus the claim/canon/secret excerpts it is judged
  against). Judge outputs are probabilities, never secret text, and only
  stable IDs/versions/numbers enter telemetry rows or shared traces.
- The AI Dungeon Master is the only DM in this product; no judge,
  question, policy, or trace copy may imply a separate human DM,
  Game Master, or moderator.

Question semantics: every judge question is a ``noul`` judgment where the
returned probability is P(violation present). A question fails when its
probability meets its policy threshold. Judgments are expected to be wrong
sometimes; shadow mode + calibration (issue #383) measure that before any
active gating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from app.decisions.contracts import (
    DecisionRequest,
    NoulQuestion,
    NoulResult,
)
from app.decisions.errors import DecisionError

logger = logging.getLogger(__name__)

JUDGE_SCHEMA_VERSION = 1
JUDGE_POLICY_VERSION = 1

JUDGE_DECISION_CLASS = "semantic_judge"

JUDGE_PC_AGENCY = "judge_pc_agency"
JUDGE_UNSUPPORTED_ADDITION = "judge_unsupported_addition"
JUDGE_CONTRADICTION = "judge_contradiction"
JUDGE_CANON_CONFLICT = "judge_canon_conflict"
JUDGE_SECRECY_RISK = "judge_secrecy_risk"

ALL_JUDGE_QUESTIONS = (
    JUDGE_PC_AGENCY,
    JUDGE_UNSUPPORTED_ADDITION,
    JUDGE_CONTRADICTION,
    JUDGE_CANON_CONFLICT,
    JUDGE_SECRECY_RISK,
)

# Failing safety questions always escalate: invented PC agency and secret
# leakage must never be repaired by blind regeneration.
SAFETY_QUESTIONS = frozenset({JUDGE_PC_AGENCY, JUDGE_SECRECY_RISK})

# Failing fidelity questions carry code-supplied residue for a targeted
# repair attempt before falling back to regeneration/escalation.
REPAIR_QUESTIONS = frozenset(
    {JUDGE_UNSUPPORTED_ADDITION, JUDGE_CONTRADICTION, JUDGE_CANON_CONFLICT}
)

# Versioned judge directives. ``pass`` accepts the candidate; ``repair``
# retries once with code-supplied targeted feedback; ``regenerate`` retries
# without targeted feedback; ``escalate`` defers to the open-ended AI DM
# path (or the existing deterministic rejection when deterministic_final).
JUDGE_PASS = "pass"
JUDGE_REPAIR = "repair"
JUDGE_REGENERATE = "regenerate"
JUDGE_ESCALATE = "escalate"
JUDGE_DIRECTIVES = frozenset(
    {JUDGE_PASS, JUDGE_REPAIR, JUDGE_REGENERATE, JUDGE_ESCALATE}
)

# Evidence truncation bounds keep judge prompts bounded and reviewable.
MAX_EVIDENCE_CLAIMS = 12
MAX_EVIDENCE_CANON = 8
MAX_EVIDENCE_SECRETS = 8
MAX_EVIDENCE_TEXT_CHARS = 4000


def _truncate(text: str, limit: int = MAX_EVIDENCE_TEXT_CHARS) -> str:
    text = text if isinstance(text, str) else str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _clean_list(
    values: Sequence[str] | None, limit: int
) -> tuple[str, ...]:
    """Code-owned evidence normalization: strip, drop empties, bound size."""
    if not values:
        return ()
    cleaned = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    return tuple(cleaned[:limit])


@dataclass(frozen=True)
class CanonEntry:
    """One authoritative canon fact plus the phrases that would break it.

    Both halves are code-supplied: ``canonical`` names the established
    truth, ``forbids`` names literal phrases a contradiction would contain.
    The judge resolves only the ambiguous middle literal checks miss —
    paraphrase, implication, and partial overlap — never canon itself.
    """

    canonical: str
    forbids: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeEvidence:
    """Code-assembled comparison material for one candidate output.

    ``candidate_text`` is the narration (or other candidate prose) under
    judgment. Every other field is deterministic caller-owned evidence:
    public claim texts ground supported content, declaration texts ground
    player-authored PC speech, canon entries ground established truth, and
    secret texts (DM-authorized, server-side judge use only) ground the
    restricted material the candidate must not reveal or promote.
    """

    candidate_text: str
    public_claim_texts: tuple[str, ...] = ()
    declaration_texts: tuple[str, ...] = ()
    canon_entries: tuple[CanonEntry, ...] = ()
    secret_texts: tuple[str, ...] = ()
    pc_tokens: tuple[str, ...] = ()
    evidence_revision: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_text, str):
            raise DecisionError(
                "judge evidence candidate_text must be a string",
                kind="malformed",
            )


def build_evidence(
    candidate_text: str,
    *,
    public_claim_texts: Sequence[str] | None = None,
    declaration_texts: Sequence[str] | None = None,
    canon_entries: Sequence[CanonEntry | Mapping[str, Any]] | None = None,
    secret_texts: Sequence[str] | None = None,
    pc_tokens: Sequence[str] | None = None,
    evidence_revision: str = "",
) -> JudgeEvidence:
    """Assemble bounded judge evidence from deterministic validator residue.

    Raw mappings ``{"canonical": ..., "forbids": [...]}`` are accepted for
    canon entries; anything else malformed raises instead of guessing.
    """
    normalized_entries: list[CanonEntry] = []
    for entry in list(canon_entries or [])[:MAX_EVIDENCE_CANON]:
        if isinstance(entry, CanonEntry):
            normalized_entries.append(entry)
            continue
        if isinstance(entry, Mapping):
            canonical = entry.get("canonical") or entry.get("value") or ""
            forbids = entry.get("forbids") or entry.get("forbids_contains") or []
            if not isinstance(canonical, str) or not canonical.strip():
                raise DecisionError(
                    "judge canon entry is missing canonical text",
                    kind="malformed",
                )
            normalized_entries.append(
                CanonEntry(
                    canonical=canonical.strip(),
                    forbids=tuple(
                        str(x).strip()
                        for x in forbids
                        if isinstance(x, str) and str(x).strip()
                    ),
                )
            )
            continue
        raise DecisionError(
            f"judge canon entry {entry!r} is not a CanonEntry or mapping",
            kind="malformed",
        )
    return JudgeEvidence(
        candidate_text=_truncate(candidate_text),
        public_claim_texts=_clean_list(
            public_claim_texts, MAX_EVIDENCE_CLAIMS
        ),
        declaration_texts=_clean_list(declaration_texts, MAX_EVIDENCE_CLAIMS),
        canon_entries=tuple(normalized_entries),
        secret_texts=_clean_list(secret_texts, MAX_EVIDENCE_SECRETS),
        pc_tokens=_clean_list(pc_tokens, MAX_EVIDENCE_CLAIMS),
        evidence_revision=str(evidence_revision or ""),
    )


def validator_residue_summary(
    violations: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Compact deterministic residue for repair feedback and calibration.

    Accepts plain ``{"validator", "code", "category"}`` mappings (e.g. from
    narration fidelity violations or the validator pipeline report) so this
    module never imports DM layers. Unknown shapes raise malformed instead
    of silently dropping rejection evidence.
    """
    if not violations:
        return {"failed": False, "codes": [], "categories": []}
    codes: list[str] = []
    categories: list[str] = []
    for violation in violations:
        if not isinstance(violation, Mapping):
            raise DecisionError(
                f"validator residue entry {violation!r} is not a mapping",
                kind="malformed",
            )
        code = violation.get("code", "")
        category = violation.get("category", "")
        codes.append(str(code))
        categories.append(str(category))
    return {
        "failed": True,
        "codes": codes,
        "categories": sorted(set(categories)),
    }


# ── Code-owned judge instructions ──────────────────────────────────────────


def _question_instructions(question_id: str, evidence: JudgeEvidence) -> str:
    """Render the bounded instruction for one judge question.

    Templates are owned by code; only evidence strings vary. Every question
    asks for P(violation present) semantics: higher probability means the
    defect is more likely present.
    """
    claims = "\n".join(f"- {c}" for c in evidence.public_claim_texts) or "- (none)"
    declarations = "\n".join(f"- {d}" for d in evidence.declaration_texts) or "- (none)"
    canon = "\n".join(
        f"- established: {e.canonical}"
        + (f" | contradicted by: {', '.join(e.forbids)}" if e.forbids else "")
        for e in evidence.canon_entries
    ) or "- (none)"
    secrets = "\n".join(f"- {s}" for s in evidence.secret_texts) or "- (none)"
    pcs = ", ".join(evidence.pc_tokens) or "(none)"

    if question_id == JUDGE_PC_AGENCY:
        return (
            "Decide P(violation present): does the candidate narration invent "
            "voluntary player-character action, speech, or thought beyond the "
            "player-authored declarations below? Reproducing a declaration "
            "verbatim with attribution is NOT a violation. Describing "
            "involuntary consequences (being hit, falling, dice outcomes) is "
            "NOT a violation. A PC deciding, vowing, shouting new words, or "
            "taking a new voluntary action IS a violation.\n"
            f"Player characters: {pcs}\n"
            f"Player-authored declarations:\n{declarations}\n"
            "Answer with a high probability only when the candidate adds "
            "voluntary PC behavior the declarations do not state."
        )
    if question_id == JUDGE_UNSUPPORTED_ADDITION:
        return (
            "Decide P(violation present): does the candidate narration add a "
            "new outcome, consequence, discovery, reward, injury, death, or "
            "piece of dialogue that goes beyond the supported claims below? "
            "Stylistic rewording of a supported claim is NOT a violation. "
            "New facts, numbers, consequences, or quoted speech with no "
            "supporting claim ARE violations.\n"
            f"Supported claims:\n{claims}\n"
            "Answer with a high probability only when the candidate states "
            "something no supported claim covers."
        )
    if question_id == JUDGE_CONTRADICTION:
        return (
            "Decide P(violation present): does the candidate narration "
            "semantically contradict any supported claim below — including "
            "paraphrased or implied contradiction, not just opposite words? "
            "Agreement, elaboration without new facts, and uncertainty that "
            "matches the claims are NOT violations.\n"
            f"Supported claims:\n{claims}\n"
            "Answer with a high probability only when the candidate asserts "
            "something a supported claim denies, in any wording."
        )
    if question_id == JUDGE_CANON_CONFLICT:
        return (
            "Decide P(violation present): does the candidate narration "
            "conflict with the established canon below where literal checks "
            "are ambiguous — paraphrase, implication, or partial overlap "
            "that a keyword check would miss? Exact agreement is NOT a "
            "violation; only genuine conflict counts.\n"
            f"Established canon:\n{canon}\n"
            "Answer with a high probability only when the candidate cannot "
            "both be true alongside the established canon."
        )
    if question_id == JUDGE_SECRECY_RISK:
        return (
            "Decide P(violation present): does the candidate narration reveal "
            "or restate — verbatim OR paraphrased — any restricted fact "
            "below, or promote an NPC claim or player belief into objective "
            "truth without support? Public claims restated plainly are NOT "
            "violations. Leaking the substance of a restricted fact in any "
            "wording IS a violation.\n"
            f"Restricted facts (never repeat them):\n{secrets}\n"
            f"Public claims (safe to restate):\n{claims}\n"
            "Answer with a high probability when a restricted fact's "
            "substance appears in the candidate, however reworded."
        )
    raise DecisionError(
        f"unknown judge question {question_id!r}; expected one of "
        f"{list(ALL_JUDGE_QUESTIONS)}",
        kind="malformed",
    )


def build_judge_questions(
    evidence: JudgeEvidence,
    *,
    only: Sequence[str] | None = None,
) -> tuple[NoulQuestion, ...]:
    """Build bounded noul judge questions over code-owned evidence.

    ``only`` selects a subset (default: all five). Unknown IDs raise
    malformed — callers cannot invent judge roles.
    """
    wanted = tuple(only) if only is not None else ALL_JUDGE_QUESTIONS
    if not wanted:
        raise DecisionError("judge question set is empty", kind="malformed")
    seen: set[str] = set()
    questions: list[NoulQuestion] = []
    for question_id in wanted:
        if question_id in seen:
            raise DecisionError(
                f"duplicate judge question {question_id!r}", kind="malformed"
            )
        seen.add(question_id)
        questions.append(
            NoulQuestion(
                question_id=question_id,
                instructions=_question_instructions(question_id, evidence),
            )
        )
    return tuple(questions)


def build_judge_request(
    evidence: JudgeEvidence,
    *,
    only: Sequence[str] | None = None,
    model: str | None = None,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
) -> DecisionRequest:
    """Build one fan-out decision call over the shared candidate state.

    All selected judge questions inspect the same candidate output in a
    single provider call. ``state`` carries only the candidate text plus
    the evidence revision — per-question comparison evidence lives in the
    code-owned instructions, and raw secret texts travel only inside the
    server-side judge call, never into telemetry or player output.
    """
    questions = build_judge_questions(evidence, only=only)
    return DecisionRequest(
        questions=questions,  # type: ignore[arg-type]
        state={
            "candidate_text": evidence.candidate_text,
            "evidence_revision": evidence.evidence_revision,
            "judge_schema_version": JUDGE_SCHEMA_VERSION,
        },
        model=model,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
    )


# ── Versioned judge policy ─────────────────────────────────────────────────


@dataclass(frozen=True)
class JudgePolicy:
    """Per-question fail thresholds + retry posture (schema v1).

    A question fails when P(violation present) meets its threshold.
    Failing safety questions (agency, secrecy) always escalate. Failing
    repair questions attempt one targeted repair while attempts remain,
    otherwise regenerate; anything else regenerates while attempts remain.
    Exhausted attempts always escalate — regeneration is bounded, never
    open-ended. There is deliberately no single global threshold: each
    question carries its own fail bar.
    """

    fail_thresholds: dict[str, float] = field(default_factory=dict)
    max_regenerations: int = 2
    schema_version: int = JUDGE_POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.max_regenerations, bool) and isinstance(
            self.max_regenerations, int
        ):
            max_regens: int = self.max_regenerations
        else:
            raise DecisionError(
                f"judge policy max_regenerations {self.max_regenerations!r} "
                "must be an integer",
                kind="malformed",
            )
        if not 0 <= max_regens <= 5:
            raise DecisionError(
                f"judge policy max_regenerations {max_regens!r} must be 0-5",
                kind="malformed",
            )
        thresholds = dict(self.fail_thresholds)
        for question_id in ALL_JUDGE_QUESTIONS:
            value = thresholds.get(question_id, 0.5)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 < float(value) < 1.0
            ):
                raise DecisionError(
                    f"judge policy threshold for {question_id!r} {value!r} "
                    "must be a number in (0, 1)",
                    kind="malformed",
                )
            thresholds[question_id] = float(value)
        object.__setattr__(self, "fail_thresholds", thresholds)
        if self.schema_version != JUDGE_POLICY_VERSION:
            raise DecisionError(
                f"unsupported judge policy schema version "
                f"{self.schema_version!r}",
                kind="unsupported_feature",
            )

    def threshold_for(self, question_id: str) -> float:
        try:
            return self.fail_thresholds[question_id]
        except KeyError as error:
            raise DecisionError(
                f"judge policy has no threshold for {question_id!r}",
                kind="malformed",
            ) from error


DEFAULT_JUDGE_POLICY = JudgePolicy()


@dataclass(frozen=True)
class JudgeFinding:
    question_id: str
    probability: float
    threshold: float
    failed: bool


@dataclass(frozen=True)
class JudgeVerdict:
    """Aggregate outcome over one fan-out judge call.

    ``deterministic_final`` is True when deterministic checks already
    rejected the candidate: the directive is then ``escalate`` regardless
    of judge probabilities, and a semantic pass never overturns it.
    """

    directive: str
    reason: str
    findings: dict[str, JudgeFinding]
    failed_questions: tuple[str, ...]
    deterministic_passed: bool
    deterministic_final: bool
    attempts_used: int
    policy_version: int = JUDGE_POLICY_VERSION
    question_version: int = JUDGE_SCHEMA_VERSION


def _checked_violation_probability(value: Any, *, question_id: str) -> float:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise DecisionError(
            f"judge result for {question_id!r} has a non-numeric probability "
            f"{value!r}",
            kind="malformed",
        )
    probability = float(value)
    if not 0.0 <= probability <= 1.0:
        raise DecisionError(
            f"judge result for {question_id!r} has an out-of-range "
            f"probability {value!r}",
            kind="malformed",
        )
    return probability


def evaluate_judges(
    results: Mapping[str, Any],
    *,
    expected_questions: Sequence[str] | None = None,
    deterministic_passed: bool,
    deterministic_codes: Sequence[str] | None = None,
    attempts_used: int = 0,
    policy: JudgePolicy | None = None,
) -> JudgeVerdict:
    """Aggregate one fan-out judge call through versioned retry policy.

    ``results`` maps question IDs to ``NoulResult`` answers. The answered
    set must exactly equal the expected set — extra or missing answers
    raise malformed instead of silently narrowing the gate.

    ``deterministic_passed`` is the code-owned deterministic outcome
    (validator pipeline + literal fidelity checks) and has no default:
    callers must pass it explicitly. ``False`` always escalates with
    ``deterministic_final=True``; truthy non-booleans raise malformed.
    """
    active = policy or DEFAULT_JUDGE_POLICY
    expected = tuple(expected_questions) if expected_questions is not None else ALL_JUDGE_QUESTIONS
    if set(results) != set(expected):
        raise DecisionError(
            "judge answer set does not match the asked questions: asked "
            f"{sorted(expected)}, answered {sorted(results)}",
            kind="malformed",
        )
    if not isinstance(deterministic_passed, bool):
        raise DecisionError(
            f"judge input deterministic_passed {deterministic_passed!r} "
            "is not a boolean",
            kind="malformed",
        )
    if isinstance(attempts_used, bool) or not isinstance(attempts_used, int):
        raise DecisionError(
            f"judge input attempts_used {attempts_used!r} is not an integer",
            kind="malformed",
        )
    if attempts_used < 0:
        raise DecisionError(
            f"judge input attempts_used {attempts_used!r} is negative",
            kind="malformed",
        )

    findings: dict[str, JudgeFinding] = {}
    for question_id in expected:
        result = results[question_id]
        if not isinstance(result, NoulResult):
            raise DecisionError(
                f"judge result for {question_id!r} must be a noul judgment, "
                f"got {type(result).__name__}",
                kind="malformed",
            )
        if result.question_id != question_id:
            raise DecisionError(
                f"judge result {result.question_id!r} does not belong to "
                f"question {question_id!r}",
                kind="malformed",
            )
        probability = _checked_violation_probability(
            result.probability, question_id=question_id
        )
        threshold = active.threshold_for(question_id)
        findings[question_id] = JudgeFinding(
            question_id=question_id,
            probability=probability,
            threshold=threshold,
            failed=probability >= threshold,
        )
    failed = tuple(qid for qid, f in findings.items() if f.failed)

    if not deterministic_passed:
        codes = list(deterministic_codes or [])
        return JudgeVerdict(
            directive=JUDGE_ESCALATE,
            reason=(
                "deterministic validation already rejected the candidate "
                f"({', '.join(codes) if codes else 'no codes'}); a semantic "
                "pass can never overturn a deterministic failure"
            ),
            findings=findings,
            failed_questions=failed,
            deterministic_passed=False,
            deterministic_final=True,
            attempts_used=attempts_used,
        )
    if not failed:
        return JudgeVerdict(
            directive=JUDGE_PASS,
            reason="deterministic checks passed and no judge question failed",
            findings=findings,
            failed_questions=(),
            deterministic_passed=True,
            deterministic_final=False,
            attempts_used=attempts_used,
        )
    if any(qid in SAFETY_QUESTIONS for qid in failed):
        safety = sorted(qid for qid in failed if qid in SAFETY_QUESTIONS)
        return JudgeVerdict(
            directive=JUDGE_ESCALATE,
            reason=(
                f"safety judge failure ({', '.join(safety)}); invented PC "
                "agency and secret leakage never regenerate blindly"
            ),
            findings=findings,
            failed_questions=failed,
            deterministic_passed=True,
            deterministic_final=False,
            attempts_used=attempts_used,
        )
    if attempts_used >= active.max_regenerations:
        return JudgeVerdict(
            directive=JUDGE_ESCALATE,
            reason=(
                f"judge failures ({', '.join(sorted(failed))}) persist after "
                f"{attempts_used} bounded attempt(s); escalating instead of "
                "regenerating without bound"
            ),
            findings=findings,
            failed_questions=failed,
            deterministic_passed=True,
            deterministic_final=False,
            attempts_used=attempts_used,
        )
    if any(qid in REPAIR_QUESTIONS for qid in failed):
        return JudgeVerdict(
            directive=JUDGE_REPAIR,
            reason=(
                f"judge failures ({', '.join(sorted(failed))}) carry "
                "code-supplied residue for one targeted repair attempt"
            ),
            findings=findings,
            failed_questions=failed,
            deterministic_passed=True,
            deterministic_final=False,
            attempts_used=attempts_used,
        )
    return JudgeVerdict(
        directive=JUDGE_REGENERATE,
        reason=f"judge failures ({', '.join(sorted(failed))}); bounded retry",
        findings=findings,
        failed_questions=failed,
        deterministic_passed=True,
        deterministic_final=False,
        attempts_used=attempts_used,
    )


def judge_retry_allowed(verdict: JudgeVerdict, *, attempts_used: int) -> bool:
    """Whether a regenerate/repair directive may still run within bounds."""
    if verdict.directive not in (JUDGE_REPAIR, JUDGE_REGENERATE):
        return False
    if isinstance(attempts_used, bool) or not isinstance(attempts_used, int):
        raise DecisionError(
            f"judge input attempts_used {attempts_used!r} is not an integer",
            kind="malformed",
        )
    return attempts_used < DEFAULT_JUDGE_POLICY.max_regenerations


def format_judge_feedback(
    verdict: JudgeVerdict,
    *,
    residue: Mapping[str, Any] | None = None,
) -> str:
    """Targeted repair feedback for regenerate/repair directives.

    Lists only failed question IDs plus deterministic residue codes —
    never secret texts or full evidence — so feedback stays reviewable.
    """
    lines = [
        "Semantic judge rejected the candidate narration:",
    ]
    for question_id in verdict.failed_questions:
        finding = verdict.findings[question_id]
        lines.append(
            f"- [{question_id}] P(violation)={finding.probability:.2f} "
            f"(threshold {finding.threshold:.2f}): "
            f"{_REPAIR_HINTS.get(question_id, 'remove the unsupported content')}"
        )
    if residue and residue.get("codes"):
        lines.append(
            "Deterministic residue: " + ", ".join(str(c) for c in residue["codes"])
        )
    lines.append(
        "Fix the narration using only the supported structured claims. "
        "Do not invent PC behavior, new consequences, or hidden material."
    )
    return "\n".join(lines)


_REPAIR_HINTS = {
    JUDGE_PC_AGENCY: (
        "remove invented voluntary PC action/speech/thought; reproduce "
        "player declarations verbatim with attribution only"
    ),
    JUDGE_UNSUPPORTED_ADDITION: (
        "remove outcomes, consequences, numbers, or dialogue beyond the "
        "supported claims"
    ),
    JUDGE_CONTRADICTION: (
        "align the narration with the supported claims; drop the "
        "contradicting statement"
    ),
    JUDGE_CANON_CONFLICT: (
        "align the narration with established canon; drop the conflicting "
        "implication"
    ),
    JUDGE_SECRECY_RISK: (
        "remove the restricted substance however reworded; restate only "
        "public claims"
    ),
}


# ── Orchestration: run, shadow, trace ──────────────────────────────────────


def run_judges(
    service: Any,
    evidence: JudgeEvidence,
    *,
    only: Sequence[str] | None = None,
    deterministic_passed: bool,
    deterministic_codes: Sequence[str] | None = None,
    attempts_used: int = 0,
    policy: JudgePolicy | None = None,
    model: str | None = None,
) -> tuple[JudgeVerdict, Any]:
    """Run one fan-out judge call and aggregate through policy.

    Active path: exactly one decision call fans out every selected judge
    question over the same candidate output. Raises :exc:`DecisionError`
    on transport/config/malformed failures — callers escalate to the
    open-ended AI DM path, never to a degraded silent pass. Returns
    ``(verdict, response)`` so callers can record full decision metadata.
    """
    from app.decisions.runtime import validate_request

    if not isinstance(deterministic_passed, bool):
        raise DecisionError(
            f"judge input deterministic_passed {deterministic_passed!r} "
            "is not a boolean",
            kind="malformed",
        )
    request = build_judge_request(evidence, only=only, model=model)
    validate_request(request)
    response = service.decide(request)
    expected = tuple(only) if only is not None else ALL_JUDGE_QUESTIONS
    verdict = evaluate_judges(
        response.results,
        expected_questions=expected,
        deterministic_passed=deterministic_passed,
        deterministic_codes=deterministic_codes,
        attempts_used=attempts_used,
        policy=policy,
    )
    return verdict, response


def judge_role(decision_class: str = JUDGE_DECISION_CLASS) -> Callable[[str], str]:
    """Per-role calibration key factory: one role per judge question.

    Calibration is per judge role (``semantic_judge/<question>``), never
    collapsed into one global judge metric.
    """

    def _role(question_id: str) -> str:
        return f"{decision_class}/{question_id}"

    return _role


def build_judge_records(
    verdict: JudgeVerdict,
    *,
    provider: str,
    model: str,
    mode: str,
    trace_id: str | None = None,
    operation_id: str | None = None,
    campaign_id: Any = None,
    turn_id: Any = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    model_version: str | None = None,
    role_fn: Callable[[str], str] | None = None,
) -> list[Any]:
    """Assemble one calibration record per answered judge question.

    Pure (no I/O): binary noul outcomes are stored as
    ``clean``/``violation`` selections with the violation probability as
    confidence, so per-role calibration summaries measure whether judge
    confidence is calibrated. Only stable IDs, versions, and numbers are
    retained — candidate text, claim/secret evidence, and instructions
    never enter a record.
    """
    from app.decisions.telemetry import DecisionRecord, _check_mode

    _check_mode(mode)
    roles = role_fn or judge_role()
    records: list[Any] = []
    for question_id, finding in verdict.findings.items():
        selected = "violation" if finding.failed else "clean"
        probability = finding.probability
        records.append(
            DecisionRecord(
                decision_class=roles(question_id),
                question_id=question_id,
                question_kind="noul",
                provider=provider,
                model=model,
                candidate_ids=("clean", "violation"),
                selected_id=selected,
                probabilities={
                    "violation": probability,
                    "clean": 1.0 - probability,
                },
                runner_up_id="clean" if finding.failed else "violation",
                margin=abs(probability - (1.0 - probability)),
                confidence=probability,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                trace_id=trace_id or "",
                operation_id=operation_id,
                campaign_id=str(campaign_id) if campaign_id is not None else None,
                turn_id=str(turn_id) if turn_id is not None else None,
                frame_id="",
                state_revision=verdict.findings[question_id].question_id,
                mode=mode,
                policy_directive=verdict.directive,
                verified=verdict.deterministic_passed,
                revalidation_error=(
                    None
                    if verdict.deterministic_passed
                    else "deterministic failure is final"
                ),
                model_version=model_version,
                policy_schema_version=JUDGE_POLICY_VERSION,
            )
        )
    return records


def record_judge_rows(
    session_factory: Any,
    records: Sequence[Any],
) -> list[Any]:
    """Persist judge calibration rows fail-soft; never breaks gameplay.

    Returns the persisted rows (``None`` entries where a write dropped).
    A ``None`` factory persists nothing and returns ``[]``.
    """
    from app.decisions.telemetry import record_fail_soft

    if session_factory is None:
        return []
    persisted: list[Any] = []
    for record in records:
        persisted.append(record_fail_soft(session_factory, record))
    return persisted


def shadow_judge(
    service: Any,
    evidence: JudgeEvidence,
    *,
    only: Sequence[str] | None = None,
    deterministic_passed: bool,
    deterministic_codes: Sequence[str] | None = None,
    attempts_used: int = 0,
    policy: JudgePolicy | None = None,
    model: str | None = None,
    session_factory: Any = None,
    trace_id: str | None = None,
    campaign_id: Any = None,
    turn_id: Any = None,
) -> JudgeVerdict | None:
    """Evaluate judges in shadow mode: no gameplay effect, never raises.

    Runs the full fan-out judge call, aggregates through policy, and
    records per-question calibration rows (issue #383) so shadow
    comparisons measure judge accuracy before any active gating. Any
    decision-plane failure returns ``None``; the caller must continue
    down the existing deterministic path unchanged.
    """
    from app.decisions.telemetry import SHADOW

    try:
        verdict, response = run_judges(
            service,
            evidence,
            only=only,
            deterministic_passed=deterministic_passed,
            deterministic_codes=deterministic_codes,
            attempts_used=attempts_used,
            policy=policy,
            model=model,
        )
    except Exception as exc:
        logger.warning("shadow judge dropped: %s", exc)
        return None
    try:
        usage = getattr(response, "usage", None) or {}
        records = build_judge_records(
            verdict,
            provider=getattr(response, "provider", "unknown"),
            model=getattr(response, "model", None) or "unknown",
            mode=SHADOW,
            trace_id=trace_id or getattr(response, "trace_id", None),
            operation_id=getattr(response, "operation_id", None),
            campaign_id=campaign_id,
            turn_id=turn_id,
            latency_ms=getattr(response, "latency_ms", None),
            cost_usd=usage.get("cost_usd"),
        )
        record_judge_rows(session_factory, records)
    except Exception as exc:
        logger.warning("shadow judge telemetry dropped: %s", exc)
    return verdict


def judge_trace(
    verdict: JudgeVerdict,
    *,
    provider: str | None = None,
    model: str | None = None,
    model_version: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Privacy-safe trace: versions, IDs, and numbers only.

    No candidate text, claim/secret evidence, instructions, or repair
    feedback enters the trace — only per-question probabilities,
    thresholds, the aggregate directive, and schema/policy/model versions.
    """
    return {
        "decision_class": JUDGE_DECISION_CLASS,
        "question_ids": sorted(verdict.findings),
        "question_version": verdict.question_version,
        "policy_version": verdict.policy_version,
        "provider": provider,
        "model": model,
        "model_version": model_version,
        "trace_id": trace_id,
        "directive": verdict.directive,
        "reason": verdict.reason,
        "failed_questions": list(verdict.failed_questions),
        "deterministic_passed": verdict.deterministic_passed,
        "deterministic_final": verdict.deterministic_final,
        "attempts_used": verdict.attempts_used,
        "findings": {
            question_id: {
                "probability": finding.probability,
                "threshold": finding.threshold,
                "failed": finding.failed,
            }
            for question_id, finding in verdict.findings.items()
        },
    }


# ── Streaming: buffered/full-candidate checkpoints ─────────────────────────


@dataclass(frozen=True)
class StreamJudgePolicy:
    """TTFT/buffering policy for judge calls over streamed narration.

    Judges never run per token: a checkpoint requires at least
    ``min_buffer_chars`` of new text since the previous checkpoint (ending
    on a word boundary when ``require_word_boundary`` is set), or the
    full-candidate completion signal. ``max_checkpoints`` bounds the total
    judge calls per stream so a long narration cannot fan out without
    limit. Streaming stays on the cheap incremental deterministic gates
    per delta; semantic judges run only at these buffered checkpoints.
    """

    min_buffer_chars: int = 240
    require_word_boundary: bool = True
    max_checkpoints: int = 3
    schema_version: int = JUDGE_POLICY_VERSION

    def __post_init__(self) -> None:
        for attr in ("min_buffer_chars", "max_checkpoints"):
            value = getattr(self, attr)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise DecisionError(
                    f"stream judge policy {attr} {value!r} must be a "
                    "positive integer",
                    kind="malformed",
                )
        if not isinstance(self.require_word_boundary, bool):
            raise DecisionError(
                "stream judge policy require_word_boundary must be a boolean",
                kind="malformed",
            )


DEFAULT_STREAM_JUDGE_POLICY = StreamJudgePolicy()


class BufferedJudgeCheckpoint:
    """Accumulates streamed deltas; judges only at buffered checkpoints.

    ``feed()`` accepts provider deltas (never a model call). ``ready`` is
    True only when enough new text buffered for a mid-stream checkpoint;
    ``full_candidate_ready`` is True once at completion for the
    authoritative full-candidate judgment. ``mark_checkpoint()`` consumes
    one bounded checkpoint slot; exceeding ``max_checkpoints`` raises
    malformed instead of issuing another call.
    """

    def __init__(self, policy: StreamJudgePolicy | None = None) -> None:
        self._policy = policy or DEFAULT_STREAM_JUDGE_POLICY
        self._cumulative: list[str] = []
        self._cumulative_len = 0
        self._last_checkpoint_len = 0
        self._checkpoints_used = 0
        self._completed = False

    @property
    def cumulative(self) -> str:
        return "".join(self._cumulative)

    @property
    def checkpoints_used(self) -> int:
        return self._checkpoints_used

    def feed(self, delta: str) -> None:
        if not isinstance(delta, str):
            raise DecisionError(
                f"stream judge buffer feed {delta!r} must be a string",
                kind="malformed",
            )
        if delta:
            self._cumulative.append(delta)
            self._cumulative_len += len(delta)

    def _pending(self) -> str:
        return self.cumulative[self._last_checkpoint_len :]

    @property
    def ready(self) -> bool:
        """Whether a mid-stream buffered checkpoint may run now."""
        if self._completed:
            return False
        if self._checkpoints_used >= self._policy.max_checkpoints:
            return False
        pending = self._pending()
        if len(pending) < self._policy.min_buffer_chars:
            return False
        if self._policy.require_word_boundary and pending[-1:].strip():
            # Mid-stream text must end on whitespace/punctuation so the
            # judge never evaluates a word cut in half by chunking.
            return False
        return True

    def mark_checkpoint(self) -> int:
        """Consume one checkpoint slot for the current cumulative text.

        Returns the 1-based checkpoint index. Raises malformed when no
        slot remains — callers must then wait for completion or skip.
        """
        if self._checkpoints_used >= self._policy.max_checkpoints:
            raise DecisionError(
                "stream judge checkpoint budget exhausted; no further "
                "mid-stream judge calls",
                kind="malformed",
            )
        self._checkpoints_used += 1
        self._last_checkpoint_len = self._cumulative_len
        return self._checkpoints_used

    def complete(self) -> None:
        """Signal full-candidate completion: authoritative judgment time."""
        self._completed = True

    @property
    def full_candidate_ready(self) -> bool:
        """Whether the authoritative full-candidate judgment may run."""
        if not self._completed:
            return False
        if self._checkpoints_used >= self._policy.max_checkpoints:
            return False
        return self._cumulative_len > self._last_checkpoint_len

    def checkpoint_trace(self) -> dict[str, Any]:
        return {
            "buffered_chars": self._cumulative_len,
            "pending_chars": len(self._pending()),
            "checkpoints_used": self._checkpoints_used,
            "max_checkpoints": self._policy.max_checkpoints,
            "min_buffer_chars": self._policy.min_buffer_chars,
            "completed": self._completed,
            "policy_version": self._policy.schema_version,
        }
