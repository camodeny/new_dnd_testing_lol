"""Issue #213 — pgvector semantic index over authoritative source records.

Covers: indexing/retrieval round-trip, source-update staleness, superseded
sources, campaign isolation, hidden-source filtering, authorized-only
reranking, no-relevant-result/defer, reranker-failure fallback, and
direct-retrieval fallback when vector work is unavailable.

The suite runs on SQLite (portable JSON path). pgvector-live assertions are
postgres-marked and degrade-skip cleanly without a disposable DB URL.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.dm.context import ContextAudience  # noqa: E402
from app.dm.evidence import execute_evidence_round, validate_evidence_requests  # noqa: E402
from app.queue.adapter import new_envelope  # noqa: E402
from app.queue.envelope import WorkerEnvelope  # noqa: E402
from app.world import semantic  # noqa: E402
from app.world.knowledge import (  # noqa: E402
    create_fact_authoritative,
    create_relation_authoritative,
    supersede_fact_authoritative,
)
from app.world.retrieval import (  # noqa: E402
    STATUS_DEFER,
    STATUS_OK,
    apply_rerank,
    lookup_fact,
)
from app.world.semantic import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_VERSION,
    SEMANTIC_INDEX_JOB_TYPE,
    STATUS_DEFER as SEM_DEFER,
    STATUS_NO_MATCH as SEM_NO_MATCH,
    STATUS_OK as SEM_OK,
    build_source_text,
    get_semantic_stats,
    handle_world_semantic_index,
    index_source_record,
    mark_stale,
    semantic_search,
)
from app.world.service import create_entity_authoritative  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEmbedding  # noqa: E402


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _setup():
    eng = _engine()
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    player = uuid.uuid4()
    outsider = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@example.com"))
    db.add(Profile(id=player, email="player@example.com"))
    db.add(Profile(id=outsider, email="outsider@example.com"))
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Semantic campaign", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.add(CampaignMember(campaign_id=camp.id, user_id=player, role="player"))
    db.commit()
    db.refresh(camp)
    return Fac, camp.id, owner, player, outsider


def _seed_fact(db, cid, rev=0, content="Asha hides the key in Cinder Keep.",
               visibility="campaign", operation_id="op-sem-fact"):
    fact, _ = create_fact_authoritative(
        db, cid, rev, content=content, epistemic_state="confirmed",
        visibility=visibility, operation_id=operation_id)
    return fact


# ── index / retrieve round-trip ──────────────────────────────────────────────

def test_index_and_retrieve_resolves_to_authoritative_evidence():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    row = index_source_record(db, cid, "world_fact", fact.id)
    assert row is not None and row.status == "active"
    assert row.source_version  # exact source record/version referenced
    assert row.embedding_model == DEFAULT_MODEL

    query = build_source_text(db, "world_fact", fact)
    outcome = semantic_search(db, cid, query, owner, dm_internal=True)
    assert outcome.status == SEM_OK
    assert outcome.visible == 1
    packet = outcome.packets[0]
    assert packet.source_type == "world_fact"
    assert packet.source_id == str(fact.id)
    assert packet.source_version == row.source_version
    assert packet.content["content"] == "Asha hides the key in Cinder Keep."
    # Vector similarity is derived ranking metadata, never canonical evidence.
    assert packet.retrieval_score == pytest.approx(outcome.top_similarity)
    assert packet.provenance["vector_similarity"] == pytest.approx(1.0)
    assert packet.provenance["semantic_model"] == DEFAULT_MODEL
    assert "retrieved_by" in packet.provenance


def test_embedding_model_version_change_does_not_corrupt_old_rows():
    Fac, cid, _owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    old = index_source_record(db, cid, "world_fact", fact.id,
                              embedding_model="stub-hash-v1", embedding_version="1")
    new = index_source_record(db, cid, "world_fact", fact.id,
                              embedding_model="stub-hash-v1", embedding_version="2")
    assert old.id != new.id
    assert db.get(WorldEmbedding, old.id).status == "active"
    # Search pins model/version: v1 rows serve v1 queries, v2 rows serve v2.
    query = build_source_text(db, "world_fact", fact)
    hit_v1 = semantic_search(db, cid, query, None, dm_internal=True, embedding_version="1")
    assert hit_v1.status == SEM_OK
    assert hit_v1.embedding_version == "1"


def test_writer_hook_enqueues_async_index_without_breaking_canon():
    from app.queue.adapter import get_queue_adapter

    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    adapter = get_queue_adapter()
    depth_before = adapter.depth() if hasattr(adapter, "depth") else 0
    fact = _seed_fact(db, cid, operation_id="op-sem-hook")
    # Canon write succeeded; derived placeholder + envelope are best-effort.
    assert fact.id is not None
    placeholder = db.execute(
        __import__("sqlalchemy").select(WorldEmbedding).where(
            WorldEmbedding.source_id == fact.id)
    ).scalars().first()
    assert placeholder is not None and placeholder.status == "stale"
    if hasattr(adapter, "depth"):
        assert adapter.depth() >= depth_before


def test_worker_handler_is_idempotent():
    Fac, cid, _owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    from app.worker.executor import execute_worker_job

    envelope = new_envelope(
        job_type=SEMANTIC_INDEX_JOB_TYPE, campaign_id=cid, aggregate_id=cid,
        operation_id="op-sem-worker", idempotency_key=f"semidx-test:{fact.id}",
        payload={"campaign_id": str(cid), "source_type": "world_fact",
                 "source_id": str(fact.id), "embedding_model": DEFAULT_MODEL,
                 "embedding_version": DEFAULT_VERSION},
    )
    result, duplicate = execute_worker_job(
        db, envelope, lambda env: handle_world_semantic_index(env, db))
    assert duplicate is False
    assert result["status"] == "active"
    result2, duplicate2 = execute_worker_job(
        db, envelope, lambda env: handle_world_semantic_index(env, db))
    assert duplicate2 is True
    assert result2["status"] == "active"
    rows = db.execute(
        __import__("sqlalchemy").select(WorldEmbedding).where(
            WorldEmbedding.source_id == fact.id,
            WorldEmbedding.embedding_model == DEFAULT_MODEL)
    ).scalars().all()
    assert len(rows) == 1


# ── staleness / supersession ─────────────────────────────────────────────────

def test_source_update_staleness_marks_and_rebuilds():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    index_source_record(db, cid, "world_fact", fact.id)
    assert mark_stale(db, cid, "world_fact", fact.id, reason="test") == 1
    query = build_source_text(db, "world_fact", fact)
    stale_outcome = semantic_search(db, cid, query, owner, dm_internal=True)
    # Stale rows never serve: nothing active remains → defer to direct reads.
    assert stale_outcome.status == SEM_DEFER
    assert stale_outcome.fallback_to_direct is True
    rebuilt = index_source_record(db, cid, "world_fact", fact.id)
    assert rebuilt.status == "active"
    fresh = semantic_search(db, cid, query, owner, dm_internal=True)
    assert fresh.status == SEM_OK and fresh.visible == 1


def test_superseded_source_is_retired_and_replaced():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, content="The bridge stands.")
    index_source_record(db, cid, "world_fact", fact.id)
    old_query = build_source_text(db, "world_fact", fact)

    fixed, _ = supersede_fact_authoritative(
        db, cid, 1, fact.id, content="The bridge has fallen.",
        epistemic_state="confirmed", visibility="campaign",
        operation_id="op-sem-supersede")
    # Writer hook retired the prior source's vectors.
    prior_rows = db.execute(
        __import__("sqlalchemy").select(WorldEmbedding).where(
            WorldEmbedding.source_id == fact.id)
    ).scalars().all()
    assert prior_rows and all(r.status == "superseded" for r in prior_rows)

    # The old vector cannot resolve to live truth even before reindexing.
    assert semantic_search(db, cid, old_query, owner, dm_internal=True).status in {
        SEM_NO_MATCH, SEM_DEFER}

    index_source_record(db, cid, "world_fact", fixed.id)
    new_query = build_source_text(db, "world_fact", fixed)
    outcome = semantic_search(db, cid, new_query, owner, dm_internal=True)
    assert outcome.status == SEM_OK
    assert outcome.packets[0].source_id == str(fixed.id)
    assert outcome.packets[0].content["content"] == "The bridge has fallen."


def test_version_mismatch_detected_at_search_time():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    entity, _ = create_entity_authoritative(
        db, cid, 0, entity_type="npc", name="Mara",
        visibility="campaign", operation_id="op-sem-ent")
    row = index_source_record(db, cid, "world_entity", entity.id)
    assert row.status == "active"
    # Simulate an out-of-band source change: stored version no longer matches.
    row.source_version = "tampered-version"
    db.add(row)
    db.commit()
    query = build_source_text(db, "world_entity", entity)
    outcome = semantic_search(db, cid, query, owner, dm_internal=True)
    assert outcome.status in {SEM_NO_MATCH, SEM_DEFER}
    assert outcome.stale_dropped >= 1 or outcome.total_candidates == 0
    db.refresh(row)
    assert row.status in {"stale", "superseded"}


# ── campaign isolation + visibility ──────────────────────────────────────────

def test_search_is_campaign_scoped():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    other_owner = uuid.uuid4()
    db.add(Profile(id=other_owner, email="other@example.com"))
    other = Campaign(id=uuid.uuid4(), owner_id=other_owner, name="Other", revision=0)
    db.add(other)
    db.flush()
    db.add(CampaignMember(campaign_id=other.id, user_id=other_owner, role="owner"))
    db.commit()

    fact = _seed_fact(db, cid, content="Campaign A battle plan.")
    index_source_record(db, cid, "world_fact", fact.id)
    other_fact, _ = create_fact_authoritative(
        db, other.id, 0, content="Campaign B secret ritual.",
        epistemic_state="confirmed", visibility="campaign",
        operation_id="op-sem-other")
    index_source_record(db, other.id, "world_fact", other_fact.id)

    query = build_source_text(db, "world_fact", other_fact)
    outcome = semantic_search(db, cid, query, owner, dm_internal=True,
                              min_similarity=0.0)
    ids = {p.source_id for p in outcome.packets}
    # Private content never leaks into another campaign's namespace.
    assert str(other_fact.id) not in ids


def test_hidden_source_filtered_before_player_paths():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    secret = _seed_fact(db, cid, content="Asha hides the key.",
                        visibility="dm_only", operation_id="op-sem-hidden")
    index_source_record(db, cid, "world_fact", secret.id)
    query = build_source_text(db, "world_fact", secret)

    player_outcome = semantic_search(db, cid, query, player, dm_internal=False)
    assert all(p.source_id != str(secret.id) for p in player_outcome.packets)
    assert player_outcome.denied >= 1

    dm_outcome = semantic_search(db, cid, query, owner, dm_internal=True)
    assert dm_outcome.status == SEM_OK
    assert dm_outcome.packets[0].source_id == str(secret.id)
    # DM-internal retrieval preserves visibility metadata for later projection.
    assert dm_outcome.packets[0].visibility == "dm_only"
    assert dm_outcome.packets[0].revealable is None


# ── no-match / defer / rerank / fallback ─────────────────────────────────────

def test_no_relevant_result_instead_of_forced_hit():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    index_source_record(db, cid, "world_fact", fact.id)
    outcome = semantic_search(
        db, cid, "completely unrelated query about naval tax codes",
        owner, dm_internal=True)
    assert outcome.status == SEM_NO_MATCH
    assert outcome.packets == []
    assert outcome.fallback_to_direct is False


def test_empty_index_defers_to_direct_retrieval():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    outcome = semantic_search(db, cid, "anything at all", owner, dm_internal=True)
    assert outcome.status == SEM_DEFER
    assert outcome.fallback_to_direct is True
    # Canonical data stays reachable through direct retrieval regardless.
    fact = _seed_fact(db, cid)
    direct = lookup_fact(db, cid, fact.id, owner, dm_internal=True)
    assert direct.status == STATUS_OK and len(direct.packets) == 1


def test_rerank_only_reorders_authorized_candidates():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    first = _seed_fact(db, cid, content="First chronicle entry.",
                       operation_id="op-sem-r1")
    second = _seed_fact(db, cid, rev=1, content="Second chronicle entry.",
                        operation_id="op-sem-r2")
    for fact in (first, second):
        index_source_record(db, cid, "world_fact", fact.id)
    outcome = semantic_search(db, cid, build_source_text(db, "world_fact", first),
                              owner, dm_internal=True, min_similarity=-1.0,
                              limit=10)
    assert len(outcome.packets) == 2
    by_id = {f"{p.source_type}:{p.source_id}": p for p in outcome.packets}

    # Invented candidate IDs are rejected; deterministic order is kept.
    rejected = apply_rerank(
        list(outcome.packets), order=["world_fact:00000000-0000-0000-0000-000000000000"])
    assert rejected.fallback is True
    assert rejected.reranked is False
    assert [p.source_id for p in rejected.packets] == [p.source_id for p in outcome.packets]
    # Provenance untouched by the rejected rerank.
    assert rejected.packets[0].provenance["retrieved_by"] == "world_retrieval_212"

    # A valid subset order reorders presentation only.
    target = f"world_fact:{second.id}"
    reordered = apply_rerank(list(outcome.packets), order=[target])
    assert reordered.reranked is True
    assert reordered.packets[0].source_id == str(second.id)
    assert reordered.packets[0].retrieval_score == by_id[target].retrieval_score

    # Reranker failure falls back to similarity order, never loses evidence.
    def _boom(_candidates):
        raise RuntimeError("reranker down")

    fell_back = apply_rerank(list(outcome.packets), reranker=_boom)
    assert fell_back.fallback is True
    assert len(fell_back.packets) == 2


def test_embedding_failure_falls_back_to_direct_retrieval(monkeypatch):
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    index_source_record(db, cid, "world_fact", fact.id)

    def _fail(_text, _model, _provider):
        raise RuntimeError("embedder offline")

    monkeypatch.setattr(semantic, "_embed_query", _fail)
    outcome = semantic_search(db, cid, "anything", owner, dm_internal=True)
    assert outcome.status == SEM_DEFER
    assert outcome.fallback_to_direct is True
    direct = lookup_fact(db, cid, fact.id, owner, dm_internal=True)
    assert direct.status == STATUS_OK


def test_semantic_stats_observability():
    Fac, cid, _owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    index_source_record(db, cid, "world_fact", fact.id)
    stats = get_semantic_stats(db, cid)
    assert stats["active"] == 1
    assert stats["total"] == 1
    mark_stale(db, cid, "world_fact", fact.id)
    stats = get_semantic_stats(db, cid)
    assert stats["active"] == 0 and stats["stale"] == 1


def test_all_supported_source_types_index_and_resolve():
    from models.campaigns import CampaignDomainEvent
    from models.dm import DmTurn
    from sqlalchemy import select as _select

    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    entity, _ = create_entity_authoritative(
        db, cid, 0, entity_type="npc", name="Asha",
        summary="Asha guards Cinder Keep.", visibility="campaign",
        operation_id="op-sem-all-ent")
    rel, _ = create_relation_authoritative(
        db, cid, 1, subject_entity_id=entity.id, relation_type="guards",
        object_label="Cinder Keep", epistemic_state="confirmed",
        visibility="campaign", operation_id="op-sem-all-rel")
    fact = _seed_fact(db, cid, rev=2, operation_id="op-sem-all-fact")
    event = db.execute(
        _select(CampaignDomainEvent).where(
            CampaignDomainEvent.campaign_id == cid).order_by(
                CampaignDomainEvent.sequence.asc()).limit(1)).scalars().first()
    turn = DmTurn(id=uuid.uuid4(), campaign_id=cid, thread_id="main",
                  audience="campaign", status="committed", source_revision=1,
                  input_set_revision=1, submission_ids=[])
    db.add(turn)
    db.commit()

    targets = [("world_entity", entity.id), ("world_relation", rel.id),
               ("world_fact", fact.id), ("domain_event", event.id),
               ("source_turn", turn.id), ("scene", cid)]
    for source_type, source_id in targets:
        if source_type == "scene":
            from app.world.service import set_scene_authoritative
            set_scene_authoritative(
                db, cid, 3, location_name="Cinder Keep",
                operation_id="op-sem-all-scene")
        row = index_source_record(db, cid, source_type, source_id)
        assert row is not None and row.status == "active", source_type

    for source_type, source_id in targets:
        record = semantic._current_record(db, cid, source_type, source_id)
        assert record is not None, source_type
        query = build_source_text(db, source_type, record)
        assert query, source_type
        outcome = semantic_search(db, cid, query, owner, dm_internal=True)
        assert outcome.status == SEM_OK, source_type
        assert outcome.packets[0].source_id == str(source_id), source_type

    stats = get_semantic_stats(db, cid)
    assert stats["active"] == len(targets)


# ── #203 mediation integration ───────────────────────────────────────────────
def test_search_campaign_memory_through_evidence_mediation():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    index_source_record(db, cid, "world_fact", fact.id)
    query = build_source_text(db, "world_fact", fact)

    audience = ContextAudience(campaign_id=str(cid), thread_id="main",
                               audience="campaign", user_ids=[str(owner)])
    requests = validate_evidence_requests([
        {"id": "sem1", "tool": "search_campaign_memory", "query": query, "limit": 5},
    ])
    results, trace = execute_evidence_round(requests, audience, db=db, timeout_s=None)
    assert results[0].status == "ok"
    assert results[0].result_count >= 1
    payload = results[0].payload
    assert payload["packets"][0]["source_id"] == str(fact.id)
    assert payload["embedding_model"] == DEFAULT_MODEL
    assert trace.tool_types == ["search_campaign_memory"]


def test_mediation_hides_unauthorized_semantic_hits():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    secret = _seed_fact(db, cid, content="Asha hides the key.",
                        visibility="dm_only", operation_id="op-sem-med")
    index_source_record(db, cid, "world_fact", secret.id)
    query = build_source_text(db, "world_fact", secret)

    audience = ContextAudience(campaign_id=str(cid), thread_id="main",
                               audience="private", user_ids=[str(player)])
    requests = validate_evidence_requests([
        {"id": "sem9", "tool": "search_campaign_memory", "query": query},
    ])
    results, _trace = execute_evidence_round(requests, audience, db=db, timeout_s=None)
    assert results[0].status == "unknown"
    assert results[0].result_count == 0
    assert "Asha hides" not in str(results[0].model_dump(mode="json"))


# ── migration shape (static; no DB required) ─────────────────────────────────

def test_migration_defines_pgvector_branch_and_fallback():
    path = Path(__file__).parent.parent / "alembic" / "versions" / \
        "e2130a17c213_world_semantic_index_213.py"
    text = path.read_text()
    assert 'down_revision' in text and 'f231turnprog01' in text
    assert 'vector(1536)' in text
    assert 'hnsw' in text.lower()
    assert 'embedding_text' in text
    assert 'world_embeddings' in text


@pytest.mark.postgres
def test_live_pgvector_column_when_postgres_available():
    url = (os.getenv("POSTGRES_URL_NON_POOLING") or os.getenv("POSTGRES_URL")
           or os.getenv("DATABASE_URL") or "")
    if not (url and "ci_test" in url):
        pytest.skip(
            "No disposable Postgres DB URL — skipping live pgvector check "
            "(set DATABASE_URL with ci_test for CI)")
    from sqlalchemy import create_engine as _create, text as _text
    from sqlalchemy.pool import NullPool

    engine = _create(url, poolclass=NullPool)
    with engine.connect() as conn:
        ext = conn.execute(_text("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()
        if ext is None:
            pytest.skip("pgvector extension missing on disposable DB")
        col = conn.execute(_text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='world_embeddings' "
            "AND column_name='embedding'")).fetchone()
        assert col is not None, "world_embeddings.embedding column missing"
        assert col[0] == "vector", f"expected vector column, got {col[0]!r}"
        hnsw = conn.execute(_text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='world_embeddings' "
            "AND indexdef ILIKE '%hnsw%'")).fetchall()
        assert hnsw, "expected HNSW index on world_embeddings.embedding"
