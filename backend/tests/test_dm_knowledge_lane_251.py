"""Issue #251 — knowledge-visibility lane reader for #202 context assembly."""

from __future__ import annotations

import uuid

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
from app.dm.context import (  # noqa: E402
    ContextRecord,
    _scope,
    _source,
)
from app.world.knowledge import (  # noqa: E402
    assert_knowledge,
    build_knowledge_visibility_values,
)
from app.world.facts import create_fact  # noqa: E402
from app.world.service import create_entity  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402


def _setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@example.com"))
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Lane", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.commit()
    return Fac, db.get(Campaign, camp.id)


def test_no_pc_yields_single_empty_value():
    Fac, camp = _setup()
    db = Fac()
    values = build_knowledge_visibility_values(db, camp, set())
    assert values == [{"perspectives": [], "note": "no_pc_in_attempt"}]


def test_unresolved_character_is_explicit_empty():
    Fac, camp = _setup()
    db = Fac()
    values = build_knowledge_visibility_values(db, camp, {uuid.uuid4()})
    assert len(values) == 1
    assert values[0]["subject_resolved"] is False
    assert values[0]["subject_entity_id"] is None
    assert values[0]["entries"] == []


def test_resolved_subject_returns_bounded_stance_refs():
    Fac, camp = _setup()
    db = Fac()
    aria, _ = commit_world_write(
        db, camp.id, 0, create_entity, entity_type="character", name="Aria",
        operation_id="op-aria-251",
        details={"character_id": "00000000-0000-0000-0000-000000000000"},
    )
    fact, _ = commit_world_write(
        db, camp.id, 1, create_fact, content="The bridge is trapped.",
        entity_refs=[aria.id], epistemic_state="confirmed",
        visibility="dm_only",
        provenance={"source": "dm_adjudication"},
        operation_id="op-truth-251",
    )
    assert_knowledge(
        db, camp, subject_kind="character", subject_entity_id=aria.id,
        target_kind="fact", target_fact_id=fact.id,
        knowledge_state="knows", acquisition_source="direct_observation",
        operation_id="op-know-251",
    )
    # Point the fake character link at the real subject entity.
    aria.details = {"character_id": str(aria.id)}
    db.flush()
    values = build_knowledge_visibility_values(db, camp, {aria.id})
    assert len(values) == 1
    value = values[0]
    assert value["subject_resolved"] is True
    assert value["subject_entity_id"] == str(aria.id)
    assert len(value["entries"]) == 1
    entry = value["entries"][0]
    assert entry["target_kind"] == "fact"
    assert entry["target_id"] == str(fact.id)
    assert entry["knowledge_state"] == "knows"
    # Stance refs only — no truth text in the lane value.
    assert "content" not in entry
    assert "The bridge" not in str(value)


def test_entries_truncate_at_limit():
    Fac, camp = _setup()
    db = Fac()
    aria, _ = commit_world_write(
        db, camp.id, 0, create_entity, entity_type="character", name="Aria",
        operation_id="op-aria-251t",
    )
    for i in range(4):
        fact, _ = commit_world_write(
            db, camp.id, i + 1, create_fact, content=f"Secret fact number {i}.",
            entity_refs=[aria.id], epistemic_state="confirmed",
            visibility="dm_only",
            provenance={"source": "dm_adjudication"},
            operation_id=f"op-truth-251t-{i}",
        )
        assert_knowledge(
            db, camp, subject_kind="character", subject_entity_id=aria.id,
            target_kind="fact", target_fact_id=fact.id,
            knowledge_state="knows", acquisition_source="direct_observation",
            operation_id=f"op-know-251t-{i}",
        )
    values = build_knowledge_visibility_values(db, camp, {aria.id}, max_entries_per_subject=2)
    assert values[0]["truncated"] is True
    assert len(values[0]["entries"]) == 2


def test_lane_records_are_dm_only_adjudication_only():
    _, camp = _setup()
    scope = _scope(camp.id)
    record = ContextRecord(
        record_id="knowledge:no-pc",
        required=False,
        priority=90,
        value={"perspectives": [], "note": "no_pc_in_attempt"},
        sources=[_source("dm_turn_attempt", uuid.uuid4(), 0, 0, lane="knowledge_visibility")],
        authorization=scope,
        visibility="dm_only",
        use="adjudication_only",
    )
    assert record.visibility == "dm_only"
    assert record.use == "adjudication_only"


def _packet_with_empty_knowledge_lane(campaign_id):
    from app.dm.context import ContextAudience, LaneName, assemble_context_packet
    cid = str(campaign_id); tid = str(uuid.uuid4())
    aud = ContextAudience(campaign_id=cid, thread_id=tid, audience="campaign", user_ids=[str(uuid.uuid4())])
    records = {lane: [] for lane in LaneName}
    status = {lane: "not_applicable" for lane in LaneName}
    status[LaneName.KNOWLEDGE_VISIBILITY] = "authoritative"
    return assemble_context_packet(audience=aud, records=records, lane_status=status)


def _setup_repair_scene():
    """Campaign with a recently introduced NPC who knows one location."""
    Fac, camp = _setup()
    db = Fac()
    npc, _ = commit_world_write(
        db, camp.id, 0, create_entity, entity_type="npc", name="Hooded Traveler",
        operation_id="op-hood-455",
    )
    well, _ = commit_world_write(
        db, camp.id, 1, create_entity, entity_type="location", name="Old Well",
        operation_id="op-well-455",
    )
    assert_knowledge(
        db, camp, subject_kind="npc", subject_entity_id=npc.id,
        target_kind="entity", target_entity_id=well.id,
        knowledge_state="knows", acquisition_source="direct_observation",
        operation_id="op-know-455",
    )
    return Fac, camp, npc, well


def _dialogue_contract(actor_id, target_id):
    from app.dm.contract import CONTRACT_VERSION, normalize_contract
    return normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "repair", "beats": [{"id": "b1", "type": "npc_dialogue", "speaker_ref": {"type": "npc", "id": actor_id}, "speaker_public_name": "Hooded traveler", "truth_status": "truthful", "claims": [{"text": "The old well is poisoned", "claim_kind": "npc_utterance", "actor_ref": {"type": "npc", "id": actor_id}, "topic_refs": [{"type": "location", "id": target_id}], "origin": "dm_adjudication"}]}]})


def test_repair_resolves_recently_introduced_npc_by_uuid():
    # Issue #455 deeper fix: a known-but-unlanned NPC is resolved by stable
    # UUID; tmp_* and unknown subjects are skipped, never guessed.
    from app.dm.context import LaneName, repair_packet_missing_perspectives
    from app.dm.validators import KnowledgeValidator, ValidatorPipeline
    Fac, camp, npc, well = _setup_repair_scene()
    db = Fac()
    pkt = _packet_with_empty_knowledge_lane(camp.id)
    repaired = repair_packet_missing_perspectives(pkt, db, camp, [str(npc.id), "tmp_ghost", "no such entity"])
    assert repaired is not None and repaired is not pkt
    lane = next(lane for lane in repaired.lanes if lane.name == LaneName.KNOWLEDGE_VISIBILITY)
    assert len(lane.records) == 1
    value = lane.records[0].value
    assert value["subject_entity_id"] == str(npc.id)
    assert value["subject_resolved"] is True
    assert "alias_ids" not in value
    assert any(str(e.get("target_id")) == str(well.id) for e in value["entries"])
    # dm_only + adjudication_only like every lane sibling
    assert lane.records[0].visibility == "dm_only"
    assert lane.records[0].use == "adjudication_only"
    # Dialogue within the NPC's knowledge now passes; the unrepaired packet fails.
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    contract = _dialogue_contract(str(npc.id), str(well.id))
    assert pipe.validate(contract, repaired).passed
    assert any(v.code == "npc_utterance_ambiguous_knowledge" for v in pipe.validate(contract, pkt).violations)


def test_repair_by_name_attaches_canonical_perspective_and_requires_id():
    # The model referenced the NPC by exact name: the canonical perspective is
    # attached under the entity ID only, the directive names that ID, and a
    # name-keyed claim still fails closed (the secrecy judge only knows IDs).
    from app.dm.context import LaneName, repair_packet_missing_perspectives
    from app.dm.validators import KnowledgeValidator, ValidatorPipeline
    Fac, camp, npc, well = _setup_repair_scene()
    db = Fac()
    pkt = _packet_with_empty_knowledge_lane(camp.id)
    repaired = repair_packet_missing_perspectives(pkt, db, camp, ["Hooded Traveler"])
    assert repaired is not None
    lane = next(lane for lane in repaired.lanes if lane.name == LaneName.KNOWLEDGE_VISIBILITY)
    value = lane.records[0].value
    assert value["subject_entity_id"] == str(npc.id)
    assert value["subject_name"] == "Hooded Traveler"
    assert "alias_ids" not in value
    directives = next(lane for lane in repaired.lanes if lane.name == LaneName.REPAIR_DIRECTIVES)
    assert any(f"Hooded Traveler -> {npc.id}" in rec.value["directive"] for rec in directives.records)
    pipe = ValidatorPipeline(validators=[KnowledgeValidator()])
    by_name = pipe.validate(_dialogue_contract("Hooded Traveler", str(well.id)), repaired)
    assert any(v.code == "npc_utterance_ambiguous_knowledge" for v in by_name.violations)
    assert pipe.validate(_dialogue_contract(str(npc.id), str(well.id)), repaired).passed


def test_repair_dedupes_name_and_id_for_same_npc():
    # Same NPC referenced by ID in one claim and by name in another: one
    # record per entity, so lane validation never sees a duplicate record ID.
    from app.dm.context import LaneName, repair_packet_missing_perspectives
    Fac, camp, npc, _ = _setup_repair_scene()
    db = Fac()
    pkt = _packet_with_empty_knowledge_lane(camp.id)
    repaired = repair_packet_missing_perspectives(pkt, db, camp, [str(npc.id), "Hooded Traveler"])
    assert repaired is not None
    lane = next(lane for lane in repaired.lanes if lane.name == LaneName.KNOWLEDGE_VISIBILITY)
    assert [rec.record_id for rec in lane.records] == [f"knowledge-repair:{npc.id}"]


def test_repair_name_for_already_laned_npc_only_adds_id_directive():
    from app.dm.context import LaneName, repair_packet_missing_perspectives
    Fac, camp, npc, _ = _setup_repair_scene()
    db = Fac()
    first = repair_packet_missing_perspectives(
        _packet_with_empty_knowledge_lane(camp.id), db, camp, [str(npc.id)],
    )
    repaired = repair_packet_missing_perspectives(first, db, camp, ["Hooded Traveler"])
    assert repaired is not None
    lane = next(lane for lane in repaired.lanes if lane.name == LaneName.KNOWLEDGE_VISIBILITY)
    assert len(lane.records) == 1
    directives = next(lane for lane in repaired.lanes if lane.name == LaneName.REPAIR_DIRECTIVES)
    assert any(f"Hooded Traveler -> {npc.id}" in rec.value["directive"] for rec in directives.records)


def test_repair_sources_do_not_borrow_sibling_subject():
    # Issue #455 review: the repaired record keeps only the sibling's
    # attempt attribution, never the sibling's character/world_entity refs.
    from app.dm.context import ContextAudience, LaneName, assemble_context_packet, repair_packet_missing_perspectives
    Fac, camp, npc, _ = _setup_repair_scene()
    db = Fac()
    attempt_id = uuid.uuid4()
    pc_id = uuid.uuid4()
    other_subject = uuid.uuid4()
    sibling = ContextRecord(
        record_id=f"knowledge:{pc_id}", required=False, priority=90,
        value={"character_id": str(pc_id), "subject_entity_id": str(other_subject),
               "subject_resolved": True, "perspective": "character", "entries": [],
               "total": 0, "truncated": False},
        sources=[
            _source("character", pc_id, 0, 0, lane="knowledge_visibility"),
            _source("world_entity", other_subject, 0, 0, lane="knowledge_visibility"),
            _source("dm_turn_attempt", attempt_id, 0, 0, lane="knowledge_visibility"),
        ],
        authorization=_scope(camp.id), visibility="dm_only", use="adjudication_only",
    )
    aud = ContextAudience(campaign_id=str(camp.id), thread_id=str(uuid.uuid4()), audience="campaign", user_ids=[str(uuid.uuid4())])
    records = {lane: [] for lane in LaneName}
    records[LaneName.KNOWLEDGE_VISIBILITY] = [sibling]
    status = {lane: "not_applicable" for lane in LaneName}
    status[LaneName.KNOWLEDGE_VISIBILITY] = "authoritative"
    pkt = assemble_context_packet(audience=aud, records=records, lane_status=status)
    repaired = repair_packet_missing_perspectives(pkt, db, camp, [str(npc.id)])
    assert repaired is not None
    lane = next(lane for lane in repaired.lanes if lane.name == LaneName.KNOWLEDGE_VISIBILITY)
    rec = next(r for r in lane.records if r.record_id == f"knowledge-repair:{npc.id}")
    assert sorted((src.source_type, src.source_id) for src in rec.sources) == sorted([
        ("dm_turn_attempt", str(attempt_id)),
        ("world_entity", str(npc.id)),
    ])


def test_repair_returns_none_when_nothing_resolves():
    from app.dm.context import repair_packet_missing_perspectives
    Fac, camp, _, _ = _setup_repair_scene()
    db = Fac()
    pkt = _packet_with_empty_knowledge_lane(camp.id)
    assert repair_packet_missing_perspectives(pkt, db, camp, ["tmp_ghost", "no such entity"]) is None
    assert repair_packet_missing_perspectives(pkt, db, camp, []) is None
