"""Issue #202 -- explicit authoritative forward-DM context lanes."""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models import Campaign, CampaignMember, Character, Dnd5eCharacterSheet, Profile  # noqa: E402
from app.campaigns.events import commit_campaign_mutation  # noqa: E402
from app.dm.context import (  # noqa: E402
    AuthorizationScope,
    ContextAssemblyError,
    ContextAudience,
    ContextAuthorizationError,
    ContextBudget,
    ContextBudgetError,
    ContextRecord,
    LaneName,
    LANE_ORDER,
    MissingAuthoritativeContextError,
    SourceRef,
    assemble_attempt_context,
    assemble_context_packet,
)
from app.dm.turns import coordinate_turn  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import create_private_thread, get_or_create_campaign_thread  # noqa: E402


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "forward_dm_context_cases.json").read_text()
)


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    return engine


def _campaign(*, private=False):
    factory = sessionmaker(bind=_engine(), expire_on_commit=False)
    db = factory()
    owner, other = uuid.uuid4(), uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=other, email="other@example.com"),
    ])
    campaign = Campaign(id=uuid.uuid4(), owner_id=owner, name="Fixture campaign", revision=0)
    db.add(campaign)
    db.flush()
    db.add_all([
        CampaignMember(campaign_id=campaign.id, user_id=owner, role="owner"),
        CampaignMember(campaign_id=campaign.id, user_id=other, role="player"),
    ])
    db.commit()
    if private:
        thread = create_private_thread(
            db, campaign.id, created_by=owner, member_ids=[owner, other], title="Whisper"
        )
    else:
        thread = get_or_create_campaign_thread(db, campaign.id, created_by=owner)
    db.commit()
    return factory, campaign.id, owner, other, str(thread.id)


def _add_character(db, owner, *, name="Aria"):
    character = Character(id=uuid.uuid4(), owner_id=owner, system="dnd5e", name=name)
    db.add(character)
    db.flush()
    sheet = Dnd5eCharacterSheet.from_frontend(
        {"name": name, "current_hp": 8, "max_hp": 11, "conditions": ["blessed"]}, owner
    )
    sheet.character_id = character.id
    db.add(sheet)
    db.commit()
    return character


def _record(
    record_id,
    *,
    campaign_id,
    visibility="campaign",
    use="narration_eligible",
    thread_ids=(),
    user_ids=(),
    required=False,
    priority=50,
    value=None,
):
    return ContextRecord(
        record_id=record_id,
        value=value or {"text": record_id},
        sources=[SourceRef(
            source_type="fixture", source_id=record_id, source_version="1",
            campaign_revision=0, provenance={"fixture": True},
        )],
        authorization=AuthorizationScope(
            campaign_id=campaign_id, thread_ids=list(thread_ids), user_ids=list(user_ids)
        ),
        visibility=visibility,
        use=use,
        required=required,
        priority=priority,
    )


def _empty_records():
    return {lane: [] for lane in LANE_ORDER}


def _fixture(name):
    return next(case for case in FIXTURES if case["name"] == name)


def test_solo_fixture_builds_all_named_lanes_and_protected_pc_authority():
    case = _fixture("solo")
    factory, campaign_id, owner, _other, thread_id = _campaign()
    db = factory()
    character = _add_character(db, owner)
    submission = accept_submission(
        db, campaign_id=campaign_id, user_id=owner, character_id=character.id,
        raw_content="I open the door", segments=[{"type": "ic", "text": "I open the door"}],
        thread_id=thread_id,
    )
    db.commit()
    _turn, attempt = coordinate_turn(db, campaign_id, thread_id)

    packet = assemble_attempt_context(db, attempt.id)

    assert [lane.name for lane in packet.lanes] == list(LANE_ORDER)
    inputs = next(lane for lane in packet.lanes if lane.name == LaneName.PLAYER_INPUTS)
    assert len(inputs.records) == case["expected_inputs"]
    assert inputs.records[0].value["submission_id"] == str(submission.id)
    protected = next(lane for lane in packet.lanes if lane.name == LaneName.PROTECTED_PCS)
    assert protected.records[0].value == {
        "character_id": str(character.id), "name": "Aria", "owner_user_id": str(owner),
        "control_policy": "player_only", "dm_may_not_choose_actions": True,
    }
    assert packet.observability.serialized_bytes == len(packet.serialize_for_adjudication().encode())
    assert all(metric.assembly_ms >= 0 for metric in packet.observability.lanes)


def test_multiplayer_fixture_preserves_exact_mixed_ic_ooc_segments():
    case = _fixture("multiplayer_mixed_ic_ooc")
    factory, campaign_id, owner, other, thread_id = _campaign()
    db = factory()
    first = accept_submission(
        db, campaign_id=campaign_id, user_id=owner, raw_content="I wave (carefully)",
        segments=[
            {"type": "ic", "text": "I wave."},
            {"type": "ooc", "text": "Carefully; I am not surrendering."},
        ], thread_id=thread_id,
    )
    second = accept_submission(
        db, campaign_id=campaign_id, user_id=other, raw_content="I watch",
        segments=[{"type": "ic", "text": "I watch the guard."}], thread_id=thread_id,
    )
    db.commit()
    _turn, attempt = coordinate_turn(db, campaign_id, thread_id)

    packet = assemble_attempt_context(db, attempt.id)
    inputs = next(lane for lane in packet.lanes if lane.name == LaneName.PLAYER_INPUTS)

    assert len(inputs.records) == case["expected_inputs"]
    assert [record.value["submission_id"] for record in inputs.records] == [str(first.id), str(second.id)]
    assert [segment["segment_type"] for segment in inputs.records[0].value["segments"]] == ["ic", "ooc"]
    assert inputs.records[0].value["segments"][1]["text"] == "Carefully; I am not surrendering."


def test_hidden_npc_truth_is_adjudication_only_and_deterministic():
    case = _fixture("hidden_npc_truth")
    campaign_id, thread_id = str(uuid.uuid4()), str(uuid.uuid4())
    audience = ContextAudience(
        campaign_id=campaign_id, thread_id=thread_id, audience="campaign", user_ids=[str(uuid.uuid4())]
    )
    records = _empty_records()
    secret = _record(
        case["narration_excludes"], campaign_id=campaign_id, visibility="dm_only",
        use="adjudication_only", value={"npc_id": "npc-1", "truth": "The guide is the spy."},
    )
    records[LaneName.KNOWLEDGE_VISIBILITY] = [secret]
    packet = assemble_context_packet(audience=audience, records=records)
    first = packet.serialize_for_adjudication()
    packet.observability.assembly_ms += 100
    second = packet.serialize_for_adjudication()

    assert "The guide is the spy" in first
    assert case["narration_excludes"] not in packet.serialize_for_narration()
    assert "The guide is the spy" not in packet.serialize_for_narration()
    assert first == second


def test_private_audience_fixture_is_scoped_and_campaign_attempt_cannot_receive_it():
    case = _fixture("private_audience")
    campaign_id, private_thread, member = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    private_record = _record(
        "private-clue", campaign_id=campaign_id, visibility=case["visibility"],
        thread_ids=[private_thread], user_ids=[member],
    )
    private_packet = assemble_context_packet(
        audience=ContextAudience(
            campaign_id=campaign_id, thread_id=private_thread, audience="private", user_ids=[member]
        ),
        records={**_empty_records(), LaneName.RELEVANT_CANON: [private_record]},
    )
    assert case["expected_included"] is True
    assert "private-clue" in private_packet.serialize_for_adjudication()

    public_packet = assemble_context_packet(
        audience=ContextAudience(
            campaign_id=campaign_id, thread_id=str(uuid.uuid4()), audience="campaign", user_ids=[member]
        ),
        records={**_empty_records(), LaneName.RELEVANT_CANON: [private_record]},
    )
    canon = next(lane for lane in public_packet.lanes if lane.name == LaneName.RELEVANT_CANON)
    assert canon.records == []
    assert any(d.reason == "not_authorized_for_attempt_audience" for d in public_packet.observability.budget_decisions)

    with pytest.raises(ContextAuthorizationError):
        assemble_context_packet(
            audience=ContextAudience(
                campaign_id=campaign_id, thread_id=str(uuid.uuid4()), audience="campaign", user_ids=[member]
            ),
            records={
                **_empty_records(),
                LaneName.RELEVANT_CANON: [private_record.model_copy(update={"required": True})],
            },
        )


def test_post_turn_lag_fixture_keeps_recent_committed_event_directly_available():
    case = _fixture("post_turn_lag")
    factory, campaign_id, owner, _other, thread_id = _campaign()
    db = factory()
    _campaign_row, event = commit_campaign_mutation(
        db, campaign_id, expected_revision=0, event_type="scene.door_opened",
        payload={"door_id": "door-7", "open": True}, visibility="campaign",
    )
    submission = accept_submission(
        db, campaign_id=campaign_id, user_id=owner, raw_content="I step through",
        segments=[{"type": "ic", "text": "I step through."}], thread_id=thread_id,
    )
    db.commit()
    _turn, attempt = coordinate_turn(db, campaign_id, thread_id)

    packet = assemble_attempt_context(db, attempt.id)
    history = next(lane for lane in packet.lanes if lane.name.value == case["lane"])

    assert history.records[-1].value["event_id"] == str(event.id)
    assert history.records[-1].value["payload"] == {"door_id": "door-7", "open": True}
    assert history.records[-1].sources[0].source_type == case["expected_source"]
    assert str(submission.id) in packet.serialize_for_adjudication()


def test_repair_directive_fixture_is_separate_and_hidden_from_narration():
    case = _fixture("repair_directive")
    campaign_id, thread_id = str(uuid.uuid4()), str(uuid.uuid4())
    repair = _record(
        "repair-1", campaign_id=campaign_id, visibility="dm_only", use="adjudication_only",
        required=True, priority=100,
        value={"validator": "agency", "directive": "Retry without choosing the PC action."},
    )
    packet = assemble_context_packet(
        audience=ContextAudience(
            campaign_id=campaign_id, thread_id=thread_id, audience="campaign", user_ids=[str(uuid.uuid4())]
        ),
        records={**_empty_records(), LaneName.REPAIR_DIRECTIVES: [repair]},
    )
    lane = next(item for item in packet.lanes if item.name.value == case["lane"])
    assert case["expected_included"] is True and lane.records[0].record_id == "repair-1"
    assert "Retry without choosing" in packet.serialize_for_adjudication()
    assert "Retry without choosing" not in packet.serialize_for_narration()


def test_budget_pressure_fixture_omits_optional_records_but_never_required_authority():
    case = _fixture("context_budget_pressure")
    campaign_id, thread_id = str(uuid.uuid4()), str(uuid.uuid4())
    required = _record(
        "exact-input", campaign_id=campaign_id, required=True, priority=100,
        value={"text": "required " * 80},
    )
    optional = _record(
        "old-canon", campaign_id=campaign_id, priority=1,
        value={"text": "optional " * 500},
    )
    packet = assemble_context_packet(
        audience=ContextAudience(
            campaign_id=campaign_id, thread_id=thread_id, audience="campaign", user_ids=[str(uuid.uuid4())]
        ),
        records={
            **_empty_records(), LaneName.PLAYER_INPUTS: [required],
            LaneName.RELEVANT_CANON: [optional],
        },
        budget=ContextBudget(max_bytes=6000, max_tokens=1500),
    )
    assert case["expected_optional_omission"] is True
    assert "exact-input" in packet.serialize_for_adjudication()
    assert "old-canon" not in packet.serialize_for_adjudication()
    omission = next(d for d in packet.observability.budget_decisions if d.record_id == "old-canon")
    assert omission.reason == "total_budget_pressure"

    with pytest.raises(ContextBudgetError) as exc_info:
        assemble_context_packet(
            audience=packet.audience,
            records={
                **_empty_records(),
                LaneName.PLAYER_INPUTS: [required.model_copy(update={"value": {"text": "x" * 8000}})],
            },
            budget=ContextBudget(max_bytes=2048, max_tokens=512),
        )
    assert exc_info.value.code == "required_context_exceeds_budget"
    assert exc_info.value.decisions[-1].reason == "required_packet_exceeds_total_budget"


def test_required_unavailable_lane_and_stale_attempt_fail_instead_of_guessing():
    campaign_id, thread_id = str(uuid.uuid4()), str(uuid.uuid4())
    audience = ContextAudience(
        campaign_id=campaign_id, thread_id=thread_id, audience="campaign", user_ids=[str(uuid.uuid4())]
    )
    with pytest.raises(MissingAuthoritativeContextError) as exc_info:
        assemble_context_packet(
            audience=audience, records=_empty_records(),
            lane_status={LaneName.CURRENT_SCENE: "unavailable"},
            source_errors={LaneName.CURRENT_SCENE: ["scene reader timed out"]},
        )
    assert "scene reader timed out" in str(exc_info.value)

    factory, cid, owner, _other, tid = _campaign()
    db = factory()
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="Go",
        segments=[{"type": "ic", "text": "Go"}], thread_id=tid,
    )
    db.commit()
    _turn, attempt = coordinate_turn(db, cid, tid)
    campaign = db.get(Campaign, cid)
    campaign.revision += 1
    db.commit()
    with pytest.raises(MissingAuthoritativeContextError, match="stale"):
        assemble_attempt_context(db, attempt.id)


def test_fixture_manifest_covers_every_required_issue_case():
    assert {case["name"] for case in FIXTURES} == {
        "solo", "multiplayer_mixed_ic_ooc", "hidden_npc_truth", "private_audience",
        "post_turn_lag", "repair_directive", "context_budget_pressure",
    }
