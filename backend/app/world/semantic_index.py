"""Semantic index over authoritative source records — issue #213.

Embeddings are a rebuildable derived index over canonical world records
(entities, relations, facts, domain events, source turns, current scene),
never a source of truth. Every embedding row points at an exact
(source_type, source_id, source_version, campaign, embedding model/version).
Storage mirrors the ``rules_embeddings`` branch pattern: ``vector(1536)`` +
HNSW on Postgres with pgvector, portable JSON text otherwise, with
``embedding_text`` always readable for the graceful-degradation path.

Async indexing: writers stage a ``stale`` placeholder row (identifiers
only); :func:`run_semantic_index_sweep` (driven by the post-turn cron)
re-reads the authoritative record and upserts the embedding idempotently.
Index work never mutates canon and never breaks canon writes.

Search lives in :mod:`app.world.semantic`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import uuid
from typing import Any, Callable

from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session

from app.rules_corpus.gemini import make_gemini_provider
from app.schema import coerce_uuid
from app.observability.tracing import structured_log
from app.world.evidence_packets import resolve_campaign, version_of
from models.campaigns import CampaignDomainEvent
from models.dm import DmTurn
from models.world import (
    SEMANTIC_SOURCE_TYPES,
    WorldEmbedding,
    WorldEntity,
    WorldFact,
    WorldRelation,
)

logger = logging.getLogger(__name__)

# ── Model defaults ───────────────────────────────────────────────────────────

DEFAULT_MODEL = "stub-hash-v1"
DEFAULT_VERSION = "1"

# Production embedding model selected when Gemini is configured (mirrors the
# #333 ingest-CLI precedence). The stub stays the explicit offline/test
# fallback — never silently minted under a real model name.
GEMINI_DEFAULT_MODEL = "gemini-embedding-2"


def is_stub_model(embedding_model: str | None) -> bool:
    """True for the explicit offline/test stub (or an unset model)."""
    if not embedding_model:
        return True
    text = str(embedding_model).strip()
    return text == DEFAULT_MODEL or text.startswith("stub")


def resolve_embedding_model(explicit_model: str | None = None) -> str:
    """Model for automatic index/search paths (issue #213 review round 1).

    Precedence: explicit model > ``GEMINI_EMBEDDING_MODEL`` env > the
    production Gemini model when a Gemini key is configured > stub-hash-v1.
    Keeps writers and search consistent: both resolve through this one
    function instead of hardcoding the stub.
    """
    raw = (str(explicit_model).strip() if explicit_model else "") or os.getenv(
        "GEMINI_EMBEDDING_MODEL", ""
    ).strip()
    if raw:
        return raw
    if (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GOOGLE_GENAI_API_KEY")
    ):
        return GEMINI_DEFAULT_MODEL
    return DEFAULT_MODEL

try:  # Single canonical indexed dimension (issue #334); safe local fallback.
    from app.rules_corpus.gemini import EMBEDDING_DIM as _DIM  # type: ignore

    EMBEDDING_DIM = int(_DIM)
except Exception:
    EMBEDDING_DIM = 1536
assert EMBEDDING_DIM == 1536, "indexed embedding dimension must be 1536"

MAX_INDEX_TEXT_CHARS = 4000


# ── Embedding math (deterministic stub; real provider injectable) ────────────

def stub_embed(text_value: str, dim: int = EMBEDDING_DIM) -> list[float]:
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


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def parse_vector(raw: Any) -> list[float] | None:
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


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def resolve_embedder(
    embedding_model: str,
    provider: Callable[[list[str]], list[list[float]]] | None,
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> Callable[[list[str]], list[list[float]]] | None:
    """Return a real embedder or None for the deterministic stub path.

    ``make_gemini_provider`` already returns a ``texts -> vectors`` callable
    (see the #223 rules-embeddings precedent), so it is used directly — never
    via a nonexistent ``.embed`` attribute. Document indexing uses
    ``RETRIEVAL_DOCUMENT``; query embedding must pass
    ``RETRIEVAL_QUERY`` so both sides share the retrieval task space.

    Real models fail visibly when no provider is available — never silently
    mint stub vectors labeled as a real model.
    """
    if provider is not None:
        return provider
    if embedding_model.startswith(("gemini", "text-embedding", "models/")):
        try:
            return make_gemini_provider(model=embedding_model, task_type=task_type)
        except Exception as exc:
            raise RuntimeError(
                f"Embedding model {embedding_model!r} requested but no provider "
                f"is available: {exc}. Set GEMINI_API_KEY or use stub-hash-v1."
            ) from exc
    return None


# ── Source text + version (code-owned, from persisted records only) ──────────

def _entity_display(db: Session, entity_id: Any, names: dict[str, str] | None = None) -> str:
    if names is not None:
        return names.get(str(entity_id), str(entity_id))
    try:
        entity = db.get(WorldEntity, entity_id)
    except Exception:
        return str(entity_id)
    if entity is None:
        return str(entity_id)
    return str(getattr(entity, "name", entity_id))


def turn_narrations(
    db: Session, campaign_id: uuid.UUID, turns: list[Any],
) -> dict[str, str]:
    """Committed narration per turn id: the completed stream of the turn's
    current attempt (else its latest completed stream). Narration is the
    public projection already shown to the turn's audience."""
    from models.dm import DMStream, DMStreamChunk

    current = {str(t.id): str(t.current_attempt_id or "") for t in turns}
    if not current:
        return {}
    rows = db.execute(
        select(DMStream.id, DMStream.turn_id, DMStream.attempt_id, DMStream.final_text,
               DMStream.completed_at)
        .where(DMStream.campaign_id == campaign_id, DMStream.status == "completed",
               DMStream.turn_id.in_(list(current)))
    ).all()
    rows = sorted(rows, key=lambda r: (
        r.attempt_id == current.get(r.turn_id),
        r.completed_at.timestamp() if r.completed_at else 0.0))
    # Same fallback as snapshots: a completed stream without final_text is
    # the concatenation of its chunks.
    unjoined = [r.id for r in rows if r.final_text is None]
    joined: dict[Any, list[str]] = {}
    if unjoined:
        for stream_id, chunk_text in db.execute(
            select(DMStreamChunk.stream_id, DMStreamChunk.text)
            .where(DMStreamChunk.stream_id.in_(unjoined))
            .order_by(DMStreamChunk.stream_id, DMStreamChunk.sequence)
        ).all():
            joined.setdefault(stream_id, []).append(chunk_text or "")
    out: dict[str, str] = {}
    for r in rows:
        text_value = r.final_text if r.final_text is not None else "".join(joined.get(r.id, []))
        if text_value:
            out[r.turn_id] = str(text_value)
    return out


def build_source_text(
    db: Session, source_type: str, record: Any, *,
    entity_names: dict[str, str] | None = None,
    submission_texts: dict[str, str] | None = None,
    narration_texts: dict[str, str] | None = None,
) -> str:
    """Deterministic index text for one authoritative record.

    Batch callers (lexical search) pass preloaded ``entity_names``,
    ``submission_texts``, and ``narration_texts`` maps so one record costs
    no extra queries.
    """
    parts: list[str] = []
    if source_type == "world_entity":
        parts = [f"{record.name} ({record.entity_type})", str(record.summary or "")]
        details = getattr(record, "details", None)
        if isinstance(details, dict):
            role = details.get("role")
            if role:
                parts.append(str(role)[:500])
    elif source_type == "world_relation":
        subject = _entity_display(db, getattr(record, "subject_entity_id", None), entity_names)
        obj_id = getattr(record, "object_entity_id", None)
        obj = _entity_display(db, obj_id, entity_names) if obj_id else str(getattr(record, "object_label", "") or "")
        parts = [
            f"{subject} {getattr(record, 'relation_type', '')} {obj}",
            f"epistemic={getattr(record, 'epistemic_state', '')}",
        ]
    elif source_type == "world_fact":
        parts = [str(getattr(record, "content", "") or "")]
        for ref in list(getattr(record, "entity_refs", None) or [])[:8]:
            try:
                parts.append(_entity_display(db, ref, entity_names))
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
        if submission_texts is not None:
            chunk = [submission_texts[str(raw_sid)]
                     for raw_sid in list(getattr(record, "submission_ids", None) or [])
                     if str(raw_sid) in submission_texts]
        else:
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
        if narration_texts is None:
            try:
                narration_texts = turn_narrations(db, record.campaign_id, [record])
            except Exception:
                narration_texts = {}
        parts = [f"turn audience={getattr(record, 'audience', '')}", *chunk,
                 narration_texts.get(str(record.id), "")]
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
def current_source_record(
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


def source_record_active(record: Any, source_type: str) -> bool:
    """Only ``active`` lifecycle rows are current truth (relations/facts)."""
    if source_type in {"world_relation", "world_fact"}:
        return str(getattr(record, "status", "active")) == "active"
    return True


def authoritative_source_version(record: Any, source_type: str) -> str:
    """Canonical version string for one authoritative record.

    Matches what evidence packets report (``_scene_packet`` uses
    ``r{revision}``): the transient scene row has no ``version`` column, so
    the generic ``_version_of`` falls back to ``updated_at`` — which is
    second-precision on some backends and never moves on rapid revision
    bumps. Using the scene ``revision`` keeps stored index versions,
    search-time staleness checks, and job keys on the same canonical
    version the rest of the world path already uses.
    """
    if source_type == "scene":
        try:
            return f"r{int(getattr(record, 'revision', 0) or 0)}"
        except (TypeError, ValueError):
            pass
    if source_type == "domain_event":
        # Matches the authoritative event packet (``seq{sequence}`` in
        # retrieval.py): CampaignDomainEvent has neither ``version`` nor
        # ``updated_at``, so the generic fallback would store "1" while
        # evidence references report the sequence.
        try:
            return f"seq{int(getattr(record, 'sequence', 0) or 0)}"
        except (TypeError, ValueError):
            pass
    return version_of(record)


# ── pgvector detection (cached; code-owned failure taxonomy) ─────────────────

_PGVECTOR_STATUS: bool | None = None
_PGVECTOR_COLUMN_VECTOR: bool | None = None


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


def embedding_column_is_vector(db: Session) -> bool:
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
    literal = vector_literal(vec)
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
    embedding_model: str | None = None,
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
    embedding_model = resolve_embedding_model(embedding_model)
    campaign = resolve_campaign(db, campaign_id)
    try:
        sid = coerce_uuid(source_id, field="source_id")
    except ValueError:
        raise

    record = current_source_record(db, campaign.id, stype, sid)
    existing = _find_row(db, campaign.id, stype, sid, embedding_model, embedding_version)
    if record is None or not source_record_active(record, stype):
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

    source_version = authoritative_source_version(record, stype)
    index_text = build_source_text(db, stype, record)
    embedder = resolve_embedder(embedding_model, provider)
    if embedder is not None:
        vectors = embedder([index_text])
    else:
        if not is_stub_model(embedding_model):
            raise RuntimeError(
                f"Embedding model {embedding_model!r} has no provider; "
                "refusing to mint stub vectors under a non-stub model name."
            )
        logger.warning(
            "world_semantic_stub_embeddings model=%s version=%s "
            "reason=stub_fallback — relevance only; set GEMINI_API_KEY for production",
            embedding_model, embedding_version,
        )
        vectors = [stub_embed(index_text)]
    if not vectors or len(vectors[0]) != EMBEDDING_DIM:
        raise ValueError(
            f"embedder returned {len(vectors[0]) if vectors else 0}-dim vector, "
            f"expected {EMBEDDING_DIM}"
        )
    vec = [float(v) for v in vectors[0]]
    vec_json = vector_literal(vec)

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
    if embedding_column_is_vector(db):
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
    campaign = resolve_campaign(db, campaign_id)
    sid = coerce_uuid(source_id, field="source_id")
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
    campaign = resolve_campaign(db, campaign_id)
    sid = coerce_uuid(source_id, field="source_id")
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


# ── Async indexing (placeholder rows + cron sweep) ──────────────────────────

def _read_source_version(
    db: Session | None,
    campaign_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
) -> str | None:
    """Best-effort current authoritative version of one source record."""
    if db is None:
        return None
    try:
        record = current_source_record(db, campaign_id, source_type, source_id)
    except Exception:
        return None
    if record is None:
        return None
    try:
        return authoritative_source_version(record, source_type)
    except Exception:
        return None


def request_semantic_index(
    db: Session | None,
    campaign_id: Any,
    source_type: Any,
    source_id: Any,
    *,
    embedding_model: str | None = None,
    embedding_version: str = DEFAULT_VERSION,
) -> uuid.UUID | None:
    """Best-effort async index request: stage a ``stale`` placeholder row.

    :func:`run_semantic_index_sweep` picks the row up and embeds it. Never
    raises — derived index work must not break canon writes. Returns the
    placeholder row id when one is pending, else None (no session, or the
    active vector already matches the current source version).
    """
    try:
        stype = validate_source_type(source_type)
        embedding_model = resolve_embedding_model(embedding_model)
        cid = coerce_uuid(campaign_id, field="campaign_id")
        sid = coerce_uuid(source_id, field="source_id")
        if db is None:
            return None
        source_version = _read_source_version(db, cid, stype, sid)
        try:
            existing = _find_row(db, cid, stype, sid, embedding_model, embedding_version)
            if existing is None:
                existing = WorldEmbedding(
                    id=uuid.uuid4(), campaign_id=cid, source_type=stype,
                    source_id=sid, source_version="pending",
                    embedding_model=embedding_model,
                    embedding_version=embedding_version, status="stale",
                    error="awaiting_async_index",
                )
                db.add(existing)
            elif (
                existing.status == "active"
                and source_version is not None
                and existing.source_version == source_version
            ):
                # Unchanged source: the valid vector keeps serving.
                return None
            else:
                existing.status = "stale"
                existing.error = "awaiting_async_index"
                db.add(existing)
            row_id = existing.id
            db.commit()
            return row_id
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("world_semantic_placeholder_failed error=%s", exc)
            return None
    except Exception as exc:
        logger.warning("world_semantic_enqueue_failed error=%s", exc)
        return None


def note_authoritative_write(
    db: Session | None,
    campaign_id: Any,
    entries: list[tuple[str, Any]],
    *,
    embedding_model: str | None = None,
    embedding_version: str = DEFAULT_VERSION,
) -> None:
    """Writer hook: request async reindex for created/changed sources."""
    embedding_model = resolve_embedding_model(embedding_model)
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


def note_turn_committed(
    db: Session | None,
    campaign_id: Any,
    turn_id: Any,
    attempt_id: Any | None = None,
    *,
    event_id: Any | None = None,
) -> dict[str, int]:
    """Post-commit hook for staged DM-turn writes (issue #213 review round 1).

    Staged ``assert_fact`` / ``upsert_relation`` effects (plus JIT-promoted
    entities) write inside the turn transaction, so a committed turn's
    records would never become searchable without this. Call once,
    AFTER the turn commit, with the committed session: it collects the
    turn's facts/relations/entities (attempt-scoped, falling back to the
    turn) and routes creates through :func:`note_authoritative_write` and
    version successors through :func:`note_supersession`. The turn record
    itself and its domain event are declared semantic sources too, so they
    are enqueued from the already-available IDs.

    Placeholder rows commit on the caller's session, so this must run after
    the turn transaction commits, never inside it. Never raises.
    """
    counts = {"entities": 0, "relations": 0, "facts": 0,
              "source_turns": 0, "domain_events": 0}
    if db is None:
        return counts
    try:
        cid = coerce_uuid(campaign_id, field="campaign_id")
        try:
            tid = coerce_uuid(turn_id, field="turn_id")
        except ValueError:
            tid = None
        try:
            aid = (
                coerce_uuid(attempt_id, field="attempt_id")
                if attempt_id is not None else None
            )
        except ValueError:
            aid = None
        if tid is None and aid is None:
            return counts
        tables: list[tuple[str, Any]] = [
            ("world_entity", WorldEntity),
            ("world_relation", WorldRelation),
            ("world_fact", WorldFact),
        ]
        for stype, model in tables:
            rows: list[Any] = []
            if aid is not None:
                try:
                    rows = list(db.execute(
                        select(model).where(
                            model.campaign_id == cid,
                            model.source_attempt_id == aid,
                        )
                    ).scalars().all())
                except Exception:
                    rows = []
            if not rows and tid is not None:
                try:
                    rows = list(db.execute(
                        select(model).where(
                            model.campaign_id == cid,
                            model.source_turn_id == tid,
                        )
                    ).scalars().all())
                except Exception:
                    rows = []
            creates: list[tuple[str, Any]] = []
            for row in rows:
                try:
                    prior_id = getattr(row, "supersedes_id", None)
                    if prior_id is not None:
                        note_supersession(
                            db, cid, (stype, prior_id), (stype, row.id))
                    else:
                        creates.append((stype, row.id))
                except Exception as exc:
                    logger.warning(
                        "world_semantic_turn_note_row_failed source_type=%s error=%s",
                        stype, exc,
                    )
            if creates:
                try:
                    note_authoritative_write(db, cid, creates)
                except Exception as exc:
                    logger.warning(
                        "world_semantic_turn_note_failed source_type=%s error=%s",
                        stype, exc,
                    )
            key = {"world_entity": "entities", "world_relation": "relations",
                   "world_fact": "facts"}[stype]
            counts[key] = len(rows)
        if tid is not None:
            try:
                request_semantic_index(db, cid, "source_turn", tid)
                counts["source_turns"] = 1
            except Exception as exc:
                logger.warning(
                    "world_semantic_turn_note_failed source_type=source_turn error=%s",
                    exc,
                )
        if event_id is not None:
            try:
                eid = coerce_uuid(event_id, field="event_id")
                request_semantic_index(db, cid, "domain_event", eid)
                counts["domain_events"] = 1
            except Exception as exc:
                logger.warning(
                    "world_semantic_turn_note_failed source_type=domain_event error=%s",
                    exc,
                )
    except Exception as exc:
        logger.warning("world_semantic_turn_note_failed error=%s", exc)
    return counts


#: Failed rows are retried by the sweep after this backoff (seconds).
SEMANTIC_RETRY_FAILED_AFTER_SECONDS = 900


def run_semantic_index_sweep(db: Session, *, limit: int = 20) -> dict[str, Any]:
    """Embed placeholder rows awaiting async indexing.

    Selects ``stale`` rows plus ``failed`` rows past the retry backoff,
    oldest first, and re-indexes each from its authoritative record via
    :func:`index_source_record`. Missing/superseded sources resolve to a
    durable ``superseded`` mark; embedder/validation failures record a
    durable ``failed`` row. Never raises per row — one bad source cannot
    stall the rest.
    """
    from datetime import datetime, timedelta, timezone

    retry_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=SEMANTIC_RETRY_FAILED_AFTER_SECONDS)
    candidates = db.execute(
        select(
            WorldEmbedding.id, WorldEmbedding.campaign_id,
            WorldEmbedding.source_type, WorldEmbedding.source_id,
            WorldEmbedding.embedding_model, WorldEmbedding.embedding_version,
        )
        .where(
            (WorldEmbedding.status == "stale")
            | ((WorldEmbedding.status == "failed")
               & (WorldEmbedding.updated_at < retry_cutoff))
        )
        .order_by(WorldEmbedding.updated_at.asc())
        .limit(max(1, limit))
    ).all()
    indexed: list[str] = []
    failed: list[dict[str, str]] = []
    for row_id, cid, stype, sid, model, version in candidates:
        try:
            index_source_record(
                db, cid, stype, sid,
                embedding_model=model, embedding_version=version, commit=True,
            )
            indexed.append(str(row_id))
        except Exception as exc:  # noqa: BLE001 — sweep must survive bad rows
            try:
                db.rollback()
            except Exception:
                pass
            error = f"{type(exc).__name__}: {exc}"
            try:
                _record_index_failure(db, row_id, error)
            except Exception:
                pass
            logger.warning(
                "world_semantic_index_failed row_id=%s source_type=%s error=%s",
                row_id, stype, error[:300],
            )
            failed.append({"row_id": str(row_id), "error": error[:300]})
    return {"indexed": indexed, "failed": failed}


def _record_index_failure(db: Session, row_id: uuid.UUID, error: str) -> None:
    row = db.get(WorldEmbedding, row_id)
    if row is None:
        return
    row.status = "failed"
    row.error = error[:500]
    db.add(row)
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
