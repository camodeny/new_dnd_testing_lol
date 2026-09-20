"""pgvector semantic index over authoritative source records — issue #213.

Embeddings are a rebuildable derived index over canonical world records
(entities, relations, facts, domain events, source turns, current scene),
never a source of truth:

- Every embedding row points at an exact (source_type, source_id,
  source_version, campaign, embedding model/version). Similarity scores are
  derived ranking metadata stored on provenance, never canonical evidence.
- Bounded semantic search resolves every candidate back to its authoritative
  record (version-checked) and returns typed evidence references through the
  same #212 authorization gates. Campaign scoping + visibility filtering
  happen BEFORE any player-visible path or decision-reranker exposure.
- Weak candidate sets return ``no_match``/``defer`` instead of forcing the
  top vector hit. Optional #380/#381 reranking may only reorder/select from
  the already-authorized candidates (via :func:`apply_rerank`).
- Embedding/search/reranker failure degrades to direct authoritative
  retrieval — it never mutates canon and never blocks direct reads.
- Storage mirrors the ``rules_embeddings`` branch pattern: ``vector(1536)``
  + HNSW on Postgres with pgvector, portable JSON text otherwise, with
  ``embedding_text`` always readable for the graceful-degradation path.

Async indexing follows the #191 pattern: writers enqueue a
``world.semantic.index`` envelope (identifiers only); the worker handler
re-reads the authoritative record and upserts the embedding idempotently.

The AI is the only DM; no copy here implies a human DM/moderator.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from app.world import retrieval as retrieval_mod
from models.campaigns import Campaign, CampaignDomainEvent
from models.dm import DmTurn
from models.world import (
    SEMANTIC_SOURCE_TYPES,
    WorldEmbedding,
    WorldEntity,
    WorldFact,
    WorldRelation,
)

logger = logging.getLogger(__name__)

SEMANTIC_SOURCE = "world_semantic_213"

# ── Job type + model defaults ────────────────────────────────────────────────

SEMANTIC_INDEX_JOB_TYPE = "world.semantic.index"

DEFAULT_MODEL = "stub-hash-v1"
DEFAULT_VERSION = "1"

try:  # Single canonical indexed dimension (issue #334); safe local fallback.
    from app.rules.gemini import EMBEDDING_DIM as _DIM  # type: ignore

    EMBEDDING_DIM = int(_DIM)
except Exception:
    EMBEDDING_DIM = 1536
assert EMBEDDING_DIM == 1536, "indexed embedding dimension must be 1536"

# Search bounds (observable on every outcome).
SEMANTIC_DEFAULT_LIMIT = 10
SEMANTIC_MAX_LIMIT = 20
SEMANTIC_MAX_CANDIDATES = 500
DEFAULT_MIN_SIMILARITY = 0.35
MAX_INDEX_TEXT_CHARS = 4000

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
    ``retrieval_score`` set to cosine similarity; vector/rerank values travel
    only as derived provenance metadata. ``fallback_to_direct`` is True when
    the vector path was unusable and callers should use direct retrieval.
    """

    status: str = STATUS_OK
    packets: list[Any] = field(default_factory=list)
    total_candidates: int = 0
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
    rerank: dict[str, Any] | None = None
    latency_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "packets": [p.to_dict() for p in self.packets],
            "total_candidates": self.total_candidates,
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
            "rerank": dict(self.rerank) if self.rerank else None,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


# ── Embedding math (deterministic stub; real provider injectable) ────────────

def _stub_embed(text_value: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic fake embedding: hash expansion, L2-normalized."""
    digest = hashlib.sha256(text_value.encode("utf-8")).digest()
    vals: list[float] = []
    counter = 0
    while len(vals) < dim:
        chunk = hashlib.sha256(digest + counter.to_bytes(4, "little")).digest()
        for byte in chunk:
            vals.append((byte / 127.5) - 1.0)
            if len(vals) >= dim:
                break
        counter += 1
    norm = math.sqrt(sum(x * x for x in vals))
    if norm > 0:
        vals = [x / norm for x in vals]
    return vals


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _parse_vector(raw: Any) -> list[float] | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        vals = json.loads(raw)
    except Exception:
        return None
    if not isinstance(vals, list) or not vals:
        return None
    try:
        return [float(v) for v in vals]
    except (TypeError, ValueError):
        return None


def _vector_json(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _resolve_embedder(
    embedding_model: str, provider: Callable[[list[str]], list[list[float]]] | None
) -> Callable[[list[str]], list[list[float]]] | None:
    """Return a real embedder or None for the deterministic stub path.

    Real models fail visibly when no provider is available — never silently
    mint stub vectors labeled as a real model.
    """
    if provider is not None:
        return provider
    if embedding_model.startswith(("gemini", "text-embedding", "models/")):
        try:
            from app.rules.gemini import make_gemini_provider

            return make_gemini_provider(model=embedding_model).embed  # type: ignore[attr-defined]
        except Exception as exc:
            raise RuntimeError(
                f"Embedding model {embedding_model!r} requested but no provider "
                f"is available: {exc}. Set GEMINI_API_KEY or use stub-hash-v1."
            ) from exc
    return None


# ── Source text + version (code-owned, from persisted records only) ──────────

def _entity_display(db: Session, entity_id: Any) -> str:
    try:
        entity = db.get(WorldEntity, entity_id)
    except Exception:
        return str(entity_id)
    if entity is None:
        return str(entity_id)
    return str(getattr(entity, "name", entity_id))


def build_source_text(db: Session, source_type: str, record: Any) -> str:
    """Deterministic index text for one authoritative record."""
    parts: list[str] = []
    if source_type == "world_entity":
        parts = [f"{record.name} ({record.entity_type})", str(record.summary or "")]
        details = getattr(record, "details", None)
        if isinstance(details, dict):
            role = details.get("role")
            if role:
                parts.append(str(role)[:500])
    elif source_type == "world_relation":
        subject = _entity_display(db, getattr(record, "subject_entity_id", None))
        obj_id = getattr(record, "object_entity_id", None)
        obj = _entity_display(db, obj_id) if obj_id else str(getattr(record, "object_label", "") or "")
        parts = [
            f"{subject} {getattr(record, 'relation_type', '')} {obj}",
            f"epistemic={getattr(record, 'epistemic_state', '')}",
        ]
    elif source_type == "world_fact":
        parts = [str(getattr(record, "content", "") or "")]
        for ref in list(getattr(record, "entity_refs", None) or [])[:8]:
            try:
                parts.append(_entity_display(db, ref))
            except Exception:
                continue
        parts.append(f"epistemic={getattr(record, 'epistemic_state', '')}")
    elif source_type == "domain_event":
        payload = getattr(record, "payload", None)
        try:
            payload_text = json.dumps(payload, default=str)[:1500] if payload else ""
        except Exception:
            payload_text = ""
        parts = [str(getattr(record, "event_type", "")), payload_text]
    elif source_type == "source_turn":
        chunk: list[str] = []
        try:
            from models.threads import PlayerSubmission

            for raw_sid in list(getattr(record, "submission_ids", None) or []):
                try:
                    sid = raw_sid if isinstance(raw_sid, uuid.UUID) else uuid.UUID(str(raw_sid))
                except (ValueError, AttributeError, TypeError):
                    continue
                submission = db.get(PlayerSubmission, sid)
                if submission is not None and submission.campaign_id == record.campaign_id:
                    chunk.append(str(getattr(submission, "raw_content", "") or ""))
        except Exception:
            pass
        parts = [f"turn audience={getattr(record, 'audience', '')}", *chunk]
    elif source_type == "scene":
        actors = getattr(record, "present_actors", None) or []
        names = [
            str(a.get("name") if isinstance(a, dict) else a)
            for a in actors if str(a.get("name") if isinstance(a, dict) else a).strip()
        ]
        parts = [
            str(getattr(record, "location_name", "") or ""),
            str(getattr(record, "fictional_time", "") or ""),
            " ".join(names[:16]),
        ]
    else:
        raise ValueError(f"unsupported semantic source type {source_type!r}")
    text_value = "\n".join(p for p in (s.strip() for s in parts if s) if p)
    return text_value[:MAX_INDEX_TEXT_CHARS]


def _current_record(
    db: Session, campaign_id: uuid.UUID, source_type: str, source_id: uuid.UUID
) -> Any | None:
    """Load the live authoritative record; None when gone or out of scope."""
    try:
        if source_type == "world_entity":
            row = db.get(WorldEntity, source_id)
        elif source_type == "world_relation":
            row = db.get(WorldRelation, source_id)
        elif source_type == "world_fact":
            row = db.get(WorldFact, source_id)
        elif source_type == "domain_event":
            row = db.get(CampaignDomainEvent, source_id)
        elif source_type == "source_turn":
            row = db.get(DmTurn, source_id)
        elif source_type == "scene":
            from models.world import CampaignCurrentScene

            row = db.get(CampaignCurrentScene, campaign_id)
            if row is not None and str(row.campaign_id) != str(campaign_id):
                return None
            return row
        else:
            return None
    except Exception:
        return None
    if row is None or getattr(row, "campaign_id", None) != campaign_id:
        return None
    return row


def _record_active(record: Any, source_type: str) -> bool:
    """Only ``active`` lifecycle rows are current truth (relations/facts)."""
    if source_type in {"world_relation", "world_fact"}:
        return str(getattr(record, "status", "active")) == "active"
    return True


# ── pgvector detection (cached; code-owned failure taxonomy) ─────────────────

_PGVECTOR_STATUS: bool | None = None
_PGVECTOR_COLUMN_VECTOR: bool | None = None


def reset_pgvector_cache() -> None:
    """Test hook: clear cached pgvector detection."""
    global _PGVECTOR_STATUS, _PGVECTOR_COLUMN_VECTOR
    _PGVECTOR_STATUS = None
    _PGVECTOR_COLUMN_VECTOR = None


def _dialect_name(db: Session) -> str:
    try:
        return db.get_bind().dialect.name
    except Exception:
        return ""


def pgvector_available(db: Session) -> bool:
    """True when the pgvector extension is installed (cached per process)."""
    global _PGVECTOR_STATUS
    if _PGVECTOR_STATUS is not None:
        return _PGVECTOR_STATUS
    try:
        if _dialect_name(db) != "postgresql":
            _PGVECTOR_STATUS = False
            return False
        row = db.execute(
            sql_text("SELECT 1 FROM pg_extension WHERE extname='vector'")
        ).fetchone()
        _PGVECTOR_STATUS = bool(row)
    except Exception as exc:
        logger.warning("world_semantic_pgvector_check_failed error=%s", exc)
        _PGVECTOR_STATUS = False
    return _PGVECTOR_STATUS


def _embedding_column_is_vector(db: Session) -> bool:
    """True when world_embeddings.embedding is a native vector column."""
    global _PGVECTOR_COLUMN_VECTOR
    if _PGVECTOR_COLUMN_VECTOR is not None:
        return _PGVECTOR_COLUMN_VECTOR
    try:
        if not pgvector_available(db):
            _PGVECTOR_COLUMN_VECTOR = False
            return False
        udt = db.execute(
            sql_text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='world_embeddings' "
                "AND column_name='embedding'"
            )
        ).scalar()
        _PGVECTOR_COLUMN_VECTOR = bool(udt == "vector")
    except Exception as exc:
        logger.warning("world_semantic_column_check_failed error=%s", exc)
        _PGVECTOR_COLUMN_VECTOR = False
    return _PGVECTOR_COLUMN_VECTOR


# ── Index writes (idempotent derived work; never mutates canon) ──────────────

def _find_row(
    db: Session,
    campaign_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
    embedding_model: str,
    embedding_version: str,
) -> WorldEmbedding | None:
    return db.execute(
        select(WorldEmbedding).where(
            WorldEmbedding.campaign_id == campaign_id,
            WorldEmbedding.source_type == source_type,
            WorldEmbedding.source_id == source_id,
            WorldEmbedding.embedding_model == embedding_model,
            WorldEmbedding.embedding_version == embedding_version,
        )
    ).scalars().first()


def _store_native_vector(db: Session, row_id: uuid.UUID, vec: list[float]) -> None:
    """Write the native vector column via cast (pgvector branch only)."""
    literal = _vector_json(vec)
    db.execute(
        sql_text("UPDATE world_embeddings SET embedding = :vec::vector WHERE id = :rid"),
        {"vec": literal, "rid": str(row_id)},
    )


def validate_source_type(source_type: Any) -> str:
    value = str(source_type or "").strip()
    if value not in SEMANTIC_SOURCE_TYPES:
        raise ValueError(
            f"source_type must be one of {sorted(SEMANTIC_SOURCE_TYPES)}"
        )
    return value


def index_source_record(
    db: Session,
    campaign_id: Any,
    source_type: Any,
    source_id: Any,
    *,
    embedding_model: str = DEFAULT_MODEL,
    embedding_version: str = DEFAULT_VERSION,
    provider: Callable[[list[str]], list[list[float]]] | None = None,
    commit: bool = True,
) -> WorldEmbedding | None:
    """Embed one authoritative record and upsert its active index row.

    Missing/out-of-scope/superseded sources flip any existing row to
    ``superseded`` instead of indexing (derived work follows canon, never
    invents it). Embedding-provider failure raises (the worker classifies it
    as retriable); validation failure raises terminally.
    """
    started = time.monotonic()
    stype = validate_source_type(source_type)
    campaign = retrieval_mod._resolve_campaign(db, campaign_id)
    try:
        sid = retrieval_mod._coerce_uuid(source_id, field_name="source_id")
    except ValueError:
        raise

    record = _current_record(db, campaign.id, stype, sid)
    existing = _find_row(db, campaign.id, stype, sid, embedding_model, embedding_version)
    if record is None or not _record_active(record, stype):
        if existing is not None:
            existing.status = "superseded"
            existing.error = None
            db.add(existing)
            if commit:
                db.commit()
        structured_log(
            logger, logging.INFO, "world_semantic_index_skipped",
            campaign_id=str(campaign.id), source_type=stype, source_id=str(sid),
            reason="source_missing_or_superseded",
        )
        return existing

    source_version = retrieval_mod._version_of(record)
    index_text = build_source_text(db, stype, record)
    embedder = _resolve_embedder(embedding_model, provider)
    if embedder is not None:
        vectors = embedder([index_text])
    else:
        if embedding_model != DEFAULT_MODEL and not embedding_model.startswith("stub"):
            raise RuntimeError(
                f"Embedding model {embedding_model!r} has no provider; "
                "refusing to mint stub vectors under a non-stub model name."
            )
        logger.warning(
            "world_semantic_stub_embeddings model=%s version=%s "
            "reason=stub_fallback — relevance only; set GEMINI_API_KEY for production",
            embedding_model, embedding_version,
        )
        vectors = [_stub_embed(index_text)]
    if not vectors or len(vectors[0]) != EMBEDDING_DIM:
        raise ValueError(
            f"embedder returned {len(vectors[0]) if vectors else 0}-dim vector, "
            f"expected {EMBEDDING_DIM}"
        )
    vec = [float(v) for v in vectors[0]]
    vec_json = _vector_json(vec)

    if existing is None:
        existing = WorldEmbedding(
            id=uuid.uuid4(),
            campaign_id=campaign.id,
            source_type=stype,
            source_id=sid,
            source_version=source_version,
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            embedding_text=vec_json,
            status="active",
        )
        db.add(existing)
        db.flush()
    else:
        existing.source_version = source_version
        existing.embedding_text = vec_json
        existing.status = "active"
        existing.error = None
        db.add(existing)
        db.flush()
    if _embedding_column_is_vector(db):
        # Native vector column: ORM Text mapping cannot write it — cast it.
        _store_native_vector(db, existing.id, vec)
    else:
        existing.embedding = vec_json
        db.add(existing)
    if commit:
        db.commit()
        db.refresh(existing)
    else:
        db.flush()
    structured_log(
        logger, logging.INFO, "world_semantic_indexed",
        campaign_id=str(campaign.id), source_type=stype, source_id=str(sid),
        source_version=source_version, embedding_model=embedding_model,
        embedding_version=embedding_version,
        latency_ms=round((time.monotonic() - started) * 1000, 3),
    )
    return existing


def mark_stale(
    db: Session,
    campaign_id: Any,
    source_type: Any,
    source_id: Any,
    *,
    reason: str = "source_changed",
    commit: bool = True,
) -> int:
    """Flip active rows for one source to ``stale`` (retryable derived work)."""
    stype = validate_source_type(source_type)
    campaign = retrieval_mod._resolve_campaign(db, campaign_id)
    sid = retrieval_mod._coerce_uuid(source_id, field_name="source_id")
    rows = db.execute(
        select(WorldEmbedding).where(
            WorldEmbedding.campaign_id == campaign.id,
            WorldEmbedding.source_type == stype,
            WorldEmbedding.source_id == sid,
            WorldEmbedding.status == "active",
        )
    ).scalars().all()
    for row in rows:
        row.status = "stale"
        row.error = reason[:500]
        db.add(row)
    if rows and commit:
        db.commit()
    elif rows:
        db.flush()
    if rows:
        structured_log(
            logger, logging.INFO, "world_semantic_marked_stale",
            campaign_id=str(campaign.id), source_type=stype,
            source_id=str(sid), count=len(rows), reason=reason,
        )
    return len(rows)


def mark_superseded(
    db: Session,
    campaign_id: Any,
    source_type: Any,
    source_id: Any,
    *,
    commit: bool = True,
) -> int:
    """Flip all live rows for a replaced source to ``superseded``."""
    stype = validate_source_type(source_type)
    campaign = retrieval_mod._resolve_campaign(db, campaign_id)
    sid = retrieval_mod._coerce_uuid(source_id, field_name="source_id")
    rows = db.execute(
        select(WorldEmbedding).where(
            WorldEmbedding.campaign_id == campaign.id,
            WorldEmbedding.source_type == stype,
            WorldEmbedding.source_id == sid,
            WorldEmbedding.status.in_(("active", "stale", "failed")),
        )
    ).scalars().all()
    for row in rows:
        row.status = "superseded"
        row.error = None
        db.add(row)
    if rows and commit:
        db.commit()
    elif rows:
        db.flush()
    return len(rows)


def get_semantic_stats(
    db: Session, campaign_id: Any, *, embedding_model: str = DEFAULT_MODEL,
    embedding_version: str = DEFAULT_VERSION,
) -> dict[str, Any]:
    """Indexing observability: counts by status + oldest-stale lag."""
    from sqlalchemy import func as _func

    campaign = retrieval_mod._resolve_campaign(db, campaign_id)
    rows = db.execute(
        select(WorldEmbedding.status, _func.count()).where(
            WorldEmbedding.campaign_id == campaign.id,
            WorldEmbedding.embedding_model == embedding_model,
            WorldEmbedding.embedding_version == embedding_version,
        ).group_by(WorldEmbedding.status)
    ).all()
    by_status = {k: int(v) for k, v in rows}
    oldest = db.execute(
        select(_func.min(WorldEmbedding.updated_at)).where(
            WorldEmbedding.campaign_id == campaign.id,
            WorldEmbedding.status.in_(("stale", "failed")),
        )
    ).scalar()
    lag_s = 0.0
    if oldest is not None:
        from datetime import datetime, timezone

        ts = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        lag_s = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    return {
        "campaign_id": str(campaign.id),
        "embedding_model": embedding_model,
        "embedding_version": embedding_version,
        "by_status": by_status,
        "total": sum(by_status.values()),
        "active": by_status.get("active", 0),
        "stale": by_status.get("stale", 0),
        "superseded": by_status.get("superseded", 0),
        "failed": by_status.get("failed", 0),
        "oldest_unindexed_lag_seconds": lag_s,
    }


# ── Async wiring (#191 pattern: identifiers-only envelope + worker) ──────────

def _deterministic_job_id(
    campaign_id: uuid.UUID, source_type: str, source_id: uuid.UUID,
    embedding_model: str, embedding_version: str,
) -> uuid.UUID:
    key = f"semidx:{campaign_id}:{source_type}:{source_id}:{embedding_model}:{embedding_version}"
    return uuid.uuid5(uuid.NAMESPACE_URL, key)


def request_semantic_index(
    db: Session | None,
    campaign_id: Any,
    source_type: Any,
    source_id: Any,
    *,
    embedding_model: str = DEFAULT_MODEL,
    embedding_version: str = DEFAULT_VERSION,
) -> str | None:
    """Best-effort async index request: stale placeholder + queue envelope.

    Never raises — derived index work must not break canon writes. Returns
    the job id when an envelope was published, else None.
    """
    try:
        stype = validate_source_type(source_type)
        cid = retrieval_mod._coerce_uuid(campaign_id, field_name="campaign_id")
        sid = retrieval_mod._coerce_uuid(source_id, field_name="source_id")
        if db is not None:
            try:
                existing = _find_row(db, cid, stype, sid, embedding_model, embedding_version)
                if existing is None:
                    db.add(WorldEmbedding(
                        id=uuid.uuid4(), campaign_id=cid, source_type=stype,
                        source_id=sid, source_version="pending",
                        embedding_model=embedding_model,
                        embedding_version=embedding_version, status="stale",
                        error="awaiting_async_index",
                    ))
                elif existing.status == "active":
                    existing.status = "stale"
                    existing.error = "awaiting_async_index"
                    db.add(existing)
                db.commit()
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning("world_semantic_placeholder_failed error=%s", exc)
        from app.queue.adapter import new_envelope, publish_envelope

        envelope = new_envelope(
            job_id=_deterministic_job_id(cid, stype, sid, embedding_model, embedding_version),
            job_type=SEMANTIC_INDEX_JOB_TYPE,
            campaign_id=cid,
            aggregate_id=cid,
            operation_id=f"semidx:{stype}:{sid}",
            idempotency_key=f"semidx:{stype}:{sid}:{embedding_model}:{embedding_version}",
            payload={
                "campaign_id": str(cid),
                "source_type": stype,
                "source_id": str(sid),
                "embedding_model": embedding_model,
                "embedding_version": embedding_version,
            },
        )
        return publish_envelope(envelope)
    except Exception as exc:
        logger.warning("world_semantic_enqueue_failed error=%s", exc)
        return None


def note_authoritative_write(
    db: Session | None,
    campaign_id: Any,
    entries: list[tuple[str, Any]],
    *,
    embedding_model: str = DEFAULT_MODEL,
    embedding_version: str = DEFAULT_VERSION,
) -> None:
    """Writer hook: request async reindex for created/changed sources."""
    for source_type, source_id in entries:
        request_semantic_index(
            db, campaign_id, source_type, source_id,
            embedding_model=embedding_model, embedding_version=embedding_version,
        )


def note_supersession(
    db: Session | None,
    campaign_id: Any,
    prior: tuple[str, Any],
    replacement: tuple[str, Any] | None = None,
) -> None:
    """Writer hook: retire the prior source's vectors, index the replacement."""
    try:
        if db is not None:
            try:
                mark_superseded(db, campaign_id, prior[0], prior[1], commit=True)
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning("world_semantic_supersede_mark_failed error=%s", exc)
        if replacement is not None:
            request_semantic_index(db, campaign_id, replacement[0], replacement[1])
    except Exception as exc:
        logger.warning("world_semantic_supersede_note_failed error=%s", exc)


def handle_world_semantic_index(envelope: Any, db: Session | None = None) -> dict[str, Any]:
    """Worker handler for ``world.semantic.index`` (identifiers only).

    Re-reads the authoritative record inside the worker; the envelope payload
    is never treated as truth. Missing/superseded sources resolve to a
    durable ``superseded`` mark (success, no retry); embedder failure
    propagates as retriable; validation failure is terminal.
    """
    from app.worker.executor import RetriableError

    payload = getattr(envelope, "payload", None) or {}
    close_after = False
    if db is None:
        from database import SessionLocal

        if SessionLocal is None:
            raise RetriableError("SessionLocal is not configured")
        db = SessionLocal()
        close_after = True
    try:
        assert db is not None
        try:
            row = index_source_record(
                db,
                payload.get("campaign_id"),
                payload.get("source_type"),
                payload.get("source_id"),
                embedding_model=str(payload.get("embedding_model") or DEFAULT_MODEL),
                embedding_version=str(payload.get("embedding_version") or DEFAULT_VERSION),
                commit=True,
            )
        except (ValueError, TypeError) as exc:
            from app.worker.executor import TerminalError

            raise TerminalError(f"world.semantic.index invalid payload: {exc}") from exc
        except RuntimeError as exc:
            # No embedder available (e.g. real model without API key):
            # record durable failure, retryable derived work.
            try:
                _record_index_failure(
                    db, payload, f"{type(exc).__name__}: {exc}", commit=True)
            except Exception:
                pass
            raise RetriableError(str(exc)[:500]) from exc
        return {
            "source_type": payload.get("source_type"),
            "source_id": payload.get("source_id"),
            "status": row.status if row is not None else "superseded",
            "source_version": row.source_version if row is not None else None,
        }
    finally:
        if close_after:
            try:
                db.close()  # type: ignore[union-attr]
            except Exception:
                pass


def _record_index_failure(db: Session, payload: dict[str, Any], error: str, *, commit: bool) -> None:
    try:
        cid = retrieval_mod._coerce_uuid(payload.get("campaign_id"), field_name="campaign_id")
        sid = retrieval_mod._coerce_uuid(payload.get("source_id"), field_name="source_id")
    except ValueError:
        return
    row = _find_row(
        db, cid, str(payload.get("source_type") or ""),
        sid, str(payload.get("embedding_model") or DEFAULT_MODEL),
        str(payload.get("embedding_version") or DEFAULT_VERSION),
    )
    if row is None:
        return
    row.status = "failed"
    row.error = error[:500]
    db.add(row)
    if commit:
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass


def register_world_semantic_worker() -> None:
    from app.queue.consumer import WORKER_HANDLERS

    WORKER_HANDLERS[SEMANTIC_INDEX_JOB_TYPE] = handle_world_semantic_index


register_world_semantic_worker()


# ── Bounded semantic search ──────────────────────────────────────────────────

def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit if limit is not None else SEMANTIC_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        value = SEMANTIC_DEFAULT_LIMIT
    return max(1, min(value, SEMANTIC_MAX_LIMIT))


def _embed_query(
    query_text: str, embedding_model: str,
    provider: Callable[[list[str]], list[list[float]]] | None,
) -> list[float]:
    cleaned = str(query_text or "").strip()
    if not cleaned:
        raise ValueError("semantic search requires a non-empty query")
    if len(cleaned) > MAX_INDEX_TEXT_CHARS:
        cleaned = cleaned[:MAX_INDEX_TEXT_CHARS]
    embedder = _resolve_embedder(embedding_model, provider)
    if embedder is not None:
        vectors = embedder([cleaned])
    else:
        if embedding_model != DEFAULT_MODEL and not embedding_model.startswith("stub"):
            raise RuntimeError(
                f"Embedding model {embedding_model!r} has no provider; "
                "refusing to mint stub vectors under a non-stub model name."
            )
        vectors = [_stub_embed(cleaned)]
    if not vectors or len(vectors[0]) != EMBEDDING_DIM:
        raise ValueError(
            f"query embedder returned {len(vectors[0]) if vectors else 0}-dim "
            f"vector, expected {EMBEDDING_DIM}"
        )
    return [float(v) for v in vectors[0]]


def _pgvector_candidates(
    db: Session, campaign: Campaign, query_vec: list[float], *,
    embedding_model: str, embedding_version: str, limit: int,
) -> list[tuple[uuid.UUID, float]] | None:
    """pgvector fast path: cosine-ordered active rows. None when unavailable."""
    try:
        if not _embedding_column_is_vector(db):
            return None
        literal = _vector_json(query_vec)
        rows = db.execute(
            sql_text("""
                SELECT id, (1 - (embedding <=> :vec::vector)) AS score
                FROM world_embeddings
                WHERE campaign_id = :cid AND status = 'active'
                  AND embedding_model = :model AND embedding_version = :ver
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> :vec::vector
                LIMIT :lim
            """),
            {"vec": literal, "cid": str(campaign.id), "model": embedding_model,
             "ver": embedding_version, "lim": max(1, min(int(limit), SEMANTIC_MAX_CANDIDATES))},
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
) -> list[tuple[WorldEmbedding, float]]:
    rows = db.execute(
        select(WorldEmbedding).where(
            WorldEmbedding.campaign_id == campaign.id,
            WorldEmbedding.status == "active",
            WorldEmbedding.embedding_model == embedding_model,
            WorldEmbedding.embedding_version == embedding_version,
        ).order_by(WorldEmbedding.created_at.asc()).limit(SEMANTIC_MAX_CANDIDATES)
    ).scalars().all()
    scored: list[tuple[WorldEmbedding, float]] = []
    for row in rows:
        vec = _parse_vector(row.embedding_text) or _parse_vector(row.embedding)
        if vec is None or len(vec) != EMBEDDING_DIM:
            continue
        scored.append((row, _cosine(query_vec, vec)))
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
        return retrieval_mod._authorize_world_record(
            db, campaign, kind, record.id, viewers, dm_internal=dm_internal)
    if source_type == "domain_event":
        if dm_internal:
            return True, None
        return retrieval_mod._event_visible_player_facing(db, campaign, record, viewers)
    if source_type == "source_turn":
        return retrieval_mod._turn_gate(db, campaign, record, viewers, dm_internal=dm_internal)
    if source_type == "scene":
        if dm_internal:
            return True, None
        if not viewers:
            return False, "viewer_required"
        from app.campaigns.service import is_campaign_member
        from app.world.service import is_world_authority, scene_visible_to_viewer

        authority = any(is_world_authority(campaign, viewer) for viewer in viewers)
        member = all(
            campaign.owner_id == viewer or is_campaign_member(db, campaign.id, viewer)
            for viewer in viewers
        )
        if not member or not scene_visible_to_viewer(record, authority):
            return False, "scene_not_visible"
        return True, None
    return False, "unsupported_source_type"


def _build_packet(
    source_type: str, record: Any, campaign_id: uuid.UUID, rank: int,
    similarity: float, *, revealable: bool | None,
    embedding_model: str, embedding_version: str,
) -> Any:
    """Authorized packet with vector similarity as derived provenance metadata."""
    if source_type == "world_entity":
        packet = retrieval_mod._entity_packet(record, campaign_id, rank, revealable=revealable)
    elif source_type == "world_relation":
        packet = retrieval_mod._relation_packet(record, campaign_id, rank, revealable=revealable)
    elif source_type == "world_fact":
        packet = retrieval_mod._fact_packet(record, campaign_id, rank, revealable=revealable)
    elif source_type == "domain_event":
        packet = retrieval_mod._event_packet(record, rank, revealable=revealable)
    elif source_type == "source_turn":
        packet = retrieval_mod._turn_packet(record, rank, revealable=revealable)
    elif source_type == "scene":
        packet = retrieval_mod._scene_packet(record, rank, revealable=revealable)
    else:  # pragma: no cover — guarded by validate_source_type at index time
        raise ValueError(f"unsupported semantic source type {source_type!r}")
    packet.retrieval_rank = rank
    packet.retrieval_score = float(similarity)
    # Derived ranking metadata only — content/version/provenance stay canonical.
    packet.provenance["semantic_source"] = SEMANTIC_SOURCE
    packet.provenance["semantic_model"] = embedding_model
    packet.provenance["semantic_model_version"] = embedding_version
    packet.provenance["vector_similarity"] = round(float(similarity), 6)
    return packet


def semantic_search(
    db: Session,
    campaign_id: Any,
    query_text: Any,
    viewer_user_id: Any = None,
    *,
    limit: Any = SEMANTIC_DEFAULT_LIMIT,
    min_similarity: Any = DEFAULT_MIN_SIMILARITY,
    embedding_model: str = DEFAULT_MODEL,
    embedding_version: str = DEFAULT_VERSION,
    provider: Callable[[list[str]], list[list[float]]] | None = None,
    dm_internal: bool = False,
) -> SemanticOutcome:
    """Bounded semantic search returning typed evidence references.

    Campaign scoping is mandatory; visibility filtering happens before any
    packet is returned. Stale/version-mismatched rows are dropped (and
    marked) rather than served. Weak sets yield ``no_match``; an empty or
    failed vector path yields ``defer`` with ``fallback_to_direct`` so
    callers use direct authoritative retrieval instead.
    """
    started = time.monotonic()
    limit_applied = _clamp_limit(limit)
    try:
        threshold = float(min_similarity)
    except (TypeError, ValueError):
        threshold = DEFAULT_MIN_SIMILARITY
    campaign = retrieval_mod._resolve_campaign(db, campaign_id)
    viewers = retrieval_mod._resolve_viewers(viewer_user_id)

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

    ranked: list[tuple[WorldEmbedding, float]] = []
    backend = "python"
    fast = _pgvector_candidates(
        db, campaign, query_vec, embedding_model=embedding_model,
        embedding_version=embedding_version, limit=limit_applied * 2,
    )
    if fast is not None:
        backend = "pgvector"
        by_id = {row_id: score for row_id, score in fast}
        if by_id:
            rows = db.execute(
                select(WorldEmbedding).where(WorldEmbedding.id.in_(list(by_id.keys())))
            ).scalars().all()
            ranked = sorted(
                ((row, by_id[row.id]) for row in rows if row.id in by_id),
                key=lambda item: item[1], reverse=True,
            )[:limit_applied]
    else:
        ranked = _python_candidates(
            db, campaign, query_vec, embedding_model=embedding_model,
            embedding_version=embedding_version, limit=limit_applied * 2,
        )
        # Python path over-retrieves for threshold filtering, then bounds.
    ranked = ranked[:limit_applied]

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

    # Resolve → version-check → authorize. Anything failing the chain is
    # dropped (stale rows are marked for rebuild); hidden rows are counted
    # as denials without leaking ids/content.
    packets: list[Any] = []
    denied_reasons: dict[str, int] = {}
    denied = 0
    stale_dropped = 0
    for row, similarity in ranked:
        if similarity < threshold:
            continue
        record = _current_record(db, campaign.id, row.source_type, row.source_id)
        if record is None or not _record_active(record, row.source_type):
            try:
                row.status = "superseded"
                db.add(row)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            stale_dropped += 1
            continue
        current_version = retrieval_mod._version_of(record)
        if current_version != row.source_version:
            try:
                row.status = "stale"
                row.error = "source_version_changed"
                db.add(row)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            stale_dropped += 1
            continue
        allowed, reason = _authorize_candidate(
            db, campaign, record, row.source_type, viewers, dm_internal=dm_internal)
        if not allowed:
            denied += 1
            denied_reasons[reason or "denied"] = denied_reasons.get(reason or "denied", 0) + 1
            continue
        packets.append(_build_packet(
            row.source_type, record, campaign.id, len(packets), similarity,
            revealable=None if dm_internal else True,
            embedding_model=embedding_model, embedding_version=embedding_version,
        ))
    if packets:
        for rank, packet in enumerate(packets):
            packet.retrieval_rank = rank
    outcome = SemanticOutcome(
        status=STATUS_OK if packets else STATUS_NO_MATCH,
        packets=packets,
        total_candidates=len(ranked),
        visible=len(packets),
        denied=denied,
        denied_reasons=denied_reasons,
        stale_dropped=stale_dropped,
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


# ── Optional second-stage rerank (#380/#381, never blocking) ─────────────────

def apply_semantic_rerank(
    outcome: SemanticOutcome,
    *,
    order: list[str] | None = None,
    defer: bool = False,
    reranker: Callable[[list[dict[str, str]]], Any] | None = None,
    reranker_model: str | None = None,
    reranker_policy: str | None = None,
) -> SemanticOutcome:
    """Reorder/select only from the already-authorized candidate packets.

    Reranker failure falls back to similarity order (``rerank.fallback``) —
    authoritative evidence is never lost. Vector + rerank scores stay derived
    metadata on provenance.
    """
    reranked = retrieval_mod.apply_rerank(
        list(outcome.packets), order=order, defer=defer, reranker=reranker,
        reranker_model=reranker_model, reranker_policy=reranker_policy,
    )
    outcome.packets = list(reranked.packets)
    if reranked.status in {retrieval_mod.STATUS_DEFER, retrieval_mod.STATUS_NO_MATCH}:
        outcome.status = STATUS_NO_MATCH if outcome.packets else STATUS_DEFER
        if not outcome.packets:
            outcome.status = STATUS_DEFER
    outcome.rerank = {
        "reranked": reranked.reranked,
        "fallback": reranked.fallback,
        "presented_ids": list(reranked.presented_ids),
        "reordered_ids": list(reranked.reordered_ids),
        "model": reranked.reranker_model,
        "policy": reranked.reranker_policy,
        "error": reranked.error,
    }
    return outcome


# ── #203 evidence-mediation tool (search_campaign_memory) ────────────────────

def handle_search_campaign_memory(req: Any, audience: Any, db: Any = None) -> dict[str, Any]:
    """Semantic recall as a #203 evidence tool: typed references, never claims.

    Embedding/search failure returns ``unknown`` with ``fallback_to_direct``
    so the loop can use direct authoritative tools instead of losing recall.
    """
    if db is None:
        # No session: behave like the mediation stub (unknown, audience-scoped
        # auth) so bounded loops continue instead of failing validation.
        try:
            campaign_id = str(getattr(audience, "campaign_id", "") or "")
        except Exception:
            campaign_id = ""
        try:
            thread_id = str(getattr(audience, "thread_id", "") or "")
        except Exception:
            thread_id = ""
        return {
            "status": "unknown",
            "sources": [],
            "visibility": "campaign",
            "authorization": {
                "campaign_id": campaign_id,
                "thread_ids": [thread_id] if thread_id else [],
            },
            "payload": {"retrieval_status": STATUS_DEFER,
                        "error": "semantic search requires a database session",
                        "fallback_to_direct": True},
            "result_count": 0,
        }
    dm_internal = getattr(audience, "audience", "campaign") != "private"
    query = (getattr(req, "query", None) or "").strip()
    if not query:
        raise ValueError("search_campaign_memory requires query")
    try:
        viewers = list(getattr(audience, "user_ids", None) or [])
    except Exception:
        viewers = []
    try:
        outcome = semantic_search(
            db, getattr(audience, "campaign_id", None), query, viewers,
            limit=getattr(req, "limit", None), dm_internal=dm_internal)
    except Exception as exc:
        logger.warning("world_semantic_tool_failed error=%s", exc)
        return {
            "status": "unknown",
            "sources": [],
            "visibility": "campaign",
            "authorization": {
                "campaign_id": str(getattr(audience, "campaign_id", "") or ""),
                "thread_ids": ([str(getattr(audience, "thread_id", ""))]
                               if getattr(audience, "thread_id", None) else []),
            },
            "payload": {"retrieval_status": STATUS_DEFER, "error": str(exc)[:300],
                        "fallback_to_direct": True},
            "result_count": 0,
        }
    packets = list(outcome.packets)
    sources = [p.to_source_ref() for p in packets]
    if outcome.status == STATUS_DEFER and not packets:
        status = "unknown"
    else:
        status = "ok" if packets else "unknown"
    if dm_internal:
        visibility = "dm_only" if any(
            p.visibility in {"private", "dm_only"} for p in packets) else "campaign"
    else:
        rank = {"public": 0, "campaign": 1, "dm_only": 2, "private": 3}
        visibility = "campaign"
        for packet in packets:
            if rank.get(packet.visibility, 1) > rank.get(visibility, 1):
                visibility = packet.visibility
    try:
        thread_id = str(getattr(audience, "thread_id", ""))
    except Exception:
        thread_id = ""
    try:
        campaign_id = str(getattr(audience, "campaign_id", ""))
    except Exception:
        campaign_id = ""
    authorization: dict[str, Any] = {"campaign_id": campaign_id, "thread_ids": []}
    if thread_id:
        authorization["thread_ids"] = [thread_id]
    if visibility == "private":
        authorization["user_ids"] = viewers
    return {
        "status": status,
        "sources": sources,
        "visibility": visibility,
        "authorization": authorization,
        "payload": {
            "retrieval_status": outcome.status,
            "packets": [p.to_dict() for p in packets],
            "total_candidates": outcome.total_candidates,
            "visible": outcome.visible,
            "denied": outcome.denied,
            "denied_reasons": dict(outcome.denied_reasons),
            "stale_dropped": outcome.stale_dropped,
            "embedding_model": outcome.embedding_model,
            "embedding_version": outcome.embedding_version,
            "top_similarity": outcome.top_similarity,
            "vector_backend": outcome.vector_backend,
            "fallback_to_direct": outcome.fallback_to_direct,
            "latency_ms": outcome.latency_ms,
        },
        "result_count": len(packets),
    }
