"""Issue #200 — durable DM turn/attempt state machine and multiplayer assembly."""
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402
from models import Campaign, Profile, CampaignMember  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from app.dm.turns import (  # noqa: E402
    AttemptSupersededError,
    coordinate_turn,
    mark_streaming_started,
    commit_turn,
    discard_superseded_result,
    recover_stuck_attempts,
    mark_attempt_running,
    StreamBoundaryError,
    StaleRevisionError,
)
from app.campaigns.events import commit_campaign_mutation  # noqa: E402


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _setup_campaign():
    eng = _engine()
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    user2 = uuid.uuid4()
    db.add_all([Profile(id=owner, email="owner@example.com"), Profile(id=user2, email="p2@example.com")])
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Test Campaign", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.add(CampaignMember(campaign_id=camp.id, user_id=user2, role="player"))
    db.commit()
    thread = get_or_create_campaign_thread(db, camp.id, created_by=owner)
    db.commit()
    db.refresh(camp)
    db.refresh(thread)
    return Fac, camp.id, owner, user2, str(thread.id)


def test_solo_turn():
    Fac, cid, owner, _p2, tid = _setup_campaign()
    db = Fac()
    sub = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="I open door", segments=[{"type": "ic", "text": "I open door"}], thread_id=tid)
    db.commit()
    result = coordinate_turn(db, cid, tid)
    assert result is not None
    turn, attempt = result
    assert turn.submission_ids == [str(sub.id)]
    assert turn.source_revision == 0
    assert turn.input_set_revision == 1
    assert attempt.attempt_number == 1
    assert turn.status == "pending"


def test_two_player_same_moment_accumulation():
    Fac, cid, owner, p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="I go left", segments=[{"type": "ic", "text": "I go left"}], thread_id=tid)
    db.commit()
    t1, a1 = coordinate_turn(db, cid, tid)
    s2 = accept_submission(db, campaign_id=cid, user_id=p2, raw_content="I go right", segments=[{"type": "ic", "text": "I go right"}], thread_id=tid)
    db.commit()
    t2, a2 = coordinate_turn(db, cid, tid)
    assert str(t1.id) == str(t2.id)
    assert set(t2.submission_ids) == {str(s1.id), str(s2.id)}
    assert a2.attempt_number == 2
    assert str(a2.parent_attempt_id) == str(a1.id)
    db2 = Fac()
    old = db2.get(models.DmTurnAttempt, a1.id)
    assert old.status == "superseded"
    assert old.invalidation_reason == "new_eligible_submission_pre_stream"
    camp = db2.get(Campaign, cid)
    assert camp.revision == 0


def test_new_input_before_stream_obsoletes_without_mutating_campaign_truth():
    Fac, cid, owner, p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="first", segments=[{"type": "ic", "text": "first"}], thread_id=tid)
    db.commit()
    t1, a1 = coordinate_turn(db, cid, tid)
    s2 = accept_submission(db, campaign_id=cid, user_id=p2, raw_content="second", segments=[{"type": "ic", "text": "second"}], thread_id=tid)
    db.commit()
    t2, a2 = coordinate_turn(db, cid, tid)
    assert a1.id != a2.id
    discard_superseded_result(db, a1.id, reason="test obsolete")
    camp = db.get(Campaign, cid)
    assert camp.revision == 0
    assert set(t2.submission_ids) == {str(s1.id), str(s2.id)}


def test_new_input_after_stream_start_cannot_silently_change_input_set():
    Fac, cid, owner, p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="first", segments=[{"type": "ic", "text": "first"}], thread_id=tid)
    db.commit()
    t1, a1 = coordinate_turn(db, cid, tid)
    mark_streaming_started(db, t1.id, a1.id)
    db = Fac()
    s2 = accept_submission(db, campaign_id=cid, user_id=p2, raw_content="late", segments=[{"type": "ic", "text": "late"}], thread_id=tid)
    db.commit()
    with pytest.raises(StreamBoundaryError):
        coordinate_turn(db, cid, tid)
    db3 = Fac()
    turn = db3.get(models.DmTurn, t1.id)
    assert turn.submission_ids == [str(s1.id)]
    assert turn.status == "streaming"


def test_stale_source_revision_cannot_commit_over_newer_state():
    Fac, cid, owner, _p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="action", segments=[{"type": "ic", "text": "action"}], thread_id=tid)
    db.commit()
    t1, a1 = coordinate_turn(db, cid, tid)
    mark_streaming_started(db, t1.id, a1.id)
    db2 = Fac()
    commit_campaign_mutation(db2, cid, expected_revision=0, event_type="campaign.external", payload={"x": 1})
    db3 = Fac()
    with pytest.raises(StaleRevisionError) as ei:
        commit_turn(db3, t1.id, a1.id, expected_revision=0)
    assert ei.value.expected_revision == 0 and ei.value.actual_revision == 1
    db4 = Fac()
    camp = db4.get(Campaign, cid)
    assert camp.revision == 1


def test_campaign_cannot_advance_past_unresolved_streaming_turn():
    Fac, cid, owner, p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="first", segments=[{"type": "ic", "text": "first"}], thread_id=tid)
    db.commit()
    t1, a1 = coordinate_turn(db, cid, tid)
    mark_streaming_started(db, t1.id, a1.id)
    db2 = Fac()
    turn2, att2, _event = commit_turn(db2, t1.id, a1.id)
    assert turn2.status == "succeeded"
    db3 = Fac()
    s2 = accept_submission(db3, campaign_id=cid, user_id=p2, raw_content="next", segments=[{"type": "ic", "text": "next"}], thread_id=tid)
    db3.commit()
    t_new, _a_new = coordinate_turn(db3, cid, tid)
    assert str(t_new.id) != str(t1.id)
    assert t_new.status == "pending"


def test_worker_crash_leaves_state_recoverable_and_retryable():
    Fac, cid, owner, _p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="crash", segments=[{"type": "ic", "text": "crash"}], thread_id=tid)
    db.commit()
    _t1, a1 = coordinate_turn(db, cid, tid)
    mark_attempt_running(db, a1.id)
    db2 = Fac()
    att = db2.get(models.DmTurnAttempt, a1.id)
    att.started_at = datetime.now(timezone.utc) - timedelta(seconds=1000)
    db2.commit()
    db3 = Fac()
    recovered = recover_stuck_attempts(db3, lease_seconds=300)
    assert recovered == 1
    db4 = Fac()
    att2 = db4.get(models.DmTurnAttempt, a1.id)
    assert att2.status == "prepared"
    # Streaming attempts must NOT be auto-recovered (visible partial)
    Fac2, cid2, owner2, _p22, tid2 = _setup_campaign()
    db5 = Fac2()
    ss = accept_submission(db5, campaign_id=cid2, user_id=owner2, raw_content="s", segments=[{"type": "ic", "text": "s"}], thread_id=tid2)
    db5.commit()
    tt, aa = coordinate_turn(db5, cid2, tid2)
    mark_streaming_started(db5, tt.id, aa.id)
    db6 = Fac2()
    aa2 = db6.get(models.DmTurnAttempt, aa.id)
    aa2.started_at = datetime.now(timezone.utc) - timedelta(seconds=1000)
    db6.commit()
    rec2 = recover_stuck_attempts(db6, lease_seconds=300)
    assert rec2 == 0
    db7 = Fac2()
    aa3 = db7.get(models.DmTurnAttempt, aa.id)
    assert aa3.status == "streaming"


def test_durable_input_set_and_revisions_are_auditable():
    Fac, cid, owner, p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="obs", segments=[{"type": "ic", "text": "obs"}], thread_id=tid)
    db.commit()
    t, a = coordinate_turn(db, cid, tid)
    assert t.assembly_window_start is not None
    assert t.assembly_window_end is not None
    assert len(t.submission_ids) == 1
    s2 = accept_submission(db, campaign_id=cid, user_id=p2, raw_content="obs2", segments=[{"type": "ic", "text": "obs2"}], thread_id=tid)
    db.commit()
    t2, a2 = coordinate_turn(db, cid, tid)
    assert t2.input_set_revision == 2
    db2 = Fac()
    old = db2.get(models.DmTurnAttempt, a.id)
    assert old.invalidation_reason == "new_eligible_submission_pre_stream"


def test_obsolete_attempt_result_discarded_harmlessly():
    Fac, cid, owner, p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="a", segments=[{"type": "ic", "text": "a"}], thread_id=tid)
    db.commit()
    t1, a1 = coordinate_turn(db, cid, tid)
    s2 = accept_submission(db, campaign_id=cid, user_id=p2, raw_content="b", segments=[{"type": "ic", "text": "b"}], thread_id=tid)
    db.commit()
    t2, a2 = coordinate_turn(db, cid, tid)
    # old attempt finishes after supersession — must be discarded, not committed
    assert a1.status == "superseded" or Fac().get(models.DmTurnAttempt, a1.id).status == "superseded"
    db3 = Fac()
    with pytest.raises(AttemptSupersededError):
        commit_turn(db3, t1.id, a1.id)
    # new attempt can still commit
    mark_streaming_started(db3, t2.id, a2.id)
    db4 = Fac()
    turn_ok, _att_ok, event = commit_turn(db4, t2.id, a2.id)
    assert turn_ok.status == "succeeded"
    assert event is not None
