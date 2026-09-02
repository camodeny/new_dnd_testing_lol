"""Controlled rules evidence tools through #203 — issue #223.

Tool output is bounded and structured: stable rule_id, corpus/version, title/path,
canonical excerpt/body, source locator, citation metadata, retrieval metadata.
Unsupported/unavailable return explicit not-found/insufficient-evidence.
"""

from __future__ import annotations

import time
from typing import Any

from app.dm.context import AuthorizationScope, ContextAudience, SourceRef
from app.dm.contract import EvidenceRequest
from app.dm.evidence import EvidenceResult, EvidenceValidationError
from app.rules.store import hybrid_search, lookup_by_rule_id
from app.rules.metadata import ATTRIBUTION, LICENSE, OFFICIAL_SRD_URL


def _auth(audience: ContextAudience) -> AuthorizationScope:
    return AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id])


def _source_for(rule_id: str, corpus_version: str) -> SourceRef:
    return SourceRef(
        source_type="dnd_srd_rule",
        source_id=rule_id,
        source_version=corpus_version,
        provenance={"corpus": "dnd-srd", "license": LICENSE},
    )


def handle_lookup_rule(request: EvidenceRequest, audience: ContextAudience, *, db: Any = None) -> EvidenceResult:
    """Lookup by stable rule_id. Expects request.query or request.rule_id? Use query field for rule_id, or 'question'.

    Contract: tool=lookup_rule, query=<rule_id> OR question=<rule_id>.
    Also accepts explicit rule_id via question field for compat.
    """
    t0 = time.monotonic()
    # Extract rule_id from query/question
    rule_id = (request.query or request.question or "").strip()
    if not rule_id:
        # also check raw dict passthrough — fallback
        rule_id = ""
    if not rule_id:
        raise EvidenceValidationError("lookup_rule requires query (rule_id)", details={"request_id": request.id})
    # Allow rule_id with or without corpus prefix
    db_sess = db
    if db_sess is None:
        from database import SessionLocal
        if SessionLocal is None:
            return EvidenceResult(
                request_id=request.id,
                tool=request.tool,
                status="tool_failure",
                sources=[],
                visibility="public",
                authorization=_auth(audience),
                payload=None,
                error="database unavailable",
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        db_sess = SessionLocal()
        close = True
    else:
        close = False
    try:
        row = lookup_by_rule_id(db_sess, rule_id)
        latency = (time.monotonic() - t0) * 1000
        if row is None:
            return EvidenceResult(
                request_id=request.id,
                tool=request.tool,
                status="missing",
                sources=[],
                visibility="public",
                authorization=_auth(audience),
                payload={"rule_id": rule_id, "error": "not_found", "citation": None},
                error="rule not found",
                result_count=0,
                latency_ms=latency,
            )
        citation = row.citation()
        return EvidenceResult(
            request_id=request.id,
            tool=request.tool,
            status="ok",
            sources=[_source_for(row.rule_id, row.corpus_version)],
            visibility="public",
            authorization=_auth(audience),
            payload={
                "rule_id": row.rule_id,
                "corpus_id": row.corpus_id,
                "corpus_version": row.corpus_version,
                "title": row.title,
                "heading_path": row.heading_path,
                "document": row.document,
                "source_locator": row.source_locator,
                "source_section_id": row.source_section_id,
                "excerpt": row.body[:800] if row.body else "",
                "body": row.body[:2000] if row.body else "",
                "citation": citation,
                "citation_text": f"{citation['title']} — {citation['corpus_id']} {citation['corpus_version']} [{citation['source_locator']}]",
                "license": citation.get("license"),
                "attribution": citation.get("attribution"),
            },
            result_count=1,
            latency_ms=latency,
        )
    finally:
        if close:
            try:
                db_sess.close()
            except Exception:
                pass


def handle_search_rules(request: EvidenceRequest, audience: ContextAudience, *, db: Any = None) -> EvidenceResult:
    """Bounded hybrid lexical+vector search for natural-language rules questions.

    Expects request.query (required) and optional limit.
    If Gemini embeddings are configured (GEMINI_API_KEY) and a Gemini model has
    been built, the query is also embedded via gemini-embedding-2 (fallback 001) for semantic
    reranking; otherwise lexical-only still succeeds.
    """
    t0 = time.monotonic()
    query = (request.query or request.question or "").strip()
    if not query:
        raise EvidenceValidationError("search_rules requires query", details={"request_id": request.id})
    limit = request.limit or 5
    limit = max(1, min(limit, 20))

    db_sess = db
    if db_sess is None:
        from database import SessionLocal
        if SessionLocal is None:
            return EvidenceResult(
                request_id=request.id,
                tool=request.tool,
                status="tool_failure",
                sources=[],
                visibility="public",
                authorization=_auth(audience),
                payload=None,
                error="database unavailable",
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        db_sess = SessionLocal()
        close = True
    else:
        close = False
    # Try to embed query via Gemini if configured — best-effort, lexical still works on failure
    query_embedding = None
    embedding_model = None
    try:
        import os

        has_gemini_key = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY"))
        if has_gemini_key:
            # Detect most recent Gemini-indexed model in DB to keep query/doc dims aligned
            try:
                from models import RulesEmbedding

                row = db_sess.query(RulesEmbedding.embedding_model).filter(RulesEmbedding.embedding_model.like("gemini%")).order_by(RulesEmbedding.created_at.desc()).first()
                if row:
                    embedding_model = row[0]
                else:
                    # also check text-embedding-004
                    row = db_sess.query(RulesEmbedding.embedding_model).filter(RulesEmbedding.embedding_model.like("text-embedding%")).first()
                    if row:
                        embedding_model = row[0]
            except Exception:
                pass
            if embedding_model or has_gemini_key:
                from app.rules.gemini import gemini_embed_query

                # Use detected model or default gemini-embedding-2
                try:
                    query_embedding = gemini_embed_query(query, model=embedding_model)
                except Exception:
                    # semantic is optional — fall back to lexical only
                    query_embedding = None
    except Exception:
        query_embedding = None

    try:
        hits = hybrid_search(db_sess, query, limit=limit, query_embedding=query_embedding, embedding_model=embedding_model)
        latency = (time.monotonic() - t0) * 1000
        if not hits:
            return EvidenceResult(
                request_id=request.id,
                tool=request.tool,
                status="missing",
                sources=[],
                visibility="public",
                authorization=_auth(audience),
                payload={"query": query, "hits": [], "error": "no_results"},
                error="no rule evidence found — insufficient evidence",
                result_count=0,
                latency_ms=latency,
            )
        # Build bounded payload — each hit has stable ID + citation
        bounded_hits = []
        sources: list[SourceRef] = []
        for h in hits[:limit]:
            sources.append(_source_for(h["rule_id"], h["corpus_version"]))
            bounded_hits.append({
                "rule_id": h["rule_id"],
                "corpus_id": h["corpus_id"],
                "corpus_version": h["corpus_version"],
                "title": h["title"],
                "heading_path": h["heading_path"],
                "document": h["document"],
                "source_locator": h["source_locator"],
                "excerpt": h["excerpt"],
                "citation": h["citation"],
                "scores": h.get("scores"),
            })
        return EvidenceResult(
            request_id=request.id,
            tool=request.tool,
            status="ok",
            sources=sources,
            visibility="public",
            authorization=_auth(audience),
            payload={"query": query, "hits": bounded_hits, "result_count": len(bounded_hits)},
            result_count=len(bounded_hits),
            latency_ms=latency,
        )
    finally:
        if close:
            try:
                db_sess.close()
            except Exception:
                pass


TOOL_HANDLERS = {
    "lookup_rule": handle_lookup_rule,
    "search_rules": handle_search_rules,
}
