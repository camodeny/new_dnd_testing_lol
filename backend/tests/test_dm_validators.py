"""Issue #205 — pre-narration validator pipeline fixtures.

Exercises every acceptance criterion before first visible chunk:
- voluntary PC action without declaration
- cross-player control (ownership)
- invented entity / duplicate identity
- unsupported player theory / lying NPC promotion
- private leak in shared audience
- stale canon contradiction
- valid involuntary consequence
- typed identity, provenance, fail-closed, regeneration feedback, extension point
"""

import uuid

import pytest

from app.dm.contract import CONTRACT_VERSION, normalize_contract
from app.dm.context import AuthorizationScope, ContextAudience, ContextRecord, LaneName, SourceRef, assemble_context_packet
import app.dm.validators as validators_mod
from app.dm.validators import (
    ValidatorError,
    ValidatorRejectionError,
    format_rejection_for_retry,
    run_with_bounded_regeneration,
    validate_contract,
    ValidatorPipeline,
)


def _packet(*, campaign_id=None, thread_id=None, audience="campaign", user_ids=None, extra_records=None, extra_status=None):
    cid = campaign_id or str(uuid.uuid4())
    tid = thread_id or str(uuid.uuid4())
    aud = ContextAudience(campaign_id=cid, thread_id=tid, audience=audience, user_ids=user_ids or [str(uuid.uuid4())])
    records = {lane: [] for lane in LaneName}
    # default not_applicable for optional lanes that would otherwise be unavailable
    status = {
        LaneName.CURRENT_SCENE: "not_applicable",
        LaneName.KNOWLEDGE_VISIBILITY: "not_applicable",
        LaneName.RELEVANT_CANON: "not_applicable",
        LaneName.REPAIR_DIRECTIVES: "not_applicable",
    }
    if extra_status:
        status.update(extra_status)
    if extra_records:
        for k, v in extra_records.items():
            records[k] = list(v)
    pkt = assemble_context_packet(audience=aud, records=records, lane_status=status)
    return pkt, cid, tid


def _known(pkt, ids):
    """Packet with an entity-registry record naming ``ids`` as canonical."""
    entities = []
    for entry in ids:
        low = str(entry).strip().lower()
        prefix = low.split(":", 1)[0] if ":" in low else ""
        kind = "character" if prefix in ("character", "char") else (
            prefix if prefix in ("npc", "location", "object", "entity") else None)
        entities.append({"id": str(entry), "kind": kind})
    if not entities:
        return pkt
    registry = ContextRecord(
        record_id="entity-registry:test", required=False, priority=10,
        value={"entities": entities},
        sources=[SourceRef(source_type="world_entity", source_id="registry", source_version="1")],
        authorization=AuthorizationScope(campaign_id=pkt.audience.campaign_id),
        visibility="dm_only", use="adjudication_only",
    )
    records = {lane.name: list(lane.records) for lane in pkt.lanes}
    records[LaneName.RELEVANT_CANON].append(registry)
    status = {lane.name: lane.authority_status for lane in pkt.lanes}
    status[LaneName.RELEVANT_CANON] = "authoritative"
    return assemble_context_packet(audience=pkt.audience, records=records, lane_status=status)


def _regen_with(pipe, *args, **kwargs):
    original = validators_mod.default_pipeline
    validators_mod.default_pipeline = pipe
    try:
        return run_with_bounded_regeneration(*args, **kwargs)
    finally:
        validators_mod.default_pipeline = original


def _base(beats, **over):
    raw = {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": beats}
    raw.update(over)
    return normalize_contract(raw)


def test_voluntary_pc_action_without_declaration_rejected():
    pkt, _, _ = _packet(user_ids=[str(uuid.uuid4())])
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara casts fireball", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}}]}])
    r = validate_contract(c, _known(pkt, {"character:char:elara", "char:elara"}))
    assert not r.passed
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r.violations)
    # before first visible chunk: public_projection would still hide, but validator already failed
    assert r.results[0].latency_ms >= 0


def test_valid_player_declaration_passes():
    pkt, _, _ = _packet()
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares she inspects", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["submission:s1"], "trigger_refs": []}]}])
    # provenance needs packet with that submission id to pass, so use resolver_evidence path or allow without packet for this unit test
    # For this specific test, skip provenance strictness by not providing packet with known_sources
    # Actually AgencyValidator doesn't need packet, so this should pass agency but provenance will fail without packet
    # So test with packet that has the source
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    aud = ContextAudience(campaign_id=cid, thread_id=tid, audience="campaign", user_ids=[str(uuid.uuid4())])
    rec = ContextRecord(record_id="submission:s1", value={"submission_id": "s1"}, sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt2, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PLAYER_INPUTS: [rec]})
    r = validate_contract(c, _known(pkt2, {"char:elara"}))
    # agency should pass; provenance should pass because evidence_refs now in known_sources (record_id)
    assert r.passed or all(v.category != "agency" for v in r.violations)


def test_valid_involuntary_consequence_passes():
    # Need packet with submission:s1 for provenance to pass
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    rec = ContextRecord(record_id="submission:s1", value={"submission_id": "s1"}, sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PLAYER_INPUTS: [rec]})
    # Constrained dice outcome is the only character-authored non-declaration that is allowed
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara is shoved prone by the guard", "claim_kind": "roll_outcome", "origin": "roll_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}, "roll_request_id": "roll_1"}]}])
    r = validate_contract(c, _known(pkt, {"char:elara"}))
    assert r.passed, [v.code for v in r.violations]

    # Correct modeling for other imposed consequences is npc actor with pc as target, not pc actor.
    # The knowledge lane covers the guard's acquaintance with Elara (#251).
    know = ContextRecord(record_id="knowledge:npc:guard", value={"character_id": "", "subject_entity_id": "npc:guard", "subject_resolved": True, "perspective": "npc", "entries": [{"knowledge_id": "k1", "target_kind": "entity", "target_id": "char:elara", "knowledge_state": "knows", "acquisition_source": "direct_observation", "visibility": "dm_only"}], "total": 1, "truncated": False}, sources=[SourceRef(source_type="world_entity", source_id="npc:guard", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="dm_only", use="adjudication_only")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PLAYER_INPUTS: [rec], LaneName.KNOWLEDGE_VISIBILITY: [know]}, extra_status={LaneName.KNOWLEDGE_VISIBILITY: "authoritative"})
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Guard shoves Elara prone", "claim_kind": "observation", "origin": "dm_adjudication", "actor_ref": {"type": "npc", "id": "npc:guard"}, "target_refs": [{"type": "character", "id": "char:elara"}], "trigger_refs": ["s1"]}]}])
    r2 = validate_contract(c2, _known(pkt, {"npc:guard", "char:elara"}))
    assert r2.passed


def test_voluntary_movement_and_attack_still_rejected():
    pkt, _, _ = _packet()
    # Voluntary movement as world_fact with character actor must be rejected even with trigger
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara moved to the door", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}, "trigger_refs": ["submission:s1"]}]}])
    r = validate_contract(c, _known(pkt, {"char:elara"}))
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r.violations)

    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara hit the guard", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}, "target_refs": [{"type": "npc", "id": "npc:guard"}]}]}])
    r2 = validate_contract(c2, _known(pkt, {"char:elara", "npc:guard"}))
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r2.violations)

    # Substring "hit" inside "white" must not bypass — still voluntary
    c3 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara admires the white wall", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}}]}])
    r3 = validate_contract(c3, _known(pkt, {"char:elara"}))
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r3.violations)


def test_voluntary_action_mislabeled_resolver_evidence_still_rejected():
    # Structured bypass attempt: voluntary attack as world_fact with resolver_evidence and valid ref
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    rec = ContextRecord(record_id="evidence:valid", value={"fact": "some evidence"}, sources=[SourceRef(source_type="evidence", source_id="valid", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.RELEVANT_CANON: [rec]})
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara attacks the guard", "claim_kind": "world_fact", "origin": "resolver_evidence", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["evidence:valid"]}]}])
    r = validate_contract(c, _known(pkt, {"char:elara", "npc:guard"}))
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r.violations)


def test_cross_player_control_rejected():
    owner = str(uuid.uuid4()); other = str(uuid.uuid4())
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    aud = ContextAudience(campaign_id=cid, thread_id=tid, audience="campaign", user_ids=[owner, other])
    pc = ContextRecord(record_id="pc-control:char:bob", value={"character_id": "char:bob", "owner_user_id": owner}, sources=[SourceRef(source_type="character", source_id="char:bob", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), use="adjudication_only", required=True, priority=100)
    sub = ContextRecord(record_id="submission:s1", value={"submission_id": "s1", "sequence": 1, "user_id": other, "character_id": "char:bob", "segments": [{"position": 0, "segment_type": "ic", "text": "I act"}]}, sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid, thread_ids=[tid]), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, user_ids=[owner, other], extra_records={LaneName.PROTECTED_PCS: [pc], LaneName.PLAYER_INPUTS: [sub]}, extra_status={LaneName.CHARACTER_STATE: "not_applicable", LaneName.RULESET_IDENTITY: "not_applicable"})
    c = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Bob declares", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:bob"}, "evidence_refs": ["s1"]}]} ], "adjudication_input": {"submission_ids": ["s1"], "segments": [{"position": 0, "segment_type": "ic", "text": "I act"}]}})
    r = validate_contract(c, _known(pkt, {"char:bob"}))
    assert not r.passed
    assert any(v.category == "ownership" for v in r.violations)


def test_invented_entity_rejected_and_new_entity_allowed():
    pkt, _, _ = _packet()
    # unknown canonical id
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See dragon", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "npc:unknown_dragon"}]}]}])
    r = validate_contract(c, _known(pkt, {"npc:known"}))
    assert not r.passed
    assert any(v.code == "unknown_canonical_id" for v in r.violations)

    # typed allowlist prevents submission ID reuse as entity
    pkt2, cid2, tid2 = _packet()
    sub = ContextRecord(record_id="submission:sub-uuid-123", value={"submission_id": "sub-uuid-123"}, sources=[SourceRef(source_type="player_submission", source_id="sub-uuid-123", source_version="1")], authorization=AuthorizationScope(campaign_id=cid2), visibility="campaign")
    # rebuild packet with that submission
    pkt3, _, _ = _packet(campaign_id=cid2, thread_id=tid2, extra_records={LaneName.PLAYER_INPUTS: [sub]})
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "sub-uuid-123"}]}]}])
    # Even though submission ID is known as a source, it is not a typed entity, so entity validator should reject
    r2 = validate_contract(c2, _known(pkt3, {"character:char:elara"}))
    assert any(v.code in ("unknown_canonical_id", "missing_identity_authority") for v in r2.violations)

    # new entity proposal passes
    c3 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state"}]} ], new_entities=[{"temp_id": "tmp_npc_1", "kind": "npc", "public_name": "New Dragon"}])
    r3 = validate_contract(c3, _known(pkt, {"npc:known"}))
    assert r3.passed

    # missing identity authority fails closed when refs exist but no allowlist
    c4 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "npc:anything"}]}]}])
    r4 = validate_contract(c4, _known(pkt, set()))
    # pkt has no typed entities, so should fail closed with missing_identity_authority
    assert any(v.code == "missing_identity_authority" for v in r4.violations)

    # typed raw escape: character:123 must not authorize npc with same raw id
    c5 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "character:123"}]}]}])
    r5 = validate_contract(c5, _known(pkt, {"character:123"}))
    assert any(v.code == "unknown_canonical_id" for v in r5.violations)
    # bare id 123 should authorize npc 123 (bare wildcard)
    c6 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "123"}]}]}])
    r6 = validate_contract(c6, _known(pkt, {"123"}))
    assert not any(v.code == "unknown_canonical_id" for v in r6.violations)


def test_provenance_unknown_source_ref_rejected():
    pkt, cid, tid = _packet()
    rec = ContextRecord(record_id="submission:known-s1", value={"submission_id": "known-s1"}, sources=[SourceRef(source_type="player_submission", source_id="known-s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt2, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PLAYER_INPUTS: [rec]})
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Fact", "claim_kind": "world_fact", "origin": "resolver_evidence", "evidence_refs": ["evidence:hallucinated"]}]}])
    r = validate_contract(c, pkt2)
    assert any(v.code == "unknown_source_ref" for v in r.violations)
    # valid ref passes
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Fact", "claim_kind": "world_fact", "origin": "resolver_evidence", "evidence_refs": ["known-s1"]}]}])
    r2 = validate_contract(c2, pkt2)
    assert not any(v.code == "unknown_source_ref" for v in r2.violations)


def test_unsupported_player_theory_promoted_rejected():
    pkt, _, _ = _packet()
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Theory X", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:a"}, "evidence_refs": ["s1"]}, {"text": "Theory X", "claim_kind": "world_fact", "origin": "dm_adjudication"}]}])
    r = validate_contract(c, _known(pkt, {"char:a"}))
    assert any(v.code == "player_claim_promoted_to_fact" for v in r.violations)
    # with evidence, promotion is allowed
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Theory X", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:a"}, "evidence_refs": ["s1"]}, {"text": "Theory X", "claim_kind": "world_fact", "origin": "resolver_evidence", "evidence_refs": ["evidence:1"]}]}])
    r2 = validate_contract(c2, _known(pkt, {"char:a"}))
    assert not any(v.code == "player_claim_promoted_to_fact" for v in r2.violations)


def test_lying_npc_promoted_rejected():
    pkt, _, _ = _packet()
    c = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": [{"id": "beat_1", "type": "npc_dialogue", "speaker_ref": {"type": "npc", "id": "npc:liar"}, "speaker_public_name": "Liar", "claims": [{"text": "Gold is free", "claim_kind": "npc_utterance", "origin": "dm_adjudication", "actor_ref": {"type": "npc", "id": "npc:liar"}}], "truth_status": "deceptive", "dm_private_context": "Lying"}, {"id": "beat_2", "type": "narration", "claims": [{"text": "Gold is free", "claim_kind": "world_fact", "origin": "dm_adjudication"}]}]})
    r = validate_contract(c, _known(pkt, {"npc:liar"}))
    assert any(v.code == "npc_utterance_promoted_to_fact" for v in r.violations)


def test_private_leak_in_shared_audience_rejected_and_private_allowed():
    pkt, _, _ = _packet(audience="campaign")
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Private secret", "claim_kind": "observation", "origin": "established_state", "visibility": "dm_private"}]}])
    r = validate_contract(c, pkt)
    assert any(v.category == "visibility" for v in r.violations)
    assert any(v.code == "private_fact_in_shared_audience" for v in r.violations)

    # private audience allows same claim
    pkt_priv, _, _ = _packet(audience="private")
    r2 = validate_contract(c, pkt_priv)
    # private audience should not flag visibility; only shared does
    assert r2.passed or not any(v.code == "private_fact_in_shared_audience" for v in r2.violations)


def test_stale_canon_contradiction_from_packet():
    # packet-derived canon: seal is intact
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    aud = ContextAudience(campaign_id=cid, thread_id=tid, audience="campaign", user_ids=[str(uuid.uuid4())])
    canon_rec = ContextRecord(record_id="canon:seal", value={"text": "The seal is intact", "fact": "seal is intact"}, sources=[SourceRef(source_type="canon", source_id="seal", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.RELEVANT_CANON: [canon_rec]})
    # claim that seal was cracked without evidence contradicts packet canon
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "The seal was cracked before presentation", "claim_kind": "world_fact", "origin": "dm_adjudication"}]}])
    r = validate_contract(c, pkt)
    assert any(v.code == "canon_contradiction" for v in r.violations)
    # with evidence, contradiction is adjudicated and allowed
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "The seal was cracked before presentation", "claim_kind": "world_fact", "origin": "resolver_evidence", "evidence_refs": ["evidence:1"]}]}])
    r2 = validate_contract(c2, pkt)
    assert not any(v.code == "canon_contradiction" for v in r2.violations)


def test_fail_closed_on_validator_execution_error():
    pkt, _, _ = _packet()
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Hello", "claim_kind": "observation", "origin": "established_state"}]}])
    class Bad:
        name = "bad"; category = "test"
        def validate(self, *a, **kw):
            raise RuntimeError("boom")
    pipe = ValidatorPipeline(validators=[Bad()])
    try:
        pipe.validate(c, pkt)
        assert False, "should raise"
    except ValidatorError:
        pass


def test_rejection_structured_and_regeneration_wires_feedback():
    pkt, _, _ = _packet()
    c_bad = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara casts fireball", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}}]}])
    r = validate_contract(c_bad, _known(pkt, {"char:elara"}))
    assert not r.passed
    fb = format_rejection_for_retry(r)
    assert r.correlation_id in fb
    assert "voluntary_pc_action" in fb

    # regeneration: first attempt bad, second good, feedback is fed
    calls = []
    def adjudicate(packet=None, feedback=None):
        calls.append(feedback)
        if len(calls) == 1:
            return c_bad
        # second call should receive feedback string
        assert feedback is not None and "voluntary_pc_action" in feedback
        # also packet should be augmented with repair_directives when packet is provided
        if packet is not None:
            has_repair = any(lane.name == LaneName.REPAIR_DIRECTIVES and lane.records for lane in packet.lanes)
            assert has_repair
        return _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares she waits", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["s1"], "trigger_refs": []}]}])
    # need a packet with that submission for provenance to pass on second try
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    rec = ContextRecord(record_id="submission:s1", value={"submission_id": "s1"}, sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt2, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PLAYER_INPUTS: [rec]})
    contract, report = run_with_bounded_regeneration(adjudicate, _known(pkt2, {"char:elara"}), max_regenerations=3)
    assert report.passed
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None

    # exhausting retries surfaces rejection
    def always_bad(packet=None, feedback=None):
        return c_bad
    try:
        run_with_bounded_regeneration(always_bad, _known(pkt, {"char:elara"}), max_regenerations=1)
        assert False
    except ValidatorRejectionError:
        pass


def test_observability_latency_per_validator():
    pkt, _, _ = _packet()
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["s1"]}]}])
    r = validate_contract(c, _known(pkt, {"char:elara"}))
    assert all(res.latency_ms >= 0 for res in r.results)
    assert r.total_latency_ms > 0
    assert len(r.results) == len(ValidatorPipeline().validators)


def test_canon_unrelated_antonym_not_flagged():
    # Canon says seal is intact, unrelated claim about door being cracked should not be flagged
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    canon_rec = ContextRecord(record_id="canon:seal", value={"text": "The seal is intact"}, sources=[SourceRef(source_type="canon", source_id="seal", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.RELEVANT_CANON: [canon_rec]})
    # Unrelated subject: door cracked — shares "cracked" but not subject "seal"
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "The wooden door is cracked", "claim_kind": "world_fact", "origin": "dm_adjudication"}]}])
    r = validate_contract(c, pkt)
    assert not any(v.code == "canon_contradiction" for v in r.violations)
    # Related subject should still be flagged
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "The seal is cracked", "claim_kind": "world_fact", "origin": "dm_adjudication"}]}])
    r2 = validate_contract(c2, pkt)
    assert any(v.code == "canon_contradiction" for v in r2.violations)


def test_entity_typed_vs_bare_allowlist():
    pkt, _, _ = _packet()
    # Packet has character:123 as typed entity
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    pc = ContextRecord(record_id="pc-control:char:123", value={"character_id": "char:123", "owner_user_id": str(uuid.uuid4())}, sources=[SourceRef(source_type="character", source_id="char:123", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), use="adjudication_only", required=True, priority=100)
    pkt_typed, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PROTECTED_PCS: [pc]}, extra_status={LaneName.CHARACTER_STATE: "not_applicable", LaneName.RULESET_IDENTITY: "not_applicable"})
    # npc with same numeric id 123 should NOT be authorized by character:123
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "123"}]}]}])
    r = validate_contract(c, _known(pkt_typed, set()))
    assert any(v.code == "unknown_canonical_id" for v in r.violations)
    # Bare caller-supplied id 123 should authorize any type
    r2 = validate_contract(c, _known(pkt_typed, {"123"}))
    assert r2.passed or not any(v.code == "unknown_canonical_id" for v in r2.violations)


def test_provenance_includes_adjudication_input():
    # adjudication_input submission_ids should be considered authoritative even if not in packet lanes
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid)
    # No PLAYER_INPUTS lane, but contract carries adjudication_input
    c = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["sub-xyz"], "trigger_refs": []}]}], "adjudication_input": {"submission_ids": ["sub-xyz"], "segments": [{"position": 0, "segment_type": "ic", "text": "I act"}]}})
    # With packet present, sub-xyz is in adjudication_input, so should NOT be flagged as unknown_source_ref
    r = validate_contract(c, _known(pkt, {"char:elara"}))
    assert not any(v.code == "unknown_source_ref" for v in r.violations)
    # Hallucinated ref not in adjudication_input should be flagged
    c2 = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["hallucinated"], "trigger_refs": []}]}], "adjudication_input": {"submission_ids": ["sub-xyz"], "segments": [{"position": 0, "segment_type": "ic", "text": "I act"}]}})
    r2 = validate_contract(c2, _known(pkt, {"char:elara"}))
    assert any(v.code == "unknown_source_ref" for v in r2.violations)


def test_packet_only_retry_fixture():
    # Packet-only adjudicator must receive packet (augmented) on retry, not a string
    pkt, cid, tid = _packet()
    rec = ContextRecord(record_id="submission:s1", value={"submission_id": "s1"}, sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PLAYER_INPUTS: [rec]})
    c_bad = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara casts fireball", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}}]}])
    c_good = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares she waits", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["s1"]}]}])
    calls = []

    def packet_only_adjudicate(packet, feedback=None):
        calls.append(type(packet).__name__ if packet else None)
        if len(calls) == 1:
            assert isinstance(packet, type(pkt))
            return c_bad
        # second call should still be packet, but augmented with repair_directives
        assert isinstance(packet, type(pkt))
        assert any(lane.name == LaneName.REPAIR_DIRECTIVES and lane.records for lane in packet.lanes)
        return c_good

    contract, report = run_with_bounded_regeneration(packet_only_adjudicate, _known(pkt, {"char:elara"}), max_regenerations=2)
    assert report.passed
    assert calls == [type(pkt).__name__, type(pkt).__name__]


def test_structural_normalization_error_retries_with_feedback():
    # A structurally invalid contract (empty beats in respond mode) must
    # retry with explicit feedback instead of failing on the first shot.
    pkt, _, _ = _packet()
    bad = {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": []}
    good = {"contract_version": CONTRACT_VERSION, "mode": "silent", "reason": "x", "beats": []}
    calls = []

    def flaky(packet, feedback):
        calls.append(feedback)
        return bad if len(calls) == 1 else good

    contract, report = run_with_bounded_regeneration(flaky, pkt, max_regenerations=2)
    assert report.passed
    assert contract.mode == "silent"
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None and "beats" in calls[1]


def _knowledge_packet_newcomer_without_perspective():
    # Issue #455: guard has a resolved perspective; the newly introduced
    # newcomer has none, so their knowledge claims fail closed.
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    know = ContextRecord(record_id="knowledge:npc:guard", value={"character_id": "", "subject_entity_id": "npc:guard", "subject_resolved": True, "perspective": "npc", "entries": [{"knowledge_id": "k1", "target_kind": "entity", "target_id": "char:elara", "knowledge_state": "knows", "acquisition_source": "direct_observation", "visibility": "dm_only"}], "total": 1, "truncated": False}, sources=[SourceRef(source_type="world_entity", source_id="npc:guard", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="dm_only", use="adjudication_only")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.KNOWLEDGE_VISIBILITY: [know]}, extra_status={LaneName.KNOWLEDGE_VISIBILITY: "authoritative"})
    return pkt


def _newcomer_dialogue_beat():
    return {"id": "beat_2", "type": "npc_dialogue", "speaker_ref": {"type": "npc", "id": "npc:newcomer"}, "speaker_public_name": "Hooded traveler", "truth_status": "truthful", "claims": [{"text": "The old well is poisoned", "claim_kind": "npc_utterance", "actor_ref": {"type": "npc", "id": "npc:newcomer"}, "topic_refs": [{"type": "object", "id": "well:1"}], "origin": "dm_adjudication"}]}


def _quiet_narration_beat():
    return {"id": "beat_1", "type": "narration", "claims": [{"text": "The room falls quiet", "claim_kind": "observation", "origin": "dm_adjudication"}]}


def test_missing_perspective_narrows_without_model_retry():
    # Issue #455: pure npc_utterance_ambiguous_knowledge must narrow
    # deterministically on attempt 1 — zero additional model calls.
    from app.dm.validators import KnowledgeValidator
    pkt = _knowledge_packet_newcomer_without_perspective()
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    bad = _base([_quiet_narration_beat(), _newcomer_dialogue_beat()])
    calls = []

    def adjudicate(packet, feedback):
        calls.append(feedback)
        return bad

    contract, report = _regen_with(pipe, adjudicate, pkt, max_regenerations=3)
    assert report.passed
    assert len(calls) == 1
    assert contract.mode == "respond"
    assert len(contract.beats) == 1
    assert contract.beats[0].type == "narration"


def test_missing_perspective_all_offending_degrades_to_silent():
    # Nothing salvageable: degrade to a silent contract, still 1 model call.
    from app.dm.validators import KnowledgeValidator
    pkt = _knowledge_packet_newcomer_without_perspective()
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    bad = _base([_newcomer_dialogue_beat()])
    calls = []

    def adjudicate(packet, feedback):
        calls.append(feedback)
        return bad

    contract, report = _regen_with(pipe, adjudicate, pkt, max_regenerations=3)
    assert report.passed
    assert len(calls) == 1
    assert contract.mode == "silent"
    assert contract.beats == []


_GUARD_LEARNS_IDS = (str(uuid.uuid4()), str(uuid.uuid4()))


def _guard_learns_effect():
    return [{"id": "eff-guard-1", "effect_type": "transfer_knowledge", "arguments": {"subject_kind": "npc", "subject_entity_id": _GUARD_LEARNS_IDS[0], "target_kind": "entity", "target_entity_id": _GUARD_LEARNS_IDS[1]}}]


def test_missing_perspective_never_silences_turn_with_effects():
    # Issue #455 review: silencing would drop the staged effect, so the model
    # gets a normal retry instead and the effect survives.
    from app.dm.validators import KnowledgeValidator
    pkt = _knowledge_packet_newcomer_without_perspective()
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    bad = _base([_newcomer_dialogue_beat()], staged_effects=_guard_learns_effect())
    good = _base([_quiet_narration_beat()], staged_effects=_guard_learns_effect())
    calls = []

    def adjudicate(packet, feedback):
        calls.append(feedback)
        return bad if len(calls) == 1 else good

    contract, report = _regen_with(pipe, adjudicate, pkt, max_regenerations=3)
    assert report.passed
    assert len(calls) == 2
    assert "npc_utterance_ambiguous_knowledge" in calls[1]
    assert contract.mode == "respond"
    assert [e.id for e in contract.staged_effects] == ["eff-guard-1"]


def test_missing_perspective_with_effects_fails_visibly_when_budget_exhausted():
    from app.dm.validators import KnowledgeValidator
    pkt = _knowledge_packet_newcomer_without_perspective()
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    bad = _base([_newcomer_dialogue_beat()], staged_effects=_guard_learns_effect())
    calls = []

    def adjudicate(packet, feedback):
        calls.append(feedback)
        return bad

    with pytest.raises(ValidatorRejectionError):
        _regen_with(pipe, adjudicate, pkt, max_regenerations=1)
    assert len(calls) == 2


def test_mixed_violations_still_retry_model_then_narrow():
    # Ambiguous perspective + an unrelated one-time failure: the model still
    # gets its retry for the fixable violation, then the perspective
    # violation narrows deterministically (2 calls total, not 4).
    from app.dm.validators import KnowledgeValidator, ValidationViolation, ValidatorResult
    pkt = _knowledge_packet_newcomer_without_perspective()

    class FlakyOnce:
        name = "flaky_once"; category = "test"
        def __init__(self):
            self.calls = 0
        def validate(self, contract, packet, **kw):
            self.calls += 1
            if self.calls == 1:
                return ValidatorResult(validator=self.name, category=self.category, passed=False, violations=[ValidationViolation(validator=self.name, category=self.category, code="custom_once", message="one-time")], latency_ms=0.1)
            return ValidatorResult(validator=self.name, category=self.category, passed=True, violations=[], latency_ms=0.1)

    pipe = ValidatorPipeline(validators=[KnowledgeValidator(), FlakyOnce()])
    bad = _base([_quiet_narration_beat(), _newcomer_dialogue_beat()])
    calls = []

    def adjudicate(packet, feedback):
        calls.append(feedback)
        return bad

    contract, report = _regen_with(pipe, adjudicate, pkt, max_regenerations=3)
    assert report.passed
    assert len(calls) == 2
    assert len(contract.beats) == 1
    assert contract.beats[0].type == "narration"


def _newcomer_perspective_repair(repairs, known_target):
    """Repair hook supplying the newcomer's stored perspective (knows ``known_target``)."""
    def repair(report, packet):
        repairs.append(packet)
        value = {"character_id": "", "subject_entity_id": "npc:newcomer", "subject_resolved": True, "perspective": "npc", "entries": [{"knowledge_id": "k9", "target_kind": "entity", "target_id": known_target, "knowledge_state": "knows", "acquisition_source": "direct_observation", "visibility": "dm_only"}], "total": 1, "truncated": False}
        rec = ContextRecord(record_id="knowledge:npc:newcomer", value=value, sources=[SourceRef(source_type="world_entity", source_id="npc:newcomer", source_version="1")], authorization=AuthorizationScope(campaign_id=packet.audience.campaign_id), visibility="dm_only", use="adjudication_only")
        if any(r.record_id == rec.record_id for lane in packet.lanes if lane.name == LaneName.KNOWLEDGE_VISIBILITY for r in lane.records):
            return None
        new_packet = packet.model_copy(deep=True)
        for lane in new_packet.lanes:
            if lane.name == LaneName.KNOWLEDGE_VISIBILITY:
                lane.records.append(rec)
        return new_packet
    return repair


def test_perspective_repair_revalidates_same_contract_without_model_call():
    # An NPC the DM brought into the turn: the hook supplies its stored
    # perspective, which covers the claim, so the same contract passes and
    # the NPC keeps their dialogue — 1 call, zero narrowing.
    from app.dm.validators import KnowledgeValidator
    pkt = _knowledge_packet_newcomer_without_perspective()
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    bad = _base([_newcomer_dialogue_beat()])
    calls = []
    repairs = []

    def adjudicate(packet, feedback):
        calls.append(feedback)
        return bad

    repair = _newcomer_perspective_repair(repairs, "well:1")
    contract, report = _regen_with(pipe, adjudicate, pkt, packet_repair=repair, max_regenerations=3)
    assert report.passed
    assert len(calls) == 1
    assert len(repairs) == 1
    assert len(contract.beats) == 1
    assert contract.beats[0].type == "npc_dialogue"


def test_perspective_repair_retries_when_perspective_does_not_cover_claim():
    # The repaired perspective resolves the NPC but not the claim's topic:
    # the model retries once against the repaired packet, with the specific
    # rejection as feedback.
    from app.dm.validators import KnowledgeValidator
    pkt = _knowledge_packet_newcomer_without_perspective()
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    bad = _base([_newcomer_dialogue_beat()])
    good = _base([_quiet_narration_beat()])
    calls = []
    repairs = []

    def adjudicate(packet, feedback):
        calls.append(feedback)
        return bad if len(calls) == 1 else good

    repair = _newcomer_perspective_repair(repairs, "other:1")
    contract, report = _regen_with(pipe, adjudicate, pkt, packet_repair=repair, max_regenerations=3)
    assert report.passed
    assert len(calls) == 2
    assert len(repairs) == 1
    assert "npc_utterance_without_knowledge" in calls[1]
    assert contract.beats[0].type == "narration"


def test_initial_contract_is_validated_before_any_model_call():
    from app.dm.validators import KnowledgeValidator
    pkt = _knowledge_packet_newcomer_without_perspective()
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    calls = []

    def adjudicate(packet, feedback):
        calls.append(feedback)
        return _base([_quiet_narration_beat()])

    initial = _base([_newcomer_dialogue_beat()])
    repair = _newcomer_perspective_repair([], "well:1")
    contract, report = _regen_with(
        pipe, adjudicate, pkt, packet_repair=repair, max_regenerations=3, initial_contract=initial,
    )
    assert report.passed
    assert calls == []
    assert contract.beats[0].type == "npc_dialogue"


def test_current_scene_location_is_typed_identity_authority():
    cid, location_id = str(uuid.uuid4()), str(uuid.uuid4())
    scene = ContextRecord(
        record_id=f"current-scene:{cid}",
        value={"campaign_id": cid, "location_entity_id": location_id, "location_name": "Tavern"},
        sources=[SourceRef(source_type="campaign_current_scene", source_id=cid, source_version="1")],
        authorization=AuthorizationScope(campaign_id=cid),
    )
    packet, _, _ = _packet(campaign_id=cid, extra_records={LaneName.CURRENT_SCENE: [scene]},
                           extra_status={LaneName.CURRENT_SCENE: "authoritative"})
    from app.dm.validators import EntityValidator
    validator = EntityValidator()
    def contract(ref):
        return _base([{"id": "scene", "type": "narration", "claims": [{
            "text": "Fog lies beyond the tavern.", "claim_kind": "observation",
            "origin": "established_state", "topic_refs": [ref],
        }]}])
    assert validator.validate(contract({"type": "location", "id": location_id}), packet).passed
    for ref in [{"type": "npc", "id": location_id}, {"type": "location", "id": cid},
                {"type": "location", "id": "Tavern"}, {"type": "location", "id": str(uuid.uuid4())}]:
        assert not validator.validate(contract(ref), packet).passed


def test_scene_present_npc_is_identity_authority_without_registry():
    # Playtest 2026-10-03: the scene showed an introduced NPC's entity_id
    # (#459) while the optional registry had been budgeted out, and every
    # reference to that NPC was refused as unknown_canonical_id.
    cid, npc_id = str(uuid.uuid4()), str(uuid.uuid4())
    scene = ContextRecord(
        record_id=f"current-scene:{cid}",
        value={"campaign_id": cid, "location_name": "Market", "present_actors": [
            {"kind": "pc", "name": "Wren"},
            {"kind": "npc", "name": "Rider Captain", "entity_id": npc_id},
        ]},
        sources=[SourceRef(source_type="campaign_current_scene", source_id=cid, source_version="1")],
        authorization=AuthorizationScope(campaign_id=cid),
    )
    packet, _, _ = _packet(campaign_id=cid, extra_records={LaneName.CURRENT_SCENE: [scene]},
                           extra_status={LaneName.CURRENT_SCENE: "authoritative"})
    from app.dm.validators import EntityValidator
    validator = EntityValidator()
    def contract(ref):
        return _base([{"id": "b1", "type": "narration", "claims": [{
            "text": "The captain watches.", "claim_kind": "observation",
            "origin": "established_state", "topic_refs": [ref],
        }]}])
    assert validator.validate(contract({"type": "npc", "id": npc_id}), packet).passed
    assert not validator.validate(contract({"type": "location", "id": npc_id}), packet).passed


def _scene_npc_observation_packet(location_id, perspective_entries, acting_character_id=None):
    cid = str(uuid.uuid4())
    knowledge = ContextRecord(
        record_id="knowledge:npc:traveler",
        value={"character_id": "", "subject_entity_id": "npc:traveler", "subject_resolved": True,
               "perspective": "npc", "entries": perspective_entries, "total": len(perspective_entries),
               "truncated": False},
        sources=[SourceRef(source_type="world_entity", source_id="npc:traveler", source_version="1")],
        authorization=AuthorizationScope(campaign_id=cid), visibility="dm_only", use="adjudication_only",
    )
    scene = ContextRecord(
        record_id=f"current-scene:{cid}",
        value={"campaign_id": cid, "location_entity_id": location_id, "location_name": "Cinderfell Chapel"},
        sources=[SourceRef(source_type="campaign_current_scene", source_id=cid, source_version="1")],
        authorization=AuthorizationScope(campaign_id=cid),
    )
    records = {LaneName.KNOWLEDGE_VISIBILITY: [knowledge], LaneName.CURRENT_SCENE: [scene]}
    if acting_character_id:
        records[LaneName.PLAYER_INPUTS] = [ContextRecord(
            record_id="submission:s1",
            value={"submission_id": "s1", "character_id": acting_character_id,
                   "segments": [{"position": 0, "segment_type": "ic", "text": "Who are you?"}]},
            sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")],
            authorization=AuthorizationScope(campaign_id=cid),
        )]
    pkt, _, _ = _packet(
        campaign_id=cid, extra_records=records,
        extra_status={LaneName.KNOWLEDGE_VISIBILITY: "authoritative", LaneName.CURRENT_SCENE: "authoritative"},
    )
    return pkt


def _npc_observation_at(location_id):
    return _base([{"id": "beat_1", "type": "narration", "claims": [{
        "text": "The hooded traveler edges into the lantern light.",
        "claim_kind": "observation", "origin": "dm_adjudication",
        "actor_ref": {"type": "npc", "id": "npc:traveler"},
        "location_ref": {"type": "location", "id": location_id},
    }]}])


def test_npc_claim_in_current_scene_location_needs_no_location_knowledge():
    from app.dm.validators import KnowledgeValidator
    pkt = _scene_npc_observation_packet("loc:chapel", [])
    result = KnowledgeValidator().validate(_npc_observation_at("loc:chapel"), pkt)
    assert result.passed, result.violations


def test_npc_claim_set_elsewhere_still_needs_location_knowledge():
    from app.dm.validators import KnowledgeValidator
    pkt = _scene_npc_observation_packet("loc:chapel", [])
    result = KnowledgeValidator().validate(_npc_observation_at("loc:crypt"), pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_without_knowledge"
    assert result.violations[0].details["unknown"] == ["loc:crypt"]


def _npc_line_to(character_id, topic_id=None):
    claim = {
        "text": "I don't know if the way down is still open.",
        "claim_kind": "npc_utterance", "origin": "dm_adjudication",
        "actor_ref": {"type": "npc", "id": "npc:traveler"},
        "target_refs": [{"type": "character", "id": character_id}],
    }
    if topic_id:
        claim["topic_refs"] = [{"type": "location", "id": topic_id}]
    return _base([{
        "id": "beat_1", "type": "npc_dialogue", "speaker_ref": {"type": "npc", "id": "npc:traveler"},
        "speaker_public_name": "Hooded traveler", "truth_status": "truthful", "claims": [claim],
    }])


def test_npc_addressing_acting_pc_about_current_location_is_co_presence():
    from app.dm.validators import KnowledgeValidator
    pkt = _scene_npc_observation_packet("loc:chapel", [], acting_character_id="char:rowan")
    result = KnowledgeValidator().validate(_npc_line_to("char:rowan", topic_id="loc:chapel"), pkt)
    assert result.passed, result.violations


def test_npc_referencing_pc_not_acting_this_turn_still_needs_knowledge():
    from app.dm.validators import KnowledgeValidator
    pkt = _scene_npc_observation_packet("loc:chapel", [], acting_character_id="char:rowan")
    result = KnowledgeValidator().validate(_npc_line_to("char:absent"), pkt)
    assert not result.passed
    assert result.violations[0].details["unknown"] == ["char:absent"]


def test_explicit_does_not_know_beats_co_presence():
    from app.dm.validators import KnowledgeValidator
    denied = [{"knowledge_id": "k1", "target_kind": "entity", "target_id": "char:rowan",
               "knowledge_state": "does_not_know", "acquisition_source": "dm_adjudication", "visibility": "dm_only"}]
    pkt = _scene_npc_observation_packet("loc:chapel", denied, acting_character_id="char:rowan")
    result = KnowledgeValidator().validate(_npc_line_to("char:rowan"), pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_denied_knowledge"
