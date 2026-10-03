"""Production BM25/cache tests against isolated SQLite source storage."""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session
from app.rules_corpus import bm25_store as store
from app.rules_corpus.bm25 import Bm25Index
from app.rules_corpus.selection_prototype import RuleIndex
from models.rules import RulesSection

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"


def row(rid, title, body, corpus="dnd-srd"):
    return RulesSection(
        rule_id=rid,
        corpus_id=corpus,
        corpus_version="5.2.1",
        source_section_id=rid,
        source_locator=rid,
        document="playing-the-game",
        heading_path=["Combat", title],
        title=title,
        body=body,
    )


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rules.db'}")
    RulesSection.__table__.create(engine)
    with Session(engine) as session:
        session.add_all(
            [
                row("grapple", "Grapple", "grapple movement speed grapple"),
                row("spell", "Spells", "spell slots cantrip"),
                row("private", "Grapple", "grapple grapple", "private-corpus"),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def test_shared_algorithm_matches_benchmarked_rankings():
    rows = [
        dict(
            rule_id=str(i),
            title=f"Spell {i}",
            body=f"fireball spell damage {'grapple ' * i}",
            heading_path=["Magic"],
            document="spells",
            corpus_version="5.2.1",
        )
        for i in range(10)
    ]
    index = Bm25Index(rows)
    prototype = RuleIndex(rows)
    for query in ("cast fireball", "grapple damage", "ordinary conversation"):
        assert index.rank_ids(query) == [r["rule_id"] for r in prototype.lexical(query)]
    assert index.rank_ids("fireball", limit=0) == []
    assert Bm25Index([]).rank_ids("fireball") == []


def test_cache_refresh_and_engine_isolation(db, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    loads = []
    original = store._load_index

    def load(engine):
        loads.append(engine)
        return original(engine)

    monkeypatch.setattr(store, "_load_index", load)
    assert store.search_bm25_rules(db, "grapple")[0]["rule_id"] == "grapple"
    store.search_bm25_rules(db, "grapple")
    assert len(loads) == 1
    clock[0] = store.INDEX_TTL_SECONDS
    store.search_bm25_rules(db, "grapple")
    assert len(loads) == 2
    other = create_engine("sqlite://")
    RulesSection.__table__.create(other)
    with Session(other) as empty:
        assert store.search_bm25_rules(empty, "grapple") == []
    other.dispose()
    assert len(loads) == 3


def test_empty_index_is_not_pinned_before_corpus_import(monkeypatch):
    engine = create_engine("sqlite://")
    RulesSection.__table__.create(engine)
    loads = []
    original = store._load_index

    def load(bind):
        loads.append(bind)
        return original(bind)

    monkeypatch.setattr(store, "_load_index", load)
    with Session(engine) as session:
        assert store.search_bm25_rules(session, "grapple") == []
        session.add(row("grapple", "Grapple", "grapple a creature"))
        session.commit()
        assert store.search_bm25_rules(session, "grapple")[0]["rule_id"] == "grapple"
    engine.dispose()
    assert len(loads) == 2


def test_cached_ids_use_fresh_canonical_bodies_and_drop_deleted(db):
    ids = [r["rule_id"] for r in store.search_bm25_rules(db, "grapple")]
    assert ids == ["grapple"]
    existing = db.get(RulesSection, "grapple")
    with Session(db.get_bind()) as writer:
        writer.get(RulesSection, "grapple").body = "Updated canonical rule"
        writer.commit()
    assert (
        store.lookup_rules_by_ids(db, ids)["grapple"].body == "Updated canonical rule"
    )
    with Session(db.get_bind()) as writer:
        writer.delete(writer.get(RulesSection, "grapple"))
        writer.commit()
    assert store.lookup_rules_by_ids(db, ids) == {}
    assert existing is not None


def test_index_does_not_cache_uncommitted_caller_writes(db):
    db.add(row("uncommitted", "Novelword", "novelword"))
    db.flush()
    assert store.search_bm25_rules(db, "novelword") == []
    db.rollback()
    assert store.search_bm25_rules(db, "novelword") == []


def test_expired_index_failure_propagates(db, monkeypatch):
    store.search_bm25_rules(db, "grapple")
    monkeypatch.setattr(store, "INDEX_TTL_SECONDS", 0)

    def fail(engine):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(store, "_load_index", fail)
    with pytest.raises(RuntimeError, match="source unavailable"):
        store.search_bm25_rules(db, "grapple")


def test_canonical_lookup_is_one_bounded_public_read(db):
    db.add_all([row(f"r{i}", "Rule", "body") for i in range(12)])
    db.commit()
    reads = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", observe)
    ids = ["private", "grapple", "grapple"] + [f"r{i}" for i in range(12)]
    result = store.lookup_rules_by_ids(db, ids)
    assert "private" not in result
    assert len(result) == 7
    assert len(reads) == 1
