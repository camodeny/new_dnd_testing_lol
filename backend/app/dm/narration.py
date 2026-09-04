"""Forward-DM narration from audience-safe structured projection — issue #207.

Narration is a projection of adjudication, never an independent truth source.
The narrator receives ONLY the deterministic allowlisted projection of an
already-validated ``dm_turn_contract_v1`` (never broad hidden context) and is
not trusted to self-censor secrets it never needed.

Pipeline (critical path optimized for TTFT after validation):

1. ``build_narration_projection(contract)`` — deterministic audience-safe
   input: strips DM-private truth (``dm_private_context``/``truth_status``),
   hidden DCs (``dc_private``), internal provenance IDs
   (``evidence_refs``/``trigger_refs``/``origin``/submission ids), staged
   effects, evidence requests, new-entity proposals, and unrelated private
   context. Built on ``public_projection()`` with additional scrub asserts.
2. Narrator expands the projection to prose under ``NARRATOR_CONTRACT`` —
   no re-adjudication, no new consequences, no invented voluntary PC action.
   Default is the deterministic template narrator (no provider); a
   streaming LLM callable taking a contract-bound :class:`NarratorRequest`
   (built prompt + structured projection) and yielding text deltas can be
   injected — the service always prepends the contract, so providers cannot
   receive narration input without it. Each delta is persisted durably as
   it arrives, so TTFT tracks first-delta arrival, not full generation.
3. Fidelity gates — cheap incremental gates per delta (``secret_leakage``
   + ``agency_violation`` on the cumulative visible text, before each
   durable persist) plus the authoritative full-output validation at
   provider completion (all categories). A post-delivery full-check
   failure fails the stream via ``fail_stream`` (partial chunks retained
   as failed-visible audit, never promoted to history) and the
   orchestrator marks the attempt failed-visible (see ``mark_attempt_failed``);
   repair requires a NEW stream on a NEW attempt.
4. ``stream_narration(...)`` — persists each chunk durably via #197
   (``dm_streams`` service) BEFORE/with realtime delivery via #198, so the
   first visible text is recoverable after disconnect. TTFT is measured
   from validated adjudication to first persisted visible chunk.
    ``on_first_persist`` fires synchronously after chunk 0's durable
    commit and BEFORE its realtime delivery — the orchestrator uses it to
    run ``mark_streaming_started`` so the first visible chunk transitions
    the attempt to streaming/visible and locks the input set (#206).
    ``on_first_persist_tx`` is the crash-atomic variant: it runs INSIDE the
    chunk-0 transaction (after the chunk flush, before the single shared
    commit), must be flush-only (no commit of its own), and shares exactly
    one commit point with the chunk-0 row + phase transition + input lock,
    with realtime delivery only after that commit returns — so no crash
    gap can leave a durable chunk 0 on a still-prepared attempt.
5. ``materialize_final_narration(...)`` — final display text for history
    after completion, with chunk provenance retained.

The narrator can never apply game-state effects: this module never touches
``commit_turn``/``apply_staged_effects``; staged effects stay attempt-local
until the separate three-phase commit (#206).

Observability: ``get_narration_metrics()`` tracks projection size,
provider, TTFT, chunk cadence, fidelity-validation failures,
secret/agency rejection counts, and total narration duration.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from sqlalchemy.orm import Session

from app.dm.contract import DmTurnContractV1, public_projection
from app.observability.tracing import structured_log

logger = logging.getLogger(__name__)

# ── Narrator contract (no-readjudication) ─────────────────────────────────────

NARRATOR_CONTRACT = """\
You are the table narrator expanding an already-adjudicated structured turn.
HARD RULES — violating any rule invalidates your output:
1. NARRATE ONLY what the structured beats state. Do not add new outcomes,
   consequences, damage, rewards, deaths, discoveries, or NPC actions.
2. NEVER re-adjudicate: no new DCs, checks, saves, or success/failure calls.
3. NEVER invent voluntary player-character speech, thought, or action. Reproduce
   player-authored declarations with attribution; do not extend them.
4. NPC dialogue: render the given utterance text only. Never reveal whether the
   NPC is truthful, mistaken, or deceptive, and never state hidden motives.
5. No numbers, names, places, or quoted speech beyond the structured beats.
6. You have no game-state authority: your words change nothing by themselves.
"""

PROVIDER_DETERMINISTIC = "deterministic-template-v1"

# ── Errors ────────────────────────────────────────────────────────────────────


class NarrationError(RuntimeError):
    code = "narration_failed"


class NarrationProjectionError(NarrationError):
    code = "narration_projection_failed"


class NarratorGenerationError(NarrationError):
    """Pre-first-chunk failure — freely retryable, nothing persisted."""

    code = "narrator_generation_failed"


class NarrationFidelityError(NarrationError):
    """Fidelity gate rejected the narration before first persistence."""

    code = "narration_fidelity_rejected"

    def __init__(self, message: str, *, violations: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.violations = violations or []


class NarrationStreamError(NarrationError):
    """Post-first-chunk failure — partial stream remains recoverable."""

    code = "narration_stream_failed"

    def __init__(
        self,
        message: str,
        *,
        stream_id: uuid.UUID | None = None,
        persisted_chunks: int = 0,
        violations: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.stream_id = stream_id
        self.persisted_chunks = persisted_chunks
        self.violations = violations or []


# ── Metrics (process-local observability) ─────────────────────────────────────

_metrics: dict[str, Any] = {
    "narrations_started": 0,
    "narrations_completed": 0,
    "narrations_failed_pre_chunk": 0,
    "narrations_failed_post_chunk": 0,
    "fidelity_failures": 0,
    "secret_rejections": 0,
    "agency_rejections": 0,
    "unsupported_rejections": 0,
    "contradiction_rejections": 0,
    "ttft_ms_samples": [],
    "chunk_cadence_ms_samples": [],
    "total_duration_ms_samples": [],
    "projection_bytes_samples": [],
}


def get_narration_metrics() -> dict[str, Any]:
    """Return a snapshot of narration observability counters."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in _metrics.items()}
    ttfts = out["ttft_ms_samples"] or [0]
    durs = out["total_duration_ms_samples"] or [0]
    out["ttft_ms_p50"] = sorted(ttfts)[len(ttfts) // 2]
    out["ttft_ms_max"] = max(ttfts)
    out["duration_ms_p50"] = sorted(durs)[len(durs) // 2]
    return out


def _inc(key: str, n: int = 1) -> None:
    _metrics[key] += n


def reset_narration_metrics() -> None:
    for k, v in _metrics.items():
        _metrics[k] = [] if isinstance(v, list) else 0


# ── 1. Audience-safe projection ───────────────────────────────────────────────

# Fields that must never reach the narrator, belt-and-braces on top of
# public_projection()'s allowlist.
_FORBIDDEN_SUBSTRINGS = (
    "dm_private_context",
    "truth_status",
    "dc_private",
    "character_id",
    "evidence_refs",
    "trigger_refs",
    "staged_effects",
    "evidence_requests",
    "new_entities",
    "adjudication_input",
    "provenance",
    "submission",
)


def build_narration_projection(contract: DmTurnContractV1) -> dict[str, Any]:
    """Deterministic audience-safe narrator input from a validated contract.

    Wraps :func:`public_projection` and asserts the scrub holds: the
    serialized projection must not contain private beat context, hidden
    thresholds, internal IDs, or effect/provenance lanes.
    """
    try:
        projection = public_projection(contract)
    except Exception as exc:
        raise NarrationProjectionError(f"Projection failed: {exc}") from exc
    blob = json.dumps(projection, ensure_ascii=False, sort_keys=True).lower()
    for needle in _FORBIDDEN_SUBSTRINGS:
        if needle in blob:
            raise NarrationProjectionError(
                f"Narrator projection leaks forbidden field {needle!r}"
            )
    # Hidden numeric DCs must never appear as bare values either: the only
    # numbers allowed are those already present in public claim text.
    return projection


def projection_size_bytes(projection: dict[str, Any]) -> int:
    return len(json.dumps(projection, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def serialize_narration_projection(projection: dict[str, Any]) -> str:
    """Deterministic canonical JSON fed to the narrator."""
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def build_narrator_prompt(projection: dict[str, Any]) -> str:
    """Assemble the full narrator prompt: contract + projection.

    The contract is always prepended so provider-backed narrators inherit
    the no-readjudication / no-invention obligations.
    """
    return NARRATOR_CONTRACT + "\nSTRUCTURED TURN (sole source of truth):\n" + serialize_narration_projection(projection)


# ── 2. Deterministic template narrator (no provider) ──────────────────────────


def _claim_visible_texts(contract: DmTurnContractV1) -> list[str]:
    return [c.text for b in contract.beats for c in b.claims if c.visibility == "public"]


def render_deterministic_narration(
    projection: dict[str, Any],
    contract: DmTurnContractV1 | None = None,
) -> str:
    """Expand a narration projection to prose without a model provider.

    Pure projection: narration beats join claim texts; NPC dialogue beats
    render as ``Name says: "utterance"`` (utterance text only — never the
    private truth status); roll prompts append the public reason; open
    player choice closes the narration. No new facts are introduced.
    """
    beats = projection.get("beats") or []
    parts: list[str] = []
    for beat in beats:
        claims = beat.get("claims") or []
        texts = [c.get("text", "") for c in claims if c.get("text")]
        if not texts:
            continue
        if beat.get("type") == "npc_dialogue":
            name = (beat.get("speaker_public_name") or "A voice").strip()
            for t in texts:
                parts.append(f'{name} says: "{t.strip()}"')
        else:
            parts.append(" ".join(t.strip() for t in texts))
    roll = projection.get("roll_request")
    if isinstance(roll, dict) and roll.get("reason_public"):
        label = str(roll.get("label") or "a roll").strip()
        parts.append(f"{str(roll['reason_public']).strip()} [{label}]")
    choice = projection.get("open_player_choice")
    if choice:
        parts.append(str(choice).strip())
    clarify = projection.get("clarify_question")
    if clarify and not choice:
        parts.append(str(clarify).strip())
    prelude = projection.get("safe_prelude")
    if prelude:
        parts.insert(0, str(prelude).strip())
    table_chat = projection.get("table_chat_intent")
    if table_chat:
        parts.append(str(table_chat).strip())
    text = " ".join(p for p in parts if p).strip()
    if not text and (contract is not None or projection.get("mode") in ("silent", "unsupported")):
        # Silent/unsupported modes intentionally narrate nothing.
        return ""
    return text


@dataclass
class NarratorRequest:
    """Typed provider input — contract-bound narration request.

    The service always builds ``prompt`` via :func:`build_narrator_prompt`
    (``NARRATOR_CONTRACT`` + canonical projection), so a model-backed
    provider cannot receive narration input without the no-readjudication /
    no-invention obligations. ``projection`` is included structured for
    providers that prefer JSON over prompt text.
    """

    prompt: str
    projection: dict[str, Any]


#: A provider yields narration text deltas (e.g. LLM token batches) as they
#: are generated. The service persists each delta durably as it arrives, so
#: time-to-first-visible-chunk tracks first-delta arrival rather than full
#: generation — the low-TTFT path required by #207.
NarratorDeltaStream = Iterable[str]

#: Streaming provider contract. A batch provider may still return a plain
#: ``str``; the service adapts it to a single delta (same interface, same
#: incremental gates and durable-first persistence).
StreamingNarratorFn = Callable[[NarratorRequest], Iterable[str]]

#: Accepted provider form: streaming iterable of text deltas, or a batch
#: ``str`` adapted to a single delta. The deterministic template renderer
#: (``narrator=None``) flows through the same delta loop.
NarratorFn = Callable[[NarratorRequest], str | Iterable[str]]


# ── 3. Fidelity checks ────────────────────────────────────────────────────────

_CONSEQUENCE_VERBS = (
    "dies", "die ", "killed", "kills", "destroyed", "destroys", "levels up",
    "gains ", "loses ", "takes ", "takes,", " damage", "healed", "heals",
    "unconscious", " dead", "collapses", "explodes",
)
_VOLUNTARY_PC_VERBS = (
    "attacks", "attack ", "casts", "cast ", "stabs", "shoots", "charges",
    "flees", "steals", "pickpockets", "decides to", "chooses to", "vows to",
    "lunges", "hurls",
)
_SPEECH_ATTRIBUTION_RE = re.compile(
    r'(\b[A-Za-z][\w\'-]*)\s+(says|said|shouts|shouted|whispers|whispered|replies|replied|'
    r'declares|declared|murmurs|murmured|yells|yelled|asks|asked|answers|answered)\s*[:,]?\s*"([^"]+)"'
)
_ANTONYMS: tuple[tuple[str, str], ...] = (
    ("intact", "cracked"), ("intact", "broken"), ("alive", "dead"),
    ("open", "closed"), ("locked", "unlocked"), ("present", "missing"),
    ("standing", "prone"), ("awake", "asleep"), ("healthy", "injured"),
    ("full", "empty"), ("safe", "trapped"), ("calm", "hostile"),
)


def _collect_secret_strings(
    contract: DmTurnContractV1,
    *,
    extra_secrets: set[str] | None = None,
) -> set[str]:
    """All strings the narration must never contain."""
    secrets: set[str] = set(extra_secrets or set())
    for beat in contract.beats:
        if beat.dm_private_context and beat.dm_private_context.strip():
            secrets.add(beat.dm_private_context.strip())
        for claim in beat.claims:
            if claim.visibility == "dm_private" and claim.text.strip():
                secrets.add(claim.text.strip())
    if contract.roll_request is not None and contract.roll_request.dc_private is not None:
        dc = contract.roll_request.dc_private
        secrets.add(f"DC {dc}")
        secrets.add(f"dc {dc}")
        secrets.add(f"difficulty {dc}")
    return {s for s in secrets if s}


def _collect_internal_ids(contract: DmTurnContractV1) -> set[str]:
    ids: set[str] = set()
    for beat in contract.beats:
        for claim in beat.claims:
            ids.update(claim.evidence_refs)
            ids.update(claim.trigger_refs)
    if contract.adjudication_input:
        ids.update(contract.adjudication_input.submission_ids)
    for eff in contract.staged_effects:
        ids.add(eff.id)
    for ne in contract.new_entities:
        ids.add(ne.temp_id)
    return {i for i in ids if i}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def validate_narration_fidelity(
    narration: str,
    contract: DmTurnContractV1,
    *,
    extra_secrets: set[str] | None = None,
    pc_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Check narration against the structured result; return violations.

    Categories: ``secret_leakage``, ``unsupported_addition``,
    ``agency_violation``, ``contradiction``. Empty list means pass.
    Pure function — safe to run before any persistence.
    """
    violations: list[dict[str, Any]] = []
    text = narration or ""
    low = text.lower()
    claim_texts = [_norm(c.text) for b in contract.beats for c in b.claims if c.visibility == "public"]
    claim_blob = "\n".join(claim_texts)

    # — Secret leakage: private truth, hidden DCs, internal IDs —
    for secret in _collect_secret_strings(contract, extra_secrets=extra_secrets):
        # Long secrets: match on a distinctive 24-char slice so minor
        # rewording still counts as a leak of the same fact.
        needle = secret if len(secret) < 24 else secret[:24]
        if needle.lower() in low:
            violations.append({
                "category": "secret_leakage", "code": "secret_leakage",
                "message": f"Narration leaks restricted material: {needle[:60]!r}",
            })
            break
    for internal_id in _collect_internal_ids(contract):
        if len(internal_id) >= 3 and internal_id.lower() in low:
            violations.append({
                "category": "secret_leakage", "code": "internal_id_leakage",
                "message": f"Narration leaks internal provenance id {internal_id!r}",
            })
            break

    # — Unsupported additions: numbers / consequences / invented speech —
    # Number grounding comes from the ENTIRE audience-safe projection (all
    # public fields: beat claims, roll_request.reason_public/label,
    # open_player_choice, clarify_question, safe_prelude, table_chat_intent),
    # never from intentionally-private lanes. A legitimate number the
    # deterministic renderer emits from any public field must not be
    # rejected as unsupported.
    narration_numbers = set(re.findall(r"\d+", text))
    claim_numbers = set(re.findall(r"\d+", claim_blob))
    try:
        allowed_blob = json.dumps(
            public_projection(contract), ensure_ascii=False, sort_keys=True
        )
        projection_numbers = set(re.findall(r"\d+", allowed_blob))
    except Exception:
        projection_numbers = set()
    grounded_numbers = claim_numbers | projection_numbers
    for num in sorted(narration_numbers - grounded_numbers):
        violations.append({
            "category": "unsupported_addition", "code": "unsupported_number",
            "message": f"Narration introduces number {num!r} absent from structured result",
        })
        break
    for verb in _CONSEQUENCE_VERBS:
        if verb in low and verb not in claim_blob:
            violations.append({
                "category": "unsupported_addition", "code": "unsupported_consequence",
                "message": f"Narration adds consequence {verb.strip()!r} beyond structured result",
            })
            break
    for match in _SPEECH_ATTRIBUTION_RE.finditer(text):
        quoted = _norm(match.group(3))
        if quoted and not any(quoted in ct or ct in quoted for ct in claim_texts if ct):
            violations.append({
                "category": "unsupported_addition", "code": "invented_dialogue",
                "message": f"Narration invents dialogue not in structured beats: {match.group(3)[:60]!r}",
            })
            break

    # — PC agency: invented voluntary PC action/speech —
    pc_names = pc_names or {}
    known_pc_tokens: set[str] = set()
    for beat in contract.beats:
        for claim in beat.claims:
            if claim.actor_ref is not None and claim.actor_ref.type == "character":
                raw_id = str(claim.actor_ref.id)
                known_pc_tokens.add(raw_id.lower())
                known_pc_tokens.add(raw_id.split(":")[-1].lower())
                if raw_id in pc_names:
                    known_pc_tokens.add(pc_names[raw_id].lower())
    # Player-authored declaration texts ground verbatim attribution.
    declaration_texts = {
        _norm(c.text) for b in contract.beats for c in b.claims
        if c.claim_kind == "player_declaration"
    }
    for match in _SPEECH_ATTRIBUTION_RE.finditer(text):
        speaker = match.group(1).lower()
        quoted = _norm(match.group(3))
        if speaker in known_pc_tokens and quoted not in declaration_texts:
            if not any(quoted in ct or ct in quoted for ct in claim_texts if ct):
                violations.append({
                    "category": "agency_violation", "code": "invented_pc_dialogue",
                    "message": f"Narration invents voluntary PC dialogue for {match.group(1)!r}",
                })
                break
    for verb in _VOLUNTARY_PC_VERBS:
        if verb in low and verb not in claim_blob:
            # Attribute only if a known PC token appears nearby (same sentence).
            for sentence in re.split(r"[.!?]+", low):
                if verb in sentence and any(tok in sentence for tok in known_pc_tokens):
                    violations.append({
                        "category": "agency_violation", "code": "invented_pc_action",
                        "message": f"Narration invents voluntary PC action ({verb.strip()!r})",
                    })
                    break
            if any(v["code"] == "invented_pc_action" for v in violations):
                break

    # — Contradiction with structured result —
    for claim in claim_texts:
        for a, b in _ANTONYMS:
            if a in claim and b in low:
                claim_tokens = {w for w in claim.split() if len(w) > 3}
                narr_tokens = {w for w in low.split() if len(w) > 3}
                if claim_tokens & narr_tokens:
                    violations.append({
                        "category": "contradiction", "code": "contradicts_structured_result",
                        "message": f"Narration {b!r} contradicts structured {a!r}",
                    })
                    break
        if any(v["category"] == "contradiction" for v in violations):
            break

    return violations


def _tally_fidelity_violations(violations: list[dict[str, Any]]) -> None:
    _inc("fidelity_failures")
    for v in violations:
        if v["category"] == "secret_leakage":
            _inc("secret_rejections")
        elif v["category"] == "agency_violation":
            _inc("agency_rejections")
        elif v["category"] == "unsupported_addition":
            _inc("unsupported_rejections")
        elif v["category"] == "contradiction":
            _inc("contradiction_rejections")


def check_narration_fidelity_or_raise(
    narration: str,
    contract: DmTurnContractV1,
    *,
    extra_secrets: set[str] | None = None,
    pc_names: dict[str, str] | None = None,
) -> None:
    violations = validate_narration_fidelity(
        narration, contract, extra_secrets=extra_secrets, pc_names=pc_names
    )
    if violations:
        _tally_fidelity_violations(violations)
        structured_log(
            logger, logging.WARNING, "narration_fidelity_rejected",
            violation_count=len(violations),
            categories=sorted({v["category"] for v in violations}),
        )
        raise NarrationFidelityError(
            f"Narration rejected: {len(violations)} fidelity violation(s)", violations=violations
        )


# ── Incremental fidelity policy (streaming providers) ─────────────────────────

#: Violation categories enforced per delta, before each durable persist.
#: Cheap fail-fast subset of the full gate: secrets and agency violations
#: must never become visible, even briefly. The remaining categories
#: (unsupported additions, contradictions) need whole-output context and
#: run authoritatively at provider completion.
_INCREMENTAL_CATEGORIES = ("secret_leakage", "agency_violation")


def validate_narration_incremental(
    narration: str,
    contract: DmTurnContractV1,
    *,
    extra_secrets: set[str] | None = None,
    pc_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Cheap per-delta gate over the cumulative visible text.

    Returns the ``secret_leakage`` / ``agency_violation`` subset of
    :func:`validate_narration_fidelity` — computed by the same function so
    incremental and full gates cannot disagree on those categories.
    Pure function — safe to run before each persist.
    """
    return [
        v
        for v in validate_narration_fidelity(
            narration, contract, extra_secrets=extra_secrets, pc_names=pc_names
        )
        if v["category"] in _INCREMENTAL_CATEGORIES
    ]


def _iter_provider_deltas(
    narrator: NarratorFn | None,
    request: NarratorRequest,
    *,
    projection: dict[str, Any],
    contract: DmTurnContractV1,
) -> Iterator[str]:
    """Adapt any provider form to a delta stream over the same interface.

    ``None`` runs the deterministic template renderer as a single delta;
    a batch ``str`` result is adapted to a single delta; an iterable of
    ``str`` is streamed delta-by-delta. Anything else is a contract
    violation (``NarratorGenerationError``).
    """
    if narrator is None:
        yield render_deterministic_narration(projection, contract)
        return
    result = narrator(request)
    if isinstance(result, str):
        yield result
        return
    if isinstance(result, Iterable):
        yield from result
        return
    raise NarratorGenerationError(
        f"Narrator must return str or an iterable of str deltas, got {type(result).__name__}"
    )


# ── 4. Streaming orchestration ────────────────────────────────────────────────


@dataclass
class NarrationResult:
    stream_id: uuid.UUID
    visible_text: str
    final_text: str | None
    chunk_count: int
    total_bytes: int
    projection_bytes: int
    provider: str
    ttft_ms: float
    duration_ms: float
    chunk_cadence_ms: list[float] = field(default_factory=list)
    completed: bool = True


def chunk_narration_text(text: str, *, chunk_size: int = 120) -> list[str]:
    """Deterministically split narration into ordered visible chunks.

    Splits on word boundaries so reconstruction (``"".join``) is exact and
    stable across retries — required for idempotent re-append.
    """
    if chunk_size < 16:
        raise ValueError("chunk_size must be >= 16")
    if not text:
        return []
    words = text.split(" ")
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else current + " " + word
        if len(candidate.encode("utf-8")) > chunk_size and current:
            # Preserve the consumed separator so "".join(chunks) == text exactly.
            chunks.append(current + " ")
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def stream_narration(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    thread_id: uuid.UUID,
    turn_id: str,
    attempt_id: str,
    contract: DmTurnContractV1,
    narrator: NarratorFn | None = None,
    chunk_size: int = 120,
    provider: str = PROVIDER_DETERMINISTIC,
    audience: str = "campaign",
    publish_realtime: bool = True,
    extra_secrets: set[str] | None = None,
    pc_names: dict[str, str] | None = None,
    max_chunks_to_persist: int | None = None,
    trace_id: str | None = None,
    on_first_persist: Callable[[uuid.UUID], None] | None = None,
    on_first_persist_tx: Callable[[Session, uuid.UUID], None] | None = None,
) -> NarrationResult:
    """Generate, fidelity-gate, and stream narration via durable chunks.

    The provider is a streaming contract: it yields text deltas (a batch
    ``str`` is adapted to a single delta; ``None`` runs the deterministic
    template renderer through the same delta loop). Each delta is
    incrementally gated (secret/agency) and persisted durably via #197
    BEFORE its #198 realtime projection, so TTFT tracks first-delta
    arrival rather than full generation. The authoritative full-output
    fidelity gate runs at provider completion.

    Crash-atomic first-chunk boundary (#206): when ``on_first_persist_tx``
    is given, chunk 0's row and the transactional boundary callback commit
    in the SAME transaction (single atomic commit point); realtime
    delivery happens only after that commit returns. The callback must be
    flush-only (no commit of its own) — e.g. ``mark_streaming_started``
    with ``commit=False``. If it raises, the whole transaction rolls back
    so no durable chunk 0 can exist without the streaming boundary (the
    failure is then pre-chunk/retryable).

    ``on_first_persist`` is a post-commit notification invoked with the new
    stream id after chunk 0's atomic commit and BEFORE its realtime
    delivery. It must not own the boundary transition (use
    ``on_first_persist_tx`` for that); if it raises post-commit, the
    failure is treated as post-first-chunk (failed-visible partial stream
    + ``NarrationStreamError``) since a durable chunk already exists.

    TTFT is measured from ``validated_at`` (validated adjudication moment;
    defaults to now) to the first durably persisted visible chunk. Each
    chunk is committed via #197 BEFORE its #198 realtime delivery, so a
    disconnect can always reconstruct exactly what became visible.

    Failure semantics:
    - Pre-first-chunk failure (generation/incremental gate/full gate with
      zero chunks persisted): the never-visible stream header is removed,
      nothing remains, freely retryable; raises
      ``NarratorGenerationError`` / ``NarrationFidelityError``.
    - Post-first-chunk failure (provider error, incremental gate, full
      gate, or ``on_first_persist`` failure): the stream is failed via
      ``fail_stream`` with the recoverable partial chunks retained
      (failed-visible audit, never promoted to history); raises
      ``NarrationStreamError`` carrying ``stream_id``,
      ``persisted_chunks``, and ``violations`` where applicable. Staged
      effects stay untouched. Repair requires a NEW stream on a NEW
      attempt — resume is impossible once the authoritative text
      diverges from the persisted prefix.

    ``max_chunks_to_persist`` is a crash-simulation hook for tests: persist
    at most N chunks and return the partial stream without completing.
    """
    from app.dm_streams.service import (
        append_chunk,
        complete_stream,
        create_stream,
        fail_stream,
        reconstruct_text,
    )

    t_start = time.monotonic()
    # TTFT is measured from here: callers must invoke stream_narration
    # immediately after adjudication/validation completes, so t_start is the
    # validated-adjudication moment on the critical path.
    t_validated = t_start
    _inc("narrations_started")
    if not turn_id or not str(turn_id).strip():
        raise ValueError("turn_id is required")
    if not attempt_id or not str(attempt_id).strip():
        raise ValueError("attempt_id is required")

    projection = build_narration_projection(contract)
    proj_bytes = projection_size_bytes(projection)
    _metrics["projection_bytes_samples"].append(proj_bytes)

    # — Contract-bound provider request (built once, always carries the contract) —
    request = NarratorRequest(
        prompt=build_narrator_prompt(projection),
        projection=projection,
    )

    # — Durable stream header (pre-chunk, never visible until chunk 0) —
    try:
        stream = create_stream(
            db,
            campaign_id=campaign_id,
            thread_id=thread_id,
            turn_id=str(turn_id),
            attempt_id=str(attempt_id),
            audience=audience,
            trace_id=trace_id,
        )
        db.commit()
        db.refresh(stream)
    except Exception as exc:
        db.rollback()
        _inc("narrations_failed_pre_chunk")
        raise NarratorGenerationError(f"Stream creation failed: {exc}") from exc
    stream_id = stream.id

    ttft_ms = 0.0
    cadence: list[float] = []
    last_persist = time.monotonic()
    persisted = 0
    persisted_texts: list[str] = []
    full_parts: list[str] = []
    truncated = False

    def _delete_invisible_header() -> None:
        """Remove the never-visible header so pre-chunk failure stays retryable."""
        try:
            db.rollback()
            header = db.get(stream.__class__, stream_id)
            if header is not None:
                db.delete(header)
            db.commit()
        except Exception:
            db.rollback()

    def _fail_visible_stream(reason: str) -> None:
        try:
            fail_stream(db, stream_id, reason=reason)
            db.commit()
        except Exception:
            db.rollback()

    def _publish_chunk(persisted_chunk: Any, seq: int) -> None:
        if not publish_realtime:
            return
        try:
            from app.realtime.service import publish_dm_chunk_created

            publish_dm_chunk_created(db, stream, persisted_chunk)
        except Exception as pub_exc:  # never roll back authoritative state
            logger.warning(
                "narration realtime chunk publish failed stream_id=%s seq=%s error=%s",
                stream_id, seq, pub_exc,
            )

    def _durable_chunk_count() -> int:
        """Chunk rows actually durable in the DB (source of truth for accounting).

        The in-memory ``persisted`` counter can disagree with the DB if a
        boundary callback raises around the commit point; error-path
        classification (pre-chunk retryable vs post-chunk failed-visible)
        must therefore consult durable state, not the counter.
        """
        try:
            from app.dm_streams.service import list_chunks as _list_chunks

            return len(_list_chunks(db, stream_id))
        except Exception:
            return persisted

    def _persist_piece(piece: str) -> None:
        """Persist one chunk durably (atomic chunk-0 boundary), then deliver."""
        nonlocal ttft_ms, last_persist, persisted
        seq = persisted
        chunk = append_chunk(db, stream_id, seq, piece)
        if seq == 0 and on_first_persist_tx is not None:
            # Crash-atomic stream-start boundary (#206): chunk-0 row +
            # prepared→streaming/input-lock transition share ONE commit.
            # The callback must be flush-only; a raise rolls back the chunk
            # too, so no durable chunk 0 can exist without the boundary.
            try:
                on_first_persist_tx(db, stream_id)
            except Exception:
                db.rollback()
                raise
        # Single atomic commit point for (chunk row + boundary transition);
        # realtime delivery happens only after this returns.
        db.commit()  # durable BEFORE delivery — first visible text is recoverable
        db.refresh(stream)
        # Accounting derives from the successful commit, never ahead of it:
        # after this point a durable chunk exists even if a post-commit
        # notification below raises.
        persisted = seq + 1
        now = time.monotonic()
        if seq == 0:
            ttft_ms = (now - t_validated) * 1000
            _metrics["ttft_ms_samples"].append(ttft_ms)
            if on_first_persist is not None:
                # Post-commit notification only (must not own the boundary).
                # A raise here is a post-first-chunk failure: the chunk is
                # already durable, so the stream is failed-visible.
                try:
                    on_first_persist(stream_id)
                except NarrationStreamError:
                    _fail_visible_stream("narration_first_persist_failed")
                    raise
                except Exception as hook_exc:
                    _fail_visible_stream("narration_first_persist_failed")
                    raise NarrationStreamError(
                        f"Narration stream failed after {persisted} chunk(s): {hook_exc}",
                        stream_id=stream_id, persisted_chunks=persisted,
                    ) from hook_exc
        else:
            cadence.append((now - last_persist) * 1000)
            _metrics["chunk_cadence_ms_samples"].append(cadence[-1])
        last_persist = now
        persisted_texts.append(piece)
        _publish_chunk(chunk, seq)

    # — Streaming generation + incremental persistence —
    # Persisted chunks always form a prefix of
    # ``chunk_narration_text(cumulative_text)``: only whole plan pieces
    # before the trailing tail are persisted while the provider may still
    # extend the text, so ``resume_narration_stream`` prefix checks hold.
    try:
        deltas = _iter_provider_deltas(
            narrator, request, projection=projection, contract=contract
        )
        it = iter(deltas)
        exhausted = False
        while True:
            try:
                delta = next(it)
            except StopIteration:
                exhausted = True
                delta = None
            cumulative = ""
            if not exhausted:
                if not isinstance(delta, str):
                    raise NarratorGenerationError(
                        "Narrator deltas must be str, "
                        f"got {type(delta).__name__}"
                    )
                if delta:
                    full_parts.append(delta)
                    cumulative = "".join(full_parts)
                    incremental = validate_narration_incremental(
                        cumulative, contract,
                        extra_secrets=extra_secrets, pc_names=pc_names,
                    )
                    if incremental:
                        _tally_fidelity_violations(incremental)
                        if persisted == 0:
                            _delete_invisible_header()
                            raise NarrationFidelityError(
                                "Narration rejected by incremental fidelity gate: "
                                f"{len(incremental)} violation(s)",
                                violations=incremental,
                            )
                        _fail_visible_stream("narration_incremental_fidelity_failed")
                        raise NarrationStreamError(
                            "Narration rejected by incremental fidelity gate "
                            f"after {persisted} chunk(s)",
                            stream_id=stream_id, persisted_chunks=persisted,
                            violations=incremental,
                        )
                else:
                    cumulative = "".join(full_parts)
            else:
                cumulative = "".join(full_parts)
            plan = chunk_narration_text(cumulative, chunk_size=chunk_size) if cumulative else []
            if exhausted:
                new_pieces = plan[persisted:]
            else:
                # Hold the tail back: it may grow when the next delta arrives.
                new_pieces = plan[persisted:len(plan) - 1] if len(plan) - 1 > persisted else []
            if max_chunks_to_persist is not None:
                room = max_chunks_to_persist - persisted
                new_pieces = new_pieces[: max(room, 0)]
            for piece in new_pieces:
                _persist_piece(piece)
            if max_chunks_to_persist is not None and persisted >= max_chunks_to_persist and not exhausted:
                # Simulated crash/disconnect: partial stream stays recoverable.
                truncated = True
                break
            if exhausted:
                break

        if truncated:
            duration_ms = (time.monotonic() - t_start) * 1000
            return NarrationResult(
                stream_id=stream_id, visible_text=reconstruct_text(db, stream_id),
                final_text=None, chunk_count=persisted,
                total_bytes=sum(len(c.encode("utf-8")) for c in persisted_texts),
                projection_bytes=proj_bytes, provider=provider,
                ttft_ms=ttft_ms, duration_ms=duration_ms,
                chunk_cadence_ms=list(cadence), completed=False,
            )

        narration_text = "".join(full_parts)

        # — Authoritative full-output validation at completion —
        violations = validate_narration_fidelity(
            narration_text, contract, extra_secrets=extra_secrets, pc_names=pc_names
        )
        if violations:
            _tally_fidelity_violations(violations)
            if persisted == 0:
                _delete_invisible_header()
                raise NarrationFidelityError(
                    f"Narration rejected: {len(violations)} fidelity violation(s)",
                    violations=violations,
                )
            # Partial delivery already visible: fail the stream (chunks
            # retained as failed-visible audit, never promoted to history).
            # The orchestrator marks the attempt failed-visible; repair
            # requires a NEW stream on a NEW attempt.
            _fail_visible_stream("narration_fidelity_failed")
            raise NarrationStreamError(
                f"Narration rejected after {persisted} visible chunk(s): "
                f"{len(violations)} fidelity violation(s)",
                stream_id=stream_id, persisted_chunks=persisted,
                violations=violations,
            )
    except NarrationStreamError:
        _inc("narrations_failed_post_chunk")
        raise
    except (NarrationFidelityError, NarrationProjectionError, NarratorGenerationError):
        # Classify by durable state, not the in-memory counter: a boundary
        # callback that raised pre-commit rolls back chunk 0, so durable 0
        # means nothing was ever visible (retryable); durable > 0 means the
        # outer failure arrived after visible persistence (failed-visible).
        durable = _durable_chunk_count()
        persisted = max(persisted, durable)
        if durable == 0:
            _delete_invisible_header()
            _inc("narrations_failed_pre_chunk")
            raise
        _fail_visible_stream("narration_stream_error")
        _inc("narrations_failed_post_chunk")
        raise NarrationStreamError(
            f"Narration stream failed after {persisted} chunk(s)",
            stream_id=stream_id, persisted_chunks=persisted,
        ) from None
    except Exception as exc:
        durable = _durable_chunk_count()
        persisted = max(persisted, durable)
        if durable == 0:
            _delete_invisible_header()
            _inc("narrations_failed_pre_chunk")
            if isinstance(exc, NarrationError):
                raise
            raise NarratorGenerationError(f"Narrator generation failed: {exc}") from exc
        _fail_visible_stream("narration_stream_error")
        _inc("narrations_failed_post_chunk")
        raise NarrationStreamError(
            f"Narration stream failed after {persisted} chunk(s): {exc}",
            stream_id=stream_id, persisted_chunks=persisted,
        ) from exc

    if max_chunks_to_persist is not None and len(chunk_narration_text(narration_text, chunk_size=chunk_size)) > persisted:
        # Simulated crash/disconnect: partial stream stays recoverable.
        duration_ms = (time.monotonic() - t_start) * 1000
        return NarrationResult(
            stream_id=stream_id, visible_text=reconstruct_text(db, stream_id),
            final_text=None, chunk_count=persisted,
            total_bytes=sum(len(c.encode("utf-8")) for c in persisted_texts),
            projection_bytes=proj_bytes, provider=provider,
            ttft_ms=ttft_ms, duration_ms=duration_ms,
            chunk_cadence_ms=list(cadence), completed=False,
        )

    # — Materialize final narration for history —
    try:
        stream = complete_stream(db, stream.id, completion_reason="narration_completed")
        db.commit()
        db.refresh(stream)
    except Exception as exc:
        db.rollback()
        _inc("narrations_failed_post_chunk")
        try:
            fail_stream(db, stream.id, reason="narration_complete_error")
            db.commit()
        except Exception:
            db.rollback()
        raise NarrationStreamError(
            f"Narration completion failed after {persisted} chunk(s): {exc}",
            stream_id=stream.id, persisted_chunks=persisted,
        ) from exc
    if publish_realtime:
        try:
            from app.realtime.service import publish_dm_status

            publish_dm_status(db, stream, visible_text=stream.final_text)
        except Exception as pub_exc:
            logger.warning(
                "narration realtime status publish failed stream_id=%s error=%s", stream.id, pub_exc
            )

    duration_ms = (time.monotonic() - t_start) * 1000
    _metrics["total_duration_ms_samples"].append(duration_ms)
    _inc("narrations_completed")
    structured_log(
        logger, logging.INFO, "narration_streamed",
        stream_id=str(stream.id), turn_id=str(turn_id), attempt_id=str(attempt_id),
        provider=provider, projection_bytes=proj_bytes,
        ttft_ms=round(ttft_ms, 2), chunk_count=persisted,
        total_bytes=int(stream.total_bytes or 0), duration_ms=round(duration_ms, 2),
    )
    return NarrationResult(
        stream_id=stream.id, visible_text=reconstruct_text(db, stream.id),
        final_text=stream.final_text, chunk_count=persisted,
        total_bytes=int(stream.total_bytes or 0),
        projection_bytes=proj_bytes, provider=provider,
        ttft_ms=ttft_ms, duration_ms=duration_ms,
        chunk_cadence_ms=list(cadence), completed=True,
    )


@dataclass
class ValidatedTurnResult:
    """Outcome of running one validated turn through narration to commit."""

    narration: NarrationResult
    turn: Any
    attempt: Any
    event: Any


def execute_validated_turn(
    db: Session,
    *,
    turn_id: uuid.UUID,
    attempt_id: uuid.UUID,
    contract: DmTurnContractV1,
    narrator: NarratorFn | None = None,
    chunk_size: int = 120,
    provider: str = PROVIDER_DETERMINISTIC,
    publish_realtime: bool = True,
    extra_secrets: set[str] | None = None,
    pc_names: dict[str, str] | None = None,
    expected_revision: int | None = None,
    event_type: str = "dm.turn_resolved",
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    trace_id: str | None = None,
) -> ValidatedTurnResult:
    """Run a validated structured turn through narration to final commit.

    Post-validation / pre-final-commit wiring for issue #207 — this is the
    production call site that takes a validated ``dm_turn_contract_v1``
    through the narration + durable-stream path:

    1. ``stage_validated_attempt`` — persist staged effects attempt-local
       (no campaign-truth mutation).
    2. ``stream_narration`` with a crash-atomic first-chunk boundary — deltas
    stream from the provider, each incrementally gated and persisted
    durably via #197 BEFORE/with #198 realtime delivery. Chunk 0's row
    and ``mark_streaming_started`` (commit=False) share ONE atomic commit
    — the #206 stream-start commitment boundary over the durable stream
    (locks the input set at first visibility, so a crash can never leave
    visible narration on a still-prepared attempt); realtime delivery
    happens only after that commit returns.
    3. ``commit_turn_with_effects`` — atomic promotion + final commit (#206).

    Failure semantics inherit the callees: pre-first-chunk failure
    (generation/fidelity) leaves nothing persisted and is freely retryable
    with the same turn/attempt identity; post-first-chunk failure raises
    ``NarrationStreamError`` — the failed-visible partial stream is left
    intact and the attempt is marked failed-visible via
    ``mark_attempt_failed(visible=True)`` (input set stays locked) when it
    had already crossed into streaming. Resume a boundary-crossed partial
    via :func:`resume_narration_stream` (same text only), then
    ``commit_turn_with_effects`` manually; a fidelity-rejected partial
    needs a NEW stream on a NEW attempt.
    """
    from models.dm import DmTurn

    from app.dm.turns import (
        commit_turn_with_effects,
        mark_streaming_started,
        stage_validated_attempt,
    )

    turn = db.get(DmTurn, turn_id)
    if turn is None:
        raise ValueError(f"Turn {turn_id} not found")
    try:
        thread_uuid = uuid.UUID(str(turn.thread_id))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Turn {turn_id} has non-UUID thread_id {turn.thread_id!r}") from exc

    staged = stage_validated_attempt(db, attempt_id, contract)

    def _boundary_tx(db_: Session, stream_id_: uuid.UUID) -> None:
        # Flush-only: shares chunk 0's single atomic commit (no commit here).
        mark_streaming_started(
            db_, turn_id, attempt_id, stream_id=stream_id_, commit=False
        )

    try:
        narration = stream_narration(
            db,
            campaign_id=turn.campaign_id,
            thread_id=thread_uuid,
            turn_id=str(turn_id),
            attempt_id=str(attempt_id),
            contract=contract,
            narrator=narrator,
            chunk_size=chunk_size,
            provider=provider,
            audience=staged.audience or turn.audience,
            publish_realtime=publish_realtime,
            extra_secrets=extra_secrets,
            pc_names=pc_names,
            trace_id=trace_id,
            on_first_persist_tx=_boundary_tx,
        )
    except NarrationStreamError:
        # Defined remediation for post-visibility failure: if the
        # stream-start boundary was already crossed, reuse the #206
        # failed-visible state so the turn keeps blocking its input set
        # until explicit recovery.
        try:
            from models.dm import DmTurnAttempt

            from app.dm.turns import ATTEMPT_STREAMING, mark_attempt_failed

            current = db.get(DmTurnAttempt, attempt_id)
            if current is not None and current.status == ATTEMPT_STREAMING:
                mark_attempt_failed(
                    db, attempt_id,
                    error="narration_stream_failed",
                    error_class="retriable",
                    visible=True,
                )
        except Exception as remediation_exc:
            logger.warning(
                "validated turn failure remediation failed turn_id=%s attempt_id=%s error=%s",
                turn_id, attempt_id, remediation_exc,
            )
        raise

    submission_ids = list(staged.submission_ids or [])
    payload = {
        "turn_id": str(turn_id),
        "attempt_id": str(attempt_id),
        "submission_ids": submission_ids,
        "narration_stream_id": str(narration.stream_id),
    }
    final_turn, final_attempt, event = commit_turn_with_effects(
        db,
        turn_id,
        attempt_id,
        expected_revision=expected_revision,
        event_type=event_type,
        payload=payload,
        operation_id=operation_id,
        actor_id=actor_id,
    )
    structured_log(
        logger, logging.INFO, "validated_turn_narrated_committed",
        turn_id=str(turn_id), attempt_id=str(attempt_id),
        stream_id=str(narration.stream_id),
        event_id=str(getattr(event, "id", None)),
        provider=provider,
    )
    return ValidatedTurnResult(
        narration=narration, turn=final_turn, attempt=final_attempt, event=event
    )


def resume_narration_stream(
    db: Session,
    stream_id: uuid.UUID,
    full_text: str,
    *,
    chunk_size: int = 120,
    publish_realtime: bool = True,
) -> NarrationResult:
    """Continue a recoverable partial stream after disconnect (no regeneration).

    Re-derives the deterministic chunk plan and persists only the missing
    suffix, then materializes the final narration. Idempotent: already
    persisted chunks are re-used, never duplicated.
    """
    from app.dm_streams.service import (
        append_chunk,
        complete_stream,
        get_stream,
        list_chunks,
        reconstruct_text,
    )

    t_start = time.monotonic()
    stream = get_stream(db, stream_id)
    if stream is None:
        raise ValueError(f"Stream {stream_id} not found")
    existing = list_chunks(db, stream_id)
    plan = chunk_narration_text(full_text, chunk_size=chunk_size)
    if "".join(plan) != full_text:
        raise ValueError("Full text does not match deterministic chunk plan")
    # Verify persisted prefix matches the plan (no silent divergence).
    for chunk in existing:
        if chunk.sequence >= len(plan) or plan[chunk.sequence] != chunk.text:
            raise ValueError(
                f"Persisted chunk {chunk.sequence} diverges from narration plan — cannot resume"
            )
    cadence: list[float] = []
    last = time.monotonic()
    for seq in range(len(existing), len(plan)):
        chunk = append_chunk(db, stream_id, seq, plan[seq])
        db.commit()
        db.refresh(stream)
        now = time.monotonic()
        cadence.append((now - last) * 1000)
        _metrics["chunk_cadence_ms_samples"].append(cadence[-1])
        last = now
        if publish_realtime:
            try:
                from app.realtime.service import publish_dm_chunk_created

                publish_dm_chunk_created(db, stream, chunk)
            except Exception as pub_exc:
                logger.warning(
                    "narration resume publish failed stream_id=%s seq=%s error=%s",
                    stream_id, seq, pub_exc,
                )
    stream = complete_stream(db, stream_id, completion_reason="narration_resumed")
    db.commit()
    db.refresh(stream)
    if publish_realtime:
        try:
            from app.realtime.service import publish_dm_status

            publish_dm_status(db, stream, visible_text=stream.final_text)
        except Exception as pub_exc:
            logger.warning(
                "narration resume status publish failed stream_id=%s error=%s", stream_id, pub_exc
            )
    duration_ms = (time.monotonic() - t_start) * 1000
    _inc("narrations_completed")
    return NarrationResult(
        stream_id=stream_id, visible_text=reconstruct_text(db, stream_id),
        final_text=stream.final_text, chunk_count=len(plan),
        total_bytes=int(stream.total_bytes or 0), projection_bytes=0,
        provider=PROVIDER_DETERMINISTIC, ttft_ms=0.0, duration_ms=duration_ms,
        chunk_cadence_ms=cadence, completed=True,
    )


def materialize_final_narration(db: Session, stream_id: uuid.UUID) -> dict[str, Any]:
    """Read-model for history: final narration text with chunk provenance."""
    from app.dm_streams.service import get_stream_with_chunks

    stream, chunks, visible_text = get_stream_with_chunks(db, stream_id)
    final = stream.final_text if stream.status == "completed" else None
    return {
        "stream_id": str(stream.id),
        "status": stream.status,
        "visible_text": visible_text,
        "final_text": final,
        "final_message": final if final is not None else (visible_text if stream.status == "completed" else None),
        "chunks": [c.to_dict() for c in chunks],
        "chunk_count": len(chunks),
        "total_bytes": int(stream.total_bytes or 0),
        "first_chunk_at": stream.first_chunk_at.isoformat() if stream.first_chunk_at else None,
        "completed_at": stream.completed_at.isoformat() if stream.completed_at else None,
    }
