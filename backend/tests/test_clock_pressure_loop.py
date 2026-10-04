"""Clock pressure loop: post-turn advances clocks, the forward DM reacts.

Post-turn evaluation stays the only writer of clock state. The DM context
shows each active clock and, when a stage was crossed or a clock finished
since the last DM turn, a directive to show it in the world.
"""
from __future__ import annotations

import pytest

from app.decisions import DecisionService
from app.dm.turns import DM_TURN_RESOLVED
from app.world import clocks as C
from models.campaigns import Campaign
from models.world import CampaignClock

from tests.test_clocks_218 import _NeverCall, _commit, _mkclock, _play, _range, _rev, _scripted, _setup


def _evaluate(db, c, decision_service=None):
    lo, hi = _play(db, c, 1)
    return C.consolidate_clocks_for_range(
        db, c.id, lo, hi, _range(db, c, lo, hi),
        decision_service=decision_service or DecisionService(_NeverCall()),
    )


def _view(db, c):
    return C.dm_pressure_view(db, c.id, through_sequence=_rev(db, c), turn_event_type=DM_TURN_RESOLVED)


def _dm_turn(db, c):
    _commit(db, c, etype=DM_TURN_RESOLVED)


def test_stage_crossing_owes_one_directive_until_a_dm_turn():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, name="The Tithe Collectors", threshold=4,
             stages=[{"at": 2, "label": "riders grow bold"}])

    _evaluate(db, c)
    [state] = _view(db, c)
    assert (state["progress"], state["current_stage"], state["directive"]) == (1, None, None)
    assert state["next_stage"] == {"at": 2, "label": "riders grow bold"}

    _evaluate(db, c)
    [state] = _view(db, c)
    assert state["current_stage"] == "riders grow bold"
    assert "reached its 'riders grow bold' stage (2/4)" in state["directive"]
    assert state["next_stage"] == {"at": 4, "label": "fills"}

    _dm_turn(db, c)
    [state] = _view(db, c)
    assert state["directive"] is None  # delivered: the DM had its turn
    assert state["status"] == "active"
    db.close()


def test_advancing_without_crossing_a_stage_owes_nothing():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, threshold=5, stages=[{"at": 4, "label": "the ritual quickens"}])
    for _ in range(3):
        _evaluate(db, c)
    [state] = _view(db, c)
    assert state["progress"] == 3 and state["directive"] is None
    db.close()


def test_filled_clock_directs_its_consequence_then_drops_out():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, name="Siege", threshold=2, completion_effect={"description": "the gates fall"},
             advancement_criteria={"kind": "deterministic", "event_types": ["game.play"], "max_advance": 2})
    lo, hi = _play(db, c, 2)
    C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                   decision_service=DecisionService(_NeverCall()))

    [state] = _view(db, c)
    assert state["status"] == "completed"
    assert "'Siege' has filled (2/2)" in state["directive"]
    assert "Consequence on record: the gates fall." in state["directive"]

    _dm_turn(db, c)
    assert _view(db, c) == []  # finished and delivered
    db.close()


def test_judged_completion_directs_the_aftermath_not_a_consequence():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, name="Usurper", threshold=6,
             advancement_criteria={"kind": "semantic", "max_advance": 1},
             completion_criteria={"kind": "semantic", "description": "the council swears fealty"})
    _evaluate(db, c, decision_service=_scripted("COMPLETE"))

    [state] = _view(db, c)
    assert "'Usurper' has ended (the council swears fealty)" in state["directive"]
    assert "has filled" not in state["directive"]
    db.close()


def test_already_full_clock_completes_as_filled_not_criteria_met():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=2)
    db.get(CampaignClock, clock.id).progress = 2
    db.flush()
    _evaluate(db, c)
    assert db.get(CampaignClock, clock.id).resolution["reason"] == "threshold_reached"
    db.close()


def test_view_is_as_of_the_attempt_revision():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, threshold=4, stages=[{"at": 1, "label": "whispers"}])
    before = _rev(db, c)
    _evaluate(db, c)
    [state] = C.dm_pressure_view(db, c.id, through_sequence=before, turn_event_type=DM_TURN_RESOLVED)
    assert state["directive"] is None  # the crossing came after this attempt's revision
    db.close()


@pytest.mark.parametrize("visibility, expected", [("campaign", "campaign"), ("dm_only", "dm_only")])
def test_dm_context_carries_pressures_with_required_directive(visibility, expected, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database import Base
    from tests.support.world_writes import commit_world_write
    from tests.test_dm_mechanics_229 import _submit
    from app.dm.context import LaneName
    from app.dm.execution import _assemble_production_context
    from models.profiles import Profile
    from models.threads import CampaignThread
    import uuid

    engine = create_engine(f"sqlite:///{tmp_path / 'p.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    owner, camp_id, thread_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.add(Profile(id=owner, email="o@x.com"))
    db.add(Campaign(id=camp_id, owner_id=owner, name="T", revision=0))
    db.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
    db.commit()
    c = db.get(Campaign, camp_id)
    commit_world_write(db, c.id, _rev(db, c), C.create_clock, name="Tithe", threshold=3,
                       advancement_criteria={"kind": "deterministic", "event_types": ["game.play"]},
                       stages=[{"at": 1, "label": "riders arrive"}], status="active",
                       visibility=visibility, provenance={"source": "test"})
    _evaluate(db, c)

    _turn, attempt = _submit(db, camp_id, thread_id)
    packet = _assemble_production_context(db, attempt.id)

    [lane] = [lane for lane in packet.lanes if lane.name == LaneName.PRESSURES]
    [record] = lane.records
    assert record.required and record.use == "adjudication_only" and record.visibility == expected
    assert "riders arrive" in record.value["directive"]
    db.close()


def test_prompt_explains_pressures():
    from app.dm.adjudication import FORWARD_DM_SYSTEM

    assert "PRESSURES:" in FORWARD_DM_SYSTEM and "You never advance a\nclock yourself" in FORWARD_DM_SYSTEM
