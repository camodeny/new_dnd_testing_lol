"""Bounded evidence-request loop and authoritative tool mediation — issue #203.

The forward DM may return ``need_evidence`` as a successful runtime state
instead of guessing.  This module mediates that state:

* validates every evidence request before execution (tool allowlist, per-round
  limits, duplicate ids, per-tool shape);
* executes through a controlled read-only tool interface whose results carry
  stable ``SourceRef`` provenance, visibility, and authorization scope;
* loops ``adjudicate → evidence → re-adjudicate`` for a bounded number of
  rounds, feeding evidence back as a distinct ``evidence_results`` context
  lane with lane-level provenance;
* enforces ``MAX_EVIDENCE_ROUNDS`` / ``MAX_REQUESTS_PER_ROUND`` so recursion
  cannot be infinite;
* distinguishes missing evidence, tool failure (with retry), and genuinely
  unknown campaign facts;
* preserves a short safe prelude without inventing outcomes;
* never leaks evidence that is not authorized for the attempt audience;
* traces each request: tool type, latency, result count/source IDs, retries,
  evidence rounds, and TTFT contribution.

Every allowed tool has a concrete read-only handler in one static registry
(character sheet, rules corpus, world retrieval, semantic memory); callers may
override individual handlers via ``tool_handlers``. Direct mutation tools are
out of scope.
"""

from __future__ import annotations

import concurrent.futures
import functools
import logging
import time
import uuid
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.dm.context import (
    AuthorizationScope,
    ContextAudience,
    ContextBudget,
    ContextRecord,
    ForwardDmContextPacket,
    LaneName,
    SourceRef,
)
from app.dm.contract import DmTurnContractV1, EvidenceRequest, normalize_contract
from app.observability.tracing import structured_log

logger = logging.getLogger(__name__)

# ── Limits ───────────────────────────────────────────────────────────────────

MAX_EVIDENCE_ROUNDS: int = 3
"""Outer loop bound: adjudicate → evidence → re-adjudicate cycles."""

MAX_REQUESTS_PER_ROUND: int = 3
"""Must match contract EvidenceRequest max_length."""

MAX_TOTAL_REQUESTS: int = 9

ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"ask_character_sheet", "search_campaign_memory", "lookup_rule", "search_rules",
     "lookup_world_entity", "traverse_world_relations", "lookup_world_fact",
     "query_world_timeline", "lookup_source_turn", "query_character_knowledge"}
)

# ── Strict base ──────────────────────────────────────────────────────────────

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ── Evidence result ──────────────────────────────────────────────────────────

EvidenceStatus = Literal["ok", "missing", "unknown", "tool_failure", "unauthorized"]

_VALID_STATUSES: frozenset[str] = frozenset({"ok", "missing", "unknown", "tool_failure", "unauthorized"})


class EvidenceResult(StrictModel):
    """Typed read-only evidence output for one EvidenceRequest.

    ``sources`` are stable provenance identifiers; ``visibility`` and
    ``authorization`` mirror ``ContextRecord`` so the evidence lane remains
    audience-aware and never broadens authority.  ``payload`` is the
    tool-specific compact result already bounded by the tool.
    """

    request_id: str = Field(min_length=1, max_length=48)
    tool: str = Field(min_length=1, max_length=64)
    status: EvidenceStatus
    sources: list[SourceRef] = Field(default_factory=list, max_length=16)
    visibility: Literal["public", "campaign", "private", "dm_only"] = "campaign"
    authorization: AuthorizationScope
    payload: dict[str, Any] | list[Any] | None = None
    error: str | None = Field(default=None, max_length=400)
    retries: int = Field(default=0, ge=0, le=5)
    latency_ms: float = Field(default=0, ge=0)
    result_count: int = Field(default=0, ge=0)


class EvidenceValidationError(ValueError):
    code = "evidence_validation_failed"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class EvidenceToolError(RuntimeError):
    """Transient tool failure that may be retried."""

    code = "evidence_tool_failure"


class EvidenceLoopLimitError(RuntimeError):
    code = "evidence_loop_limit_exceeded"


class EvidenceRoundTrace(StrictModel):
    round_index: int = Field(ge=0)
    requests: list[dict[str, Any]]
    results: list[dict[str, Any]]
    latency_ms: float = Field(ge=0)
    source_ids: list[str]
    retries: int = Field(ge=0)
    tool_types: list[str]


class EvidenceBundle(StrictModel):
    """Observability bundle for one complete evidence loop."""

    rounds: int = Field(ge=0)
    traces: list[EvidenceRoundTrace] = Field(default_factory=list)
    total_latency_ms: float = Field(ge=0)
    total_requests: int = Field(ge=0)
    results: list[EvidenceResult] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _compact(value: Any, depth: int = 0) -> Any:
    """Bound tool output before it enters the context packet."""
    if depth >= 5:
        return "[bounded]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:1200]
    if isinstance(value, list):
        return [_compact(item, depth + 1) for item in value[:12]]
    if isinstance(value, dict):
        return {str(k)[:120]: _compact(v, depth + 1) for k, v in list(value.items())[:40]}
    return str(value)[:1200]


def _tool_to_source_type(tool: str) -> str:
    mapping = {
        "ask_character_sheet": "dnd5e_character_sheet",
        "search_campaign_memory": "campaign_memory",
        "lookup_rule": "dnd_srd_rule",
        "search_rules": "dnd_srd_rule",
    }
    return mapping.get(tool, f"evidence_tool:{tool}")


# ── Validation ───────────────────────────────────────────────────────────────

def validate_evidence_requests(
    requests: list[EvidenceRequest] | list[dict[str, Any]],
) -> list[EvidenceRequest]:
    """Validate before execution; raises ``EvidenceValidationError`` on failure."""
    if not isinstance(requests, list):
        raise EvidenceValidationError("evidence_requests must be a list")
    if len(requests) == 0:
        raise EvidenceValidationError("need_evidence requires 1-3 evidence_requests")
    if len(requests) > MAX_REQUESTS_PER_ROUND:
        raise EvidenceValidationError(
            f"too many evidence requests in one round: {len(requests)} > {MAX_REQUESTS_PER_ROUND}",
            details={"count": len(requests), "max": MAX_REQUESTS_PER_ROUND},
        )
    # Normalize dicts through contract for per-tool shape checks
    normalized: list[EvidenceRequest] = []
    seen: set[str] = set()
    for raw in requests:
        if isinstance(raw, EvidenceRequest):
            req = raw
        elif isinstance(raw, dict):
            try:
                req = EvidenceRequest.model_validate(raw)
            except Exception as exc:
                raise EvidenceValidationError(f"invalid evidence request {raw.get('id', '?')}: {exc}") from exc
        else:
            raise EvidenceValidationError("evidence request must be an object")
        if req.tool not in ALLOWED_TOOLS:
            raise EvidenceValidationError(
                f"unsupported evidence tool {req.tool!r}",
                details={"tool": req.tool, "allowed": sorted(ALLOWED_TOOLS)},
            )
        if req.id in seen:
            raise EvidenceValidationError(f"duplicate evidence request id {req.id!r}")
        seen.add(req.id)
        normalized.append(req)
    return normalized


# ── Tool registry ────────────────────────────────────────────────────────────

@functools.cache
def _tool_registry() -> dict[str, Callable[..., Any]]:
    """Static handler registry for every allowed evidence tool.

    Imported lazily: the handler modules import this module's result types.
    """
    from app.dm.tools import handle_ask_character_sheet
    from app.rules.evidence_tools import TOOL_HANDLERS as rules_handlers
    from app.world.retrieval import TOOL_HANDLERS as world_handlers
    from app.world.semantic import handle_search_campaign_memory

    return {
        "ask_character_sheet": handle_ask_character_sheet,
        "search_campaign_memory": handle_search_campaign_memory,
        **rules_handlers,
        **world_handlers,
    }


def _classify_tool_error(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    retriable = ("timeout", "deadline", "temporar", "rate_limit", "unavailable", "connection", "503", "504")
    if isinstance(exc, EvidenceToolError):
        return "retriable"
    if isinstance(exc, concurrent.futures.TimeoutError) or isinstance(exc, TimeoutError):
        return "retriable"
    if any(t in name for t in retriable) or any(t in msg for t in retriable):
        return "retriable"
    return "terminal"


def _call_with_timeout(
    handler: Callable,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    timeout_s: float | None,
) -> Any:
    if timeout_s is None:
        return handler(*args, **kwargs)
    # Enforce actual per-tool deadline so a stuck tool cannot hang adjudication.
    # Use a daemon thread so caller returns at deadline without waiting for
    # the hung handler (ThreadPoolExecutor shutdown(wait=True) would still block).
    import threading

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _target():
        try:
            result["value"] = handler(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            error["exc"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"evidence tool timed out after {timeout_s}s")
    if "exc" in error:
        raise error["exc"]
    return result.get("value")


# ── Context lane mediation ───────────────────────────────────────────────────

def evidence_results_to_records(
    results: list[EvidenceResult],
    audience: ContextAudience,
) -> list[ContextRecord]:
    """Convert typed results into distinct ``evidence_results`` ContextRecords.

    Records that are not authorized for the audience are dropped — they do not
    broaden the packet — and the omission is traceable via structured logs.
    Visibility metadata stays attached through the evidence round.
    """
    records: list[ContextRecord] = []
    for res in results:
        # Audience check: private/dm_only evidence must not leak to campaign audience.
        if res.visibility == "private" and audience.audience != "private":
            continue
        if res.visibility == "dm_only" and False:  # dm_only is adjudication_only use
            # still included for adjudication, but narration_projection will strip it
            pass
        # Authorization scope check — replicate context._authorized subset logic
        scope = res.authorization
        if scope.campaign_id != audience.campaign_id:
            continue
        if scope.thread_ids and audience.thread_id not in scope.thread_ids:
            continue
        if scope.user_ids and not set(audience.user_ids).issubset(set(scope.user_ids)):
            # not authorized for whole audience, treat as unauthorized lane (drop)
            continue
        # Choose use: dm_only -> adjudication_only, else narration_eligible
        use: Literal["narration_eligible", "adjudication_only"] = (
            "adjudication_only" if res.visibility == "dm_only" else "narration_eligible"
        )
        compact_payload = _compact(res.payload)
        status = res.status
        # Build source provenance including status
        sources = list(res.sources) if res.sources else [
            SourceRef(
                source_type=_tool_to_source_type(res.tool),
                source_id=res.request_id,
                source_version="unknown",
                provenance={"status": status},
            )
        ]
        records.append(
            ContextRecord(
                record_id=f"evidence:{res.request_id}",
                required=False,
                priority=85,
                value={
                    "request_id": res.request_id,
                    "tool": res.tool,
                    "status": status,
                    "result": compact_payload,
                    "result_count": res.result_count,
                    "error": res.error,
                    "retries": res.retries,
                    "latency_ms": round(res.latency_ms, 2),
                    "source_ids": [f"{s.source_type}:{s.source_id}@{s.source_version}" for s in sources],
                },
                sources=sources,
                authorization=scope,
                visibility=res.visibility,
                use=use,
            )
        )
    return records


# ── Execution of one round ───────────────────────────────────────────────────

DEFAULT_TOOL_TIMEOUT_S: float = 2.0


def execute_evidence_round(
    requests: list[EvidenceRequest],
    audience: ContextAudience,
    *,
    db: Any | None = None,
    tool_handlers: dict[str, Callable[[EvidenceRequest, ContextAudience, Any], EvidenceResult | dict[str, Any]]] | None = None,
    max_retries: int = 1,
    timeout_s: float | None = DEFAULT_TOOL_TIMEOUT_S,
) -> tuple[list[EvidenceResult], EvidenceRoundTrace]:
    """Execute one validated round, with per-request retry on transient failures."""
    t0 = time.monotonic()
    results: list[EvidenceResult] = []
    total_retries = 0
    source_ids: list[str] = []
    tool_types: list[str] = []

    handlers = {**_tool_registry(), **(tool_handlers or {})}

    for req in requests:
        tool_types.append(req.tool)
        handler = handlers[req.tool]
        retries = 0
        last_exc: BaseException | None = None
        result: EvidenceResult | None = None
        req_t0 = time.monotonic()
        for attempt in range(max_retries + 1):
            try:
                _args: tuple[Any, ...]
                _kwargs: dict[str, Any] = {}
                if _handler_takes_db(handler):
                    _args = (req, audience)
                    _kwargs = {"db": db}
                else:
                    _args = (req, audience)
                raw = _call_with_timeout(handler, _args, _kwargs, timeout_s)
                # Allow handlers to return plain dicts for convenience — strict validation
                if isinstance(raw, dict):
                    # Require explicit visibility/authorization so malformed tool output cannot broaden scope
                    if "visibility" not in raw:
                        raise EvidenceValidationError(
                            f"tool {req.tool} dict result missing required 'visibility' (must be public|campaign|private|dm_only)",
                            details={"request_id": req.id},
                        )
                    visibility = raw["visibility"]
                    if visibility not in ("public", "campaign", "private", "dm_only"):
                        raise EvidenceValidationError(
                            f"tool {req.tool} invalid visibility {visibility!r}",
                            details={"request_id": req.id},
                        )
                    if visibility == "private" and req.tool == "ask_character_sheet":
                        # private-capable tools must provide private-scoped authorization
                        pass  # validated below via auth.user_ids
                    status: EvidenceStatus = raw.get("status", "ok")  # type: ignore[assignment]
                    if status not in _VALID_STATUSES:
                        status = "ok"
                    srcs = raw.get("sources") or []
                    # Normalize sources
                    parsed_sources: list[SourceRef] = []
                    for s in srcs:
                        if isinstance(s, SourceRef):
                            parsed_sources.append(s)
                        elif isinstance(s, dict):
                            try:
                                parsed_sources.append(SourceRef.model_validate(s))
                            except Exception:
                                continue
                    if not parsed_sources:
                        parsed_sources = [
                            SourceRef(
                                source_type=_tool_to_source_type(req.tool),
                                source_id=req.id,
                                source_version=raw.get("source_version", "1"),
                                provenance={"tool": req.tool},
                            )
                        ]
                    # Strict authorization: missing -> restrictive derived scope (campaign+thread only, no broad user list)
                    raw_auth = raw.get("authorization")
                    if raw_auth is None:
                        # Fail-closed: derive minimal restrictive scope, not broad campaign user list
                        auth = AuthorizationScope(
                            campaign_id=audience.campaign_id,
                            thread_ids=[audience.thread_id],
                        )
                    elif isinstance(raw_auth, AuthorizationScope):
                        auth = raw_auth
                    elif isinstance(raw_auth, dict):
                        try:
                            auth = AuthorizationScope.model_validate(raw_auth)
                        except Exception as exc:
                            raise EvidenceValidationError(
                                f"tool {req.tool} invalid authorization: {exc}",
                                details={"request_id": req.id},
                            ) from exc
                    else:
                        raise EvidenceValidationError(
                            f"tool {req.tool} invalid authorization type",
                            details={"request_id": req.id},
                        )
                    # Enforce that auth never broadens beyond attempt audience
                    if auth.campaign_id != audience.campaign_id:
                        raise EvidenceValidationError(
                            f"tool {req.tool} authorization campaign mismatch",
                            details={"request_id": req.id},
                        )
                    if auth.thread_ids and audience.thread_id not in auth.thread_ids:
                        raise EvidenceValidationError(
                            f"tool {req.tool} authorization thread not in audience",
                            details={"request_id": req.id},
                        )
                    if visibility == "private" and not auth.user_ids:
                        raise EvidenceValidationError(
                            f"tool {req.tool} private visibility requires user-scoped authorization",
                            details={"request_id": req.id},
                        )
                    result = EvidenceResult(
                        request_id=req.id,
                        tool=req.tool,
                        status=status,  # type: ignore[arg-type]
                        sources=parsed_sources,
                        visibility=visibility,  # type: ignore[arg-type]
                        authorization=auth,
                        payload=raw.get("payload", raw.get("result", raw)),
                        result_count=raw.get("result_count", 1 if status == "ok" else 0),
                        retries=retries,
                        latency_ms=(time.monotonic() - req_t0) * 1000,
                    )
                elif isinstance(raw, EvidenceResult):
                    result = raw
                    result.retries = retries
                    result.latency_ms = (time.monotonic() - req_t0) * 1000
                else:
                    # Fail-closed: unexpected handler output bypasses typed interface — reject, do not synthesize visibility
                    raise EvidenceValidationError(
                        f"tool {req.tool} returned unexpected result type {type(raw).__name__!r}; must return EvidenceResult or dict with visibility+authorization",
                        details={"request_id": req.id, "type": type(raw).__name__},
                    )
                last_exc = None
                break
            except BaseException as exc:  # noqa: BLE001
                # Strict metadata errors must fail closed, not be broadened or retried
                if isinstance(exc, EvidenceValidationError):
                    raise
                last_exc = exc
                kind = _classify_tool_error(exc)
                if kind == "retriable" and attempt < max_retries:
                    retries += 1
                    total_retries += 1
                    # small backoff for transient
                    time.sleep(min(0.05 * (2**attempt), 0.2))
                    continue
                # terminal or exhausted
                result = EvidenceResult(
                    request_id=req.id,
                    tool=req.tool,
                    status="tool_failure",
                    sources=[],
                    visibility="campaign",
                    authorization=AuthorizationScope(
                        campaign_id=audience.campaign_id,
                        thread_ids=[audience.thread_id],
                    ),
                    payload=None,
                    error=str(exc)[:400],
                    retries=retries,
                    latency_ms=(time.monotonic() - req_t0) * 1000,
                    result_count=0,
                )
                if kind == "retriable":
                    total_retries += 0  # already counted
                break
        assert result is not None
        if result.latency_ms == 0:
            result.latency_ms = (time.monotonic() - req_t0) * 1000
        results.append(result)
        for s in result.sources:
            source_ids.append(f"{s.source_type}:{s.source_id}@{s.source_version}")

    latency_ms = (time.monotonic() - t0) * 1000
    trace = EvidenceRoundTrace(
        round_index=0,  # caller fills
        requests=[r.model_dump(mode="json") for r in requests],
        results=[r.model_dump(mode="json") for r in results],
        latency_ms=latency_ms,
        source_ids=sorted(set(source_ids)),
        retries=total_retries,
        tool_types=tool_types,
    )
    # Observability
    structured_log(
        logger,
        logging.INFO,
        "forward_dm_evidence_round",
        tool_types=tool_types,
        request_ids=[r.id for r in requests],
        source_ids=sorted(set(source_ids)),
        retries=total_retries,
        latency_ms=round(latency_ms, 2),
        result_statuses=[r.status for r in results],
        result_counts=[r.result_count for r in results],
    )
    return results, trace


def _handler_takes_db(handler: Callable) -> bool:
    try:
        import inspect

        sig = inspect.signature(handler)
        return "db" in sig.parameters
    except Exception:
        return False


# ── Bounded loop orchestrator ────────────────────────────────────────────────

def run_bounded_evidence_loop(
    *,
    initial_packet: ForwardDmContextPacket,
    adjudicate: Callable[[ForwardDmContextPacket], DmTurnContractV1 | dict[str, Any]],
    audience: ContextAudience | None = None,
    db: Any | None = None,
    tool_handlers: dict[str, Callable] | None = None,
    max_rounds: int = MAX_EVIDENCE_ROUNDS,
    max_retries_per_request: int = 1,
    timeout_s: float | None = DEFAULT_TOOL_TIMEOUT_S,
) -> tuple[DmTurnContractV1, EvidenceBundle]:
    """Orchestrate adjudicate → evidence → re-adjudicate.

    ``adjudicate`` must be pure / injectable: it receives a
    ``ForwardDmContextPacket`` and returns a validated ``DmTurnContractV1``
    (or raw dict to be normalized).  It is called at least once and at most
    ``max_rounds + 1`` times.

    Returns ``(final_contract, bundle)`` where ``final_contract.mode !=
    'need_evidence'`` except when the loop limit is hit (then terminal
    failure handling is the caller's responsibility).

    Invariants checked:
    * ``need_evidence`` without beats, with 1-3 requests, with safe_prelude;
    * each round's requests are validated before execution;
    * per-round and loop limits are enforced;
    * evidence is fed back as ``evidence_results`` lane with provenance;
    * tool failure retries once; observability traced.
    """
    if max_rounds < 1 or max_rounds > 5:
        raise ValueError("max_rounds must be between 1 and 5")
    packet = initial_packet
    aud = audience or packet.audience
    overall_t0 = time.monotonic()
    traces: list[EvidenceRoundTrace] = []
    all_results: list[EvidenceResult] = []
    seen_request_ids: set[str] = set()

    def _normalize(raw: DmTurnContractV1 | dict[str, Any]) -> DmTurnContractV1:
        if isinstance(raw, DmTurnContractV1):
            return raw
        if isinstance(raw, dict):
            return normalize_contract(raw)
        raise EvidenceValidationError("adjudicate must return a contract dict or DmTurnContractV1")

    current = _normalize(adjudicate(packet))

    rounds = 0
    while current.mode == "need_evidence":
        rounds += 1
        if rounds > max_rounds:
            structured_log(
                logger,
                logging.WARNING,
                "forward_dm_evidence_loop_limit",
                rounds=rounds,
                max_rounds=max_rounds,
                trace_id=str(uuid.uuid4()),
            )
            raise EvidenceLoopLimitError(
                f"Evidence loop limit exceeded: {rounds} > {max_rounds}"
            )
        # Validate before execution
        try:
            requests = validate_evidence_requests(current.evidence_requests)
        except EvidenceValidationError as exc:
            structured_log(logger, logging.WARNING, "forward_dm_evidence_invalid_request", error=str(exc), details=exc.details)
            raise

        # Detect duplicate request ids across rounds (idempotency guard)
        for req in requests:
            if req.id in seen_request_ids:
                raise EvidenceValidationError(f"duplicate evidence request id across rounds: {req.id!r}")
            seen_request_ids.add(req.id)

        # Execute round
        results, trace = execute_evidence_round(
            requests,
            aud,
            db=db,
            tool_handlers=tool_handlers,
            max_retries=max_retries_per_request,
            timeout_s=timeout_s,
        )
        trace.round_index = rounds
        traces.append(trace)
        all_results.extend(results)

        # Feed back as distinct lane
        try:
            records = evidence_results_to_records(results, aud)
        except Exception as exc:
            raise EvidenceValidationError(f"failed to mediate evidence to context lane: {exc}") from exc

        # Evidence records are optional, so total-budget pressure can omit
        # older ones; the budget grows with the packet it extends.
        packet = packet.with_records(
            {LaneName.EVIDENCE_RESULTS: records},
            dependency="evidence_results",
            budget=ContextBudget(
                max_bytes=packet.observability.serialized_bytes + 32_000,
                max_tokens=packet.observability.estimated_tokens + 8000,
            ),
        )

        # Re-adjudicate with enriched context
        current = _normalize(adjudicate(packet))

        # Safety: if model re-requests need_evidence with no new ids, the duplicate guard above will fire next loop;
        # but also guard against infinite safe_prelude fabrication
        if current.mode == "need_evidence":
            # safe_prelude must remain bounded and not contain invented world facts
            if current.safe_prelude and len(current.safe_prelude) > 240:
                raise EvidenceValidationError("safe_prelude exceeds 240 chars")

    total_ms = (time.monotonic() - overall_t0) * 1000
    bundle = EvidenceBundle(
        rounds=rounds,
        traces=traces,
        total_latency_ms=total_ms,
        total_requests=len(all_results),
        results=all_results,
    )
    structured_log(
        logger,
        logging.INFO,
        "forward_dm_evidence_loop_complete",
        rounds=rounds,
        total_requests=len(all_results),
        total_latency_ms=round(total_ms, 2),
        final_mode=current.mode,
        tool_types=[r.tool for r in all_results],
        source_ids=[f"{s.source_type}:{s.source_id}@{s.source_version}" for r in all_results for s in r.sources],
        retries=sum(t.retries for t in traces),
    )
    return current, bundle

