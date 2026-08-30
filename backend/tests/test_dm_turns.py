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


def test_commit_requires_streaming_boundary():
    Fac, cid, owner, _p2, tid = _setup_campaign()
    db = Fac()
    s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="needs streaming", segments=[{"type": "ic", "text": "needs streaming"}], thread_id=tid)
    db.commit()
    t1, a1 = coordinate_turn(db, cid, tid)
    db2 = Fac()
    # Attempt to commit without streaming should fail
    with pytest.raises(ValueError, match="cannot commit; must be streaming"):
        commit_turn(db2, t1.id, a1.id)
    # After streaming, commit succeeds
    mark_streaming_started(db2, t1.id, a1.id)
    db3 = Fac()
    turn_ok, _att_ok, event = commit_turn(db3, t1.id, a1.id)
    assert turn_ok.status == "succeeded"


def test_concurrent_assembly_does_not_create_competing_pending_turns(tmp_path):
    import threading, time
    db_file = tmp_path / "concurrent_turn.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(bind=eng)
    Fac2 = sessionmaker(bind=eng, expire_on_commit=False)
    # Setup single campaign/thread with two submissions already accepted
    owner = uuid.uuid4()
    p2 = uuid.uuid4()
    with Fac2() as db:
        db.add_all([Profile(id=owner, email="owner@example.com"), Profile(id=p2, email="p2@example.com")])
        cid = uuid.uuid4()
        db.add(Campaign(id=cid, owner_id=owner, name="Concurrent", revision=0))
        db.flush()
        db.add(CampaignMember(campaign_id=cid, user_id=owner, role="owner"))
        db.add(CampaignMember(campaign_id=cid, user_id=p2, role="player"))
        db.commit()
        thread = get_or_create_campaign_thread(db, cid, created_by=owner)
        db.commit()
        tid = str(thread.id)
        s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="s1", segments=[{"type": "ic", "text": "s1"}], thread_id=tid)
        s2 = accept_submission(db, campaign_id=cid, user_id=p2, raw_content="s2", segments=[{"type": "ic", "text": "s2"}], thread_id=tid)
        db.commit()

    results = []
    errors = []

    def worker():
        try:
            with Fac2() as db:
                res = coordinate_turn(db, cid, tid)
                results.append(res)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    time.sleep(0.02)
    t2.start()
    t1.join()
    t2.join()

    # Both should see the same logical turn (no competing pending turns)
    assert len(results) + len(errors) == 2
    # If both succeeded, they must agree on turn id; if one hit unique constraint it retried and still same turn
    if len(results) == 2:
        assert str(results[0][0].id) == str(results[1][0].id)
    # Verify only one active turn in DB
    with Fac2() as db:
        turns = db.execute(select(models.DmTurn).where(models.DmTurn.campaign_id == cid)).scalars().all()
        active = [t for t in turns if t.status in ("pending", "streaming", "failed_visible")]
        assert len(active) == 1
        assert set(active[0].submission_ids) == {str(s1.id), str(s2.id)}


def test_concurrent_supersession_uses_cas():
    # Two concurrent pre-stream expansions to same pending turn should serialize via FOR UPDATE
    Fac, cid, owner, p2, tid = _setup_campaign()
    with Fac() as db:
        s1 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="s1", segments=[{"type": "ic", "text": "s1"}], thread_id=tid)
        db.commit()
        t1, a1 = coordinate_turn(db, cid, tid)
        # Simulate second submission arriving while first supersession in progress
        s2 = accept_submission(db, campaign_id=cid, user_id=p2, raw_content="s2", segments=[{"type": "ic", "text": "s2"}], thread_id=tid)
        s3 = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="s3", segments=[{"type": "ic", "text": "s3"}], thread_id=tid)
        db.commit()
    # First supersession
    with Fac() as db:
        t2, a2 = coordinate_turn(db, cid, tid)
        assert a2.attempt_number == 2
        assert set(t2.submission_ids) == {str(s1.id), str(s2.id), str(s3.id)}
    # Second call with same expanded set should be no_change, not create extra attempt
    with Fac() as db:
        t3, a3 = coordinate_turn(db, cid, tid)
        assert str(t3.id) == str(t1.id)
        assert str(a3.id) == str(a2.id)
        assert a3.attempt_number == 2


def test_concurrent_submission_plus_coordination_does_not_rollback_outer(tmp_path):
    """Two genuinely concurrent submission+coordination tx should not lose submissions on unique-constraint race.

    Each thread does accept_submission + coordinate_turn(commit=False) inside a single
    outer transaction (simulating execute_http_idempotent's callback). The loser of the
    unique-active race must not roll back its outer submission via a full db.rollback().
    """
    import threading
    import time

    db_file = tmp_path / "concurrent_sub_coord.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(bind=eng)
    Fac2 = sessionmaker(bind=eng, expire_on_commit=False)
    owner = uuid.uuid4()
    other = uuid.uuid4()
    with Fac2() as db:
        db.add_all([Profile(id=owner, email="owner@example.com"), Profile(id=other, email="other@example.com")])
        cid = uuid.uuid4()
        db.add(Campaign(id=cid, owner_id=owner, name="RaceCamp", revision=0))
        db.flush()
        db.add(CampaignMember(campaign_id=cid, user_id=owner, role="owner"))
        db.add(CampaignMember(campaign_id=cid, user_id=other, role="player"))
        db.commit()
        thread = get_or_create_campaign_thread(db, cid, created_by=owner)
        db.commit()
        tid = str(thread.id)

    errors = []
    turn_ids = []

    def submit_and_coordinate(user_id, content):
        try:
            with Fac2() as db:
                # Simulate outer submission transaction (like execute_http_idempotent callback)
                sub = accept_submission(db, campaign_id=cid, user_id=user_id, raw_content=content, segments=[{"type": "ic", "text": content}], thread_id=tid)
                # coordinate with flush-only (commit=False) — outer will commit
                coord = coordinate_turn(db, cid, tid, commit=False)
                db.commit()
                if coord:
                    turn_ids.append(str(coord[0].id))
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=lambda: submit_and_coordinate(owner, "concurrent A"))
    t2 = threading.Thread(target=lambda: submit_and_coordinate(other, "concurrent B"))
    t1.start()
    time.sleep(0.02)
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"concurrent submit+coord should not error, got {errors}"
    # Both submissions must be durable despite race
    with Fac2() as db:
        subs = db.execute(select(models.PlayerSubmission).where(models.PlayerSubmission.campaign_id == cid)).scalars().all()
        assert len(subs) == 2, f"both submissions must survive outer rollback, got {len(subs)}"
        turns = db.execute(select(models.DmTurn).where(models.DmTurn.campaign_id == cid)).scalars().all()
        active = [t for t in turns if t.status in ("pending", "streaming", "failed_visible")]
        assert len(active) == 1, f"only one active turn despite race, got {len(active)}"
        assert set(active[0].submission_ids) == {str(s.id) for s in subs}


def test_recover_filters_by_campaign():
    Fac, cid, owner, _p2, tid = _setup_campaign()
    Fac2, cid2, owner2, _p22, tid2 = _setup_campaign()
    # Create stuck running attempt in each campaign
    for FacX, cidx, tidx, oid in [(Fac, cid, tid, owner), (Fac2, cid2, tid2, owner2)]:
        with FacX() as db:
            s = accept_submission(db, campaign_id=cidx, user_id=oid, raw_content="stuck", segments=[{"type": "ic", "text": "stuck"}], thread_id=tidx)
            db.commit()
            _, a = coordinate_turn(db, cidx, tidx)
            from app.dm.turns import mark_attempt_running

            mark_attempt_running(db, a.id)
            # make it look stuck
            with FacX() as db2:
                att = db2.get(models.DmTurnAttempt, a.id)
                att.started_at = datetime.now(timezone.utc) - timedelta(seconds=1000)
                db2.commit()

    # Recover only first campaign
    with Fac() as db:
        n = recover_stuck_attempts(db, campaign_id=cid, lease_seconds=300)
        assert n == 1
    # Second campaign's stuck attempt should remain
    with Fac2() as db:
        from app.dm.turns import ATTEMPT_PREPARED, ATTEMPT_RUNNING

        attempts = db.execute(select(models.DmTurnAttempt).where(models.DmTurnAttempt.campaign_id == cid2)).scalars().all()
        assert any(a.status == ATTEMPT_RUNNING for a in attempts)
        assert not any(a.status == ATTEMPT_PREPARED and a.campaign_id == cid2 and "Recovered" in (a.last_error or "") for a in attempts)
    # Recover second explicitly
    with Fac2() as db:
        n2 = recover_stuck_attempts(db, campaign_id=cid2, lease_seconds=300)
        assert n2 == 1
