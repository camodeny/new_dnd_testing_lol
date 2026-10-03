"""Rules guidance retrieval + advisory — unit tests with fakes, no network."""

from __future__ import annotations

import types
import uuid
import copy

import app.dm.rules_guidance as rg
from app.decisions.runtime import DecisionService
from app.dm.context import (
    AuthorizationScope,
    ContextAudience,
    ContextRecord,
    LaneName,
    LANE_ORDER,
    SourceRef,
    assemble_context_packet,
)
from tests.support.fake_decisions import FakeDecisionAdapter


def _audience():
    cid, tid, uid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    return ContextAudience(
        campaign_id=cid, thread_id=tid, audience="campaign", user_ids=[uid]
    )


def _rec(record_id, campaign_id, value, **over):
    return ContextRecord(
        record_id=record_id,
        value=value,
        sources=[
            SourceRef(source_type="fixture", source_id=record_id, source_version="1")
        ],
        authorization=AuthorizationScope(campaign_id=campaign_id),
        visibility=over.get("visibility", "campaign"),
        use=over.get("use", "narration_eligible"),
    )


def _packet(audience=None, texts=("I shove the goblin",), scene="Tavern"):
    aud = audience or _audience()
    records = {lane: [] for lane in LANE_ORDER}
    records[LaneName.PLAYER_INPUTS] = [
        _rec(
            "submission:1",
            aud.campaign_id,
            {
                "submission_id": "s1",
                "segments": [
                    {"position": i, "segment_type": "ic", "text": t}
                    for i, t in enumerate(texts)
                ],
            },
        )
    ]
    records[LaneName.CURRENT_SCENE] = [
        _rec(
            "scene:1",
            aud.campaign_id,
            {"location_name": scene, "fictional_time": "dusk"},
        )
    ]
    return assemble_context_packet(audience=aud, records=records)


def _hit(rule_id, **over):
    """Minimal BM25 hit: rule_id only unless a test forges extra fields."""
    hit = {"rule_id": rule_id}
    hit.update(over)
    return hit


def _lookup_row(rule_id, body="canonical body"):
    return types.SimpleNamespace(
        rule_id=rule_id,
        corpus_version="5.2.1",
        title=f"Title {rule_id}",
        heading_path=["Combat"],
        body=body,
        citation=lambda: {"rule_id": rule_id, "title": f"Title {rule_id}"},
    )


def _mock_retrieval(monkeypatch, hits, rows_by_id=None):
    """Fake search_bm25_rules + batched lookup_rules_by_ids (one DB read)."""
    monkeypatch.setattr(
        rg, "search_bm25_rules", lambda db, query, *, limit=8: list(hits)
    )
    if rows_by_id is None:
        rows_by_id = {
            h["rule_id"]: _lookup_row(h["rule_id"])
            for h in hits
            if isinstance(h, dict) and h.get("rule_id")
        }
    monkeypatch.setattr(
        rg,
        "lookup_rules_by_ids",
        lambda db, ids: {rid: rows_by_id[rid] for rid in ids if rid in rows_by_id},
    )


def test_relevant_filtering_retains_only_relevant(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    packet = _packet()
    _mock_retrieval(monkeypatch, [_hit("rule-a"), _hit("rule-b")])
    service = DecisionService(
        FakeDecisionAdapter(
            answers={
                "rules-relevance-0": "RELEVANT",
                "rules-relevance-1": "IRRELEVANT",
                "rules-priority-0": 3,
                "rules-priority-1": 0,
            }
        )
    )
    enriched = rg.enrich_rules_context(None, packet, decision_service=service)
    lane = next(
        item for item in enriched.lanes if item.name == LaneName.EVIDENCE_RESULTS
    )
    ids = [r.record_id for r in lane.records]
    assert "rules-guidance:rule-a" in ids
    assert "rules-guidance:rule-b" not in ids
    rec = next(r for r in lane.records if r.record_id == "rules-guidance:rule-a")
    assert rec.visibility == "public" and rec.use == "adjudication_only"
    assert rec.sources[0].source_type == "dnd_srd_rule"
    assert rec.value["ranking"] == "ranked"


def test_no_relevant_marker_is_guidance_not_source_claim(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    packet = _packet()
    _mock_retrieval(monkeypatch, [_hit("rule-a"), _hit("rule-b")])
    service = DecisionService(
        FakeDecisionAdapter(
            answers={
                "rules-relevance-0": "IRRELEVANT",
                "rules-relevance-1": "INSUFFICIENT_EVIDENCE",
                "rules-priority-0": 0,
                "rules-priority-1": 0,
            }
        )
    )
    enriched = rg.enrich_rules_context(None, packet, decision_service=service)
    lane = next(
        item for item in enriched.lanes if item.name == LaneName.EVIDENCE_RESULTS
    )
    assert len(lane.records) == 1
    marker = lane.records[0]
    assert marker.record_id == rg.NO_RELEVANT_ID
    assert marker.value["status"] == "insufficient_evidence"
    assert "not a claim about source existence" in marker.value["note"]


def test_unknown_ids_yield_marker_and_failure_preserves_packet(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    packet = _packet()
    # Unknown IDs: BM25 hit IDs absent from batched lookup dict are dropped.
    _mock_retrieval(monkeypatch, [_hit("ghost-1")], rows_by_id={})
    enriched = rg.enrich_rules_context(None, packet)
    lane = next(
        item for item in enriched.lanes if item.name == LaneName.EVIDENCE_RESULTS
    )
    assert lane.records[0].record_id == rg.NO_RELEVANT_ID

    # Tool failure: packet unchanged, no invented canonical records.
    packet2 = _packet()

    def _boom(db, query, *, limit=8):
        raise RuntimeError("db down")

    monkeypatch.setattr(rg, "search_bm25_rules", _boom)
    monkeypatch.setattr(rg, "lookup_rules_by_ids", lambda db, ids: {})
    out = rg.enrich_rules_context(None, packet2)
    lane2 = next(item for item in out.lanes if item.name == LaneName.EVIDENCE_RESULTS)
    assert len(lane2.records) == 1
    assert lane2.records[0].value["status"] == "unavailable"
    assert all(src.source_type != "dnd_srd_rule" for src in lane2.records[0].sources)


def test_unranked_without_provider_preserves_retrieval_order(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    packet = _packet()
    _mock_retrieval(monkeypatch, [_hit("rule-a"), _hit("rule-b")])
    enriched = rg.enrich_rules_context(None, packet)
    lane = next(
        item for item in enriched.lanes if item.name == LaneName.EVIDENCE_RESULTS
    )
    assert [r.record_id for r in lane.records] == [
        "rules-guidance:rule-a",
        "rules-guidance:rule-b",
    ]
    assert all(r.value["ranking"] == "unranked" for r in lane.records)


def test_query_omits_fictional_time(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    captured = {}

    def fake_search(db, query, *, limit=8):
        captured["query"] = query
        captured["limit"] = limit
        return []

    monkeypatch.setattr(rg, "search_bm25_rules", fake_search)
    monkeypatch.setattr(rg, "lookup_rules_by_ids", lambda db, ids: {})
    rg.enrich_rules_context(None, _packet())
    assert captured["limit"] == 8
    query = captured["query"]
    assert "I shove the goblin" in query
    assert "Tavern" in query
    assert "dusk" not in query


def _mechanical_contract():
    return {
        "contract_version": "dm_turn_contract_v1",
        "mode": "await_roll",
        "reason": "athletics check",
        "beats": [
            {
                "id": "beat_1",
                "type": "narration",
                "claims": [
                    {
                        "text": "You shove.",
                        "claim_kind": "observation",
                        "origin": "dm_adjudication",
                    }
                ],
            }
        ],
        "roll_request": {
            "request_id": "roll_1",
            "roll_kind": "check",
            "ability_or_skill": "athletics",
            "label": "Shove",
            "reason_public": "Shove the goblin",
        },
    }


def test_advisory_contradiction_non_mutating_and_citations_exact(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    packet = _packet()
    _mock_retrieval(monkeypatch, [_hit("rule-a")])
    enriched = rg.enrich_rules_context(None, packet)
    contract = _mechanical_contract()
    before = copy.deepcopy(contract)
    adapter = FakeDecisionAdapter(answers={"rules-advisory": "CONTRADICTED"})
    service = DecisionService(adapter)
    summary = rg.check_rules_advisory(enriched, contract, decision_service=service)
    assert summary["outcome"] == "CONTRADICTED"
    assert summary["citations"] == ["rule-a"]
    assert contract == before
    proposal = adapter.calls[0]["state"]["proposal"]
    assert proposal["roll_request"] == contract["roll_request"]
    assert proposal["beats"] == contract["beats"]


def test_advisory_missing_evidence_without_provider(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    packet = _packet()
    summary = rg.check_rules_advisory(packet, _mechanical_contract())
    assert summary["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert summary["citations"] == []


def test_advisory_skips_non_mechanical_modes():
    packet = _packet()
    for mode in ("silent", "table_chat", "need_evidence"):
        contract = {"mode": mode, "beats": []}
        summary = rg.check_rules_advisory(packet, contract)
        assert summary["outcome"] == "NOT_MECHANICAL"
        assert summary["status"] == "skipped"


def test_projection_excludes_guidance_and_budget_bounded(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    packet = _packet()
    hits = [_hit(f"rule-{i}") for i in range(8)]
    rows = {f"rule-{i}": _lookup_row(f"rule-{i}", body="x" * 5000) for i in range(8)}
    _mock_retrieval(monkeypatch, hits, rows_by_id=rows)
    enriched = rg.enrich_rules_context(None, packet)
    lane = next(
        item for item in enriched.lanes if item.name == LaneName.EVIDENCE_RESULTS
    )
    assert len(lane.records) <= 3
    total = sum(len(r.value.get("body", "")) for r in lane.records)
    assert total <= 12_000
    narration = enriched.serialize_for_narration()
    assert "rule-" not in narration
    assert "rules-guidance" not in narration


def test_reranks_all_candidates_in_one_call_and_reuses_packet(monkeypatch):
    hits = [_hit("rule-a"), _hit("rule-b")]
    searches = []
    lookups = []

    def search(db, query, *, limit=8):
        searches.append((query, limit))
        assert limit == 8
        return list(hits)

    def lookup(db, ids):
        lookups.append(tuple(ids))
        return {rid: _lookup_row(rid) for rid in ids if rid in {"rule-a", "rule-b"}}

    monkeypatch.setattr(rg, "search_bm25_rules", search)
    monkeypatch.setattr(rg, "lookup_rules_by_ids", lookup)
    adapter = FakeDecisionAdapter(
        answers={
            "rules-relevance-0": "RELEVANT",
            "rules-priority-0": 2,
            "rules-relevance-1": "RELEVANT",
            "rules-priority-1": 3,
        }
    )
    service = DecisionService(adapter)
    enriched = rg.enrich_rules_context(None, _packet(), decision_service=service)
    records = next(
        item.records
        for item in enriched.lanes
        if item.name == LaneName.EVIDENCE_RESULTS
    )
    assert [r.value["rule_id"] for r in records] == ["rule-b", "rule-a"]
    assert len(adapter.calls) == 1
    assert rg.enrich_rules_context(None, enriched, decision_service=service) is enriched
    assert len(searches) == 1
    assert len(lookups) == 1


def test_invalid_provider_answer_preserves_unranked_canonical_evidence(monkeypatch):
    _mock_retrieval(monkeypatch, [_hit("rule-a")])
    adapter = FakeDecisionAdapter(
        answers={"rules-relevance-0": "invented-id", "rules-priority-0": 3}
    )
    enriched = rg.enrich_rules_context(
        None, _packet(), decision_service=DecisionService(adapter)
    )
    records = next(
        item.records
        for item in enriched.lanes
        if item.name == LaneName.EVIDENCE_RESULTS
    )
    assert records[0].value["ranking"] == "unranked"
    assert records[0].value["request_status"] == "retrieval_only_provider_failed"
    assert records[0].value["body"] == "canonical body"


def test_unknown_rule_with_forged_body_never_becomes_source(monkeypatch):
    # BM25 hit carries convincing prose but batched lookup has no such ID.
    _mock_retrieval(
        monkeypatch,
        [
            _hit(
                "invented",
                body="convincing prose",
                title="Forged",
                excerpt="convincing prose",
            )
        ],
        rows_by_id={},
    )
    enriched = rg.enrich_rules_context(None, _packet())
    records = next(
        item.records
        for item in enriched.lanes
        if item.name == LaneName.EVIDENCE_RESULTS
    )
    assert len(records) == 1
    assert records[0].record_id == rg.NO_RELEVANT_ID
    assert records[0].sources[0].source_type == "rules_guidance"


def test_respond_ruling_is_judged_even_without_roll_markers(monkeypatch):
    _mock_retrieval(monkeypatch, [_hit("rule-a")])
    enriched = rg.enrich_rules_context(None, _packet())
    adapter = FakeDecisionAdapter(answers={"rules-advisory": "CONTRADICTED"})
    ruling = {
        "mode": "respond",
        "beats": [{"claims": [{"text": "Shoving always succeeds without a roll."}]}],
    }
    result = rg.check_rules_advisory(
        enriched, ruling, decision_service=DecisionService(adapter)
    )
    assert result["outcome"] == "CONTRADICTED"
    assert adapter.calls[0]["state"]["proposal"]["beats"] == ruling["beats"]


def test_advisory_uses_explicit_rule_lookup_after_initial_retrieval_miss():
    packet = _packet()
    evidence = rg.ContextRecord(
        record_id="evidence:lookup-1",
        value={
            "tool": "lookup_rule",
            "status": "ok",
            "result": _hit("rule-a", title="Title rule-a", body="canonical body"),
        },
        sources=[
            SourceRef(
                source_type="dnd_srd_rule", source_id="rule-a", source_version="5.2.1"
            )
        ],
        authorization=AuthorizationScope(campaign_id=packet.audience.campaign_id),
        use="adjudication_only",
    )
    packet = packet.with_records(
        {LaneName.EVIDENCE_RESULTS: [evidence]}, dependency="evidence_results"
    )
    service = DecisionService(
        FakeDecisionAdapter(answers={"rules-advisory": "SUPPORTED"})
    )
    result = rg.check_rules_advisory(
        packet, _mechanical_contract(), decision_service=service
    )
    assert result["outcome"] == "SUPPORTED"
    assert result["citations"] == ["rule-a"]
