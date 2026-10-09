"""Hybrid keyword + vector world-memory search and by-description lookups.

Keyword recall must work with no embedding index at all (no Gemini key,
unindexed records), stay behind the same authorization gates as the vector
path, and by-id tools given a description must answer with search matches
instead of failing.
"""

from __future__ import annotations

import uuid

from tests.test_world_semantic_213 import _seed_fact, _setup
from tests.support.world_writes import commit_world_write
from app.dm.context import ContextAudience
from app.dm.evidence import execute_evidence_round, validate_evidence_requests
from app.world.lexical import lexical_candidates
from app.world.semantic import STATUS_NO_MATCH, STATUS_OK, search_world_memory
from app.world.semantic_index import index_source_record
from app.world.service import create_entity
from models.world import WorldEmbedding


def _run(db, cid, users, *requests, audience="campaign"):
    aud = ContextAudience(campaign_id=str(cid), thread_id="main", audience=audience,
                          user_ids=[str(u) for u in users])
    results, _ = execute_evidence_round(
        validate_evidence_requests(list(requests)), aud, db=db, timeout_s=None)
    return results


def test_keyword_recall_needs_no_embedding_index():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, content="A rusty iron grate covers the old drain.")
    assert db.query(WorldEmbedding).filter_by(status="active").count() == 0

    outcome = search_world_memory(db, cid, "the rusty grate", owner, dm_internal=True)
    assert outcome.status == STATUS_OK
    assert outcome.packets[0].source_id == str(fact.id)
    assert outcome.packets[0].provenance["lexical_score"] > 0
    assert "vector_similarity" not in outcome.packets[0].provenance


def test_no_keyword_or_vector_match_is_no_match():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    _seed_fact(db, cid, content="A rusty iron grate covers the old drain.")
    outcome = search_world_memory(db, cid, "dragon hoard", owner, dm_internal=True)
    assert outcome.status == STATUS_NO_MATCH
    assert outcome.packets == []


def test_vector_and_keyword_votes_fuse_into_one_packet():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, content="A rusty iron grate covers the old drain.")
    index_source_record(db, cid, "world_fact", fact.id)
    # Stub vectors are exact-text hashes: query with the indexed text so the
    # vector path also votes.
    from app.world.semantic_index import build_source_text

    query = build_source_text(db, "world_fact", fact)
    outcome = search_world_memory(db, cid, query, owner, dm_internal=True)
    ids = [p.source_id for p in outcome.packets]
    assert ids.count(str(fact.id)) == 1
    packet = outcome.packets[ids.index(str(fact.id))]
    assert packet.provenance["vector_similarity"] > 0.99
    assert packet.provenance["lexical_score"] > 0


def test_stale_vector_row_still_serves_through_keyword_match():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, content="A rusty iron grate covers the old drain.")
    row = index_source_record(db, cid, "world_fact", fact.id)
    from app.world.semantic_index import build_source_text

    query = build_source_text(db, "world_fact", fact)
    row.source_version = "tampered-version"
    db.add(row)
    db.commit()
    outcome = search_world_memory(db, cid, query, owner, dm_internal=True)
    assert [p.source_id for p in outcome.packets][:1] == [str(fact.id)]
    assert "vector_similarity" not in outcome.packets[0].provenance
    db.refresh(row)
    assert row.status == "stale"


def test_keyword_path_hides_unauthorized_records_from_players():
    Fac, cid, _owner, player, _ = _setup()
    db = Fac()
    _seed_fact(db, cid, content="Asha hides the silver key under the altar.",
               visibility="dm_only", operation_id="op-hyb-secret")
    outcome = search_world_memory(db, cid, "silver key altar", player, dm_internal=False)
    assert outcome.packets == []
    assert outcome.denied >= 1
    results = _run(db, cid, [player],
                   {"id": "m1", "tool": "search_campaign_memory", "query": "silver key altar"},
                   audience="private")
    assert results[0].status == "missing"
    assert "silver key" not in str(results[0].model_dump(mode="json"))


def test_search_campaign_memory_finds_records_without_index():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, content="Osric's brass lantern was left at the ferry dock.")
    results = _run(db, cid, [owner],
                   {"id": "m1", "tool": "search_campaign_memory", "query": "Osric lantern"})
    assert results[0].status == "ok"
    assert results[0].payload["packets"][0]["source_id"] == str(fact.id)


def test_lookup_world_fact_with_description_returns_world_matches():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    fact = _seed_fact(db, cid, content="Osric's brass lantern was left at the ferry dock.")
    commit_world_write(db, cid, 1, create_entity, entity_type="npc", name="Osric",
                       summary="Ferryman with a brass lantern.", operation_id="op-hyb-osric")
    results = _run(db, cid, [owner],
                   {"id": "f1", "tool": "lookup_world_fact", "query": "evidence_osric_lantern"})
    assert results[0].status == "ok"
    packets = results[0].payload["packets"]
    assert {p["source_type"] for p in packets} <= {"world_fact", "world_relation", "world_entity"}
    assert str(fact.id) in {p["source_id"] for p in packets}
    assert "not one" in results[0].payload["note"]


def test_entity_tools_resolve_exact_names_and_search_descriptions():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    entity, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="npc", name="Osric Vale",
        summary="Ferryman who carries a brass lantern.", operation_id="op-hyb-ent")
    exact = _run(db, cid, [owner],
                 {"id": "e1", "tool": "lookup_world_entity", "query": "osric vale"})
    assert exact[0].status == "ok"
    assert exact[0].payload["packets"][0]["source_id"] == str(entity.id)
    assert "note" not in exact[0].payload

    described = _run(db, cid, [owner],
                     {"id": "e2", "tool": "traverse_world_relations",
                      "query": "the ferryman with the lantern"})
    assert described[0].status == "ok"
    assert described[0].payload["packets"][0]["source_id"] == str(entity.id)
    assert "note" in described[0].payload


def test_lexical_candidates_respect_source_type_filter_and_campaign():
    Fac, cid, owner, _player, _ = _setup()
    db = Fac()
    _seed_fact(db, cid, content="The rusty grate hides a tunnel.")
    commit_world_write(db, cid, 1, create_entity, entity_type="item", name="Rusty Grate",
                       operation_id="op-hyb-grate")
    only_entities = lexical_candidates(db, cid, "rusty grate",
                                       source_types=frozenset({"world_entity"}))
    assert {stype for stype, _, _ in only_entities} == {"world_entity"}
    assert lexical_candidates(db, uuid.uuid4(), "rusty grate") == []
