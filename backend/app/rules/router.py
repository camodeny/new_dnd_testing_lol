"""Rules corpus API — bounded structured search/lookup for citations."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from app.rules.store import lookup_by_rule_id, hybrid_search
from models.rules import RulesCorpus

router = APIRouter(prefix="/api/rules", tags=["rules"])
logger = logging.getLogger(__name__)


@router.get("/lookup")
def lookup(rule_id: str = Query(..., max_length=256), db: Session = Depends(get_db)):
    row = lookup_by_rule_id(db, rule_id)
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "rule_id": rule_id})
    return row.to_dict()


@router.get("/search")
def search(q: str = Query(..., max_length=240), limit: int = Query(5, ge=1, le=20), corpus_id: str | None = None, db: Session = Depends(get_db)):
    # Best-effort Gemini query embedding for hybrid reranking. Degradation to
    # lexical-only search is intentional but must be explicit: each failure
    # mode narrows its catch and logs a stable reason instead of swallowing.
    query_embedding = None
    embedding_model = None
    import os

    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY"):
        try:
            from models.rules import RulesEmbedding

            row = db.query(RulesEmbedding.embedding_model).filter(RulesEmbedding.embedding_model.like("gemini%")).order_by(RulesEmbedding.created_at.desc()).first()
        except ImportError as exc:
            logger.warning("rules search embedding unavailable reason=import_error error=%s", exc)
        except Exception as exc:
            logger.warning("rules search embedding lookup failed reason=%s error=%s", type(exc).__name__, exc)
        else:
            if row:
                embedding_model = row[0]
                try:
                    from app.rules.gemini import gemini_embed_query
                except ImportError as exc:
                    logger.warning("rules search embedding gemini module unavailable error=%s", exc)
                else:
                    try:
                        query_embedding = gemini_embed_query(q, model=embedding_model)
                    except Exception as exc:
                        query_embedding = None
                        logger.warning("rules search embedding degraded reason=%s error=%s", type(exc).__name__, exc)
    hits = hybrid_search(db, q, corpus_id=corpus_id, limit=limit, query_embedding=query_embedding, embedding_model=embedding_model)
    return {"query": q, "limit": limit, "hits": hits, "count": len(hits)}


@router.get("/corpora")
def list_corpora(db: Session = Depends(get_db)):
    rows = db.query(RulesCorpus).all()
    return {"corpora": [r.to_dict() for r in rows]}
