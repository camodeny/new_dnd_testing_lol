"""Bounded semantic recall over the world semantic index — issue #213.

- Bounded semantic search resolves every candidate back to its authoritative
  record (version-checked) and returns typed evidence references through the
  same #212 audience gates. Campaign scoping + visibility filtering happen
  BEFORE any player-visible path.
- Weak candidate sets return ``no_match``/``defer`` instead of forcing the
  top vector hit.
- Embedding/search failure degrades to direct authoritative retrieval — it
  never mutates canon and never blocks direct reads.

Indexing lives in :mod:`app.world.semantic_index`.

The AI is the only DM; no copy here implies a human DM/moderator.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from app.world._common import clamp_limit
from app.world.evidence_packets import (
    audience_viewers,
    authorize_world_record,
    entity_packet,
    event_packet,
    event_visible_player_facing,
    fact_packet,
    relation_packet,
    resolve_campaign,
    resolve_viewers,
    scene_gate,
    tool_result,
    turn_gate,
    turn_packet,
    scene_packet,
)
from app.world.semantic_index import (
    DEFAULT_MODEL,
    DEFAULT_VERSION,
    EMBEDDING_DIM,
    MAX_INDEX_TEXT_CHARS,
    authoritative_source_version,
    cosine_similarity,
    current_source_record,
    embedding_column_is_vector,
    is_stub_model,
    parse_vector,
    resolve_embedder,
    resolve_embedding_model,
    source_record_active,
    stub_embed,
    vector_literal,
)
from models.campaigns import Campaign
from models.world import WorldEmbedding

logger = logging.getLogger(__name__)

SEMANTIC_SOURCE = "world_semantic_213"

# Search bounds (observable on every outcome).
SEMANTIC_DEFAULT_LIMIT = 10
SEMANTIC_MAX_LIMIT = 20
SEMANTIC_MAX_CANDIDATES = 500
DEFAULT_MIN_SIMILARITY = 0.35

# Outcome statuses. ``no_match`` (weak set) / ``defer`` (nothing usable yet)
# are explicit — never a forced top hit.
STATUS_OK = "ok"
STATUS_NO_MATCH = "no_match"
STATUS_DEFER = "defer"


# ── Typed outcome ────────────────────────────────────────────────────────────

@dataclass
class SemanticOutcome:
    """Envelope for one bounded semantic search.

    ``packets`` are authorized #212 evidence packets in similarity order with
    ``retrieval_score`` set to cosine similarity; vector values travel
    only as derived provenance metadata. ``fallback_to_direct`` is True when
    the vector path was unusable and callers should use direct retrieval.
    """

    status: str = STATUS_OK
    packets: list[Any] = field(default_factory=list)
    total_candidates: int = 0
    lexical_candidates: int = 0
    vector_candidates: int = 0
    visible: int = 0
    denied: int = 0
    denied_reasons: dict[str, int] = field(default_factory=dict)
    stale_dropped: int = 0
    embedding_model: str = DEFAULT_MODEL
    embedding_version: str = DEFAULT_VERSION
    min_similarity: float = DEFAULT_MIN_SIMILARITY
    top_similarity: float = 0.0
    limit_applied: int = SEMANTIC_DEFAULT_LIMIT
    truncated: bool = False
    vector_backend: str = "python"
    fallback_to_direct: bool = False
    latency_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "packets": [p.to_dict() for p in self.packets],
            "total_candidates": self.total_candidates,
            "lexical_candidates": self.lexical_candidates,
            "vector_candidates": self.vector_candidates,
            "visible": self.visible,
            "denied": self.denied,
            "denied_reasons": dict(self.denied_reasons),
            "stale_dropped": self.stale_dropped,
            "embedding_model": self.embedding_model,
            "embedding_version": self.embedding_version,
            "min_similarity": self.min_similarity,
            "top_similarity": self.top_similarity,
            "limit_applied": self.limit_applied,
            "truncated": self.truncated,
            "vector_backend": self.vector_backend,
            "fallback_to_direct": self.fallback_to_direct,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


# ── Bounded semantic search ──────────────────────────────────────────────────

def _embed_query(
    query_text: str, embedding_model: str,
    provider: Callable[[list[str]], list[list[float]]] | None,
) -> list[float]:
    cleaned = str(query_text or "").strip()
    if not cleaned:
        raise ValueError("semantic search requires a non-empty query")
    if len(cleaned) > MAX_INDEX_TEXT_CHARS:
        cleaned = cleaned[:MAX_INDEX_TEXT_CHARS]
    embedding_model = resolve_embedding_model(embedding_model)
    embedder = resolve_embedder(
        embedding_model, provider, task_type="RETRIEVAL_QUERY")
    if embedder is not None:
        vectors = embedder([cleaned])
    else:
        if not is_stub_model(embedding_model):
            raise RuntimeError(
                f"Embedding model {embedding_model!r} has no provider; "
                "refusing to mint stub vectors under a non-stub model name."
            )
        vectors = [stub_embed(cleaned)]
    if not vectors or len(vectors[0]) != EMBEDDING_DIM:
        raise ValueError(
            f"query embedder returned {len(vectors[0]) if vectors else 0}-dim "
            f"vector, expected {EMBEDDING_DIM}"
        )
    return [float(v) for v in vectors[0]]


def _pgvector_candidates(
    db: Session, campaign: Campaign, query_vec: list[float], *,
    embedding_model: str, embedding_version: str, limit: int,
    source_types: frozenset[str] | None = None,
) -> list[tuple[uuid.UUID, float]] | None:
    """pgvector fast path: cosine-ordered active rows. None when unavailable."""
    try:
        if not embedding_column_is_vector(db):
            return None
        literal = vector_literal(query_vec)
        type_filter = "AND source_type = ANY(:types)" if source_types else ""
        params: dict[str, Any] = {
            "vec": literal, "cid": str(campaign.id), "model": embedding_model,
            "ver": embedding_version, "lim": max(1, min(int(limit), SEMANTIC_MAX_CANDIDATES)),
        }
        if source_types:
            params["types"] = sorted(source_types)
        rows = db.execute(
            sql_text(f"""
                SELECT id, (1 - (embedding <=> :vec::vector)) AS score
                FROM world_embeddings
                WHERE campaign_id = :cid AND status = 'active'
                  AND embedding_model = :model AND embedding_version = :ver
                  AND embedding IS NOT NULL {type_filter}
                ORDER BY embedding <=> :vec::vector
                LIMIT :lim
            """),
            params,
        ).fetchall()
        return [(r[0] if isinstance(r[0], uuid.UUID) else uuid.UUID(str(r[0])), float(r[1] or 0.0)) for r in rows]
    except Exception as exc:
        logger.warning("world_semantic_pgvector_search_failed error=%s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _python_candidates(
    db: Session, campaign: Campaign, query_vec: list[float], *,
    embedding_model: str, embedding_version: str, limit: int,
    source_types: frozenset[str] | None = None,
) -> list[tuple[WorldEmbedding, float]]:
    conditions = [
        WorldEmbedding.campaign_id == campaign.id,
        WorldEmbedding.status == "active",
        WorldEmbedding.embedding_model == embedding_model,
        WorldEmbedding.embedding_version == embedding_version,
    ]
    if source_types:
        conditions.append(WorldEmbedding.source_type.in_(sorted(source_types)))
    rows = db.execute(
        select(WorldEmbedding).where(*conditions).order_by(WorldEmbedding.created_at.asc()).limit(SEMANTIC_MAX_CANDIDATES)
    ).scalars().all()
    scored: list[tuple[WorldEmbedding, float]] = []
    for row in rows:
        vec = parse_vector(row.embedding_text) or parse_vector(row.embedding)
        if vec is None or len(vec) != EMBEDDING_DIM:
            continue
        scored.append((row, cosine_similarity(query_vec, vec)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[: max(1, min(int(limit), SEMANTIC_MAX_LIMIT))]


def _authorize_candidate(
    db: Session, campaign: Campaign, record: Any, source_type: str,
    viewers: list[uuid.UUID], *, dm_internal: bool,
) -> tuple[bool, str | None]:
    """Same gates as #212 retrieval: entity/relation/fact, event, turn, scene."""
    if source_type in {"world_entity", "world_relation", "world_fact"}:
        kind = {"world_entity": "entity", "world_relation": "relation",
                "world_fact": "fact"}[source_type]
        return authorize_world_record(
            db, campaign, kind, record.id, viewers, dm_internal=dm_internal)
    if source_type == "domain_event":
        if dm_internal:
            return True, None
        return event_visible_player_facing(db, campaign, record, viewers)
    if source_type == "source_turn":
        return turn_gate(db, campaign, record, viewers, dm_internal=dm_internal)
    if source_type == "scene":
        return scene_gate(db, campaign, record, viewers, dm_internal=dm_internal)
    return False, "unsupported_source_type"


@dataclass
class _Candidate:
    """One ranked source awaiting resolve → version-check → authorize.

    ``row`` is the vector index row when the vector path proposed it; a
    keyword-only candidate has none and is read straight from canon.
    """

    source_type: str
    source_id: uuid.UUID
    score: float
    vector_similarity: float | None = None
    lexical_score: float | None = None
    row: WorldEmbedding | None = None


def _build_packet(
    db: Session, candidate: _Candidate, record: Any, campaign_id: uuid.UUID, rank: int,
    *, revealable: bool | None, embedding_model: str, embedding_version: str,
) -> Any:
    """Authorized packet with retrieval scores as derived provenance metadata."""
    source_type = candidate.source_type
    if source_type == "world_entity":
        packet = entity_packet(record, campaign_id, rank, revealable=revealable)
    elif source_type == "world_relation":
        packet = relation_packet(record, campaign_id, rank, revealable=revealable)
    elif source_type == "world_fact":
        packet = fact_packet(record, campaign_id, rank, revealable=revealable)
    elif source_type == "domain_event":
        packet = event_packet(db, record, rank, revealable=revealable)
    elif source_type == "source_turn":
        packet = turn_packet(record, rank, revealable=revealable)
    elif source_type == "scene":
        packet = scene_packet(record, rank, revealable=revealable)
    else:  # pragma: no cover — guarded by validate_source_type at index time
        raise ValueError(f"unsupported semantic source type {source_type!r}")
    packet.retrieval_rank = rank
    packet.retrieval_score = float(candidate.score)
    # Derived ranking metadata only — content/version/provenance stay canonical.
    packet.provenance["semantic_source"] = SEMANTIC_SOURCE
    if candidate.vector_similarity is not None:
        packet.provenance["semantic_model"] = embedding_model
        packet.provenance["semantic_model_version"] = embedding_version
        packet.provenance["vector_similarity"] = round(float(candidate.vector_similarity), 6)
    if candidate.lexical_score is not None:
        packet.provenance["lexical_score"] = round(float(candidate.lexical_score), 6)
    return packet


def _mark_row(db: Session, row: WorldEmbedding, status: str, error: str | None = None) -> None:
    try:
        row.status = status
        if error is not None:
            row.error = error
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


@dataclass
class _Resolved:
    packets: list[Any] = field(default_factory=list)
    denied: int = 0
    denied_reasons: dict[str, int] = field(default_factory=dict)
    stale_dropped: int = 0


def _resolve_candidates(
    db: Session, campaign: Campaign, candidates: list[_Candidate],
    viewers: list[uuid.UUID], *, dm_internal: bool, limit: int,
    embedding_model: str, embedding_version: str,
) -> _Resolved:
    """Resolve → version-check → authorize, in rank order.

    Anything failing the chain is dropped (stale vector rows are marked for
    rebuild); hidden rows count as denials without leaking ids/content. A
    stale vector row only loses its vector vote: when keyword search also
    matched, the live record still serves. The loop stops once the
    authorized window is full — later pool rows are never served.
    """
    out = _Resolved()
    for candidate in candidates:
        if len(out.packets) >= limit:
            break
        row = candidate.row
        record = current_source_record(
            db, campaign.id, candidate.source_type, candidate.source_id)
        if record is None or not source_record_active(record, candidate.source_type):
            if row is not None:
                _mark_row(db, row, "superseded")
            out.stale_dropped += 1
            continue
        if row is not None:
            current_version = authoritative_source_version(record, candidate.source_type)
            if current_version != row.source_version:
                _mark_row(db, row, "stale", "source_version_changed")
                out.stale_dropped += 1
                if candidate.lexical_score is None:
                    continue
                candidate.vector_similarity = None
        allowed, reason = _authorize_candidate(
            db, campaign, record, candidate.source_type, viewers, dm_internal=dm_internal)
        if not allowed:
            out.denied += 1
            out.denied_reasons[reason or "denied"] = out.denied_reasons.get(reason or "denied", 0) + 1
            continue
        out.packets.append(_build_packet(
            db, candidate, record, campaign.id, len(out.packets),
            revealable=None if dm_internal else True,
            embedding_model=embedding_model, embedding_version=embedding_version,
        ))
    return out


def _vector_ranked(
    db: Session, campaign: Campaign, query_vec: list[float], *,
    embedding_model: str, embedding_version: str, limit: int,
    source_types: frozenset[str] | None = None,
) -> tuple[list[tuple[WorldEmbedding, float]], str]:
    """Similarity-ordered active index rows plus the backend that served them."""
    fast = _pgvector_candidates(
        db, campaign, query_vec, embedding_model=embedding_model,
        embedding_version=embedding_version, limit=limit, source_types=source_types,
    )
    if fast is None:
        # Python path over-retrieves for threshold filtering, then bounds.
        return _python_candidates(
            db, campaign, query_vec, embedding_model=embedding_model,
            embedding_version=embedding_version, limit=limit, source_types=source_types,
        ), "python"
    by_id = {row_id: score for row_id, score in fast}
    if not by_id:
        return [], "pgvector"
    rows = db.execute(
        select(WorldEmbedding).where(WorldEmbedding.id.in_(list(by_id.keys())))
    ).scalars().all()
    return sorted(
        ((row, by_id[row.id]) for row in rows if row.id in by_id),
        key=lambda item: item[1], reverse=True,
    ), "pgvector"


def semantic_search(
    db: Session,
    campaign_id: Any,
    query_text: Any,
    viewer_user_id: Any = None,
    *,
    limit: Any = SEMANTIC_DEFAULT_LIMIT,
    min_similarity: Any = DEFAULT_MIN_SIMILARITY,
    embedding_model: str | None = None,
    embedding_version: str = DEFAULT_VERSION,
    provider: Callable[[list[str]], list[list[float]]] | None = None,
    dm_internal: bool = False,
) -> SemanticOutcome:
    """Bounded vector-only search returning typed evidence references.

    Campaign scoping is mandatory; visibility filtering happens before any
    packet is returned. Stale/version-mismatched rows are dropped (and
    marked) rather than served. Weak sets yield ``no_match``; an empty or
    failed vector path yields ``defer`` with ``fallback_to_direct`` so
    callers use direct authoritative retrieval instead. The DM evidence
    tool uses :func:`search_world_memory`, which adds keyword recall.
    """
    started = time.monotonic()
    limit_applied = clamp_limit(limit, default=SEMANTIC_DEFAULT_LIMIT, maximum=SEMANTIC_MAX_LIMIT)
    embedding_model = resolve_embedding_model(embedding_model)
    try:
        threshold = float(min_similarity)
    except (TypeError, ValueError):
        threshold = DEFAULT_MIN_SIMILARITY
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)

    def _fail(status: str, error: str, *, fallback: bool) -> SemanticOutcome:
        outcome = SemanticOutcome(
            status=status, embedding_model=embedding_model,
            embedding_version=embedding_version, min_similarity=threshold,
            limit_applied=limit_applied, latency_ms=(time.monotonic() - started) * 1000,
            error=error, fallback_to_direct=fallback, vector_backend="unavailable",
        )
        _log_search(campaign, outcome, dm_internal=dm_internal)
        return outcome

    try:
        query_vec = _embed_query(str(query_text or ""), embedding_model, provider)
    except (ValueError, RuntimeError) as exc:
        # Embedding failure degrades retrieval quality but never mutates canon.
        return _fail(STATUS_DEFER, f"{type(exc).__name__}: {exc}", fallback=True)

    # No pre-filter truncation to limit_applied: resolve → authorize runs
    # over the bounded overfetch pool so a hidden/stale top-ranked row
    # cannot suppress a valid hit behind it in the window.
    ranked, backend = _vector_ranked(
        db, campaign, query_vec, embedding_model=embedding_model,
        embedding_version=embedding_version, limit=limit_applied * 2,
    )
    if not ranked:
        return _fail(STATUS_DEFER, "no active semantic index rows; use direct retrieval",
                     fallback=True)

    top_similarity = max((score for _, score in ranked), default=0.0)
    if top_similarity < threshold:
        outcome = SemanticOutcome(
            status=STATUS_NO_MATCH, total_candidates=len(ranked),
            embedding_model=embedding_model, embedding_version=embedding_version,
            min_similarity=threshold, top_similarity=top_similarity,
            limit_applied=limit_applied, latency_ms=(time.monotonic() - started) * 1000,
            vector_backend=backend,
        )
        _log_search(campaign, outcome, dm_internal=dm_internal)
        return outcome

    candidates = [
        _Candidate(row.source_type, row.source_id, similarity,
                   vector_similarity=similarity, row=row)
        for row, similarity in ranked if similarity >= threshold
    ]
    resolved = _resolve_candidates(
        db, campaign, candidates, viewers, dm_internal=dm_internal,
        limit=limit_applied, embedding_model=embedding_model,
        embedding_version=embedding_version,
    )
    outcome = SemanticOutcome(
        status=STATUS_OK if resolved.packets else STATUS_NO_MATCH,
        packets=resolved.packets,
        total_candidates=len(ranked),
        visible=len(resolved.packets),
        denied=resolved.denied,
        denied_reasons=resolved.denied_reasons,
        stale_dropped=resolved.stale_dropped,
        embedding_model=embedding_model,
        embedding_version=embedding_version,
        min_similarity=threshold,
        top_similarity=top_similarity,
        limit_applied=limit_applied,
        latency_ms=(time.monotonic() - started) * 1000,
        vector_backend=backend,
    )
    _log_search(campaign, outcome, dm_internal=dm_internal)
    return outcome


#: Reciprocal-rank-fusion constant (same as the rules hybrid search).
RRF_K = 60


def search_world_memory(
    db: Session,
    campaign_id: Any,
    query_text: Any,
    viewer_user_id: Any = None,
    *,
    limit: Any = SEMANTIC_DEFAULT_LIMIT,
    min_similarity: Any = DEFAULT_MIN_SIMILARITY,
    source_types: frozenset[str] | None = None,
    embedding_model: str | None = None,
    embedding_version: str = DEFAULT_VERSION,
    provider: Callable[[list[str]], list[list[float]]] | None = None,
    dm_internal: bool = False,
) -> SemanticOutcome:
    """Hybrid keyword + vector world-memory search, fused by reciprocal rank.

    Keyword (BM25) candidates come straight from live records, so recall
    never depends on the embedding provider or index freshness; vector
    candidates add meaning-level matches above ``min_similarity``. Every
    candidate passes the same resolve → version-check → authorize chain as
    :func:`semantic_search`. ``source_types`` restricts both halves.
    """
    from app.world.lexical import lexical_candidates

    started = time.monotonic()
    limit_applied = clamp_limit(limit, default=SEMANTIC_DEFAULT_LIMIT, maximum=SEMANTIC_MAX_LIMIT)
    embedding_model = resolve_embedding_model(embedding_model)
    try:
        threshold = float(min_similarity)
    except (TypeError, ValueError):
        threshold = DEFAULT_MIN_SIMILARITY
    campaign = resolve_campaign(db, campaign_id)
    viewers = resolve_viewers(viewer_user_id)
    query = str(query_text or "").strip()
    pool = limit_applied * 2

    lexical = lexical_candidates(
        db, campaign.id, query, source_types=source_types, limit=pool)

    vector: list[tuple[WorldEmbedding, float]] = []
    backend = "unavailable"
    error: str | None = None
    try:
        query_vec = _embed_query(query, embedding_model, provider)
        vector, backend = _vector_ranked(
            db, campaign, query_vec, embedding_model=embedding_model,
            embedding_version=embedding_version, limit=pool, source_types=source_types,
        )
    except Exception as exc:  # noqa: BLE001 — provider/transport failure
        # The keyword half still answers; the failure stays observable.
        error = f"{type(exc).__name__}: {exc}"
    top_similarity = max((score for _, score in vector), default=0.0)

    fused: dict[tuple[str, uuid.UUID], _Candidate] = {}
    for rank, (row, similarity) in enumerate(
            item for item in vector if item[1] >= threshold):
        fused[(row.source_type, row.source_id)] = _Candidate(
            row.source_type, row.source_id, 1 / (RRF_K + rank + 1),
            vector_similarity=similarity, row=row)
    for rank, (source_type, source_id, score) in enumerate(lexical):
        key = (source_type, source_id)
        candidate = fused.get(key)
        if candidate is None:
            candidate = fused[key] = _Candidate(source_type, source_id, 0.0)
        candidate.score += 1 / (RRF_K + rank + 1)
        candidate.lexical_score = score
    candidates = sorted(fused.values(), key=lambda c: c.score, reverse=True)

    resolved = _resolve_candidates(
        db, campaign, candidates, viewers, dm_internal=dm_internal,
        limit=limit_applied, embedding_model=embedding_model,
        embedding_version=embedding_version,
    )
    outcome = SemanticOutcome(
        status=STATUS_OK if resolved.packets else STATUS_NO_MATCH,
        packets=resolved.packets,
        total_candidates=len(candidates),
        lexical_candidates=len(lexical),
        vector_candidates=sum(1 for c in candidates if c.vector_similarity is not None),
        visible=len(resolved.packets),
        denied=resolved.denied,
        denied_reasons=resolved.denied_reasons,
        stale_dropped=resolved.stale_dropped,
        embedding_model=embedding_model,
        embedding_version=embedding_version,
        min_similarity=threshold,
        top_similarity=top_similarity,
        limit_applied=limit_applied,
        latency_ms=(time.monotonic() - started) * 1000,
        vector_backend=backend,
        error=error,
    )
    _log_search(campaign, outcome, dm_internal=dm_internal)
    return outcome


def _log_search(campaign: Campaign, outcome: SemanticOutcome, *, dm_internal: bool) -> None:
    structured_log(
        logger, logging.INFO, "world_semantic_search",
        campaign_id=str(campaign.id),
        dm_internal=dm_internal,
        status=outcome.status,
        embedding_model=outcome.embedding_model,
        embedding_version=outcome.embedding_version,
        vector_backend=outcome.vector_backend,
        candidate_count=outcome.total_candidates,
        lexical_candidates=outcome.lexical_candidates,
        vector_candidates=outcome.vector_candidates,
        visible=outcome.visible,
        denied=outcome.denied,
        denied_reasons=dict(outcome.denied_reasons),
        stale_dropped=outcome.stale_dropped,
        top_similarity=round(outcome.top_similarity, 4),
        min_similarity=outcome.min_similarity,
        fallback_to_direct=outcome.fallback_to_direct,
        latency_ms=round(outcome.latency_ms, 3),
        error=outcome.error,
    )


# ── #203 evidence-mediation tool (search_campaign_memory) ────────────────────

def _defer_result(audience: Any, error: str) -> dict[str, Any]:
    campaign_id = str(getattr(audience, "campaign_id", "") or "")
    thread_id = str(getattr(audience, "thread_id", "") or "")
    return {
        "status": "unknown",
        "sources": [],
        "visibility": "campaign",
        "authorization": {"campaign_id": campaign_id, "thread_ids": [thread_id] if thread_id else []},
        "payload": {"retrieval_status": STATUS_DEFER, "error": error, "fallback_to_direct": True},
        "result_count": 0,
    }


def world_memory_result(
    db: Any, audience: Any, query: str, *, limit: Any = None,
    source_types: frozenset[str] | None = None, note: str | None = None,
) -> dict[str, Any]:
    """Hybrid world-memory search wrapped as a #203 evidence-tool result.

    Packets carry each record's ``source_id``, so the DM can follow up with
    an exact by-id tool. A search that ran but matched nothing is
    ``missing``; ``unknown`` is reserved for search that could not run.
    """
    if db is None:
        # No session: unknown, audience-scoped auth, so bounded loops continue
        # instead of failing validation.
        return _defer_result(audience, "world memory search requires a database session")
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    try:
        outcome = search_world_memory(
            db, getattr(audience, "campaign_id", None), query, audience_viewers(audience),
            limit=limit, source_types=source_types, dm_internal=dm_internal)
    except Exception as exc:
        logger.warning("world_semantic_tool_failed error=%s", exc)
        return _defer_result(audience, str(exc)[:300])
    packets = list(outcome.packets)
    payload = {
        "retrieval_status": outcome.status,
        "packets": [p.to_dict() for p in packets],
        "total_candidates": outcome.total_candidates,
        "lexical_candidates": outcome.lexical_candidates,
        "vector_candidates": outcome.vector_candidates,
        "visible": outcome.visible,
        "denied": outcome.denied,
        "denied_reasons": dict(outcome.denied_reasons),
        "stale_dropped": outcome.stale_dropped,
        "embedding_model": outcome.embedding_model,
        "embedding_version": outcome.embedding_version,
        "top_similarity": outcome.top_similarity,
        "vector_backend": outcome.vector_backend,
        "latency_ms": outcome.latency_ms,
    }
    if note:
        payload["note"] = note
    return tool_result(audience, status="ok" if packets else "missing", packets=packets,
                       payload=payload, dm_internal=dm_internal)


def handle_search_campaign_memory(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    """Keyword + semantic recall as a #203 evidence tool: typed references, never claims."""
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("search_campaign_memory requires query")
    return world_memory_result(db, audience, query, limit=getattr(req, "limit", None))
