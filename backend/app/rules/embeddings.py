"""Rebuildable derived embeddings — issue #223.

Vectors are derived, never canonical. Rebuilding with a new model/chunking
must not change rule_id/citation.

Default: deterministic hash-based stub for tests/offline. Plug real provider via env.
Gemini Embeddings 2 (gemini-embedding-001) is supported via app.rules.gemini.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from typing import Any

from sqlalchemy.orm import Session

from models.rules import RulesSection
from models.rules import RulesEmbedding

DEFAULT_MODEL = "stub-hash-v1"
DEFAULT_VERSION = "1"
DEFAULT_DIM = 1536

logger = logging.getLogger(__name__)

# Gemini Embeddings 2 default (can be overridden via GEMINI_EMBEDDING_MODEL)
GEMINI_DEFAULT_MODEL = "gemini-embedding-2"
GEMINI_DEFAULT_VERSION = "1"


def _stub_embed(text: str, dim: int = DEFAULT_DIM) -> list[float]:
    """Deterministic fake embedding: hash chunks -> float vector, normalized."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand via repeated hashing to dim
    vals: list[float] = []
    counter = 0
    while len(vals) < dim:
        chunk = hashlib.sha256(h + counter.to_bytes(4, "little")).digest()
        for b in chunk:
            vals.append((b / 127.5) - 1.0)
            if len(vals) >= dim:
                break
        counter += 1
    # L2 normalize
    norm = sum(x * x for x in vals) ** 0.5
    if norm > 0:
        vals = [x / norm for x in vals]
    return vals


def _section_text(section: RulesSection) -> str:
    """Semantically coherent section with heading/path context — no arbitrary chunking."""
    path = " > ".join(section.heading_path) if isinstance(section.heading_path, list) else str(section.heading_path)
    return f"{path}\n{section.title}\n{section.body}"


def _resolve_provider(embedding_model: str, provider: Any | None):
    """Auto-resolve Gemini provider when model looks like Gemini.

    Real models must fail visibly if provider cannot be constructed (e.g. missing
    GEMINI_API_KEY) — never silently create stub vectors labeled as gemini.
    Only the explicit stub model may use stub embeddings.
    """
    if provider is not None:
        return provider
    # Only auto-resolve for real embedding models; stub models fall through to stub
    if embedding_model.startswith("gemini") or embedding_model.startswith("text-embedding") or embedding_model.startswith("models/"):
        try:
            from app.rules.gemini import make_gemini_provider

            return make_gemini_provider(model=embedding_model)
        except Exception as exc:
            raise RuntimeError(
                f"Embedding model {embedding_model!r} requested but Gemini provider cannot be constructed: {exc}. "
                f"Set GEMINI_API_KEY or use stub-hash-v1 for tests."
            ) from exc
    return None


def build_embeddings(
    db: Session,
    *,
    corpus_id: str | None = None,
    corpus_version: str | None = None,
    embedding_model: str = DEFAULT_MODEL,
    embedding_version: str = DEFAULT_VERSION,
    build_id: str | None = None,
    dim: int = DEFAULT_DIM,
    provider: Any | None = None,
) -> str:
    """Build/rebuild embeddings for matching sections.

    provider: optional callable(texts: list[str]) -> list[list[float]] for real embeddings.
    If provider is None and embedding_model looks like a Gemini model, a Gemini provider
    is auto-resolved (requires GEMINI_API_KEY). Otherwise falls back to stub.
    Returns build_id.
    """
    # If build_id not forced, only embed missing to save cost (WHERE NOT EXISTS)
    is_forced_rebuild = build_id is not None
    bid = build_id or f"emb_{uuid.uuid4().hex[:12]}"
    q = db.query(RulesSection)
    if corpus_id:
        q = q.filter(RulesSection.corpus_id == corpus_id)
    if corpus_version:
        q = q.filter(RulesSection.corpus_version == corpus_version)
    sections = q.all()
    if not sections:
        return bid

    # Cost saver: WHERE NOT EXISTS before embedding — only call provider for missing
    if not is_forced_rebuild:
        existing_q = db.query(RulesEmbedding.rule_id).filter(
            RulesEmbedding.embedding_model == embedding_model,
            RulesEmbedding.embedding_version == embedding_version,
        )
        if corpus_id:
            existing_q = existing_q.filter(RulesEmbedding.corpus_id == corpus_id)
        if corpus_version:
            existing_q = existing_q.filter(RulesEmbedding.corpus_version == corpus_version)
        existing_ids = {r[0] for r in existing_q.all()}
        if existing_ids:
            missing = [s for s in sections if s.rule_id not in existing_ids]
            if not missing:
                # All already embedded — reuse latest build_id, no API cost
                latest = (
                    db.query(RulesEmbedding.build_id)
                    .filter(
                        RulesEmbedding.embedding_model == embedding_model,
                        RulesEmbedding.embedding_version == embedding_version,
                    )
                    .order_by(RulesEmbedding.created_at.desc())
                    .first()
                )
                return latest[0] if latest else bid
            sections = missing
            # Reuse latest build_id for the new missing rows if one exists, else use bid
            # Keeping one build_id per model keeps hybrid_search simple
            latest = (
                db.query(RulesEmbedding.build_id)
                .filter(
                    RulesEmbedding.embedding_model == embedding_model,
                    RulesEmbedding.embedding_version == embedding_version,
                )
                .order_by(RulesEmbedding.created_at.desc())
                .first()
            )
            if latest:
                bid = latest[0]

    # Resolve provider: explicit > gemini auto > stub
    resolved = _resolve_provider(embedding_model, provider)
    if resolved is not None:
        provider = resolved

    texts = [_section_text(s) for s in sections]
    if provider:
        vectors = provider(texts)
        # Gemini (and other real providers) decide dim; dim param is advisory for stub only
    else:
        # Explicit degradation: stub vectors are test/offline only and must never
        # silently stand in for the retrieval path (issue #333).
        logger.warning(
            "rules embeddings using stub model=%s version=%s reason=stub_fallback "
            "count=%d — lexical-only retrieval expected; set GEMINI_API_KEY or pass a real --model",
            embedding_model,
            embedding_version,
            len(sections),
        )
        vectors = [_stub_embed(t, dim=dim) for t in texts]

    for sec, vec in zip(sections, vectors):
        # Store as JSON text for portability; pgvector path will cast
        vec_json = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        existing = db.get(RulesEmbedding, (sec.rule_id, embedding_model, bid))
        if existing:
            existing.embedding = vec_json
            existing.embedding_text = vec_json
            existing.corpus_id = sec.corpus_id
            existing.corpus_version = sec.corpus_version
            existing.embedding_version = embedding_version
        else:
            emb = RulesEmbedding(
                rule_id=sec.rule_id,
                corpus_id=sec.corpus_id,
                corpus_version=sec.corpus_version,
                embedding_model=embedding_model,
                embedding_version=embedding_version,
                build_id=bid,
                embedding=vec_json,
                embedding_text=vec_json,
                chunk_strategy="section_with_heading_context",
            )
            db.add(emb)
    db.commit()
    return bid


def clear_embeddings(db: Session, *, embedding_model: str | None = None, build_id: str | None = None):
    q = db.query(RulesEmbedding)
    if embedding_model:
        q = q.filter(RulesEmbedding.embedding_model == embedding_model)
    if build_id:
        q = q.filter(RulesEmbedding.build_id == build_id)
    q.delete(synchronize_session=False)
    db.commit()
