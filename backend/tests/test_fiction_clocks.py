"""Fiction-driven clocks: the judge weighs what happened, not event counts.

Playtest 2026-10-04: the seeded pressure ticked +1 per two DM turns and
"filled" (the wells give out) exactly while the players were restoring the
water, because semantic evaluation only ever saw event ids and types.
"""
from __future__ import annotations

from sqlalchemy import select

from app.decisions import DecisionService
from app.dm.contract import CONTRACT_VERSION, normalize_contract
from app.dm.turns import DM_TURN_RESOLVED
from app.world import clocks as C
from models.campaigns import Campaign, CampaignDomainEvent
from models.world import CampaignClock
from tests.support.fake_decisions import FakeDecisionAdapter
from tests.support.world_writes import commit_world_write
from tests.test_dm_mechanics_229 import _run, table  # noqa: F401  (fixture)

PRESSURE = "The Dry Wells gains ground while the party fails to check it."


def _turn(public_text):
    claims = [{"text": public_text, "claim_kind": "observation", "origin": "dm_adjudication", "visibility": "public"}]
    raw = {"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "story",
           "beats": [{"id": "beat_1", "type": "narration", "claims": claims}]}
    return lambda packet, feedback=None: normalize_contract(raw)


def _semantic_clock(s, camp_id):
    c = s.get(Campaign, camp_id)
    clock, _ = commit_world_write(
        s, c.id, int(c.revision or 0), C.create_clock, name="The Dry Wells", threshold=5,
        advancement_criteria={"kind": "semantic", "description": PRESSURE,
                              "event_types": [DM_TURN_RESOLVED], "max_advance": 1},
        completion_criteria={"kind": "semantic", "description": "The party decisively ends the drought",
                             "event_types": [DM_TURN_RESOLVED]},
        status="active", provenance={"source": "test"},
    )
    s.commit()
    return clock


def _evaluate(s, camp_id, answer):
    adapter = FakeDecisionAdapter(answers={C.CLOCK_QUESTION_ID: answer})
    events = s.execute(select(CampaignDomainEvent).where(CampaignDomainEvent.campaign_id == camp_id)
                       .order_by(CampaignDomainEvent.sequence)).scalars().all()
    out = C.consolidate_clocks_for_range(s, camp_id, 1, int(events[-1].sequence), events,
                                         decision_service=DecisionService(adapter))
    s.commit()
    return adapter, out


def test_advancement_description_is_kept_for_the_judge():
    criteria = C.validate_advancement_criteria(
        {"kind": "semantic", "description": PRESSURE, "event_types": [DM_TURN_RESOLVED]})
    assert criteria["description"] == PRESSURE
    assert "description" not in C.validate_advancement_criteria(
        {"kind": "deterministic", "event_types": [DM_TURN_RESOLVED]})


def test_judge_sees_the_story_and_the_criteria(table):
    s, camp_id, thread_id, _ = table
    clock = _semantic_clock(s, camp_id)
    _turn_row, attempt = _run(s, camp_id, thread_id, _turn("Ledger riders padlock the last public well."))
    # Private canon can ride in any contract (e.g. a private thread); inject
    # one so the filter is exercised regardless of validator scoping.
    snapshot = dict(attempt.contract_snapshot)
    snapshot["beats"] = snapshot["beats"] + [{"id": "beat_2", "type": "narration", "claims": [{
        "text": "The Ledger secretly poisons the cistern.", "claim_kind": "observation",
        "origin": "dm_adjudication", "visibility": "dm_private"}]}]
    attempt.contract_snapshot = snapshot
    s.commit()

    adapter, out = _evaluate(s, camp_id, "ADVANCE_1")

    state = adapter.calls[0]["state"]
    assert state["advancement_criteria"]["description"] == PRESSURE
    [turn_ref] = [ref for ref in state["evidence"] if ref["event_type"] == DM_TURN_RESOLVED]
    assert "Player: I pull the lever." in turn_ref["story"]
    assert "DM: Ledger riders padlock the last public well." in turn_ref["story"]
    assert "poisons" not in turn_ref["story"]  # DM-private claims never enter evidence
    assert out["results"][0]["outcome"] == "advanced"
    assert s.get(CampaignClock, clock.id).progress == 1


def test_party_undoing_the_pressure_can_hold_it(table):
    s, camp_id, thread_id, _ = table
    clock = _semantic_clock(s, camp_id)
    _run(s, camp_id, thread_id, _turn("The sluice-gate lifts and clear water runs to the wells."))

    _adapter, out = _evaluate(s, camp_id, "NO_CHANGE")

    assert s.get(CampaignClock, clock.id).progress == 0
    assert not s.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.event_type == C.CLOCK_ADVANCED_EVENT)).scalars().first()


def test_member_frames_drop_restricted_stories():
    clock = CampaignClock(name="Hidden", threshold=3, status="active", progress=0, revision=1,
                          advancement_criteria={"kind": "semantic"}, visibility="campaign", provenance={})
    refs = [{"sequence": 1, "event_id": "a", "event_type": DM_TURN_RESOLVED, "visibility": "private",
             "story": "Player: a private whisper"},
            {"sequence": 2, "event_id": "b", "event_type": DM_TURN_RESOLVED, "visibility": "public",
             "story": "DM: the gate opens"}]
    frame = C.build_clock_frame(clock, evidence=refs, evidence_total=2, from_sequence=1, to_sequence=2,
                                is_authority=False)
    assert [ref["story"] for ref in frame.state["evidence"]] == ["DM: the gate opens"]


def test_story_text_is_bounded_newest_first(table):
    s, camp_id, thread_id, _ = table
    for n in range(14):
        _run(s, camp_id, thread_id, _turn(f"Turn {n}: " + "dust " * 120))
    events = s.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.event_type == DM_TURN_RESOLVED).order_by(CampaignDomainEvent.sequence)).scalars().all()

    refs = C.story_evidence(s, events)

    assert all(len(ref.get("story", "")) <= C.STORY_CHARS_PER_EVENT for ref in refs)
    assert sum(len(ref.get("story", "")) for ref in refs) <= C.STORY_CHARS_TOTAL
    assert "story" in refs[-1] and "story" not in refs[0]  # newest keep their story
