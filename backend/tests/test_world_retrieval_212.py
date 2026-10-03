"""Issue #212 — world retrieval: graph traversal, timeline, fact, source-turn evidence."""

from __future__ import annotations

import uuid

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
from tests.support.world_writes import commit_world_write  # noqa: E402
from app.dm.context import ContextAudience  # noqa: E402
from app.dm.contract import EvidenceRequest  # noqa: E402
from app.dm.evidence import execute_evidence_round, validate_evidence_requests  # noqa: E402
from app.world.knowledge import assert_knowledge  # noqa: E402
from app.world.facts import (  # noqa: E402
    create_fact,
    create_relation,
)
from app.world.retrieval import (  # noqa: E402
    RETRIEVAL_MAX_DEPTH,
    STATUS_NOT_FOUND,
    STATUS_OK,
    fact_source_evidence,
    lookup_fact,
    lookup_source_turn,
    query_character_knowledge,
    query_timeline,
    retrieve_entity,
    traverse_relations,
)
from app.world.service import (  # noqa: E402
    create_entity,
)
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.dm import DmTurn  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import PlayerSubmission  # noqa: E402


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
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Retrieval campaign", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.add(CampaignMember(campaign_id=camp.id, user_id=player, role="player"))
    db.commit()
    db.refresh(camp)
    return Fac, camp.id, owner, player, outsider


def _seed_graph(db, cid, rev=0):
    """A --knows--> B --located_at--> C plus a dm_only secret fact on A."""
    a, _ = commit_world_write(
        db, cid, rev, create_entity, event_type="world.entity_created",
        event_visibility="public", entity_type="npc", name="Asha",
        visibility="campaign", operation_id="op-ent-a")
    b, _ = commit_world_write(
        db, cid, rev + 1, create_entity, event_type="world.entity_created",
        event_visibility="public", entity_type="npc", name="Bram",
        visibility="campaign", operation_id="op-ent-b")
    c, _ = commit_world_write(
        db, cid, rev + 2, create_entity, event_type="world.entity_created",
        event_visibility="public", entity_type="location", name="Cinder Keep",
        visibility="campaign", operation_id="op-ent-c")
    rev += 3
    rel_ab, _ = commit_world_write(
        db, cid, rev, create_relation, event_type="world.relation_created",
        event_visibility="public", subject_entity_id=a.id, relation_type="knows",
        object_entity_id=b.id, epistemic_state="confirmed",
        visibility="campaign", operation_id="op-rel-ab")
    rev += 1
    rel_bc, _ = commit_world_write(
        db, cid, rev, create_relation, event_type="world.relation_created",
        event_visibility="public", subject_entity_id=b.id, relation_type="located_at",
        object_entity_id=c.id, epistemic_state="believed",
        visibility="campaign", operation_id="op-rel-bc")
    rev += 1
    secret, _ = commit_world_write(
        db, cid, rev, create_fact, event_type="world.fact_asserted", content="Asha hides the key.",
        entity_refs=[a.id], epistemic_state="confirmed",
        visibility="dm_only", operation_id="op-fact-secret")
    rev += 1
    return {"a": a, "b": b, "c": c, "rel_ab": rel_ab,
            "rel_bc": rel_bc, "secret": secret, "rev": rev}


def _seed_turn(db, cid, player, text="I search the keep."):
    turn = DmTurn(
        id=uuid.uuid4(), campaign_id=cid, thread_id="main",
        audience="campaign", status="committed", source_revision=1,
        input_set_revision=1, submission_ids=[],
    )
    db.add(turn)
    db.flush()
    submission = PlayerSubmission(
        id=uuid.uuid4(), campaign_id=cid, user_id=player,
        thread_id="main", audience="campaign", sequence=1,
        raw_content=text,
    )
    db.add(submission)
    db.flush()
    turn.submission_ids = [str(submission.id)]
    db.flush()
    return turn, submission


# ── entity → relation traversal ──────────────────────────────────────────────

def test_entity_relation_traversal_depth_two():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    outcome = traverse_relations(
        db, cid, seed["a"].id, player, depth=2, dm_internal=False)
    assert outcome.status == STATUS_OK
    assert outcome.depth_applied == 2
    kinds = {(p.source_type, p.source_id) for p in outcome.packets}
    assert ("world_entity", str(seed["a"].id)) in kinds
    assert ("world_relation", str(seed["rel_ab"].id)) in kinds
    assert ("world_entity", str(seed["b"].id)) in kinds
    assert ("world_relation", str(seed["rel_bc"].id)) in kinds
    assert ("world_entity", str(seed["c"].id)) in kinds
    # Stable source identity on every packet.
    for packet in outcome.packets:
        assert packet.source_id and packet.source_version
        assert packet.provenance.get("retrieved_by") == "world_retrieval_212"
    # Relation packets carry epistemic state; traversal is bounded/observed.
    rel_packets = [p for p in outcome.packets if p.source_type == "world_relation"]
    assert {p.epistemic_state for p in rel_packets} == {"confirmed", "believed"}
    assert outcome.source_ids
    assert outcome.latency_ms >= 0


def test_traversal_depth_zero_returns_only_root():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    outcome = traverse_relations(db, cid, seed["a"].id, player, depth=0)
    assert outcome.status == STATUS_OK
    assert [p.source_type for p in outcome.packets] == ["world_entity"]


def test_traversal_limit_is_bounded_and_observable():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    outcome = traverse_relations(db, cid, seed["a"].id, player, depth=5, limit=1)
    assert outcome.depth_applied == RETRIEVAL_MAX_DEPTH  # clamped, not 5
    assert outcome.limit_applied == 1
    assert outcome.truncated is True
    assert len(outcome.packets) == 2  # root + 1 bounded result


def test_traverse_unknown_entity_is_not_found_not_fabricated():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    outcome = traverse_relations(db, cid, uuid.uuid4(), player, depth=2)
    assert outcome.status == STATUS_NOT_FOUND
    assert outcome.packets == []
    assert outcome.error


def test_retrieve_entity_by_stable_id():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    outcome = retrieve_entity(db, cid, seed["b"].id, player)
    assert outcome.status == STATUS_OK
    assert outcome.packets[0].content["name"] == "Bram"
    assert outcome.packets[0].visibility == "campaign"


# ── fact → source event ──────────────────────────────────────────────────────

def test_fact_source_evidence_follows_provenance_links():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    turn, _submission = _seed_turn(db, cid, player)
    fact, _ = commit_world_write(
        db, cid, seed["rev"], create_fact, content="The gate fell at dusk.",
        entity_refs=[seed["c"].id], epistemic_state="confirmed",
        visibility="campaign",
        source_turn_id=turn.id,
        operation_id="op-fact-gate")
    outcome = fact_source_evidence(db, cid, fact.id, player)
    assert outcome.status == STATUS_OK
    by_type = {p.source_type for p in outcome.packets}
    assert "world_fact" in by_type
    assert "source_turn" in by_type
    fact_packet = next(p for p in outcome.packets if p.source_type == "world_fact")
    assert fact_packet.epistemic_state == "confirmed"
    assert fact_packet.provenance["source_turn_id"] == str(turn.id)
    turn_packet = next(p for p in outcome.packets if p.source_type == "source_turn")
    assert turn_packet.content["established_records"]["fact_ids"] == [str(fact.id)]


def test_lookup_fact_not_found():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    outcome = lookup_fact(db, cid, uuid.uuid4(), player)
    assert outcome.status == STATUS_NOT_FOUND
    assert outcome.packets == []


# ── timeline range ───────────────────────────────────────────────────────────

def test_timeline_range_and_type_filter():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    full = query_timeline(db, cid, owner, dm_internal=True)
    assert full.status == STATUS_OK
    assert full.visible >= 6  # 3 entities + 2 relations + 1 fact
    seqs = [p.revision_or_sequence for p in full.packets]
    assert seqs == sorted(seqs)
    # Bounded range.
    ranged = query_timeline(
        db, cid, owner, from_sequence=2, to_sequence=3, dm_internal=True)
    assert {p.revision_or_sequence for p in ranged.packets} == {2, 3}
    # Event-type prefix filter (no free-form SQL).
    filtered = query_timeline(
        db, cid, owner, event_types="world.entity", dm_internal=True)
    assert filtered.packets
    assert all(p.provenance["event_type"].startswith("world.entity")
               for p in filtered.packets)
    with pytest.raises(ValueError):
        query_timeline(db, cid, owner, from_sequence=5, to_sequence=2,
                       dm_internal=True)


def test_timeline_player_facing_hides_restricted_events():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    _seed_graph(db, cid)  # secret fact emits a dm_only event
    member_feed = query_timeline(db, cid, player, dm_internal=False)
    internal = query_timeline(db, cid, owner, dm_internal=True)
    assert internal.visible > member_feed.visible
    assert member_feed.denied > 0
    assert all(p.visibility == "public" for p in member_feed.packets)
    # DM-internal retrieval preserves visibility metadata, never erases it.
    assert {p.visibility for p in internal.packets} >= {"public", "dm_only"}


# ── source-turn retrieval ────────────────────────────────────────────────────

def test_source_turn_retrieval_with_submissions_and_records():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    turn, submission = _seed_turn(db, cid, player)
    rel, _ = commit_world_write(
        db, cid, seed["rev"], create_relation, subject_entity_id=seed["a"].id,
        relation_type="owes", object_entity_id=seed["b"].id,
        epistemic_state="claimed", visibility="campaign",
        source_turn_id=turn.id, operation_id="op-rel-turn")
    outcome = lookup_source_turn(db, cid, turn.id, player)
    assert outcome.status == STATUS_OK
    packet = outcome.packets[0]
    assert packet.source_type == "source_turn"
    assert packet.content["submissions"][0]["raw_content"] == "I search the keep."
    assert packet.content["established_records"]["relation_ids"] == [str(rel.id)]
    assert packet.provenance["thread_id"] == "main"


def test_source_turn_not_found():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    outcome = lookup_source_turn(db, cid, uuid.uuid4(), player)
    assert outcome.status == STATUS_NOT_FOUND


# ── character knowledge ──────────────────────────────────────────────────────

def test_character_knowledge_query_returns_target_snapshot():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    campaign = db.get(Campaign, cid)
    row, _ = assert_knowledge(
        db, campaign, subject_kind="character",
        subject_entity_id=seed["a"].id, target_kind="fact",
        target_fact_id=seed["secret"].id, knowledge_state="knows",
        acquisition_source="overheard", visibility="campaign")
    db.commit()
    outcome = query_character_knowledge(db, cid, seed["a"].id, owner,
                                        dm_internal=True)
    assert outcome.status == STATUS_OK
    assert outcome.visible == 1
    entry = outcome.packets[0]
    assert entry.source_type == "knowledge"
    assert entry.epistemic_state == "knows"
    assert entry.content["target"]["content"] == "Asha hides the key."
    assert entry.content["target_id"] == str(seed["secret"].id)


def test_character_knowledge_hidden_target_counts_denial_without_leak():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    campaign = db.get(Campaign, cid)
    # Knowledge row itself is campaign-visible but the truth target is dm_only.
    assert_knowledge(
        db, campaign, subject_kind="character",
        subject_entity_id=seed["a"].id, target_kind="fact",
        target_fact_id=seed["secret"].id, knowledge_state="believes",
        visibility="campaign")
    db.commit()
    outcome = query_character_knowledge(db, cid, seed["a"].id, player,
                                        dm_internal=False)
    assert outcome.packets == []  # target hidden: no ids/content leak
    assert outcome.denied == 1
    assert outcome.denied_reasons.get("target_not_visible") == 1


# ── hidden evidence handling ─────────────────────────────────────────────────

def test_hidden_evidence_denied_player_facing_preserved_dm_internal():
    Fac, cid, owner, player, outsider = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    # Player-facing: hidden row filtered, denial counted, nothing leaked.
    denied = lookup_fact(db, cid, seed["secret"].id, player, dm_internal=False)
    assert denied.packets == []
    assert denied.denied == 1
    assert denied.denied_reasons.get("dm_only_requires_authority") == 1
    assert "Asha hides" not in str(denied.to_dict())
    # Non-members get nothing at all.
    stranger = retrieve_entity(db, cid, seed["a"].id, outsider, dm_internal=False)
    assert stranger.packets == []
    assert stranger.denied_reasons.get("not_campaign_member") == 1
    # DM-internal: full record WITH visibility metadata intact.
    internal = lookup_fact(db, cid, seed["secret"].id, player, dm_internal=True)
    assert internal.status == STATUS_OK
    assert internal.packets[0].content["content"] == "Asha hides the key."
    assert internal.packets[0].visibility == "dm_only"
    assert internal.packets[0].revealable is None


# ── optional reranking ───────────────────────────────────────────────────────

# ── #203 mediation integration ──────────────────────────────────────────────

def test_world_tools_execute_through_evidence_mediation():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    audience = ContextAudience(campaign_id=str(cid), thread_id="main",
                               audience="campaign", user_ids=[str(owner)])
    requests = validate_evidence_requests([
        {"id": "ev1", "tool": "lookup_world_entity", "query": str(seed["a"].id)},
        {"id": "ev2", "tool": "traverse_world_relations", "query": str(seed["a"].id), "limit": 5},
        {"id": "ev3", "tool": "query_world_timeline", "limit": 5},
    ])
    results, trace = execute_evidence_round(
        requests, audience, db=db, timeout_s=None)
    assert [r.status for r in results] == ["ok", "ok", "ok"]
    assert all(r.result_count >= 1 for r in results)
    assert all(r.sources for r in results)
    assert set(trace.tool_types) == {
        "lookup_world_entity", "traverse_world_relations", "query_world_timeline"}
    assert trace.source_ids
    # Payloads carry provenance + bounds without provider-specific knowledge.
    payload = results[1].payload
    assert payload["packets"][0]["provenance"]["retrieved_by"] == "world_retrieval_212"
    assert payload["limit_applied"] == 5


def test_world_tool_private_audience_filters_hidden_evidence():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    audience = ContextAudience(campaign_id=str(cid), thread_id="main",
                               audience="private", user_ids=[str(player)])
    requests = validate_evidence_requests([
        {"id": "ev1", "tool": "lookup_world_fact", "query": str(seed["secret"].id)},
    ])
    results, _trace = execute_evidence_round(
        requests, audience, db=db, timeout_s=None)
    assert results[0].status == "unknown"
    assert results[0].result_count == 0
    # No world source identity leaks; the mediation placeholder (if any)
    # carries only the request id, never the hidden record.
    assert all(s.source_type != "world_fact" for s in results[0].sources)
    assert results[0].payload["packets"] == []
    assert "Asha hides" not in str(results[0].model_dump(mode="json"))


def test_world_tool_contract_validation():
    with pytest.raises(Exception):
        EvidenceRequest.model_validate({"id": "x", "tool": "lookup_world_entity"})
    with pytest.raises(Exception):
        EvidenceRequest.model_validate(
            {"id": "x", "tool": "traverse_world_relations", "query": "   "})
    req = EvidenceRequest.model_validate(
        {"id": "ok1", "tool": "query_world_timeline", "limit": 5})
    assert req.limit == 5


# ── AI review round 1 regressions ──────────────────────────────────────────

def test_private_source_turn_thread_member_allowed_owner_denied():
    from app.threads.service import create_private_thread

    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    private_thread = create_private_thread(
        db, campaign_id=cid, created_by=player, member_ids=[player])
    db.commit()
    turn = DmTurn(
        id=uuid.uuid4(), campaign_id=cid, thread_id=str(private_thread.id),
        audience="private", status="committed", source_revision=1,
        input_set_revision=1, submission_ids=[],
    )
    db.add(turn)
    db.commit()
    member_ok = lookup_source_turn(db, cid, turn.id, player)
    assert member_ok.status == STATUS_OK
    assert member_ok.packets[0].source_type == "source_turn"
    owner_denied = lookup_source_turn(db, cid, turn.id, owner)
    assert owner_denied.packets == []
    assert owner_denied.denied == 1
    assert owner_denied.denied_reasons.get("turn_not_visible") == 1


def test_timeline_outsider_gets_no_public_events():
    Fac, cid, owner, player, outsider = _setup()
    db = Fac()
    _seed_graph(db, cid)
    denied = query_timeline(db, cid, outsider, dm_internal=False)
    assert denied.packets == []
    assert denied.denied >= 1
    assert denied.denied_reasons.get("not_campaign_member") >= 1
    assert "not_campaign_member" not in str(denied.source_ids)


def test_private_knowledge_internal_preserves_visibility_player_filters():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)
    fact, _ = commit_world_write(
        db, cid, seed["rev"], create_fact, content="The well is dry.",
        entity_refs=[seed["c"].id], epistemic_state="believed",
        visibility="public", operation_id="op-fact-well-private-k")
    campaign = db.get(Campaign, cid)
    assert_knowledge(
        db, campaign, subject_kind="character",
        subject_entity_id=seed["a"].id, target_kind="fact",
        target_fact_id=fact.id, knowledge_state="knows",
        acquisition_source="overheard", visibility="private")
    db.commit()
    internal = query_character_knowledge(
        db, cid, seed["a"].id, owner, dm_internal=True)
    assert internal.status == STATUS_OK
    assert internal.visible == 1
    assert internal.packets[0].visibility == "private"
    assert internal.packets[0].revealable is None
    player_view = query_character_knowledge(
        db, cid, seed["a"].id, player, dm_internal=False)
    assert player_view.packets == []
    assert player_view.denied >= 1
    assert player_view.denied_reasons.get("knowledge_not_visible") == 1


# ── AI review round 2 regression ───────────────────────────────────────────

def test_campaign_knowledge_of_hidden_target_stays_restricted_in_mediation():
    from app.dm.evidence import evidence_results_to_records

    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)  # seed["secret"] is a dm_only fact
    campaign = db.get(Campaign, cid)
    # Campaign-visible knowledge row pointing at a dm_only truth target.
    assert_knowledge(
        db, campaign, subject_kind="character",
        subject_entity_id=seed["a"].id, target_kind="fact",
        target_fact_id=seed["secret"].id, knowledge_state="knows",
        acquisition_source="overheard", visibility="campaign")
    db.commit()
    internal = query_character_knowledge(
        db, cid, seed["a"].id, owner, dm_internal=True)
    assert internal.visible == 1
    # Packet visibility is the stricter of row and target: dm_only wins.
    assert internal.packets[0].visibility == "dm_only"
    assert "Asha hides" in str(internal.packets[0].content.get("target", {}))
    # Through #203 mediation the evidence stays dm_only / adjudication-only.
    audience = ContextAudience(campaign_id=str(cid), thread_id="main",
                               audience="campaign", user_ids=[str(owner)])
    requests = validate_evidence_requests([
        {"id": "evk", "tool": "query_character_knowledge",
         "query": str(seed["a"].id)},
    ])
    results, _trace = execute_evidence_round(
        requests, audience, db=db, timeout_s=None)
    assert results[0].status == "ok"
    assert results[0].visibility == "dm_only"
    records = evidence_results_to_records(results, audience)
    # dm_only evidence is adjudication-only: never narration-eligible.
    assert records[0].use == "adjudication_only"


def test_private_knowledge_of_dm_only_target_stays_dm_only():
    Fac, cid, owner, player, _ = _setup()
    db = Fac()
    seed = _seed_graph(db, cid)  # seed["secret"] is a dm_only fact
    campaign = db.get(Campaign, cid)
    # Private knowledge row pointing at a dm_only truth target: dm_only
    # dominates private (adjudication-only always beats audience-scoped
    # private), so the composed packet must stay dm_only.
    assert_knowledge(
        db, campaign, subject_kind="character",
        subject_entity_id=seed["a"].id, target_kind="fact",
        target_fact_id=seed["secret"].id, knowledge_state="knows",
        acquisition_source="overheard", visibility="private")
    db.commit()
    internal = query_character_knowledge(
        db, cid, seed["a"].id, owner, dm_internal=True)
    assert internal.visible == 1
    assert internal.packets[0].visibility == "dm_only"
