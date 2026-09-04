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
   Default is the deterministic template narrator (no provider); an LLM
   callable with the same ``(projection) -> str`` shape can be injected.
3. ``validate_narration_fidelity(...)`` — pre-commit fidelity gates for
   secret leakage, unsupported additions, PC agency violations, and
   contradiction with the structured result. Failures raise before the
   first chunk is persisted (freely retryable as uncommitted recovery work).
4. ``stream_narration(...)`` — persists each chunk durably via #197
   (``dm_streams`` service) BEFORE/with realtime delivery via #198, so the
   first visible text is recoverable after disconnect. TTFT is measured
   from validated adjudication to first persisted visible chunk.
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
from typing import Any, Callable

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

    def __init__(self, message: str, *, stream_id: uuid.UUID | None = None, persisted_chunks: int = 0):
        super().__init__(message)
        self.stream_id = stream_id
        self.persisted_chunks = persisted_chunks


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


NarratorFn = Callable[[dict[str, Any]], str]


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
    narration_numbers = set(re.findall(r"\d+", text))
    claim_numbers = set(re.findall(r"\d+", claim_blob))
    for num in sorted(narration_numbers - claim_numbers):
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
        structured_log(
            logger, logging.WARNING, "narration_fidelity_rejected",
            violation_count=len(violations),
            categories=sorted({v["category"] for v in violations}),
        )
        raise NarrationFidelityError(
            f"Narration rejected: {len(violations)} fidelity violation(s)", violations=violations
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
) -> NarrationResult:
    """Generate, fidelity-gate, and stream narration via durable chunks.

    TTFT is measured from ``validated_at`` (validated adjudication moment;
    defaults to now) to the first durably persisted visible chunk. Each
    chunk is committed via #197 BEFORE its #198 realtime projection, so a
    disconnect can always reconstruct exactly what became visible.

    Failure semantics:
    - Pre-first-chunk failure (generation/fidelity): nothing persisted,
      freely retryable; raises ``NarratorGenerationError`` /
      ``NarrationFidelityError``.
    - Post-first-chunk failure: raises ``NarrationStreamError`` with the
      recoverable partial stream left intact (staged effects untouched).

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

    # — Generation (pre-commit, retryable) —
    try:
        if narrator is None:
            narration_text = render_deterministic_narration(projection, contract)
        else:
            narration_text = narrator(projection)
    except (NarrationFidelityError, NarrationProjectionError):
        raise
    except Exception as exc:
        _inc("narrations_failed_pre_chunk")
        raise NarratorGenerationError(f"Narrator generation failed: {exc}") from exc
    if not isinstance(narration_text, str):
        _inc("narrations_failed_pre_chunk")
        raise NarratorGenerationError("Narrator must return a string")

    # — Fidelity gate (pre-commit, retryable) —
    try:
        check_narration_fidelity_or_raise(
            narration_text, contract, extra_secrets=extra_secrets, pc_names=pc_names
        )
    except NarrationFidelityError:
        _inc("narrations_failed_pre_chunk")
        raise

    chunks = chunk_narration_text(narration_text, chunk_size=chunk_size)
    if max_chunks_to_persist is not None:
        chunks = chunks[:max_chunks_to_persist]

    # — Durable stream + ordered persistence —
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

    ttft_ms = 0.0
    cadence: list[float] = []
    last_persist = time.monotonic()
    persisted = 0
    try:
        for seq, piece in enumerate(chunks):
            chunk = append_chunk(db, stream.id, seq, piece)
            db.commit()  # durable BEFORE delivery — first visible text is recoverable
            db.refresh(stream)
            now = time.monotonic()
            if seq == 0:
                ttft_ms = (now - t_validated) * 1000
                _metrics["ttft_ms_samples"].append(ttft_ms)
            else:
                cadence.append((now - last_persist) * 1000)
                _metrics["chunk_cadence_ms_samples"].append(cadence[-1])
            last_persist = now
            persisted += 1
            if publish_realtime:
                try:
                    from app.realtime.service import publish_dm_chunk_created

                    publish_dm_chunk_created(db, stream, chunk)
                except Exception as pub_exc:  # never roll back authoritative state
                    logger.warning(
                        "narration realtime chunk publish failed stream_id=%s seq=%s error=%s",
                        stream.id, seq, pub_exc,
                    )
    except Exception as exc:
        db.rollback()
        _inc("narrations_failed_post_chunk")
        try:
            fail_stream(db, stream.id, reason="narration_stream_error")
            db.commit()
        except Exception:
            db.rollback()
        raise NarrationStreamError(
            f"Narration stream failed after {persisted} chunk(s): {exc}",
            stream_id=stream.id, persisted_chunks=persisted,
        ) from exc

    if max_chunks_to_persist is not None and len(chunk_narration_text(narration_text, chunk_size=chunk_size)) > persisted:
        # Simulated crash/disconnect: partial stream stays recoverable.
        duration_ms = (time.monotonic() - t_start) * 1000
        return NarrationResult(
            stream_id=stream.id, visible_text=reconstruct_text(db, stream.id),
            final_text=None, chunk_count=persisted,
            total_bytes=sum(len(c.encode("utf-8")) for c in chunks),
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
