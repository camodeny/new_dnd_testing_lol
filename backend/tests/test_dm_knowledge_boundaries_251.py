"""Issue #251 — knowledge boundaries: perspective frames, deterministic
unavailable-knowledge validation, transfer effects, judge scoping, and
epistemic separation regressions."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

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
from app.decisions.errors import DecisionError  # noqa: E402
from app.decisions.frames import (  # noqa: E402
    CandidateRecord,
    FramePerspective,
    build_frame,
    filter_candidates_by_perspective,
    frame_trace,
    perspective_allows_target,
    perspective_target_ref,
    rebuild_frame,
)
from app.dm.context import (  # noqa: E402
    AuthorizationScope,
    ContextAudience,
    ContextRecord,
    LaneName,
    SourceRef,
    assemble_context_packet,
)
from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.effects import _handle_transfer_knowledge  # noqa: E402
from app.dm.narration import build_narration_judge_evidence  # noqa: E402
from app.dm.validators import KnowledgeValidator  # noqa: E402
from app.world.epistemics import (  # noqa: E402
    assert_knowledge_inline,
    grant_visibility_inline,
    what_does_subject_know,
    who_knows_target,
)
from app.world.knowledge import create_fact_authoritative  # noqa: E402
from app.world.service import create_entity_authoritative  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldKnowledge  # noqa: E402


# ── shared DB ─────────────────────────────────────────────────────────────

def _setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    alice = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=alice, email="alice@example.com"),
    ])
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Boundaries", revision=0)
    db.add(camp)
    db.flush()
    db.add_all([
        CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"),
        CampaignMember(campaign_id=camp.id, user_id=alice, role="player"),
    ])
    db.commit()
    return Fac, db.get(Campaign, camp.id), owner, alice


# ── frames: perspective contract ──────────────────────────────────────────

def _candidates():
    return (
        CandidateRecord(id="attack:goblin_a", label="Attack goblin A", source="rules:attack", payload_ref="entity:goblin_a"),
        CandidateRecord(id="attack:goblin_b", label="Attack goblin B", source="rules:attack", payload_ref="entity:goblin_b"),
    )


def test_perspective_rejects_bad_shape():
    with pytest.raises(DecisionError):
        FramePerspective(subject_kind="dragon")
    with pytest.raises(DecisionError):
        FramePerspective(subject_kind="  ")
    with pytest.raises(DecisionError):
        FramePerspective(subject_kind="npc", known_target_refs=("",))


def test_perspective_allows_target():
    assert perspective_allows_target(None, "entity", "x") is True
    dm = FramePerspective(subject_kind="dm")
    assert perspective_allows_target(dm, "fact", "anything") is True
    npc = FramePerspective(subject_kind="npc", subject_entity_id="npc:vera",
                           known_target_refs=("entity:goblin_a",))
    assert perspective_allows_target(npc, "entity", "goblin_a") is True
    assert perspective_allows_target(npc, "entity", "goblin_b") is False
    assert perspective_allows_target(npc, "fact", "goblin_a") is False
    assert perspective_allows_target(npc, "entity", "") is False
    assert perspective_target_ref("entity", "goblin_a") == "entity:goblin_a"


def test_filter_keeps_escapes_and_untargeted():
    npc = FramePerspective(subject_kind="npc", known_target_refs=("entity:goblin_a",))
    cands = _candidates() + (
        CandidateRecord(id="wait", label="Wait", source="rules:wait"),
    )

    def target_of(c):
        if c.id.startswith("attack:"):
            return ("entity", c.id.split(":")[-1])
        return None

    kept = filter_candidates_by_perspective(cands, perspective=npc, target_ref_of=target_of)
    assert {c.id for c in kept} == {"attack:goblin_a", "wait"}


def test_build_frame_filters_before_escapes_and_traces_perspective():
    npc = FramePerspective(subject_kind="npc", subject_entity_id="npc:vera",
                           known_target_refs=("entity:goblin_a",))
    frame = build_frame(
        decision_class="test", question_id="q", instructions="Choose.",
        state={}, state_revision=7, candidates=_candidates(),
        perspective=npc,
        perspective_filter=lambda c: ("entity", c.id.split(":")[-1]),
    )
    ids = {c.id for c in frame.candidates}
    assert "attack:goblin_a" in ids
    assert "attack:goblin_b" not in ids
    assert "OPEN_ENDED_DM" in ids and "CLARIFY" in ids
    trace = frame_trace(frame)
    assert trace["perspective"]["subject_kind"] == "npc"
    assert trace["perspective"]["known_target_refs"] == ["entity:goblin_a"]


def test_rebuild_preserves_perspective():
    npc = FramePerspective(subject_kind="character", known_target_refs=("entity:goblin_a",))
    frame = build_frame(
        decision_class="test", question_id="q", instructions="Choose.",
        state={}, state_revision=7, candidates=_candidates(), perspective=npc,
    )
    fresh = rebuild_frame(frame, state={}, state_revision=8, candidates=_candidates())
    assert fresh.perspective == npc
    assert fresh.frame_id != frame.frame_id


# ── validator: unavailable knowledge ──────────────────────────────────────

def _knowledge_packet(*, subject_id, target_id=None, resolved=True, state="knows"):
    cid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    aud = ContextAudience(campaign_id=cid, thread_id=tid, audience="campaign",
                          user_ids=[str(uuid.uuid4())])
    entries = []
    if target_id is not None:
        entries.append({"knowledge_id": "k1", "target_kind": "entity",
                        "target_id": target_id, "knowledge_state": state,
                        "acquisition_source": "direct_observation", "visibility": "dm_only"})
    value = {"character_id": "", "subject_entity_id": subject_id,
             "subject_resolved": resolved, "perspective": "npc",
             "entries": entries, "total": len(entries), "truncated": False}
    rec = ContextRecord(
        record_id=f"knowledge:{subject_id}", required=False, priority=90,
        value=value,
        sources=[SourceRef(source_type="world_entity", source_id=subject_id,
                           source_version="1")],
        authorization=AuthorizationScope(campaign_id=cid),
        visibility="dm_only", use="adjudication_only",
    )
    records = {lane: [] for lane in LaneName}
    records[LaneName.KNOWLEDGE_VISIBILITY] = [rec]
    status = {
        LaneName.CURRENT_SCENE: "not_applicable",
        LaneName.CLOCKS_PRESSURES: "not_applicable",
        LaneName.COMBAT_HOOKS: "not_applicable",
        LaneName.RELEVANT_CANON: "not_applicable",
        LaneName.REPAIR_DIRECTIVES: "not_applicable",
    }
    return assemble_context_packet(audience=aud, records=records, lane_status=status)


def _utterance(actor, topic_id, **claim_over):
    base = {"text": "I know all about that place.", "claim_kind": "npc_utterance",
            "origin": "dm_adjudication", "visibility": "public",
            "actor_ref": {"type": "npc", "id": actor},
            "topic_refs": [{"type": "location", "id": topic_id}]}
    base.update(claim_over)
    return normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond",
                               "reason": "x", "beats": [
                                   {"id": "beat_1", "type": "npc_dialogue",
                                    "speaker_ref": {"type": "npc", "id": actor},
                                    "speaker_public_name": "Vera",
                                    "truth_status": "truthful",
                                    "claims": [base]}
                               ]})


def test_npc_utterance_without_knowledge_rejected():
    pkt = _knowledge_packet(subject_id="npc:vera", target_id=str(uuid.uuid4()))
    contract = _utterance("npc:vera", str(uuid.uuid4()))
    result = KnowledgeValidator().validate(contract, pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_without_knowledge"


def test_npc_utterance_with_knowledge_or_transfer_passes():
    target = str(uuid.uuid4())
    pkt = _knowledge_packet(subject_id="npc:vera", target_id=target)
    assert KnowledgeValidator().validate(_utterance("npc:vera", target), pkt).passed
    # An unrelated trigger does NOT teach: unknown target still fails.
    pkt2 = _knowledge_packet(subject_id="npc:vera", target_id=str(uuid.uuid4()))
    other = str(uuid.uuid4())
    prompted = _utterance("npc:vera", other, evidence_refs=["submission:s1"],
                          trigger_refs=["submission:s1"])
    result = KnowledgeValidator().validate(prompted, pkt2)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_without_knowledge"
    # A matching in-turn transfer IS the learning source: passes.
    vera_id = str(uuid.uuid4())
    taught = normalize_contract(
        {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
         "beats": [{"id": "beat_1", "type": "npc_dialogue",
                    "speaker_ref": {"type": "npc", "id": vera_id},
                    "speaker_public_name": "Vera",
                    "truth_status": "truthful",
                    "claims": [{"text": "I know all about that place.",
                                "claim_kind": "npc_utterance",
                                "origin": "dm_adjudication", "visibility": "public",
                                "actor_ref": {"type": "npc", "id": vera_id},
                                "topic_refs": [{"type": "location", "id": other}]}]}],
         "staged_effects": [{"id": "eff-tell-1", "effect_type": "transfer_knowledge",
                             "arguments": {"subject_kind": "npc",
                                           "subject_entity_id": vera_id,
                                           "target_kind": "entity",
                                           "target_entity_id": other}}]})
    pkt3 = _knowledge_packet(subject_id=vera_id, target_id=str(uuid.uuid4()))
    assert KnowledgeValidator().validate(taught, pkt3).passed


def test_denial_transfer_does_not_teach():
    """A matching does_not_know transfer never clears the claim."""
    vera_id, target = str(uuid.uuid4()), str(uuid.uuid4())
    denied = normalize_contract(
        {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
         "beats": [{"id": "beat_1", "type": "npc_dialogue",
                    "speaker_ref": {"type": "npc", "id": vera_id},
                    "speaker_public_name": "Vera",
                    "truth_status": "truthful",
                    "claims": [{"text": "I know all about that place.",
                                "claim_kind": "npc_utterance",
                                "origin": "dm_adjudication", "visibility": "public",
                                "actor_ref": {"type": "npc", "id": vera_id},
                                "topic_refs": [{"type": "location", "id": target}]}]}],
         "staged_effects": [{"id": "eff-deny-1", "effect_type": "transfer_knowledge",
                             "arguments": {"subject_kind": "npc",
                                           "subject_entity_id": vera_id,
                                           "target_kind": "entity",
                                           "target_entity_id": target,
                                           "knowledge_state": "does_not_know"}}]})
    pkt = _knowledge_packet(subject_id=vera_id, target_id=target, state="does_not_know")
    result = KnowledgeValidator().validate(denied, pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_denied_knowledge"


def test_validator_skips_unresolvable_or_unrelated():
    pkt = _knowledge_packet(subject_id="npc:vera", target_id=str(uuid.uuid4()), resolved=False)
    contract = _utterance("npc:vera", str(uuid.uuid4()))
    result = KnowledgeValidator().validate(contract, pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_ambiguous_knowledge"
    # Non-NPC actors are out of scope: the system does not police roleplay.
    human = normalize_contract(
        {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
         "beats": [{"id": "beat_1", "type": "narration", "claims": [
             {"text": "Elara declares she inspects.", "claim_kind": "player_declaration",
              "origin": "player_transcript",
              "actor_ref": {"type": "character", "id": "char:elara"},
              "topic_refs": [{"type": "location", "id": str(uuid.uuid4())}]}]}]})
    assert KnowledgeValidator().validate(human, pkt).passed
    # does_not_know is an explicit denial, not coverage.
    target = str(uuid.uuid4())
    pkt3 = _knowledge_packet(subject_id="npc:vera", target_id=target, state="does_not_know")
    result = KnowledgeValidator().validate(_utterance("npc:vera", target), pkt3)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_denied_knowledge"


# ── transfer effect ───────────────────────────────────────────────────────

def test_transfer_knowledge_effect_writes_stance_idempotently():
    Fac, camp, owner, _ = _setup()
    db = Fac()
    subject, _ = create_entity_authoritative(
        db, camp.id, 0, entity_type="character", name="Aria", operation_id="op-aria-tk")
    fact, _ = create_fact_authoritative(
        db, camp.id, 1, content="The vault is under the chapel.",
        entity_refs=[subject.id], epistemic_state="confirmed", visibility="dm_only",
        provenance={"source": "dm_adjudication"}, operation_id="op-truth-tk")
    turn = SimpleNamespace(id=uuid.uuid4())
    attempt = SimpleNamespace(id=uuid.uuid4(), commit_operation_id=None)
    effect = {"id": "eff-tell-1", "effect_type": "transfer_knowledge", "arguments": {
        "subject_kind": "character", "subject_entity_id": str(subject.id),
        "target_kind": "fact", "target_fact_id": str(fact.id),
        "knowledge_state": "knows", "transfer_kind": "tell"}}
    _handle_transfer_knowledge(db, camp, effect, turn, attempt)
    _handle_transfer_knowledge(db, camp, effect, turn, attempt)
    rows = db.execute(
        select(WorldKnowledge).where(WorldKnowledge.campaign_id == camp.id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].knowledge_state == "knows"
    assert rows[0].acquisition_source == "tell"
    # Truth untouched: still exactly one fact, still confirmed.
    assert rows[0].target_fact_id == fact.id


# ── judge evidence scoping ────────────────────────────────────────────────

def test_judge_evidence_merges_knowledge_restricted_texts():
    contract = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond",
                                   "reason": "x", "beats": [
                                       {"id": "beat_1", "type": "narration", "claims": [
                                           {"text": "The party walks on.", "claim_kind": "observation",
                                            "origin": "dm_adjudication", "visibility": "public"}]}]})
    without = build_narration_judge_evidence("The party walks on.", contract)
    assert "The vault combination is 12-34-56." not in without.secret_texts
    with_scope = build_narration_judge_evidence(
        "The party walks on.", contract,
        knowledge_restricted_texts={"The vault combination is 12-34-56."})
    assert "The vault combination is 12-34-56." in with_scope.secret_texts


# ── epistemic separation regressions ──────────────────────────────────────

def test_one_pc_discovery_is_not_party_knowledge():
    Fac, camp, owner, alice, *_ = _setup()
    db = Fac()
    aria, _ = create_entity_authoritative(
        db, camp.id, 0, entity_type="character", name="Aria", operation_id="op-aria-sep")
    bram, _ = create_entity_authoritative(
        db, camp.id, 1, entity_type="character", name="Bram", operation_id="op-bram-sep")
    fact, _ = create_fact_authoritative(
        db, camp.id, 2, content="The bridge is trapped.",
        entity_refs=[aria.id], epistemic_state="confirmed", visibility="dm_only",
        provenance={"source": "dm_adjudication"}, operation_id="op-truth-sep")
    assert_knowledge_inline(
        db, camp, subject_kind="character", subject_entity_id=aria.id,
        target_kind="fact", target_fact_id=fact.id,
        knowledge_state="knows", acquisition_source="direct_observation",
        operation_id="op-know-sep")
    assert what_does_subject_know(db, camp, aria.id, owner)["visible"] == 1
    assert what_does_subject_know(db, camp, bram.id, owner)["visible"] == 0
    knowers = who_knows_target(db, camp, "fact", fact.id, owner)["knowers"]
    assert [k["subject_entity_id"] for k in knowers] == [str(aria.id)]


def test_human_disclosure_does_not_create_character_knowledge():
    Fac, camp, owner, alice = _setup()
    db = Fac()
    bram, _ = create_entity_authoritative(
        db, camp.id, 0, entity_type="character", name="Bram", operation_id="op-bram-hum")
    fact, _ = create_fact_authoritative(
        db, camp.id, 1, content="The vault is under the chapel.",
        entity_refs=[bram.id], epistemic_state="confirmed", visibility="private",
        provenance={"source": "dm_adjudication"}, operation_id="op-truth-hum")
    # The human may receive the truth via an explicit grant...
    grant_visibility_inline(
        db, camp, target_kind="fact", target_id=fact.id, grantee_user_id=alice,
        operation_id="op-grant-hum")
    from app.world.epistemics import may_user_receive
    assert may_user_receive(db, camp, "fact", fact.id, alice)["allowed"] is True
    # ...but no character knowledge row exists for anyone.
    assert what_does_subject_know(db, camp, bram.id, owner)["visible"] == 0
    assert who_knows_target(db, camp, "fact", fact.id, owner)["total"] == 0


# ── review findings: contract, mixed refs, scene relevance, lane NPCs ─────

def _transfer_effect(subject_id, fact_id, **over):
    args = {"subject_kind": "character", "subject_entity_id": str(subject_id),
            "target_kind": "fact", "target_fact_id": str(fact_id),
            "knowledge_state": "knows", "transfer_kind": "tell"}
    args.update(over)
    return {"id": "eff-tell-1", "effect_type": "transfer_knowledge", "arguments": args}


def test_transfer_knowledge_effect_normalizes_in_contract():
    subject_id, fact_id = str(uuid.uuid4()), str(uuid.uuid4())
    contract = normalize_contract(
        {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
         "beats": [{"id": "beat_1", "type": "narration", "claims": [
             {"text": "Aria tells Bram the way.", "claim_kind": "observation",
              "origin": "dm_adjudication", "visibility": "public"}]}],
         "staged_effects": [_transfer_effect(subject_id, fact_id)]})
    assert contract.staged_effects[0].effect_type == "transfer_knowledge"
    # Missing target fails closed at normalization, before any handler runs.
    with pytest.raises(Exception):
        normalize_contract(
            {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
             "beats": [{"id": "beat_1", "type": "narration", "claims": [
                 {"text": "Aria tells Bram the way.", "claim_kind": "observation",
                  "origin": "dm_adjudication", "visibility": "public"}]}],
             "staged_effects": [_transfer_effect(subject_id, fact_id, target_fact_id=None,
                                                target_kind="fact")]})
    # Unknown effect types stay rejected.
    with pytest.raises(Exception):
        normalize_contract(
            {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
             "beats": [{"id": "beat_1", "type": "narration", "claims": [
                 {"text": "Something happens.", "claim_kind": "observation",
                  "origin": "dm_adjudication", "visibility": "public"}]}],
             "staged_effects": [{"id": "eff-x", "effect_type": "invent_knowledge",
                                 "arguments": {}}]})


def test_transfer_knowledge_contract_rejects_kind_mismatch():
    subject_id, entity_id = str(uuid.uuid4()), str(uuid.uuid4())

    def _contract(args):
        return {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
                "beats": [{"id": "beat_1", "type": "narration", "claims": [
                    {"text": "Aria tells Bram the way.", "claim_kind": "observation",
                     "origin": "dm_adjudication", "visibility": "public"}]}],
                "staged_effects": [{"id": "eff-tell-1", "effect_type": "transfer_knowledge",
                                    "arguments": args}]}

    # target_kind=fact with only an entity target fails at normalization.
    with pytest.raises(Exception):
        normalize_contract(_contract({"subject_kind": "character",
                                      "subject_entity_id": subject_id,
                                      "target_kind": "fact",
                                      "target_entity_id": entity_id}))
    # The polymorphic target_id form stays valid for any kind.
    ok = normalize_contract(_contract({"subject_kind": "character",
                                       "subject_entity_id": subject_id,
                                       "target_kind": "fact",
                                       "target_id": entity_id}))
    assert ok.staged_effects[0].effect_type == "transfer_knowledge"


def _utterance_multi(actor, topic_ids, **claim_over):
    base = {"text": "I know all about those places.", "claim_kind": "npc_utterance",
            "origin": "dm_adjudication", "visibility": "public",
            "actor_ref": {"type": "npc", "id": actor},
            "topic_refs": [{"type": "location", "id": tid} for tid in topic_ids]}
    base.update(claim_over)
    return normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond",
                               "reason": "x", "beats": [
                                   {"id": "beat_1", "type": "npc_dialogue",
                                    "speaker_ref": {"type": "npc", "id": actor},
                                    "speaker_public_name": "Vera",
                                    "truth_status": "truthful",
                                    "claims": [base]}
                               ]})


def test_validator_rejects_mixed_known_and_unknown_refs():
    known, unknown = str(uuid.uuid4()), str(uuid.uuid4())
    pkt = _knowledge_packet(subject_id="npc:vera", target_id=known)
    result = KnowledgeValidator().validate(_utterance_multi("npc:vera", [known, unknown]), pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_without_knowledge"
    assert result.violations[0].details["unknown"] == [unknown]


def test_validator_missing_perspective_fails_closed():
    actor = str(uuid.uuid4())
    pkt = _knowledge_packet(subject_id="npc:vera", target_id=None)
    contract = _utterance(actor, str(uuid.uuid4()))
    result = KnowledgeValidator().validate(contract, pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_ambiguous_knowledge"


def test_validator_npc_action_claim_is_knowledge_checked():
    """observation claims with an NPC actor are NPC actions, not narration."""
    target = str(uuid.uuid4())
    pkt = _knowledge_packet(subject_id="npc:vera", target_id=None)
    action = normalize_contract(
        {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
         "beats": [{"id": "beat_1", "type": "narration", "claims": [
             {"text": "Vera pockets the signet ring.", "claim_kind": "observation",
              "origin": "dm_adjudication", "visibility": "public",
              "actor_ref": {"type": "npc", "id": "npc:vera"},
              "target_refs": [{"type": "object", "id": target}]}]}]})
    result = KnowledgeValidator().validate(action, pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_without_knowledge"
    # The same action with a covering perspective passes.
    pkt2 = _knowledge_packet(subject_id="npc:vera", target_id=target)
    assert KnowledgeValidator().validate(action, pkt2).passed


def test_lane_builder_includes_npc_subjects():
    from app.world.epistemics import build_knowledge_visibility_values

    Fac, camp, *_ = _setup()
    db = Fac()
    mara, _ = create_entity_authoritative(
        db, camp.id, 0, entity_type="npc", name="Mara", operation_id="op-mara-lane")
    fact, _ = create_fact_authoritative(
        db, camp.id, 1, content="The cellar connects to the old mine.",
        entity_refs=[mara.id], epistemic_state="confirmed", visibility="dm_only",
        provenance={"source": "dm_adjudication"}, operation_id="op-truth-lane")
    assert_knowledge_inline(
        db, camp, subject_kind="npc", subject_entity_id=mara.id,
        target_kind="fact", target_fact_id=fact.id,
        knowledge_state="suspects", acquisition_source="eavesdropping",
        operation_id="op-know-lane")
    values = build_knowledge_visibility_values(db, camp, set(), npc_entity_ids=[mara.id])
    assert len(values) == 1
    assert values[0]["perspective"] == "npc"
    assert values[0]["subject_entity_id"] == str(mara.id)
    assert values[0]["entries"][0]["knowledge_state"] == "suspects"


def test_collect_subject_restricted_fact_texts():
    from app.world.epistemics import collect_subject_restricted_fact_texts

    Fac, camp, *_ = _setup()
    db = Fac()
    mara, _ = create_entity_authoritative(
        db, camp.id, 0, entity_type="npc", name="Mara", operation_id="op-mara-rs")
    theo, _ = create_entity_authoritative(
        db, camp.id, 1, entity_type="npc", name="Theo", operation_id="op-theo-rs")
    known_fact, _ = create_fact_authoritative(
        db, camp.id, 2, content="Mara knows the cellar route.",
        entity_refs=[mara.id], epistemic_state="confirmed", visibility="dm_only",
        provenance={"source": "dm_adjudication"}, operation_id="op-truth-rs1")
    hidden_fact, _ = create_fact_authoritative(
        db, camp.id, 3, content="The vault combination is 12-34-56.",
        entity_refs=[mara.id], epistemic_state="confirmed", visibility="dm_only",
        provenance={"source": "dm_adjudication"}, operation_id="op-truth-rs2")
    # Campaign-visible OOC truth is still a speaker's misuse to state.
    open_fact, _ = create_fact_authoritative(
        db, camp.id, 4, content="The festival is at dusk.",
        entity_refs=[mara.id], epistemic_state="confirmed", visibility="campaign",
        provenance={"source": "dm_adjudication"}, operation_id="op-truth-rs3")
    assert_knowledge_inline(
        db, camp, subject_kind="npc", subject_entity_id=mara.id,
        target_kind="fact", target_fact_id=known_fact.id,
        knowledge_state="knows", acquisition_source="direct_observation",
        operation_id="op-know-rs")
    scoped = collect_subject_restricted_fact_texts(db, camp, [str(mara.id), str(theo.id)])
    # Per-speaker: Theo learned nothing, so every fact maps to Theo...
    assert "The vault combination is 12-34-56." in scoped[str(theo.id)]
    assert "The festival is at dusk." in scoped[str(theo.id)]
    assert "Mara knows the cellar route." in scoped[str(theo.id)]
    # ...while Mara's known fact is excluded for Mara only (mixed knowers
    # stay detectable per speaker instead of suppressing each other).
    assert "The vault combination is 12-34-56." in scoped[str(mara.id)]
    assert "The festival is at dusk." in scoped[str(mara.id)]
    assert "Mara knows the cellar route." not in scoped[str(mara.id)]
    # Unresolvable speakers contribute no scope.
    assert collect_subject_restricted_fact_texts(db, camp, ["not-a-uuid"]) == {}
    assert collect_subject_restricted_fact_texts(db, camp, []) == {}


def test_stream_narration_forwards_knowledge_scope_to_shadow_judge(monkeypatch):
    import app.decisions.judges as judges_module
    from app.dm.narration import stream_narration

    Fac, camp, *_ = _setup()
    db = Fac()
    captured = {}

    def _capture(service, evidence, **kwargs):
        captured["secrets"] = set(evidence.secret_texts)
        return None

    monkeypatch.setattr(judges_module, "shadow_judge", _capture)
    contract = normalize_contract(
        {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
         "beats": [{"id": "beat_1", "type": "narration", "claims": [
             {"text": "The party walks on.", "claim_kind": "observation",
              "origin": "dm_adjudication", "visibility": "public"}]}]})
    result = stream_narration(
        db, campaign_id=camp.id, thread_id=uuid.uuid4(),
        turn_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()),
        contract=contract, publish_realtime=False,
        judge_service=object(),
        knowledge_restricted_texts={"The vault combination is 12-34-56."})
    assert result.completed
    assert "The vault combination is 12-34-56." in captured["secrets"]


def test_validator_checks_location_ref():
    """An unknown location referenced only via location_ref still fails."""
    target = str(uuid.uuid4())
    pkt = _knowledge_packet(subject_id="npc:vera", target_id=str(uuid.uuid4()))
    contract = normalize_contract(
        {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
         "beats": [{"id": "beat_1", "type": "npc_dialogue",
                    "speaker_ref": {"type": "npc", "id": "npc:vera"},
                    "speaker_public_name": "Vera",
                    "truth_status": "truthful",
                    "claims": [{"text": "Meet me at the old mill.", "claim_kind": "npc_utterance",
                                "origin": "dm_adjudication", "visibility": "public",
                                "actor_ref": {"type": "npc", "id": "npc:vera"},
                                "location_ref": {"type": "location", "id": target}}]}]})
    result = KnowledgeValidator().validate(contract, pkt)
    assert not result.passed
    assert result.violations[0].code == "npc_utterance_without_knowledge"
    assert result.violations[0].details["unknown"] == [target]


def test_speaker_scope_covers_npc_actions():
    """Derivation matches the validator: NPC observations/world_facts count."""
    from app.dm.narration import derive_knowledge_speaker_scope

    npc_id = str(uuid.uuid4())
    contract = normalize_contract(
        {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
         "beats": [{"id": "beat_1", "type": "narration", "claims": [
             {"text": "Vera pockets the signet ring.", "claim_kind": "observation",
              "origin": "dm_adjudication", "visibility": "public",
              "actor_ref": {"type": "npc", "id": npc_id},
              "target_refs": [{"type": "object", "id": str(uuid.uuid4())}]},
             {"text": "The room is quiet.", "claim_kind": "observation",
              "origin": "dm_adjudication", "visibility": "public"},
             {"text": "Elara declares she waits.", "claim_kind": "player_declaration",
              "origin": "player_transcript",
              "actor_ref": {"type": "character", "id": "char:elara"}}]}]})
    assert derive_knowledge_speaker_scope(contract) == {npc_id}
