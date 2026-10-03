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
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from tests.support.world_writes import commit_world_write  # noqa: E402
from app.dm.context import ContextAudience  # noqa: E402
from app.dm.evidence import execute_evidence_round, validate_evidence_requests  # noqa: E402
from app.world import semantic, semantic_index  # noqa: E402
from app.world.facts import (  # noqa: E402
    create_fact,
    create_relation,
    list_facts,
    list_relations,
    supersede_fact,
)
from app.world.retrieval import (  # noqa: E402
    STATUS_DEFER,
    STATUS_OK,
    lookup_fact,
)
from app.world.semantic import (  # noqa: E402
    STATUS_DEFER as SEM_DEFER,
    STATUS_NO_MATCH as SEM_NO_MATCH,
    STATUS_OK as SEM_OK,
    semantic_search,
)
from app.world.semantic_index import (  # noqa: E402
    DEFAULT_MODEL,
    build_source_text,
    index_source_record,
    mark_stale,
    note_authoritative_write,
    note_supersession,
    request_semantic_index,
    resolve_embedding_model,
    run_semantic_index_sweep,
)
from app.world.service import create_entity  # noqa: E402
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
    fact, event = commit_world_write(
        db, cid, rev, create_fact, content=content, epistemic_state="confirmed",
        visibility=visibility, operation_id=operation_id)
    note_authoritative_write(db, cid, [("world_fact", fact.id), ("domain_event", event.id)])
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


def test_write_hook_stages_async_index_without_breaking_canon():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, operation_id="op-sem-hook")
    # Canon write succeeded; the derived placeholder is best-effort.
    assert fact.id is not None
    placeholder = db.execute(
        select(WorldEmbedding).where(WorldEmbedding.source_id == fact.id)
    ).scalars().first()
    assert placeholder is not None and placeholder.status == "stale"
    assert placeholder.error == "awaiting_async_index"


def test_sweep_indexes_pending_rows_and_is_idempotent():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, operation_id="op-sem-sweep")
    pending = db.execute(
        select(WorldEmbedding).where(WorldEmbedding.campaign_id == cid)
    ).scalars().all()
    assert pending and all(r.status == "stale" for r in pending)

    result = run_semantic_index_sweep(db)
    assert result["failed"] == []
    assert set(result["indexed"]) == {str(r.id) for r in pending}
    row = db.execute(
        select(WorldEmbedding).where(
            WorldEmbedding.source_id == fact.id,
            WorldEmbedding.embedding_model == DEFAULT_MODEL)
    ).scalars().one()
    assert row.status == "active" and row.error is None
    assert row.source_version != "pending"
    query = build_source_text(db, "world_fact", fact)
    outcome = semantic_search(db, cid, query, owner, dm_internal=True)
    assert outcome.status == SEM_OK
    assert outcome.packets[0].source_id == str(fact.id)

    # Nothing left to do; the active vector is untouched.
    assert run_semantic_index_sweep(db) == {"indexed": [], "failed": []}
    rows = db.execute(
        select(WorldEmbedding).where(WorldEmbedding.source_id == fact.id)
    ).scalars().all()
    assert len(rows) == 1


def test_sweep_records_failure_and_retries_after_backoff(monkeypatch):
    from datetime import datetime, timedelta, timezone

    Fac, cid, _owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, operation_id="op-sem-fail")

    def _boom(*_a, **_k):
        raise RuntimeError("embedder offline")

    monkeypatch.setattr(semantic_index, "index_source_record", _boom)
    result = run_semantic_index_sweep(db)
    assert result["indexed"] == [] and result["failed"]
    row = db.execute(
        select(WorldEmbedding).where(WorldEmbedding.source_id == fact.id)
    ).scalars().one()
    assert row.status == "failed" and "embedder offline" in row.error
    monkeypatch.undo()

    # Inside the backoff window the failed row is not reselected.
    assert str(row.id) not in run_semantic_index_sweep(db)["indexed"]
    row.updated_at = datetime.now(timezone.utc) - timedelta(
        seconds=semantic_index.SEMANTIC_RETRY_FAILED_AFTER_SECONDS + 60)
    db.commit()
    assert str(row.id) in run_semantic_index_sweep(db)["indexed"]
    db.refresh(row)
    assert row.status == "active"


def test_post_turn_cron_drains_semantic_index(monkeypatch):
    from fastapi.testclient import TestClient

    import database
    from app.factory import create_app

    monkeypatch.setenv("ALLOW_INSECURE_CRON", "1")
    monkeypatch.delenv("CRON_SECRET", raising=False)
    Fac, cid, _owner, _player, _ = _setup()
    with Fac() as db:
        fact = _seed_fact(db, cid, operation_id="op-sem-cron")
        fact_id = fact.id
    app = create_app()

    def _db():
        with Fac() as s:
            yield s

    app.dependency_overrides[database.get_db] = _db
    resp = TestClient(app).post("/api/cron/post-turn")
    assert resp.status_code == 200
    assert resp.json()["semantic_index"]["failed"] == []
    with Fac() as db:
        row = db.execute(
            select(WorldEmbedding).where(WorldEmbedding.source_id == fact_id)
        ).scalars().one()
        assert row.status == "active"


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

    fixed, _ = commit_world_write(
        db, cid, 1, supersede_fact, fact.id, content="The bridge has fallen.",
        epistemic_state="confirmed", visibility="campaign",
        operation_id="op-sem-supersede")
    note_supersession(db, cid, ("world_fact", fact.id), ("world_fact", fixed.id))
    # Supersession hook retired the prior source's vectors.
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
    entity, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="npc", name="Mara",
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
    other_fact, _ = commit_world_write(
        db, other.id, 0, create_fact, content="Campaign B secret ritual.",
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


def test_all_supported_source_types_index_and_resolve():
    from models.campaigns import CampaignDomainEvent
    from models.dm import DmTurn
    from sqlalchemy import select as _select

    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    entity, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="npc", name="Asha",
        summary="Asha guards Cinder Keep.", visibility="campaign",
        operation_id="op-sem-all-ent")
    rel, _ = commit_world_write(
        db, cid, 1, create_relation, subject_entity_id=entity.id, relation_type="guards",
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
            from app.world.service import apply_scene_update
            commit_world_write(
                db, cid, 3, apply_scene_update, location_name="Cinder Keep",
                operation_id="op-sem-all-scene")
        row = index_source_record(db, cid, source_type, source_id)
        assert row is not None and row.status == "active", source_type

    for source_type, source_id in targets:
        record = semantic_index.current_source_record(db, cid, source_type, source_id)
        assert record is not None, source_type
        query = build_source_text(db, source_type, record)
        assert query, source_type
        outcome = semantic_search(db, cid, query, owner, dm_internal=True)
        assert outcome.status == SEM_OK, source_type
        assert outcome.packets[0].source_id == str(source_id), source_type


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


# ── AI review round 1 regressions ────────────────────────────────────────────

def test_default_model_selection_prefers_configured_real_model(monkeypatch):
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY",
                "GEMINI_EMBEDDING_MODEL"):
        monkeypatch.delenv(var, raising=False)
    # Offline: explicit stub fallback.
    assert resolve_embedding_model(None) == "stub-hash-v1"
    # Configured key: production model, never silent stub.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert resolve_embedding_model(None) == "gemini-embedding-2"
    # Explicit env/model always wins.
    monkeypatch.setenv("GEMINI_EMBEDDING_MODEL", "text-embedding-004")
    assert resolve_embedding_model(None) == "text-embedding-004"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_EMBEDDING_MODEL", raising=False)
    assert resolve_embedding_model("custom-model") == "custom-model"


def test_non_stub_provider_path_uses_callable_and_task_modes(monkeypatch):
    import app.rules.gemini as gemini_mod

    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY",
                "GEMINI_EMBEDDING_MODEL"):
        monkeypatch.delenv(var, raising=False)
    seen_tasks: list[str] = []

    def _fake_factory(*, model=None, api_key=None,
                      task_type="RETRIEVAL_DOCUMENT",
                      output_dimensionality=None):
        seen_tasks.append(task_type)

        def _fake_provider(texts: list[str]):
            return [[0.05] * 1536 for _ in texts]

        return _fake_provider

    monkeypatch.setattr(gemini_mod, "make_gemini_provider", _fake_factory)

    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid)
    # The factory returns a plain callable (no .embed attribute): the old
    # code raised AttributeError here and degraded to "no provider".
    row = index_source_record(db, cid, "world_fact", fact.id,
                              embedding_model="gemini-embedding-2")
    assert row is not None and row.status == "active"
    assert row.embedding_model == "gemini-embedding-2"
    assert "RETRIEVAL_DOCUMENT" in seen_tasks

    query = build_source_text(db, "world_fact", fact)
    outcome = semantic_search(db, cid, query, owner, dm_internal=True,
                              embedding_model="gemini-embedding-2")
    assert outcome.status == SEM_OK
    assert outcome.packets[0].source_id == str(fact.id)
    assert "RETRIEVAL_QUERY" in seen_tasks

    # A real model with no usable provider fails visibly — never mints stub
    # vectors labeled as the real model.
    def _boom(*, model=None, api_key=None, task_type="RETRIEVAL_DOCUMENT",
              output_dimensionality=None):
        raise RuntimeError("GEMINI_API_KEY not set")

    monkeypatch.setattr(gemini_mod, "make_gemini_provider", _boom)
    with pytest.raises(RuntimeError, match="no provider"):
        index_source_record(db, cid, "world_fact", fact.id,
                            embedding_model="gemini-embedding-2")
    leftovers = db.execute(
        select(WorldEmbedding).where(
            WorldEmbedding.source_id == fact.id,
            WorldEmbedding.embedding_model == "gemini-embedding-2")
    ).scalars().all()
    assert all(r.status == "active" for r in leftovers)


def _commit_knowledge_turn(db, cid, owner, tid, staged_effects):
    from app.dm.turns import commit_turn, coordinate_turn, mark_streaming_started
    from app.runtime.submissions import accept_submission
    from models.dm import DMStream, DMStreamChunk

    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="The DM speaks",
        segments=[{"type": "ic", "text": "The DM speaks."}], thread_id=tid,
    )
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    attempt.staged_effects = staged_effects
    attempt.contract_snapshot = {"contract_version": "dm_turn_contract_v1",
                                 "new_entities": [], "staged_effects": []}
    db.flush()
    db.commit()
    stream = DMStream(
        id=uuid.uuid4(), campaign_id=turn.campaign_id,
        thread_id=uuid.UUID(str(turn.thread_id)),
        turn_id=str(turn.id), attempt_id=str(attempt.id),
        status="streaming", audience=turn.audience,
    )
    db.add(stream)
    db.flush()
    db.add(DMStreamChunk(
        id=uuid.uuid4(), stream_id=stream.id, sequence=0,
        text="Narration begins.", byte_length=len("Narration begins.".encode()),
    ))
    stream.first_chunk_at = datetime.now(timezone.utc)
    stream.chunk_count = 1
    db.flush()
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    return commit_turn(db, turn.id, attempt.id)


def test_committed_turn_staged_effects_become_searchable():
    from app.runtime.threads import get_or_create_campaign_thread

    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    entity, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="npc", name="Mara",
        visibility="campaign", operation_id="op-turn-ent")
    guild, _ = commit_world_write(
        db, cid, 1, create_entity, entity_type="faction", name="Guild",
        visibility="campaign", operation_id="op-turn-guild")
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    turn, attempt, event = _commit_knowledge_turn(db, cid, owner, str(thread.id), [
        {"id": "eff-rel-1", "effect_type": "upsert_relation", "arguments": {
            "subject_entity_id": str(entity.id), "relation_type": "works_for",
            "object_entity_id": str(guild.id), "epistemic_state": "confirmed",
            "visibility": "campaign",
        }},
        {"id": "eff-fact-1", "effect_type": "assert_fact", "arguments": {
            "content": "Mara serves the Guild openly.",
            "entity_refs": [str(entity.id), str(guild.id)],
            "epistemic_state": "confirmed", "visibility": "campaign",
        }},
    ])
    assert event is not None
    rels = list_relations(db, cid)
    facts = list_facts(db, cid)
    assert len(rels) == 1 and len(facts) == 1

    # The committed turn's inline writes published async index work
    # (placeholders) even though they bypassed the *_authoritative hooks —
    # including the turn record itself and its domain event, which are
    # declared semantic sources too.
    placeholders = db.execute(
        select(WorldEmbedding).where(WorldEmbedding.campaign_id == cid)
    ).scalars().all()
    assert {(p.source_type, str(p.source_id)) for p in placeholders} >= {
        ("world_relation", str(rels[0].id)), ("world_fact", str(facts[0].id)),
        ("source_turn", str(turn.id)), ("domain_event", str(event.id))}
    assert all(p.status == "stale" for p in placeholders)

    # The sweep indexes each record; exact-text search resolves each back
    # to its authoritative row.
    result = run_semantic_index_sweep(db, limit=50)
    assert result["failed"] == []
    assert all(r.status == "active" for r in db.execute(
        select(WorldEmbedding).where(WorldEmbedding.campaign_id == cid)
    ).scalars().all())
    for source_type, record in (("world_relation", rels[0]),
                                ("world_fact", facts[0])):
        query = build_source_text(db, source_type, record)
        outcome = semantic_search(db, cid, query, owner, dm_internal=True)
        assert outcome.status == SEM_OK, source_type
        assert outcome.packets[0].source_id == str(record.id), source_type
    turn_record = db.get(__import__("models.dm", fromlist=["DmTurn"]).DmTurn, turn.id)
    turn_query = build_source_text(db, "source_turn", turn_record)
    turn_outcome = semantic_search(db, cid, turn_query, owner, dm_internal=True)
    assert turn_outcome.status == SEM_OK
    assert turn_outcome.packets[0].source_id == str(turn.id)


def test_domain_event_embedding_version_matches_evidence_packet():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    _fact, event = commit_world_write(
        db, cid, 0, create_fact, content="The bridge has fallen.",
        epistemic_state="confirmed", visibility="campaign",
        operation_id="op-sem-evtver")
    row = index_source_record(db, cid, "domain_event", event.id)
    assert row is not None and row.status == "active"
    # Embedding row version and resolved evidence packet version agree.
    assert row.source_version == f"seq{int(event.sequence)}"
    query = build_source_text(db, "domain_event", event)
    outcome = semantic_search(db, cid, query, owner, dm_internal=True)
    assert outcome.status == SEM_OK
    assert outcome.packets[0].source_id == str(event.id)
    assert outcome.packets[0].source_version == row.source_version


def test_same_id_version_change_rebuilds_index():
    from app.world.service import apply_scene_update

    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    scene, _ = commit_world_write(
        db, cid, 0, apply_scene_update, location_name="Cinder Keep",
        operation_id="op-scene-1")
    assert scene is not None
    assert request_semantic_index(db, cid, "scene", cid) is not None

    def _scene_row():
        return db.execute(
            select(WorldEmbedding).where(
                WorldEmbedding.campaign_id == cid,
                WorldEmbedding.source_type == "scene",
                WorldEmbedding.embedding_model == DEFAULT_MODEL)
        ).scalars().one()

    run_semantic_index_sweep(db, limit=50)
    row = _scene_row()
    assert row.status == "active"
    first_version = row.source_version

    # Same-ID source change: the scene row keeps its id, the version moves.
    rev1 = int(scene.revision)
    scene2, _ = commit_world_write(
        db, cid, 1, apply_scene_update, location_name="Ember Gate",
        operation_id="op-scene-2")
    assert scene2 is not None
    assert int(scene2.revision) == rev1 + 1
    request_semantic_index(db, cid, "scene", cid)
    db.refresh(row)
    assert row.status == "stale"
    run_semantic_index_sweep(db, limit=50)
    db.refresh(row)
    assert row.status == "active"
    assert row.source_version != first_version
    second_version = row.source_version

    # Same-version re-request leaves the valid vector serving.
    assert request_semantic_index(db, cid, "scene", cid) is None
    db.refresh(row)
    assert row.status == "active"
    assert row.source_version == second_version


def test_hidden_top_hit_does_not_suppress_visible_result():
    import math

    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    hidden = _seed_fact(db, cid, content="Hidden vault rumor.",
                        visibility="dm_only", operation_id="op-wind-hidden")
    visible = _seed_fact(db, cid, content="Open market rumor.", rev=1,
                         visibility="campaign", operation_id="op-wind-open")
    # Crafted vectors: the hidden row outranks the visible row (1.0 > 0.9).
    dim = 1536
    query_vec = [1.0] + [0.0] * (dim - 1)
    visible_vec = [0.9, math.sqrt(1.0 - 0.81)] + [0.0] * (dim - 2)
    hidden_text = build_source_text(db, "world_fact", hidden)
    visible_text = build_source_text(db, "world_fact", visible)
    query_text = "vault rumor query"

    def _provider(texts: list[str]):
        table = {hidden_text: query_vec, visible_text: visible_vec,
                 query_text: query_vec}
        return [list(table[t]) for t in texts]

    index_source_record(db, cid, "world_fact", hidden.id, provider=_provider)
    index_source_record(db, cid, "world_fact", visible.id, provider=_provider)

    # Player window of 1: the hidden top hit is denied, the visible
    # runner-up must still be returned — never a forced NO_MATCH.
    outcome = semantic_search(db, cid, query_text, player, dm_internal=False,
                              limit=1, provider=_provider)
    assert outcome.status == SEM_OK
    assert [p.source_id for p in outcome.packets] == [str(visible.id)]
    assert outcome.denied >= 1
    # The window stays bounded: DM-internal search still returns only the
    # authorized top hit.
    dm_outcome = semantic_search(db, cid, query_text, owner, dm_internal=True,
                                 limit=1, provider=_provider)
    assert dm_outcome.status == SEM_OK
    assert [p.source_id for p in dm_outcome.packets] == [str(hidden.id)]
