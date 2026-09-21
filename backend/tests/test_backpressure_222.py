"""Issue #222 — safe lag/backpressure for post-turn processing.

Forward play continues while post-turn trails, but only while the forward
DM can still reason from accumulated unprocessed committed history. Past
the configurable safe context budget, new AI progression pauses (accepted
input stays durable) until cumulative catch-up clears the backlog.
"""
from __future__ import annotations

import uuid

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
from app.campaigns.events import commit_campaign_mutation  # noqa: E402
from app.dm.context import LaneName, assemble_attempt_context  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.dm.turns import coordinate_turn  # noqa: E402
from app.post_turn.backpressure import (  # noqa: E402
    BackpressureBlocked,
    describe_client_state,
    estimate_unprocessed_cost,
    evaluate_backpressure,
    get_safe_context_budget,
    require_forward_progress,
)
from app.post_turn.service import (  # noqa: E402
    get_checkpoint,
    get_post_turn_status,
    run_post_turn_range,
)
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.post_turn import PostTurnRun  # noqa: E402
from models.profiles import Profile  # noqa: E402


def _factory():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _campaign(factory, **kw):
    db = factory()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email=f"{owner}@x.com"))
    db.commit()
    camp = Campaign(owner_id=owner, name="bp", revision=0, **kw)
    db.add(camp)
    db.commit()
    thread = get_or_create_campaign_thread(db, camp.id, created_by=owner)
    db.commit()
    tid, cid = str(thread.id), camp.id
    db.close()
    return cid, owner, tid


def _commit(db, cid, rev, etype="game.play", payload=None, visibility="campaign"):
    _row, event = commit_campaign_mutation(
        db, cid, expected_revision=rev, event_type=etype,
        payload=payload if payload is not None else {"n": rev},
        visibility=visibility, operation_id=f"op-{rev}-{uuid.uuid4().hex[:6]}",
    )
    return event


def _turn(db, cid, owner, tid, text="I look around"):
    sub = accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content=text,
        segments=[{"type": "ic", "text": text}], thread_id=tid,
    )
    db.commit()
    return coordinate_turn(db, cid, tid)


def _stub_status():
    return {
        LaneName.CURRENT_SCENE: "not_applicable",
        LaneName.KNOWLEDGE_VISIBILITY: "not_applicable",
        LaneName.CLOCKS_PRESSURES: "not_applicable",
        LaneName.COMBAT_HOOKS: "not_applicable",
        LaneName.RELEVANT_CANON: "not_applicable",
        LaneName.REPAIR_DIRECTIVES: "not_applicable",
    }


def _history_lane(packet):
    return next(l for l in packet.lanes if l.name == LaneName.RECENT_HISTORY)


# ── Safe lag: unprocessed history reaches the forward DM ──────────────────

def test_small_safe_lag_included_in_context_with_marker():
    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    e1 = _commit(db, cid, 0)
    e2 = _commit(db, cid, 1)
    _turn(db, cid, owner, tid)
    db.commit()
    _turn_obj, attempt = coordinate_turn(db, cid, tid)
    packet = assemble_attempt_context(db, attempt.id, supplemental_status=_stub_status())
    lane = _history_lane(packet)
    by_seq = {r.value["sequence"]: r for r in lane.records if "sequence" in r.value}
    assert e1.sequence in by_seq and e2.sequence in by_seq
    for seq in (e1.sequence, e2.sequence):
        marker = by_seq[seq].value.get("post_turn")
        assert marker is not None and marker["processed"] is False
        assert marker["processed_through"] == 0
    db.close()


def test_cumulative_lag_beyond_window_still_included_as_required():
    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    events = [_commit(db, cid, i, payload={"n": i, "pad": "x" * 40}) for i in range(6)]
    _turn_obj, attempt = _turn(db, cid, owner, tid)
    packet = assemble_attempt_context(
        db, attempt.id, supplemental_status=_stub_status(), recent_event_limit=2,
    )
    lane = _history_lane(packet)
    seqs = {r.value["sequence"] for r in lane.records if "sequence" in r.value}
    for e in events:
        if e.sequence <= attempt.source_revision:
            assert e.sequence in seqs, f"unprocessed seq {e.sequence} missing from context"
    gap = [r for r in lane.records if r.value.get("sequence") == events[0].sequence]
    assert gap and gap[0].required is True
    db.close()


# ── Budget-based threshold (never a fixed message count) ─────────────────

def test_threshold_is_budget_based_not_count_based(monkeypatch):
    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    for i in range(3):
        _commit(db, cid, i)
    db.commit()
    assert evaluate_backpressure(db, cid)["blocked"] is False
    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_BYTES", "200")
    status = evaluate_backpressure(db, cid)
    assert status["blocked"] is True and status["reason"] == "over_safe_budget"
    assert status["outstanding"] == 3
    # Same 3 events, generous budget → safe again: count alone never blocks.
    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_BYTES", "1000000")
    assert evaluate_backpressure(db, cid)["blocked"] is False
    db.close()


def test_safe_budget_env_is_single_canonical_knob(monkeypatch):
    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_BYTES", "12345")
    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_TOKENS", "2222")
    assert get_safe_context_budget() == (12345, 2222)


# ── Threshold crossing pauses AI progression, keeps input durable ─────────

def test_threshold_crossing_pauses_execution_and_fires_critical_catchup(monkeypatch):
    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_BYTES", "200")
    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    for i in range(3):
        _commit(db, cid, i, payload={"n": i, "pad": "y" * 60})
    _turn_obj, attempt = _turn(db, cid, owner, tid)
    aid = attempt.id
    result = execute_dm_attempt(db, aid)
    assert result is None  # paused, not executed, not failed
    db.expire_all()
    from models.dm import DmTurnAttempt

    assert db.get(DmTurnAttempt, aid).status == "prepared"
    runs = db.execute(
        select(PostTurnRun).where(PostTurnRun.campaign_id == cid)
    ).scalars().all()
    assert any(r.trigger == "critical" for r in runs), "blocked progression must fire critical catch-up"
    with pytest.raises(BackpressureBlocked):
        require_forward_progress(db, cid)
    db.close()


def test_accepted_input_remains_durable_while_blocked(monkeypatch):
    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_BYTES", "200")
    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    for i in range(3):
        _commit(db, cid, i, payload={"n": i, "pad": "z" * 60})
    # Coordination (player-input acceptance) still works under backpressure.
    turn, attempt = _turn(db, cid, owner, tid, text="I wait patiently")
    assert turn is not None and attempt is not None
    assert attempt.status == "prepared"
    second = accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="I also listen",
        segments=[{"type": "ic", "text": "I also listen."}], thread_id=tid,
    )
    db.commit()
    turn2, attempt2 = coordinate_turn(db, cid, tid)
    assert turn2 is not None and attempt2 is not None
    assert str(second.id) in list(attempt2.submission_ids or [])
    db.close()


def test_catchup_removes_backpressure_state(monkeypatch):
    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_BYTES", "300")
    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    for i in range(3):
        _commit(db, cid, i, payload={"n": i, "pad": "w" * 60})
    assert evaluate_backpressure(db, cid)["blocked"] is True
    out = run_post_turn_range(db, cid, 1, 3, consolidate_fn=lambda events: {})
    assert out["duplicate"] is False
    assert get_checkpoint(db, cid, commit=False).processed_through_sequence == 3
    status = evaluate_backpressure(db, cid)
    assert status["blocked"] is False and status["reason"] == "within_budget"
    db.close()


# ── Privacy: unprocessed private history stays scoped ─────────────────────

def test_private_history_scoped_and_never_broadened_through_lag_path():
    from app.dm.context import (
        ContextAudience,
        _history_record,
        assemble_context_packet,
    )

    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    scoped = _commit(db, cid, 0, etype="whisper.heard", payload={"thread_id": tid, "text": "secret"},
                     visibility="private")
    scopeless = _commit(db, cid, 1, etype="table.talk", payload={"text": "no scope"},
                        visibility="private")
    # Scopeless private history is never widened through the lag path.
    assert _history_record(cid, scopeless, post_turn_processed_through=0) is None
    rec = _history_record(cid, scoped, post_turn_processed_through=0)
    assert rec is not None and rec.visibility == "private"
    assert rec.authorization.thread_ids == [tid]
    assert rec.value["post_turn"] == {"processed": False, "processed_through": 0}

    records = {lane: [] for lane in LaneName}
    records[LaneName.RECENT_HISTORY] = [rec]
    campaign_aud = ContextAudience(
        campaign_id=str(cid), thread_id=tid, audience="campaign", user_ids=[str(owner)])
    packet = assemble_context_packet(audience=campaign_aud, records=records)
    lane = next(l for l in packet.lanes if l.name == LaneName.RECENT_HISTORY)
    assert lane.records == []
    assert "secret" not in packet.serialize_for_adjudication()

    private_aud = ContextAudience(
        campaign_id=str(cid), thread_id=tid, audience="private", user_ids=[str(owner)])
    packet2 = assemble_context_packet(audience=private_aud, records={lane2: [] for lane2 in LaneName} | {LaneName.RECENT_HISTORY: [rec]})
    lane2 = next(l for l in packet2.lanes if l.name == LaneName.RECENT_HISTORY)
    assert [r.record_id for r in lane2.records] == [rec.record_id]
    # Scope stays thread-bound even when authorized: the lag path adds no
    # wider authorization of its own.
    assert lane2.records[0].authorization.thread_ids == [tid]
    db.close()


# ── Fail-safe: estimation failure blocks ──────────────────────────────────

def test_estimation_failure_blocks_conservative(monkeypatch):
    F = _factory()
    cid, _owner, _tid = _campaign(F)
    db = F()
    _commit(db, cid, 0)
    import app.post_turn.backpressure as bp

    monkeypatch.setattr(bp, "estimate_unprocessed_cost",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cost boom")))
    status = evaluate_backpressure(db, cid)
    assert status["blocked"] is True and status["reason"] == "estimation_failed"
    with pytest.raises(BackpressureBlocked):
        require_forward_progress(db, cid)
    db.close()


def test_estimate_is_conservative_vs_assembled_records():
    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    for i in range(3):
        _commit(db, cid, i, payload={"n": i})
    cost = estimate_unprocessed_cost(db, cid)
    _turn_obj, attempt = _turn(db, cid, owner, tid)
    packet = assemble_attempt_context(db, attempt.id, supplemental_status=_stub_status())
    from app.dm.context import _size as _ctx_size

    lane = _history_lane(packet)
    actual = sum(_ctx_size(r.model_dump(mode="json")) for r in lane.records)
    assert cost["estimated_bytes"] >= actual
    db.close()


# ── Client state hides internals ──────────────────────────────────────────

BANNED = ("queue", "provider", "model", "checkpoint", "outbox", "worker",
          "trigger", "backpressure", "infrastructure")


def test_client_state_ready_and_processing_hide_internals():
    ready = describe_client_state({"blocked": False})
    assert ready == {"dm_state": "ready"}
    processing = describe_client_state({"blocked": True, "outstanding": 9})
    assert processing["dm_state"] == "processing"
    assert "saved" in processing["message"]
    blob = (processing["dm_state"] + " " + processing["message"]).lower()
    for word in BANNED:
        assert word not in blob, f"client state leaks {word!r}"
    assert describe_client_state(None) == {"dm_state": "ready"}


def test_post_turn_status_exposes_backpressure_observability():
    F = _factory()
    cid, _owner, _tid = _campaign(F)
    db = F()
    for i in range(2):
        _commit(db, cid, i)
    st = get_post_turn_status(db, cid)
    bp = st["backpressure"]
    assert bp["blocked"] is False
    assert bp["outstanding"] == 2
    assert bp["estimated_bytes"] > 0
    assert bp["safe_budget_bytes"] > 0
    assert bp["processed_through"] == 0
    db.close()


# ── Review #419 round 1: resume must survive revision-advancing catch-up ───

def _respond_contract():
    from app.dm.contract import CONTRACT_VERSION, normalize_contract

    return normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "respond",
            "reason": "story continuation",
            "beats": [
                {
                    "id": "beat_1",
                    "type": "narration",
                    "claims": [
                        {
                            "text": "The dust settles and the room is quiet.",
                            "claim_kind": "observation",
                            "origin": "dm_adjudication",
                            "visibility": "public",
                        }
                    ],
                }
            ],
            "open_player_choice": "What do you do?",
        }
    )


def test_blocked_input_resolves_after_revision_advancing_catchup(monkeypatch):
    """Catch-up that advances Campaign.revision must not strand blocked input.

    The blocked attempt is prepared at the pre-catch-up revision; normal
    post-turn consolidation (e.g. clock completion emits a domain event)
    bumps the revision before the backlog clears. On resume the executor
    must rebase the never-executed prepared attempt onto current authority
    (supersede, same submissions) so the accepted input resolves instead of
    failing visible as stale.
    """
    from models.dm import DmTurn, DmTurnAttempt

    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_BYTES", "300")
    F = _factory()
    cid, owner, tid = _campaign(F)
    db = F()
    for i in range(3):
        _commit(db, cid, i, payload={"n": i, "pad": "w" * 60})
    _turn_obj, attempt = _turn(db, cid, owner, tid)
    old_aid = attempt.id
    assert execute_dm_attempt(db, old_aid) is None  # paused, stays prepared
    # Catch-up that itself advances the campaign revision, then clears the backlog.
    _commit(db, cid, 3, etype="clock.completed", payload={"clock": "doom", "pad": "v" * 10})
    out = run_post_turn_range(db, cid, 1, 4, consolidate_fn=lambda events: {})
    assert out["duplicate"] is False
    assert evaluate_backpressure(db, cid)["blocked"] is False

    result = execute_dm_attempt(
        db, old_aid,
        adjudicate=lambda packet, feedback=None: _respond_contract(),
        narrator="deterministic",
    )
    assert result is not None  # resolved, not failed-visible
    db.expire_all()
    old = db.get(DmTurnAttempt, old_aid)
    assert old.status == "superseded"
    assert old.invalidation_reason == "backpressure_revision_refresh"
    turn = db.get(DmTurn, _turn_obj.id)
    assert turn.status == "succeeded"
    assert turn.current_attempt_id != old_aid
    new = db.get(DmTurnAttempt, turn.current_attempt_id)
    assert new.status == "succeeded"
    assert list(new.submission_ids or []) == list(old.submission_ids or [])
    db.close()


def test_backpressured_attempts_defer_behind_ready_work(monkeypatch):
    """Persistently blocked attempts must not starve ready turns in sweeps.

    Five blocked attempts (oldest) plus one ready attempt (newest) with a
    sweep limit of 5: the first sweep defers the blocked ones via the
    existing retry-eligibility mechanism, and the next sweep executes the
    ready turn instead of reselecting the blocked five forever.
    """
    from datetime import datetime, timedelta, timezone

    from app.dm.execution import find_prepared_attempts, run_dm_execute_sweep
    from models.dm import DmTurn, DmTurnAttempt

    monkeypatch.setenv("FORWARD_DM_SAFE_CONTEXT_BYTES", "200")
    F = _factory()
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    db = F()
    for n in range(5):
        cid, owner, tid = _campaign(F)
        for i in range(3):
            # Campaign revision tracks the latest commit; coordinate after.
            camp_rev = db.execute(
                select(Campaign).where(Campaign.id == cid)
            ).scalars().first().revision
            _commit(db, cid, int(camp_rev), payload={"n": i, "pad": "y" * 60})
        _t, attempt = _turn(db, cid, owner, tid, text=f"blocked {n}")
        db.execute(
            select(DmTurnAttempt).where(DmTurnAttempt.id == attempt.id)
        ).scalars().first().created_at = base + timedelta(seconds=n)
        db.commit()
    cid_r, owner_r, tid_r = _campaign(F)
    camp_rev_r = db.execute(
        select(Campaign).where(Campaign.id == cid_r)
    ).scalars().first().revision
    assert int(camp_rev_r) == 0
    turn_r, attempt_r = _turn(db, cid_r, owner_r, tid_r, text="ready to go")
    ready_aid = str(attempt_r.id)
    db.execute(
        select(DmTurnAttempt).where(DmTurnAttempt.id == attempt_r.id)
    ).scalars().first().created_at = base + timedelta(seconds=60)
    db.commit()

    first = run_dm_execute_sweep(
        db, limit=5,
        adjudicate=lambda packet, feedback=None: _respond_contract(),
        narrator="deterministic",
    )
    assert ready_aid not in first["executed"]  # limit covers only the older blocked five
    assert first["executed"] == []
    db.expire_all()
    # Blocked attempts deferred behind ready work via retry eligibility.
    for row in db.execute(select(DmTurnAttempt)).scalars().all():
        if str(row.id) == ready_aid:
            assert row.next_retry_at is None
            continue
        assert row.status == "prepared"
        assert row.next_retry_at is not None
    assert ready_aid in [str(a.id) for a in find_prepared_attempts(db, limit=5)]

    second = run_dm_execute_sweep(
        db, limit=5,
        adjudicate=lambda packet, feedback=None: _respond_contract(),
        narrator="deterministic",
    )
    assert second["executed"] == [ready_aid]
    db.expire_all()
    assert db.get(DmTurn, turn_r.id).status == "succeeded"
    db.close()
