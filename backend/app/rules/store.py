"""Canonical rule storage + retrieval — issue #223.

- canonical records in Postgres, independent of campaign tables
- lexical via tsvector (postgres) / LIKE fallback (sqlite)
- vector via pgvector as derived index (optional)
- exact stable-ID lookup independent of search
- hybrid bounded retrieval with deterministic fusion
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from models.rules import RulesSection
from models.rules import RulesCorpus
from models.rules import RulesEmbedding

logger = logging.getLogger(__name__)


def get_corpus(db: Session, corpus_id: str, corpus_version: str) -> RulesCorpus | None:
    return db.get(RulesCorpus, (corpus_id, corpus_version))


def lookup_by_rule_id(db: Session, rule_id: str, *, corpus_version: str | None = None) -> RulesSection | None:
    """Exact stable-ID lookup — works independent of FTS/vector."""
    t0 = time.monotonic()
    if corpus_version:
        # rule_id is global; filter by version if provided for historical stability
        row = db.query(RulesSection).filter(RulesSection.rule_id == rule_id, RulesSection.corpus_version == corpus_version).first()
    else:
        row = db.get(RulesSection, rule_id)
    latency = (time.monotonic() - t0) * 1000
    logger.info("rules_lookup", extra={"rule_id": rule_id, "found": row is not None, "latency_ms": round(latency, 2)})
    return row


def lookup_by_locator(db: Session, source_locator: str, *, corpus_id: str = "dnd-srd", corpus_version: str = "5.2.1") -> RulesSection | None:
    return db.query(RulesSection).filter(
        RulesSection.source_locator == source_locator,
        RulesSection.corpus_id == corpus_id,
        RulesSection.corpus_version == corpus_version,
    ).first()


def _dialect(db: Session) -> str:
    try:
        return db.bind.dialect.name if db.bind else "sqlite"
    except Exception:
        return "sqlite"


def search_lexical(db: Session, query: str, *, corpus_id: str | None = None, limit: int = 5) -> list[tuple[RulesSection, float]]:
    """Lexical search — postgres FTS or sqlite LIKE fallback. Returns (section, score)."""
    if not query or not query.strip():
        return []
    dialect = _dialect(db)
    limit = max(1, min(limit, 20))
    if dialect == "postgresql":
        # Use tsvector rank; safe param binding
        sql = """
            SELECT rule_id, ts_rank(to_tsvector('english', title || ' ' || body), plainto_tsquery('english', :q)) as rank
            FROM rules_sections
            WHERE to_tsvector('english', title || ' ' || body) @@ plainto_tsquery('english', :q)
        """
        params: dict[str, Any] = {"q": query}
        if corpus_id:
            sql += " AND corpus_id = :cid"
            params["cid"] = corpus_id
        sql += " ORDER BY rank DESC LIMIT :lim"
        params["lim"] = limit
        rows = db.execute(text(sql), params).fetchall()
        result = []
        for r in rows:
            sec = db.get(RulesSection, r[0])
            if sec:
                result.append((sec, float(r[1] or 0)))
        return result
    else:
        # sqlite fallback: LIKE on title/body, simple score by occurrence
        q = f"%{query.lower()}%"
        qry = db.query(RulesSection).filter(
            (RulesSection.title.ilike(q)) | (RulesSection.body.ilike(q))
        )
        if corpus_id:
            qry = qry.filter(RulesSection.corpus_id == corpus_id)
        rows = qry.limit(limit).all()
        return [(r, 1.0) for r in rows]


def search_vector(db: Session, query_embedding: list[float] | None, *, corpus_id: str | None = None, limit: int = 5, embedding_model: str | None = None) -> list[tuple[RulesSection, float]]:
    """Vector search if pgvector available and embedding provided; else empty."""
    if not query_embedding:
        return []
    dialect = _dialect(db)
    if dialect != "postgresql":
        return []
    # Check if vector extension exists
    try:
        has = db.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()
        if not has:
            return []
    except Exception:
        return []
    limit = max(1, min(limit, 20))
    # We store embedding as vector type; cosine similarity. Use raw vector param.
    # psycopg2 vector literal: '[1,2,3]'
    vec_literal = "[" + ",".join(str(float(x)) for x in query_embedding) + "]"
    params: dict[str, Any] = {"vec": vec_literal, "lim": limit}
    model_filter = ""
    if embedding_model:
        model_filter = " AND e.embedding_model = :model"
        params["model"] = embedding_model
    corpus_filter = ""
    if corpus_id:
        corpus_filter = " AND e.corpus_id = :cid"
        params["cid"] = corpus_id
    try:
        rows = db.execute(text(f"""
            SELECT e.rule_id, (1 - (e.embedding <=> :vec::vector)) as score
            FROM rules_embeddings e
            WHERE e.embedding IS NOT NULL {model_filter} {corpus_filter}
            ORDER BY e.embedding <=> :vec::vector
            LIMIT :lim
        """), params).fetchall()
    except Exception as e:
        logger.warning("rules_vector_search_failed", extra={"error": str(e)})
        return []
    result = []
    for r in rows:
        sec = db.get(RulesSection, r[0])
        if sec:
            result.append((sec, float(r[1] or 0)))
    return result


def hybrid_search(
    db: Session,
    query: str,
    *,
    corpus_id: str | None = None,
    corpus_version: str | None = None,
    limit: int = 5,
    query_embedding: list[float] | None = None,
    embedding_model: str | None = None,
) -> list[dict]:
    """Bounded hybrid lexical + vector retrieval, fused via RRF, resolving to canonical records.

    Returns list of dicts: {section, citation, scores: {lexical, vector, fused}, retrieval_metadata}
    Scores are metadata only — citations come from canonical record.
    """
    t0 = time.monotonic()
    limit = max(1, min(limit, 20))
    lexical = search_lexical(db, query, corpus_id=corpus_id, limit=limit * 2)
    vector = search_vector(db, query_embedding, corpus_id=corpus_id, limit=limit * 2, embedding_model=embedding_model) if query_embedding else []

    # Optional corpus_version filter post-hoc (keeps functions corpus_id-only for simplicity)
    if corpus_version:
        lexical = [(s, sc) for s, sc in lexical if s.corpus_version == corpus_version]
        vector = [(s, sc) for s, sc in vector if s.corpus_version == corpus_version]

    # Reciprocal Rank Fusion (k=60) — deterministic
    k = 60
    fused: dict[str, dict] = {}
    lex_rank = {sec.rule_id: i + 1 for i, (sec, _) in enumerate(lexical)}
    vec_rank = {sec.rule_id: i + 1 for i, (sec, _) in enumerate(vector)}
    all_ids = set(lex_rank) | set(vec_rank)
    # If no vector, fusion = lexical rank only
    if not vector:
        for sec, lscore in lexical:
            fused[sec.rule_id] = {"section": sec, "lexical": lscore, "vector": None, "fused": 1 / (k + lex_rank[sec.rule_id])}
    else:
        for rid in all_ids:
            lex_r = lex_rank.get(rid)
            vec_r = vec_rank.get(rid)
            score = 0
            if lex_r is not None:
                score += 1 / (k + lex_r)
            if vec_r is not None:
                score += 1 / (k + vec_r)
            # find section object
            sec = next((s for s, _ in lexical if s.rule_id == rid), None)
            if sec is None:
                sec = next((s for s, _ in vector if s.rule_id == rid), None)
            if sec is None:
                continue
            lex_s = next((sc for s, sc in lexical if s.rule_id == rid), None)
            vec_s = next((sc for s, sc in vector if s.rule_id == rid), None)
            fused[rid] = {"section": sec, "lexical": lex_s, "vector": vec_s, "fused": score}

    sorted_ids = sorted(fused.keys(), key=lambda rid: fused[rid]["fused"], reverse=True)[:limit]
    results = []
    for rid in sorted_ids:
        entry = fused[rid]
        sec: RulesSection = entry["section"]
        results.append({
            "rule_id": sec.rule_id,
            "corpus_id": sec.corpus_id,
            "corpus_version": sec.corpus_version,
            "title": sec.title,
            "heading_path": sec.heading_path,
            "document": sec.document,
            "source_locator": sec.source_locator,
            "source_section_id": sec.source_section_id,
            "excerpt": sec.body[:600] if sec.body else "",
            "body": sec.body,
            "citation": sec.citation(),
            "scores": {"lexical": entry["lexical"], "vector": entry["vector"], "fused": entry["fused"]},
            "retrieval_metadata": {
                "lexical_score": entry["lexical"],
                "vector_score": entry["vector"],
                "fused_score": entry["fused"],
            },
        })
    # Fallback: if hybrid empty but lexical had hits beyond fused truncation, return lexical top
    if not results and lexical:
        for sec, sc in lexical[:limit]:
            results.append({
                "rule_id": sec.rule_id,
                "corpus_id": sec.corpus_id,
                "corpus_version": sec.corpus_version,
                "title": sec.title,
                "heading_path": sec.heading_path,
                "document": sec.document,
                "source_locator": sec.source_locator,
                "source_section_id": sec.source_section_id,
                "excerpt": sec.body[:600],
                "body": sec.body,
                "citation": sec.citation(),
                "scores": {"lexical": sc, "vector": None, "fused": None},
                "retrieval_metadata": {"lexical_score": sc},
            })
    latency = (time.monotonic() - t0) * 1000
    logger.info("rules_hybrid_search", extra={"query": query[:80], "limit": limit, "lexical": len(lexical), "vector": len(vector), "results": len(results), "latency_ms": round(latency, 2)})
    return results


def format_citation(section: RulesSection) -> str:
    c = section.citation()
    path = c.get("path") or "/".join(c.get("heading_path") or [])
    return f"{c['title']} — {c['corpus_id']} {c['corpus_version']} [{path}] ({c['source_locator']}) · {c['license']}"

