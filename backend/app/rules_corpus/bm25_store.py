"""Cached public SRD postings; candidate IDs are hints, never source evidence."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.rules_corpus.bm25 import Bm25Index
from app.rules_corpus.metadata import CORPUS_ID, CORPUS_VERSION
from models.rules import RulesSection

# Sections are versioned and canonical bodies are re-read per turn, so a stale
# index only affects candidate order. Refresh rarely to keep reloads off turns.
INDEX_TTL_SECONDS = 3600


@dataclass
class _CachedIndex:
    index: Bm25Index | None = None
    refreshed_at: float = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_cache = WeakKeyDictionary()
_cache_lock = threading.Lock()


def _load_index(bind) -> Bm25Index:
    # An independent read transaction prevents uncommitted caller writes from
    # leaking into the process cache. Only official public corpus rows enter it.
    with Session(bind=bind) as reader:
        if bind.dialect.name == "postgresql":
            reader.execute(text("SET TRANSACTION READ ONLY"))
            reader.execute(text("SET LOCAL statement_timeout='3s'"))
        rows = (
            reader.execute(
                select(
                    RulesSection.rule_id,
                    RulesSection.title,
                    RulesSection.heading_path,
                    RulesSection.body,
                ).where(
                    RulesSection.corpus_id == CORPUS_ID,
                    RulesSection.corpus_version == CORPUS_VERSION,
                )
            )
            .mappings()
            .all()
        )
        return Bm25Index([dict(row) for row in rows])


def search_bm25_rules(db: Session, query: str, *, limit: int = 8) -> list[dict]:
    """Warm ranking with an hourly postings cache, scoped to the DB engine.

    No player text, private data, live sessions, or rule bodies are cached.
    Fresh canonical reads must hydrate the returned IDs before Jev or context.
    Refresh failure propagates so the caller can mark retrieval unavailable.
    """
    if db is None or not query.strip():
        return []
    bind = db.get_bind()
    # ORM sessions generally bind an Engine; connection-bound transactions use
    # their engine for independent reads, not the caller's live Connection.
    engine = getattr(bind, "engine", bind)
    with _cache_lock:
        cached = _cache.get(engine)
        if cached is None:
            cached = _CachedIndex()
            _cache[engine] = cached
    # One refresh per engine; unrelated engines never wait on its DB read.
    with cached.lock:
        if (
            cached.index is None
            or time.monotonic() - cached.refreshed_at >= INDEX_TTL_SECONDS
        ):
            index = _load_index(engine)
            # Do not pin an empty index before the corpus has been imported.
            if index.rule_ids:
                cached.index = index
                cached.refreshed_at = time.monotonic()
        else:
            index = cached.index
    return [{"rule_id": rid} for rid in index.rank_ids(query, limit)]


def lookup_rules_by_ids(db: Session, rule_ids: list[str]) -> dict:
    """One fresh canonical read, bounded to eight IDs from the public corpus."""
    ids = list(dict.fromkeys(rule_ids))[:8]
    if db is None or not ids:
        return {}
    with db.no_autoflush:
        rows = db.scalars(
            select(RulesSection)
            .where(
                RulesSection.rule_id.in_(ids),
                RulesSection.corpus_id == CORPUS_ID,
                RulesSection.corpus_version == CORPUS_VERSION,
            )
            .execution_options(populate_existing=True)
        ).all()
    return {row.rule_id: row for row in rows}
