"""Issue #254 — accepted-resolution guarantee with high-intensity grace.

Builds on the #253 monetary ledger and the #200/#204/#206/#216 turn
lifecycle: an accepted player submission is guaranteed its complete owed
resolution (roll, narration, required post-turn) even past 100%; bounded
high-intensity grace precedes AI pause; pause blocks only new AI work;
resume needs no wizard; idle never advances fiction.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember  # noqa: E402
from models.characters import Character  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt, DMStream, DMStreamChunk  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import AIRun, OperationTrace  # noqa: E402
from models.usage import CampaignUsageEntry  # noqa: E402
from models.world import CampaignClock  # noqa: E402

from app.billing.ledger import (  # noqa: E402
    NonBillableRunError,
    get_capacity_summary,
    record_ai_spend_for_run,
    record_entry,
)
from app.billing.resolution_guarantee import (  # noqa: E402
    CapacityPausedError,
    capacity_state_payload,
    describe_owed_work,
    evaluate_new_work,
    is_high_intensity,
    is_owed_turn,
    require_new_ai_work,
)
from app.dm.turns import commit_turn, coordinate_turn, mark_streaming_started  # noqa: E402
from app.runtime.submissions import accept_submission, list_submissions  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _setup():
    eng = _engine()
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    player = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=player, email="p2@example.com"),
    ])
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Guarantee table", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.add(CampaignMember(campaign_id=camp.id, user_id=player, role="player"))
    char = Character(id=uuid.uuid4(), owner_id=owner, name="Hero")
    db.add(char)
    db.commit()
    thread = get_or_create_campaign_thread(db, camp.id, created_by=owner)
    db.commit()
    return Fac, camp.id, owner, player, char.id, str(thread.id)


def _fund(db, camp, cents, key="alloc-1", entry_type="allocation"):
    record_entry(db, campaign_id=camp, entry_type=entry_type,
                 amount_cents=cents, idempotency_key=key)
    db.commit()


def _spend(db, camp, cost_usd, tag=None):
    """Charge one primary billable run attributed to the campaign."""
    tag = tag or uuid.uuid4().hex[:8]
    trace_id = f"trace-254-{tag}"
    db.add(OperationTrace(trace_id=trace_id, operation_id=f"op-{tag}",
                          campaign_id=camp, submitted_at=datetime.now(timezone.utc)))
    db.flush()
    run = AIRun(trace_id=trace_id, operation_id=f"op-{tag}", logical_operation="narrate",
                role="ai_dm", provider="test", model="m", attempt=1,
                classification="primary", billable=True, status="succeeded",
                started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
                cost_usd=cost_usd)
    db.add(run)
    db.flush()
    entry = record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    return entry


def _submit_and_coordinate(Fac, cid, owner, tid, text="I advance"):
    db = Fac()
    sub = accept_submission(db, campaign_id=cid, user_id=owner, raw_content=text,
                            segments=[{"type": "ic", "text": text}], thread_id=tid)
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    return sub, turn, attempt


def _stream(db, cid, tid, turn, attempt):
    stream = DMStream(id=uuid.uuid4(), campaign_id=cid, thread_id=uuid.UUID(tid),
                      turn_id=str(turn.id), attempt_id=str(attempt.id),
                      status="streaming", audience=turn.audience)
    db.add(stream)
    db.flush()
    db.add(DMStreamChunk(id=uuid.uuid4(), stream_id=stream.id, sequence=0,
                         text="The AI DM narrates.", byte_length=20))
    stream.first_chunk_at = datetime.now(timezone.utc)
    stream.chunk_count = 1
    stream.last_sequence = 0
    db.flush()
    return stream


# ── 1. boundary before a new turn ────────────────────────────────────────────

def test_boundary_before_new_turn():
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 1000)
    _spend(db, cid, 10.00, tag="exhaust1")  # consumed == funded, no DM response yet
    db = Fac()
    decision = evaluate_new_work(db, cid, tid)
    assert decision["allowed"] is False
    assert decision["ai_paused"] is True
    assert decision["reason"] == "capacity_exhausted_paused"
    with pytest.raises(CapacityPausedError):
        require_new_ai_work(db, cid, tid)
    db.rollback()
    # And the coordinator refuses to start a *new* turn while paused.
    db = Fac()
    sub = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="late arrival",
                            segments=[{"type": "ic", "text": "late arrival"}], thread_id=tid)
    db.commit()
    with pytest.raises(CapacityPausedError):
        coordinate_turn(db, cid, tid)
    db.rollback()
    db.close()


# ── 2. accepted resolution completes past 100% ───────────────────────────────

def test_accepted_resolution_completes_past_100():
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 100)
    _sub, turn, attempt = _submit_and_coordinate(Fac, cid, owner, tid)
    turn_id, attempt_id = turn.id, attempt.id
    # Cost crosses 100% mid-resolution (owed work already accepted).
    db = Fac()
    _spend(db, cid, 1.50, tag="cross1")
    assert get_capacity_summary(db, cid)["remaining_cents"] == -50
    # New work is now blocked …
    assert evaluate_new_work(db, cid, tid)["allowed"] is False
    # … but the owed turn still streams and commits completely.
    db = Fac()
    stream = _stream(db, cid, tid, db.get(DmTurn, turn_id), db.get(DmTurnAttempt, attempt_id))
    db.commit()
    mark_streaming_started(db, turn_id, attempt_id, stream_id=stream.id)
    done_turn, _done_att, _event = commit_turn(db, turn_id, attempt_id)
    assert done_turn.status == "succeeded"
    assert is_owed_turn(db, turn_id) is False
    owed = describe_owed_work(db, cid, tid)
    assert owed["accepted_submission_ids"] == []
    assert owed["active_turns"] == []
    # The committed event's post-turn consolidation is now the remaining
    # owed work (completed by the required post-turn path, see test 4).
    assert owed["post_turn_lag"] == 1
    db.close()


# ── 3. roll requested as part of owed work submits after the threshold ──────

def test_roll_after_threshold_still_submits_and_narrates():
    from app.rolls.service import fulfill_roll, has_pending_rolls, request_rolls

    Fac, cid, owner, _p2, char, tid = _setup()
    db = Fac()
    _fund(db, cid, 100)
    _sub, turn, attempt = _submit_and_coordinate(Fac, cid, owner, tid)
    rows = request_rolls(db, campaign_id=cid, turn_id=turn.id, attempt_id=attempt.id, requests=[{
        "request_key": "owed-roll-1", "requested_user_id": owner, "character_id": char,
        "roll_kind": "check", "ability_or_skill": "perception", "label": "Spot",
        "advantage_state": "normal", "reason_public": "The AI DM calls for a check",
        "dc_private": None,
    }])
    db.commit()
    request_id = rows[0].id
    turn_id = turn.id
    # Exhaustion lands while the player-owned roll is still pending.
    db = Fac()
    _spend(db, cid, 5.00, tag="cross2")
    assert evaluate_new_work(db, cid, tid)["allowed"] is False
    # The owed roll still fulfills and resumes the SAME logical turn.
    db = Fac()
    assert has_pending_rolls(db, turn_id) is True
    _req, _ful, resumed, _enc = fulfill_roll(db, request_id=request_id, actor_id=owner, payload={
        "source": "app", "visibility": "public", "raw_rolls": [14],
        "modifier": 2, "total": 16,
    })
    db.commit()
    assert has_pending_rolls(db, turn_id) is False
    assert resumed is not None and str(resumed.turn_id) == str(turn_id)
    assert db.get(DmTurn, turn_id).status == "pending"  # ready to narrate, not stranded
    db.close()


# ── 4. required post-turn work completes despite exhaustion ──────────────────

def test_required_post_turn_after_threshold():
    from app.campaigns.events import commit_campaign_mutation
    from app.post_turn.service import get_checkpoint, run_post_turn_range

    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 50)
    _sub, turn, attempt = _submit_and_coordinate(Fac, cid, owner, tid)
    stream = _stream(db, cid, tid, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    commit_turn(db, turn.id, attempt.id)
    assert get_capacity_summary(db, cid)["remaining_cents"] == 50
    # Exhaust, then prove the owed post-turn range still consolidates.
    _spend(db, cid, 5.00, tag="cross3")
    db = Fac()
    assert evaluate_new_work(db, cid, tid)["allowed"] is False
    result = run_post_turn_range(db, cid, 1, 1,
                                 consolidate_fn=lambda events: {"event_count": len(events)})
    assert result["duplicate"] is False
    assert get_checkpoint(db, cid).processed_through_sequence == 1
    assert describe_owed_work(db, cid, tid)["post_turn_lag"] == 0
    # The committed event really exists (owed work was real work).
    assert db.execute(select(func.count()).select_from(CampaignDomainEvent)
                      .where(CampaignDomainEvent.campaign_id == cid)).scalar() == 1
    db.close()


# ── 5. rapid-message grace ───────────────────────────────────────────────────

def test_high_intensity_grace(monkeypatch):
    monkeypatch.setenv("RESOLUTION_GRACE_CADENCE_SECONDS", "120")
    monkeypatch.setenv("RESOLUTION_GRACE_OVERAGE_PCT", "10")
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 1000)
    # One full resolution just completed → DM response is "now".
    _sub, turn, attempt = _submit_and_coordinate(Fac, cid, owner, tid, text="first push")
    stream = _stream(db, cid, tid, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    commit_turn(db, turn.id, attempt.id)
    _spend(db, cid, 10.00, tag="topline")  # consumed == funded exactly
    db = Fac()
    assert get_capacity_summary(db, cid)["remaining_cents"] == 0
    # Rapid follow-up: bounded grace allows the next obligation.
    fast = evaluate_new_work(db, cid, tid)
    assert fast["allowed"] is True and fast["grace_active"] is True
    assert fast["reason"] == "high_intensity_grace"
    assert fast["overage_allowance_cents"] == 100  # 10% of 1000
    assert is_high_intensity(db, cid, tid) is True
    # Slow follow-up: same ledger, no grace — new work pauses.
    slow = evaluate_new_work(db, cid, tid, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert slow["allowed"] is False and slow["ai_paused"] is True
    assert is_high_intensity(db, cid, tid, now=datetime.now(timezone.utc) + timedelta(hours=1)) is False
    db.close()


# ── 6. grace exhaustion ──────────────────────────────────────────────────────

def test_grace_exhaustion_blocks_even_when_rapid(monkeypatch):
    monkeypatch.setenv("RESOLUTION_GRACE_CADENCE_SECONDS", "120")
    monkeypatch.setenv("RESOLUTION_GRACE_OVERAGE_PCT", "10")
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 1000)
    _sub, turn, attempt = _submit_and_coordinate(Fac, cid, owner, tid)
    stream = _stream(db, cid, tid, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    commit_turn(db, turn.id, attempt.id)
    _spend(db, cid, 11.50, tag="over")  # 1150 > 1000 + 100 overage
    db = Fac()
    decision = evaluate_new_work(db, cid, tid)
    assert decision["allowed"] is False and decision["ai_paused"] is True
    assert decision["grace_active"] is False
    with pytest.raises(CapacityPausedError):
        require_new_ai_work(db, cid, tid)
    db.rollback()
    db.close()


# ── 7. non-AI access while paused ────────────────────────────────────────────

def test_non_ai_access_usable_while_paused():
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 100)
    sub = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="history row",
                            segments=[{"type": "ic", "text": "history row"}], thread_id=tid)
    db.commit()
    coordinate_turn(db, cid, tid)
    _spend(db, cid, 9.99, tag="pause4")
    db = Fac()
    assert evaluate_new_work(db, cid, tid)["allowed"] is False
    # State hook, history reads, and ledger aggregates all stay available.
    state = capacity_state_payload(db, cid)
    assert state["ai_paused"] is True
    assert set(state) >= {"funded_cents", "consumed_cents", "remaining_cents",
                          "percent_used", "contributor_count", "ai_paused",
                          "grace_active", "has_owed_work"}
    assert "idempotency_key" not in str(state)
    rows = list_submissions(db, cid, thread_id=tid)
    assert any(r["id"] == str(sub.id) for r in rows)
    assert get_capacity_summary(db, cid)["remaining_cents"] < 0
    db.close()


# ── 8. funding restoration resumes immediately, same state ───────────────────

def test_funding_restoration_resumes_from_same_state():
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 100)
    _spend(db, cid, 1.00, tag="pause5")
    assert evaluate_new_work(db, cid, tid)["allowed"] is False
    revision_before = db.get(Campaign, cid).revision
    # Top-up lands: normal play resumes with no recovery wizard.
    _fund(db, cid, 500, key="added-1", entry_type="added_funds")
    db = Fac()
    decision = evaluate_new_work(db, cid, tid)
    assert decision["allowed"] is True and decision["ai_paused"] is False
    assert db.get(Campaign, cid).revision == revision_before  # same authoritative state
    sub = accept_submission(db, campaign_id=cid, user_id=owner, raw_content="we return",
                            segments=[{"type": "ic", "text": "we return"}], thread_id=tid)
    db.commit()
    turn, _attempt = coordinate_turn(db, cid, tid)
    assert turn.status == "pending" and turn.submission_ids == [str(sub.id)]
    db.close()


# ── 9. policy failure never abandons owed work ───────────────────────────────

def test_policy_failure_fails_open_for_owed_work(monkeypatch):
    from app.billing import ledger as ledger_mod

    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 100)
    _sub, turn, attempt = _submit_and_coordinate(Fac, cid, owner, tid)
    turn_id, attempt_id = turn.id, attempt.id
    db.close()

    def _boom(db, campaign_id):
        raise RuntimeError("meter is down")

    monkeypatch.setattr(ledger_mod, "get_capacity_summary", _boom)
    db = Fac()
    decision = evaluate_new_work(db, cid, tid)
    assert decision["allowed"] is True
    assert decision["reason"] == "policy_error_fail_open_for_owed_work"
    assert decision["ai_paused"] is False
    # The owed turn is still owed and still committable.
    assert is_owed_turn(db, turn_id) is True
    stream = _stream(db, cid, tid, db.get(DmTurn, turn_id), db.get(DmTurnAttempt, attempt_id))
    db.commit()
    mark_streaming_started(db, turn_id, attempt_id, stream_id=stream.id)
    done, _att, _ev = commit_turn(db, turn_id, attempt_id)
    assert done.status == "succeeded"
    db.close()


# ── 10. duplicates grant nothing twice ───────────────────────────────────────

def test_duplicate_evaluation_grants_nothing_twice(monkeypatch):
    monkeypatch.setenv("RESOLUTION_GRACE_CADENCE_SECONDS", "120")
    monkeypatch.setenv("RESOLUTION_GRACE_OVERAGE_PCT", "10")
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 1000)
    _sub, turn, attempt = _submit_and_coordinate(Fac, cid, owner, tid)
    stream = _stream(db, cid, tid, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    commit_turn(db, turn.id, attempt.id)
    _spend(db, cid, 10.00, tag="dup1")
    before = db.execute(select(func.count()).select_from(CampaignUsageEntry)
                        .where(CampaignUsageEntry.campaign_id == cid)).scalar()
    db = Fac()
    first = evaluate_new_work(db, cid, tid)
    second = evaluate_new_work(db, cid, tid)
    assert first["allowed"] is True and second["allowed"] is True
    assert first["overage_allowance_cents"] == second["overage_allowance_cents"] == 100
    after = db.execute(select(func.count()).select_from(CampaignUsageEntry)
                       .where(CampaignUsageEntry.campaign_id == cid)).scalar()
    assert after == before  # read-only gate: no grace rows minted, no duplicates possible
    # Blocked evaluations are equally side-effect free.
    _spend(db, cid, 5.00, tag="dup2")
    count_before_block = db.execute(select(func.count()).select_from(CampaignUsageEntry)
                                    .where(CampaignUsageEntry.campaign_id == cid)).scalar()
    for _ in range(3):
        with pytest.raises(CapacityPausedError):
            require_new_ai_work(db, cid, tid)
        db.rollback()
    count_after_block = db.execute(select(func.count()).select_from(CampaignUsageEntry)
                                   .where(CampaignUsageEntry.campaign_id == cid)).scalar()
    assert count_after_block == count_before_block
    db.close()


# ── 11. idle wall-clock during pause advances no fiction ─────────────────────

def test_idle_during_pause_advances_no_fiction():
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 100)
    _spend(db, cid, 1.00, tag="idle1")
    assert evaluate_new_work(db, cid, tid)["allowed"] is False
    revision = db.get(Campaign, cid).revision
    events = db.execute(select(func.count()).select_from(CampaignDomainEvent)
                        .where(CampaignDomainEvent.campaign_id == cid)).scalar()
    clocks = db.execute(select(func.count()).select_from(CampaignClock)
                        .where(CampaignClock.campaign_id == cid)).scalar()
    ledger_rows = db.execute(select(func.count()).select_from(CampaignUsageEntry)
                             .where(CampaignUsageEntry.campaign_id == cid)).scalar()
    # A long real-world idle: only reads happen (state hooks, gate checks).
    later = datetime.now(timezone.utc) + timedelta(hours=6)
    db = Fac()
    capacity_state_payload(db, cid)
    evaluate_new_work(db, cid, tid, now=later)
    describe_owed_work(db, cid, tid)
    db = Fac()
    assert db.get(Campaign, cid).revision == revision
    assert db.execute(select(func.count()).select_from(CampaignDomainEvent)
                      .where(CampaignDomainEvent.campaign_id == cid)).scalar() == events
    assert db.execute(select(func.count()).select_from(CampaignClock)
                      .where(CampaignClock.campaign_id == cid)).scalar() == clocks
    assert db.execute(select(func.count()).select_from(CampaignUsageEntry)
                      .where(CampaignUsageEntry.campaign_id == cid)).scalar() == ledger_rows
    db.close()


# ── 12. retries/failover are non-billable recovery ───────────────────────────

def test_owed_retry_is_non_billable_recovery():
    Fac, cid, _owner, _p2, _char, _tid = _setup()
    db = Fac()
    _fund(db, cid, 1000)
    trace_id = f"trace-254-retry-{uuid.uuid4().hex[:8]}"
    db.add(OperationTrace(trace_id=trace_id, operation_id="op-retry",
                          campaign_id=cid, submitted_at=datetime.now(timezone.utc)))
    db.flush()
    recovery = AIRun(trace_id=trace_id, operation_id="op-retry", logical_operation="narrate",
                     role="ai_dm", provider="test", model="m", attempt=2,
                     classification="recovery", billable=False, status="succeeded",
                     started_at=datetime.now(timezone.utc),
                     completed_at=datetime.now(timezone.utc), cost_usd=3.00)
    db.add(recovery)
    db.flush()
    db.commit()  # durable before the excluded charge attempt
    with pytest.raises(NonBillableRunError):
        record_ai_spend_for_run(db, campaign_id=cid, ai_run=recovery)
    db.rollback()
    assert get_capacity_summary(db, cid)["consumed_cents"] == 0
    assert get_capacity_summary(db, cid)["remaining_cents"] == 1000
    db.close()


# ── 13/14. observability + grace/narrative separation ────────────────────────

def test_decisions_carry_observability_and_no_narrative():
    Fac, cid, owner, _p2, _char, tid = _setup()
    db = Fac()
    _fund(db, cid, 1000)
    open_decision = evaluate_new_work(db, cid, tid)
    assert set(open_decision) == {
        "funded_cents", "consumed_cents", "remaining_cents", "percent_used",
        "overage_allowance_cents", "grace_cadence_seconds", "grace_overage_pct",
        "allowed", "reason", "ai_paused", "grace_active",
    }
    _spend(db, cid, 10.00, tag="obs1")
    paused = evaluate_new_work(db, cid, tid)
    assert paused["funded_cents"] == 1000 and paused["consumed_cents"] == 1000
    assert paused["remaining_cents"] == 0
    # Cost/timing/entitlement aggregates only — never narrative content.
    blob = str(open_decision) + str(paused)
    for banned in ("raw_content", "narration", "prompt", "backstory", "segment"):
        assert banned not in blob
    db.close()


def test_grace_config_is_policy_not_story(monkeypatch):
    import app.billing.resolution_guarantee as guarantee

    monkeypatch.setenv("RESOLUTION_GRACE_CADENCE_SECONDS", "not-a-number")
    monkeypatch.setenv("RESOLUTION_GRACE_OVERAGE_PCT", "-5")
    assert guarantee.grace_cadence_seconds() == 120.0  # safe default on bad config
    assert guarantee.grace_overage_pct() == 0.0  # clamped, never negative
    assert guarantee.grace_overage_cents(1000) == 0
    monkeypatch.setenv("RESOLUTION_GRACE_OVERAGE_PCT", "10")
    assert guarantee.grace_overage_cents(1000) == 100
    assert guarantee.grace_overage_cents(0) == 0  # no funding → no overage
