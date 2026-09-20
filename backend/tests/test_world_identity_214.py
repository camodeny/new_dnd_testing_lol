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


def test_exact_alias_and_uuid_proposals_reuse_owner_without_model_call():
    db, campaign = setup_db()
    mara_venn = make_entity(db, campaign, "Mara Venn")
    add_alias(db, mara_venn, "Mara", provenance={"turn": "t1"})
    service = DecisionService(FakeDecisionAdapter(answers={}))
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    alias_attempt = type("Attempt", (), {"id": uuid.uuid4(), "commit_operation_id": "op-alias",
        "contract_snapshot": {"new_entities": [{
            "temp_id": "tmp-alias", "kind": "npc", "public_name": "Mara"}]}})()
    reused = promote_new_entities_from_contract(
        db, campaign, turn, alias_attempt, identity_decision_service=service)
    assert reused[0].id == mara_venn.id
    uuid_attempt = type("Attempt", (), {"id": uuid.uuid4(), "commit_operation_id": "op-uuid",
        "contract_snapshot": {"new_entities": [{
            "temp_id": "tmp-uuid", "kind": "npc", "public_name": str(mara_venn.id)}]}})()
    reused_uuid = promote_new_entities_from_contract(
        db, campaign, turn, uuid_attempt, identity_decision_service=service)
    assert reused_uuid[0].id == mara_venn.id
    assert service.adapter.calls == []
    assert [row.id for row in db.query(type(mara_venn)).all()] == [mara_venn.id]
    assert exact_identity(db, campaign.id, "Mara").id == mara_venn.id


def test_keep_distinct_rejected_when_name_is_another_entity_alias():
    db, campaign = setup_db()
    mara_venn = make_entity(db, campaign, "Mara Venn")
    add_alias(db, mara_venn, "Mara", provenance={"turn": "t1"})
    frame = build_identity_frame(db, campaign, name="Mara", entity_type="npc")
    with pytest.raises(ValueError, match="alias"):
        create_entity_after_resolution(
            db, campaign, frame, KEEP_DISTINCT, entity_type="npc", name="Mara",
            idempotency_key="identity:attempt:alias-guard")
    assert [row.id for row in db.query(type(mara_venn)).all()] == [mara_venn.id]
    assert exact_identity(db, campaign.id, "Mara").id == mara_venn.id


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


# ── Pre-narration bounded resolution (#214 re-review) ─────────────────────────

def _attempt_fake(contract_snapshot):
    return type("Attempt", (), {
        "id": uuid.uuid4(), "commit_operation_id": "op-pre",
        "contract_snapshot": contract_snapshot, "identity_resolutions": None,
    })()


def test_pre_narration_resolution_persists_attempt_local_outcome():
    from app.world.service import resolve_new_entity_identities_pre_narration
    db, campaign = setup_db()
    make_entity(db, campaign, "Mara Venn")
    attempt = _attempt_fake({"new_entities": [{
        "temp_id": "tmp", "kind": "npc", "public_name": "Mara"}]})
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": KEEP_DISTINCT}))
    outcomes = resolve_new_entity_identities_pre_narration(
        db, campaign, turn, attempt, attempt.contract_snapshot,
        identity_decision_service=service)
    assert len(outcomes) == 1
    assert outcomes[0]["temp_id"] == "tmp"
    assert outcomes[0]["outcome"] == KEEP_DISTINCT
    assert outcomes[0]["frame"]["frame_id"]
    assert attempt.identity_resolutions == outcomes
    assert len(service.adapter.calls) == 1
    # Nothing durably created pre-commit: resolution never writes authority.
    from models.world import WorldEntity as _WorldEntity
    assert [row.name for row in db.query(_WorldEntity).all()] == ["Mara Venn"]


def test_commit_revalidation_applies_stored_outcome_without_new_decision():
    from app.world.service import resolve_new_entity_identities_pre_narration
    db, campaign = setup_db()
    north_guard = make_entity(db, campaign, "The Guard", location_ref="north gate")
    snapshot = {"new_entities": [{
        "temp_id": "tmp-south", "kind": "npc", "public_name": "The Guard",
        "location_ref": "south gate"}]}
    attempt = _attempt_fake(snapshot)
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    pre_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": KEEP_DISTINCT}))
    resolve_new_entity_identities_pre_narration(
        db, campaign, turn, attempt, snapshot, identity_decision_service=pre_service)
    assert len(pre_service.adapter.calls) == 1
    # Commit-time promotion applies the stored outcome. A fresh service that
    # would DEFER proves no second model call happens: success means zero calls.
    commit_service = DecisionService(FakeDecisionAdapter(answers={}))
    promoted = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=commit_service)
    assert promoted[0].name == "The Guard"
    assert promoted[0].id != north_guard.id
    assert promoted[0].details["identity_resolution"]["outcome"] == KEEP_DISTINCT
    assert commit_service.adapter.calls == []
    # Idempotent retry returns the same row with still no decision call.
    retried = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=commit_service)
    assert retried[0].id == promoted[0].id
    assert commit_service.adapter.calls == []


def test_pre_narration_defer_aborts_before_anything_durable():
    from app.world.service import resolve_new_entity_identities_pre_narration
    db, campaign = setup_db()
    original = make_entity(db, campaign, "Mara Venn")
    attempt = _attempt_fake({"new_entities": [{
        "temp_id": "tmp", "kind": "npc", "public_name": "Mara"}]})
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": DEFER}))
    with pytest.raises(ValueError, match="deferred"):
        resolve_new_entity_identities_pre_narration(
            db, campaign, turn, attempt, attempt.contract_snapshot,
            identity_decision_service=service)
    # All-or-nothing: no outcome persisted, no entity created.
    assert attempt.identity_resolutions is None
    assert [row.id for row in db.query(type(original)).all()] == [original.id]


def test_stored_outcome_stale_revision_fails_closed_at_commit():
    from app.world.service import resolve_new_entity_identities_pre_narration
    db, campaign = setup_db()
    make_entity(db, campaign, "Mara Venn")
    snapshot = {"new_entities": [{
        "temp_id": "tmp", "kind": "npc", "public_name": "Mara"}]}
    attempt = _attempt_fake(snapshot)
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    pre_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": KEEP_DISTINCT}))
    resolve_new_entity_identities_pre_narration(
        db, campaign, turn, attempt, snapshot, identity_decision_service=pre_service)
    # Fresh identity state drifted after the pre-narration decision.
    campaign.revision += 1; db.flush()
    commit_service = DecisionService(FakeDecisionAdapter(answers={}))
    with pytest.raises(DecisionError) as exc:
        promote_new_entities_from_contract(
            db, campaign, turn, attempt, identity_decision_service=commit_service)
    assert exc.value.kind == "stale"
    assert commit_service.adapter.calls == []
    from models.world import WorldEntity as _WorldEntity2
    assert [row.name for row in db.query(_WorldEntity2).all()] == ["Mara Venn"]


def test_pre_narration_exact_alias_reuses_owner_with_zero_model_calls():
    from app.world.service import resolve_new_entity_identities_pre_narration
    db, campaign = setup_db()
    mara_venn = make_entity(db, campaign, "Mara Venn")
    add_alias(db, mara_venn, "Mara", provenance={"turn": "t1"})
    snapshot = {"new_entities": [{
        "temp_id": "tmp-alias", "kind": "npc", "public_name": "Mara"}]}
    attempt = _attempt_fake(snapshot)
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    service = DecisionService(FakeDecisionAdapter(answers={}))
    outcomes = resolve_new_entity_identities_pre_narration(
        db, campaign, turn, attempt, snapshot, identity_decision_service=service)
    assert outcomes[0] == {
        "temp_id": "tmp-alias", "outcome": str(mara_venn.id),
        "via": "exact_stable", "jit_key": outcomes[0]["jit_key"],
    }
    assert service.adapter.calls == []
    promoted = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=service)
    assert promoted[0].id == mara_venn.id
    assert service.adapter.calls == []
    assert [row.id for row in db.query(type(mara_venn)).all()] == [mara_venn.id]


# ── Pre-narration vs first-visible-chunk ordering (failed-visible audit) ──────

def _streaming_setup():
    from models.dm import DmTurn, DmTurnAttempt
    from models.threads import CampaignThread
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    owner = uuid.uuid4()
    camp_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    db.add(Profile(id=owner, email="stream@example.com"))
    campaign = Campaign(id=camp_id, owner_id=owner, name="Streamed", revision=0)
    db.add(campaign)
    db.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
    db.commit()
    return db, campaign, thread_id


def _coordinated_attempt(db, campaign, thread_id):
    from app.runtime.submissions import accept_submission
    from app.dm.turns import coordinate_turn
    user_id = uuid.uuid4()
    accept_submission(
        db, campaign_id=campaign.id, user_id=user_id,
        raw_content="We greet the stranger.",
        segments=[{"type": "ic", "text": "We greet the stranger."}],
        thread_id=str(thread_id),
    )
    db.commit()
    return coordinate_turn(db, campaign.id, str(thread_id))


def _respond_with_new_entity(public_name, temp_id="tmp_npc_1"):
    from app.dm.contract import CONTRACT_VERSION, normalize_contract
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond",
        "reason": "a stranger arrives",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [{"text": "A stranger steps from the treeline.",
                        "claim_kind": "observation", "origin": "established_state",
                        "visibility": "public"}],
        }],
        "new_entities": [{
            "temp_id": temp_id, "kind": "npc", "public_name": public_name,
        }],
        "open_player_choice": "What do you do?",
    })


def test_ambiguous_identity_defer_leaves_no_visible_narration():
    from sqlalchemy import select
    from models.dm import DMStream, DMStreamChunk, DmTurn, DmTurnAttempt
    from models.world import WorldEntity as _WorldEntity
    from app.dm.narration import execute_validated_turn
    db, campaign, thread_id = _streaming_setup()
    make_entity(db, campaign, "Mara Venn")
    turn, attempt = _coordinated_attempt(db, campaign, thread_id)
    contract = _respond_with_new_entity("Mara")
    defer_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": DEFER}))
    with pytest.raises(ValueError, match="deferred"):
        execute_validated_turn(
            db, turn_id=turn.id, attempt_id=attempt.id, contract=contract,
            publish_realtime=False, identity_decision_service=defer_service)
    # Abort happened before the first visible chunk: no stream, no chunks,
    # no failed-visible audit, no stranded duplicate.
    assert db.query(DMStream).count() == 0
    assert db.query(DMStreamChunk).count() == 0
    assert [row.name for row in db.query(_WorldEntity).all()] == ["Mara Venn"]
    fresh_attempt = db.get(DmTurnAttempt, attempt.id)
    assert fresh_attempt.status not in ("streaming", "failed_visible", "succeeded")
    assert fresh_attempt.identity_resolutions is None
    assert fresh_attempt.stream_id is None
    fresh_turn = db.get(DmTurn, turn.id)
    assert fresh_turn.status not in ("streaming", "failed_visible", "succeeded")


def test_same_name_keep_distinct_narrates_then_commits_with_one_decision():
    from models.world import WorldEntity as _WorldEntity
    from app.dm.narration import execute_validated_turn
    db, campaign, thread_id = _streaming_setup()
    north_guard = make_entity(db, campaign, "The Guard", location_ref="north gate")
    turn, attempt = _coordinated_attempt(db, campaign, thread_id)
    contract = _respond_with_new_entity("The Guard")
    keep_service = DecisionService(FakeDecisionAdapter(
        answers={"resolve_world_entity_identity": KEEP_DISTINCT}))
    out = execute_validated_turn(
        db, turn_id=turn.id, attempt_id=attempt.id, contract=contract,
        publish_realtime=False, identity_decision_service=keep_service)
    assert out.narration.completed and out.narration.chunk_count >= 1
    assert out.turn.status == "succeeded"
    # Exactly one bounded decision (pre-narration); commit revalidated/applied.
    assert len(keep_service.adapter.calls) == 1
    rows = db.query(_WorldEntity).all()
    assert sorted(row.name for row in rows) == ["The Guard", "The Guard"]
    assert {row.id for row in rows} != {north_guard.id}
    south = next(row for row in rows if row.id != north_guard.id)
    assert south.details["identity_resolution"]["outcome"] == KEEP_DISTINCT
