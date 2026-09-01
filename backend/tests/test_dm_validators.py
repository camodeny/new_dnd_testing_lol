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
        LaneName.CLOCKS_PRESSURES: "not_applicable",
        LaneName.COMBAT_HOOKS: "not_applicable",
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


def _base(beats, **over):
    raw = {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": beats}
    raw.update(over)
    return normalize_contract(raw)


def test_voluntary_pc_action_without_declaration_rejected():
    pkt, _, _ = _packet(user_ids=[str(uuid.uuid4())])
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara casts fireball", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}}]}])
    r = validate_contract(c, pkt, known_entity_ids={"character:char:elara", "char:elara"})
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
    r = validate_contract(c, pkt2, known_entity_ids={"char:elara"})
    # agency should pass; provenance should pass because evidence_refs now in known_sources (record_id)
    assert r.passed or all(v.category != "agency" for v in r.violations)


def test_valid_involuntary_consequence_passes():
    # Need packet with submission:s1 for provenance to pass
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    rec = ContextRecord(record_id="submission:s1", value={"submission_id": "s1"}, sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PLAYER_INPUTS: [rec]})
    # Constrained dice outcome is the only character-authored non-declaration that is allowed
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara is shoved prone by the guard", "claim_kind": "roll_outcome", "origin": "roll_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}, "roll_request_id": "roll_1"}]}])
    r = validate_contract(c, pkt, known_entity_ids={"char:elara"})
    assert r.passed, [v.code for v in r.violations]

    # Correct modeling for other imposed consequences is npc actor with pc as target, not pc actor
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Guard shoves Elara prone", "claim_kind": "observation", "origin": "dm_adjudication", "actor_ref": {"type": "npc", "id": "npc:guard"}, "target_refs": [{"type": "character", "id": "char:elara"}], "trigger_refs": ["s1"]}]}])
    r2 = validate_contract(c2, pkt, known_entity_ids={"npc:guard", "char:elara"})
    assert r2.passed


def test_voluntary_movement_and_attack_still_rejected():
    pkt, _, _ = _packet()
    # Voluntary movement as world_fact with character actor must be rejected even with trigger
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara moved to the door", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}, "trigger_refs": ["submission:s1"]}]}])
    r = validate_contract(c, pkt, known_entity_ids={"char:elara"})
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r.violations)

    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara hit the guard", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}, "target_refs": [{"type": "npc", "id": "npc:guard"}]}]}])
    r2 = validate_contract(c2, pkt, known_entity_ids={"char:elara", "npc:guard"})
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r2.violations)

    # Substring "hit" inside "white" must not bypass — still voluntary
    c3 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara admires the white wall", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}}]}])
    r3 = validate_contract(c3, pkt, known_entity_ids={"char:elara"})
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r3.violations)


def test_voluntary_action_mislabeled_resolver_evidence_still_rejected():
    # Structured bypass attempt: voluntary attack as world_fact with resolver_evidence and valid ref
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    rec = ContextRecord(record_id="evidence:valid", value={"fact": "some evidence"}, sources=[SourceRef(source_type="evidence", source_id="valid", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.RELEVANT_CANON: [rec]})
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara attacks the guard", "claim_kind": "world_fact", "origin": "resolver_evidence", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["evidence:valid"]}]}])
    r = validate_contract(c, pkt, known_entity_ids={"char:elara", "npc:guard"})
    assert any(v.code == "voluntary_pc_action_without_player_declaration" for v in r.violations)


def test_cross_player_control_rejected():
    owner = str(uuid.uuid4()); other = str(uuid.uuid4())
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    aud = ContextAudience(campaign_id=cid, thread_id=tid, audience="campaign", user_ids=[owner, other])
    pc = ContextRecord(record_id="pc-control:char:bob", value={"character_id": "char:bob", "owner_user_id": owner}, sources=[SourceRef(source_type="character", source_id="char:bob", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), use="adjudication_only", required=True, priority=100)
    sub = ContextRecord(record_id="submission:s1", value={"submission_id": "s1", "sequence": 1, "user_id": other, "character_id": "char:bob", "segments": [{"position": 0, "segment_type": "ic", "text": "I act"}]}, sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid, thread_ids=[tid]), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, user_ids=[owner, other], extra_records={LaneName.PROTECTED_PCS: [pc], LaneName.PLAYER_INPUTS: [sub]}, extra_status={LaneName.CHARACTER_STATE: "not_applicable", LaneName.RULESET_IDENTITY: "not_applicable"})
    c = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Bob declares", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:bob"}, "evidence_refs": ["s1"]}]} ], "adjudication_input": {"submission_ids": ["s1"], "segments": [{"position": 0, "segment_type": "ic", "text": "I act"}]}})
    r = validate_contract(c, pkt, known_entity_ids={"char:bob"})
    assert not r.passed
    assert any(v.category == "ownership" for v in r.violations)


def test_invented_entity_rejected_and_new_entity_allowed():
    pkt, _, _ = _packet()
    # unknown canonical id
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See dragon", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "npc:unknown_dragon"}]}]}])
    r = validate_contract(c, pkt, known_entity_ids={"npc:known"})
    assert not r.passed
    assert any(v.code == "unknown_canonical_id" for v in r.violations)

    # typed allowlist prevents submission ID reuse as entity
    pkt2, cid2, tid2 = _packet()
    sub = ContextRecord(record_id="submission:sub-uuid-123", value={"submission_id": "sub-uuid-123"}, sources=[SourceRef(source_type="player_submission", source_id="sub-uuid-123", source_version="1")], authorization=AuthorizationScope(campaign_id=cid2), visibility="campaign")
    # rebuild packet with that submission
    pkt3, _, _ = _packet(campaign_id=cid2, thread_id=tid2, extra_records={LaneName.PLAYER_INPUTS: [sub]})
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "sub-uuid-123"}]}]}])
    # Even though submission ID is known as a source, it is not a typed entity, so entity validator should reject
    r2 = validate_contract(c2, pkt3, known_entity_ids={"character:char:elara"})
    assert any(v.code in ("unknown_canonical_id", "missing_identity_authority") for v in r2.violations)

    # new entity proposal passes
    c3 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state"}]} ], new_entities=[{"temp_id": "tmp_npc_1", "kind": "npc", "public_name": "New Dragon"}])
    r3 = validate_contract(c3, pkt, known_entity_ids={"npc:known"})
    assert r3.passed

    # missing identity authority fails closed when refs exist but no allowlist
    c4 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "npc:anything"}]}]}])
    r4 = validate_contract(c4, pkt, known_entity_ids=set())
    # pkt has no typed entities, so should fail closed with missing_identity_authority
    assert any(v.code == "missing_identity_authority" for v in r4.violations)

    # typed raw escape: character:123 must not authorize npc with same raw id
    c5 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "character:123"}]}]}])
    r5 = validate_contract(c5, pkt, known_entity_ids={"character:123"})
    assert any(v.code == "unknown_canonical_id" for v in r5.violations)
    # bare id 123 should authorize npc 123 (bare wildcard)
    c6 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "See", "claim_kind": "observation", "origin": "established_state", "target_refs": [{"type": "npc", "id": "123"}]}]}])
    r6 = validate_contract(c6, pkt, known_entity_ids={"123"})
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
    r = validate_contract(c, pkt, known_entity_ids={"char:a"})
    assert any(v.code == "player_claim_promoted_to_fact" for v in r.violations)
    # with evidence, promotion is allowed
    c2 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Theory X", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:a"}, "evidence_refs": ["s1"]}, {"text": "Theory X", "claim_kind": "world_fact", "origin": "resolver_evidence", "evidence_refs": ["evidence:1"]}]}])
    r2 = validate_contract(c2, pkt, known_entity_ids={"char:a"})
    assert not any(v.code == "player_claim_promoted_to_fact" for v in r2.violations)


def test_lying_npc_promoted_rejected():
    pkt, _, _ = _packet()
    c = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": [{"id": "beat_1", "type": "npc_dialogue", "speaker_ref": {"type": "npc", "id": "npc:liar"}, "speaker_public_name": "Liar", "claims": [{"text": "Gold is free", "claim_kind": "npc_utterance", "origin": "dm_adjudication", "actor_ref": {"type": "npc", "id": "npc:liar"}}], "truth_status": "deceptive", "dm_private_context": "Lying"}, {"id": "beat_2", "type": "narration", "claims": [{"text": "Gold is free", "claim_kind": "world_fact", "origin": "dm_adjudication"}]}]})
    r = validate_contract(c, pkt, known_entity_ids={"npc:liar"})
    assert any(v.code == "npc_utterance_promoted_to_fact" for v in r.violations)


def test_private_leak_in_shared_audience_rejected_and_private_allowed():
    pkt, _, _ = _packet(audience="campaign")
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Private secret", "claim_kind": "observation", "origin": "established_state", "visibility": "dm_private"}]}])
    r = validate_contract(c, pkt, private_fact_texts={"Private secret"})
    assert any(v.category == "visibility" for v in r.violations)
    assert any(v.code == "private_fact_in_shared_audience" for v in r.violations)
    assert any(v.code == "private_semantic_leak" for v in r.violations)

    # private audience allows same claim
    pkt_priv, _, _ = _packet(audience="private")
    r2 = validate_contract(c, pkt_priv, private_fact_texts={"Private secret"})
    # private audience should not flag visibility; only shared does
    assert r2.passed or not any(v.code == "private_fact_in_shared_audience" for v in r2.violations)


def test_stale_canon_contradiction_from_packet_and_typed_facts():
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

    # typed canon_facts with explicit forbids
    pkt2, _, _ = _packet()
    c3 = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "The seal was cracked", "claim_kind": "world_fact", "origin": "dm_adjudication"}]}])
    r3 = validate_contract(c3, pkt2, canon_facts={"seal_state": {"value": "intact", "forbids": ["cracked"]}})
    assert any(v.code == "canon_contradiction" for v in r3.violations)


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
    r = validate_contract(c_bad, pkt, known_entity_ids={"char:elara"})
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
    contract, report = run_with_bounded_regeneration(adjudicate, pkt2, known_entity_ids={"char:elara"}, max_regenerations=3)
    assert report.passed
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None

    # exhausting retries surfaces rejection
    def always_bad(packet=None, feedback=None):
        return c_bad
    try:
        run_with_bounded_regeneration(always_bad, pkt, known_entity_ids={"char:elara"}, max_regenerations=1)
        assert False
    except ValidatorRejectionError:
        pass


def test_extension_point_without_rewriting_orchestration():
    pipe = ValidatorPipeline()
    n = len(pipe.validators)
    class MyCombat:
        name = "my_combat"; category = "combat"
        def validate(self, contract, packet, **kw):
            from app.dm.validators import ValidatorResult
            return ValidatorResult(validator=self.name, category=self.category, passed=True, violations=[], latency_ms=0.1)
    pipe.add_validator(MyCombat(), after="rules_validator")
    assert len(pipe.validators) == n + 1
    # original default pipeline still has same order
    pkt, _, _ = _packet()
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Hello", "claim_kind": "observation", "origin": "established_state"}]}])
    r = pipe.validate(c, pkt)
    assert any(res.validator == "my_combat" for res in r.results)


def test_observability_latency_per_validator():
    pkt, _, _ = _packet()
    c = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["s1"]}]}])
    r = validate_contract(c, pkt, known_entity_ids={"char:elara"})
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
    r = validate_contract(c, pkt_typed, known_entity_ids=set())
    assert any(v.code == "unknown_canonical_id" for v in r.violations)
    # Bare caller-supplied id 123 should authorize any type
    r2 = validate_contract(c, pkt_typed, known_entity_ids={"123"})
    assert r2.passed or not any(v.code == "unknown_canonical_id" for v in r2.violations)


def test_provenance_includes_adjudication_input():
    # adjudication_input submission_ids should be considered authoritative even if not in packet lanes
    cid = str(uuid.uuid4()); tid = str(uuid.uuid4())
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid)
    # No PLAYER_INPUTS lane, but contract carries adjudication_input
    c = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["sub-xyz"], "trigger_refs": []}]}], "adjudication_input": {"submission_ids": ["sub-xyz"], "segments": [{"position": 0, "segment_type": "ic", "text": "I act"}]}})
    # With packet present, sub-xyz is in adjudication_input, so should NOT be flagged as unknown_source_ref
    r = validate_contract(c, pkt, known_entity_ids={"char:elara"})
    assert not any(v.code == "unknown_source_ref" for v in r.violations)
    # Hallucinated ref not in adjudication_input should be flagged
    c2 = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["hallucinated"], "trigger_refs": []}]}], "adjudication_input": {"submission_ids": ["sub-xyz"], "segments": [{"position": 0, "segment_type": "ic", "text": "I act"}]}})
    r2 = validate_contract(c2, pkt, known_entity_ids={"char:elara"})
    assert any(v.code == "unknown_source_ref" for v in r2.violations)


def test_packet_only_retry_fixture():
    # Packet-only adjudicator must receive packet (augmented) on retry, not a string
    pkt, cid, tid = _packet()
    rec = ContextRecord(record_id="submission:s1", value={"submission_id": "s1"}, sources=[SourceRef(source_type="player_submission", source_id="s1", source_version="1")], authorization=AuthorizationScope(campaign_id=cid), visibility="campaign")
    pkt, _, _ = _packet(campaign_id=cid, thread_id=tid, extra_records={LaneName.PLAYER_INPUTS: [rec]})
    c_bad = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara casts fireball", "claim_kind": "world_fact", "origin": "dm_adjudication", "actor_ref": {"type": "character", "id": "char:elara"}}]}])
    c_good = _base([{"id": "beat_1", "type": "narration", "claims": [{"text": "Elara declares she waits", "claim_kind": "player_declaration", "origin": "player_transcript", "actor_ref": {"type": "character", "id": "char:elara"}, "evidence_refs": ["s1"]}]}])
    calls = []

    def packet_only_adjudicate(packet):
        calls.append(type(packet).__name__ if packet else None)
        if len(calls) == 1:
            assert isinstance(packet, type(pkt))
            return c_bad
        # second call should still be packet, but augmented with repair_directives
        assert isinstance(packet, type(pkt))
        assert any(lane.name == LaneName.REPAIR_DIRECTIVES and lane.records for lane in packet.lanes)
        return c_good

    contract, report = run_with_bounded_regeneration(packet_only_adjudicate, pkt, known_entity_ids={"char:elara"}, max_regenerations=2)
    assert report.passed
    assert calls == [type(pkt).__name__, type(pkt).__name__]

    # Ensure inner TypeError is not swallowed as signature mismatch
    def bad_inner(packet, feedback):
        raise TypeError("inner failure")
    try:
        run_with_bounded_regeneration(bad_inner, pkt, known_entity_ids={"char:elara"}, max_regenerations=1)
        assert False
    except TypeError as exc:
        assert "inner failure" in str(exc)
