"""Issue #214 — canonical aliases and bounded identity resolution."""
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

from database import Base
import models  # noqa: F401
from app.decisions import DecisionError, DecisionService, record_fail_soft, to_decision_request
from app.decisions.adapters.fake import FakeDecisionAdapter
from app.world.identity import (DEFER, KEEP_DISTINCT, NEW_ENTITY, add_alias,
    build_identity_frame, candidate_entities, create_entity_after_resolution, decide_identity, exact_identity,
    normalize_alias, supersede_entity)
from app.world.service import create_entity_inline, promote_new_entities_from_contract
from models.campaigns import Campaign
from models.profiles import Profile


def setup_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="identity@example.com"))
    campaign = Campaign(id=uuid.uuid4(), owner_id=owner, name="Identity", revision=7)
    db.add(campaign); db.flush()
    return db, campaign


def make_entity(db, campaign, name, kind="npc", visibility="campaign", idempotency_key=None, **details):
    return create_entity_inline(db, campaign, entity_type=kind, name=name,
                                visibility=visibility, details=details,
                                idempotency_key=idempotency_key)[0]


def test_normalized_alias_and_stable_ref_resolve_without_model():
    db, campaign = setup_db()
    mara = make_entity(db, campaign, "Mara Venn")
    add_alias(db, mara, "  The—Fox  ", provenance={"turn": "t1"})
    assert normalize_alias("THE fox") == "the fox"
    assert exact_identity(db, campaign.id, "the fox").id == mara.id
    assert exact_identity(db, campaign.id, str(mara.id)).id == mara.id
    assert mara.revision == 2


def test_same_name_is_ambiguous_and_keep_distinct_is_explicit():
    db, campaign = setup_db()
    make_entity(db, campaign, "The Guard", location_ref="north")
    make_entity(db, campaign, "The Guard", location_ref="south")
    assert exact_identity(db, campaign.id, "The Guard") is None
    frame = build_identity_frame(db, campaign, name="The Guard", entity_type="npc", location_ref="south")
    assert KEEP_DISTINCT in {c.id for c in frame.candidates}
    result = decide_identity(db, campaign, frame, DecisionService(FakeDecisionAdapter(
        answers={frame.question_id: KEEP_DISTINCT})))
    assert result.selected_id == KEEP_DISTINCT


def test_hidden_alias_exact_for_authority_but_never_leaks_to_player_candidates():
    db, campaign = setup_db()
    spy = make_entity(db, campaign, "Quiet Merchant", visibility="campaign")
    add_alias(db, spy, "Nightblade", visibility="dm_only")
    assert exact_identity(db, campaign.id, "nightblade").id == spy.id
    candidates = candidate_entities(db, campaign.id, name="Nightblade", is_authority=False)
    assert spy.id not in {c.id for c in candidates}


def test_obvious_duplicate_jit_proposal_rejected_and_retry_stays_idempotent():
    db, campaign = setup_db()
    original = make_entity(db, campaign, "Mara Venn", idempotency_key="first")
    attempt = type("Attempt", (), {"id": uuid.uuid4(), "commit_operation_id": "op", "contract_snapshot": {
        "new_entities": [{"temp_id": "tmp", "kind": "npc", "public_name": "mara venn"}]}})()
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    # Exact hit now enters the bounded frame instead of raising upfront.
    # DEFER fails closed with no insert.
    defer_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": DEFER}))
    with pytest.raises(ValueError, match="deferred"):
        promote_new_entities_from_contract(
            db, campaign, turn, attempt, identity_decision_service=defer_service)
    assert exact_identity(db, campaign.id, "Mara Venn").id == original.id
    # Plain NEW_ENTITY against an exact collision also fails closed.
    new_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": NEW_ENTITY}))
    with pytest.raises(ValueError, match="collides"):
        promote_new_entities_from_contract(
            db, campaign, turn, attempt, identity_decision_service=new_service)
    assert [row.id for row in db.query(type(original)).all()] == [original.id]
    # Selecting the canonical entity reuses it without inserting.
    reuse_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": str(original.id)}))
    reused = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=reuse_service)
    assert reused[0].id == original.id
    assert [row.id for row in db.query(type(original)).all()] == [original.id]


def test_exact_same_name_distinct_context_keep_distinct_via_promotion():
    db, campaign = setup_db()
    north_guard = make_entity(db, campaign, "The Guard", location_ref="north gate")
    south_attempt = type("Attempt", (), {"id": uuid.uuid4(), "commit_operation_id": "op-south",
        "contract_snapshot": {"new_entities": [{
            "temp_id": "tmp-south", "kind": "npc", "public_name": "The Guard",
            "location_ref": "south gate"}]}})()
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    # DEFER on the exact same name creates nothing.
    defer_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": DEFER}))
    with pytest.raises(ValueError, match="deferred"):
        promote_new_entities_from_contract(
            db, campaign, turn, south_attempt, identity_decision_service=defer_service)
    rows = db.query(type(north_guard)).all()
    assert [row.id for row in rows] == [north_guard.id]
    # Policy-approved KEEP_DISTINCT persists the same-name distinct entity.
    keep_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": KEEP_DISTINCT}))
    promoted = promote_new_entities_from_contract(
        db, campaign, turn, south_attempt, identity_decision_service=keep_service)
    assert promoted[0].name == "The Guard"
    assert promoted[0].id != north_guard.id
    assert promoted[0].details["identity_resolution"]["outcome"] == KEEP_DISTINCT
    assert len(keep_service.adapter.calls) == 1
    # Duplicate retry returns the same row idempotently without another decision.
    retried = promote_new_entities_from_contract(
        db, campaign, turn, south_attempt, identity_decision_service=keep_service)
    assert retried[0].id == promoted[0].id
    assert len(keep_service.adapter.calls) == 1


def test_real_promotion_path_near_name_requires_bounded_outcome():
    db, campaign = setup_db()
    original = make_entity(db, campaign, "Mara Venn")
    attempt = type("Attempt", (), {"id": uuid.uuid4(), "commit_operation_id": "op", "contract_snapshot": {
        "new_entities": [{"temp_id": "tmp", "kind": "npc", "public_name": "Mara"}]}})()
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": DEFER}))
    with pytest.raises(ValueError, match="deferred"):
        promote_new_entities_from_contract(
            db, campaign, turn, attempt, identity_decision_service=service)
    assert service.adapter.calls
    assert [row.id for row in db.query(type(original)).all()] == [original.id]

    keep_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": KEEP_DISTINCT}))
    promoted = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=keep_service)
    assert promoted[0].name == "Mara"
    assert promoted[0].details["identity_resolution"]["outcome"] == KEEP_DISTINCT
    assert len(keep_service.adapter.calls) == 1
    retried = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=keep_service)
    assert retried[0].id == promoted[0].id
    assert len(keep_service.adapter.calls) == 1


def test_bounded_candidates_and_stale_revalidation_fail_closed():
    db, campaign = setup_db()
    candidate = make_entity(db, campaign, "Mara", kind="npc")
    frame = build_identity_frame(db, campaign, name="Mara Venn", entity_type="npc")
    ids = {c.id for c in frame.candidates}
    assert ids == {str(candidate.id), NEW_ENTITY, KEEP_DISTINCT, DEFER}
    campaign.revision += 1; db.flush()
    with pytest.raises(DecisionError) as exc:
        decide_identity(db, campaign, frame, DecisionService(FakeDecisionAdapter(
            answers={frame.question_id: str(candidate.id)})))
    assert exc.value.kind == "stale"


def test_provider_failure_defers_and_supersession_is_auditable():
    db, campaign = setup_db()
    canonical = make_entity(db, campaign, "Mara Venn")
    duplicate = make_entity(db, campaign, "Mara of the Gate")
    frame = build_identity_frame(db, campaign, name="Mara", entity_type="npc")
    result = decide_identity(db, campaign, frame, DecisionService(FakeDecisionAdapter()))
    assert result.selected_id == DEFER
    supersede_entity(db, duplicate, canonical, provenance={"repair_id": "r1"})
    assert duplicate.superseded_by_id == canonical.id
    assert duplicate.details["identity_supersession"]["provenance"] == {"repair_id": "r1"}


def test_keep_distinct_can_create_same_name_and_retry_is_idempotent():
    db, campaign = setup_db()
    make_entity(db, campaign, "Mara", location_ref="north")
    frame = build_identity_frame(db, campaign, name="Mara", entity_type="npc", location_ref="south")
    created, was_created = create_entity_after_resolution(
        db, campaign, frame, KEEP_DISTINCT, entity_type="npc", name="Mara",
        idempotency_key="identity:attempt:tmp", details={"location_ref": "south"})
    retried, retry_created = create_entity_after_resolution(
        db, campaign, frame, KEEP_DISTINCT, entity_type="npc", name="Mara",
        idempotency_key="identity:attempt:tmp", details={"location_ref": "south"})
    assert was_created is True and retry_created is False
    assert retried.id == created.id


def test_same_name_candidates_carry_distinguishing_context_to_model():
    db, campaign = setup_db()
    make_entity(db, campaign, "The Guard", location_ref="north gate")
    south = make_entity(db, campaign, "The Guard", location_ref="south gate")
    add_alias(db, south, "Southerner", visibility="campaign")
    frame = build_identity_frame(db, campaign, name="The Guard", entity_type="npc",
                                 location_ref="south gate")
    request = to_decision_request(frame)
    real = [c for c in request.questions[0].candidates
            if c.id not in {NEW_ENTITY, KEEP_DISTINCT, DEFER}]
    assert len(real) == 2
    descriptions = {c.id: c.description or "" for c in real}
    assert len(set(descriptions.values())) == 2
    assert "south gate" in descriptions[str(south.id)]
    assert "Southerner" in descriptions[str(south.id)]
    assert any("north gate" in desc for desc in descriptions.values())


def test_non_authority_frame_hides_secret_alias_in_labels():
    db, campaign = setup_db()
    merchant = make_entity(db, campaign, "Quiet Merchant", visibility="campaign")
    add_alias(db, merchant, "Nightblade", visibility="dm_only")
    player_frame = build_identity_frame(db, campaign, name="Quiet Merchant", entity_type="npc",
                                        is_authority=False)
    player_real = [c for c in player_frame.candidates
                   if c.id not in {NEW_ENTITY, KEEP_DISTINCT, DEFER}]
    assert len(player_real) == 1
    assert "Nightblade" not in player_real[0].label
    authority_frame = build_identity_frame(db, campaign, name="Quiet Merchant", entity_type="npc",
                                           is_authority=True)
    authority_real = [c for c in authority_frame.candidates
                      if c.id not in {NEW_ENTITY, KEEP_DISTINCT, DEFER}]
    assert len(authority_real) == 1
    assert "Nightblade" in authority_real[0].label


def test_locked_promotion_collects_telemetry_outbox_without_independent_write():
    from sqlalchemy import select

    from models.reliability import DecisionTelemetry

    db, campaign = setup_db()
    make_entity(db, campaign, "The Guard", location_ref="north gate")
    attempt = type("Attempt", (), {"id": uuid.uuid4(), "commit_operation_id": "op-lock",
        "contract_snapshot": {"new_entities": [{
            "temp_id": "tmp-lock", "kind": "npc", "public_name": "The Guard",
            "location_ref": "south gate"}]}})()
    turn = type("Turn", (), {"id": uuid.uuid4()})()

    def _exploding_factory():
        raise AssertionError("no independent telemetry session while campaign lock is held")

    outbox: list = []
    service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": KEEP_DISTINCT}))
    promoted = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=service,
        identity_session_factory=_exploding_factory, identity_telemetry_outbox=outbox)
    assert promoted[0].name == "The Guard"
    assert len(outbox) == 1
    assert outbox[0].selected_id == KEEP_DISTINCT
    # Post-commit flush persists the deferred record fail-soft on its own session.
    flush_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    for record in outbox:
        assert record_fail_soft(flush_factory, record) is not None
    rows = db.execute(select(DecisionTelemetry)).scalars().all()
    assert [row.selected_id for row in rows] == [KEEP_DISTINCT]
