"""Issue #218 — criteria-driven campaign clocks through bounded decisions."""
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
from app.campaigns.events import commit_campaign_mutation, list_campaign_events  # noqa: E402
from app.decisions import DecisionService  # noqa: E402
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.post_turn.service import get_checkpoint, run_post_turn_range  # noqa: E402
from app.world import clocks as C  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import DecisionTelemetry  # noqa: E402
from models.world import CampaignClock  # noqa: E402

QUESTION = "evaluate_campaign_clock"


@pytest.fixture(autouse=True)
def _no_auto_trigger(monkeypatch):
    monkeypatch.setenv("POST_TURN_AUTO_TRIGGER", "0")


class _NeverCall(FakeDecisionAdapter):
    def execute(self, request, *, model, timeout):
        raise AssertionError("deterministic clock path must not call the model")


def _factory():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _setup():
    F = _factory()
    db = F()
    owner = uuid.uuid4()
    member = uuid.uuid4()
    outsider = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@x.com"))
    db.add(Profile(id=member, email="member@x.com"))
    db.add(Profile(id=outsider, email="out@x.com"))
    db.flush()
    c = Campaign(owner_id=owner, name="clocks")
    db.add(c)
    db.flush()
    db.add(CampaignMember(campaign_id=c.id, user_id=member))
    db.commit()
    db.refresh(c)
    return F, db, c, owner, member, outsider


def _rev(db, c):
    return int(db.get(Campaign, c.id).revision or 0)


def _commit(db, c, etype="game.play", payload=None, visibility="public", op=None):
    rev = _rev(db, c)
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type=etype, payload=payload or {"n": rev + 1},
        visibility=visibility, operation_id=op or f"op-{rev + 1}-{uuid.uuid4().hex[:6]}",
    )
    return event


def _range(db, c, lo, hi):
    return list(db.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.campaign_id == c.id,
        CampaignDomainEvent.sequence >= lo, CampaignDomainEvent.sequence <= hi,
    ).order_by(CampaignDomainEvent.sequence.asc())).scalars().all())


def _mkclock(db, c, **kw):
    params = {
        "name": "Ritual", "threshold": 4,
        "advancement_criteria": {"kind": "deterministic", "event_types": ["game.play"]},
        "status": "active", "provenance": {"source": "test"},
    }
    params.update(kw)
    row, _event = C.create_clock_authoritative(db, c.id, _rev(db, c), **params)
    return row


def _play(db, c, n, **kw):
    """Commit n gameplay events; return the (lo, hi) gameplay sequence range.

    Clock lifecycle events occupy their own sequences, so tests count only
    the gameplay span they committed.
    """
    lo = _rev(db, c) + 1
    for _ in range(n):
        _commit(db, c, **kw)
    return lo, _rev(db, c)


def _clock_events(db, etype):
    return db.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.event_type == etype)).scalars().all()


def _scripted(answer):
    return DecisionService(FakeDecisionAdapter(answers={QUESTION: answer}))


# ── Creation validation (fail closed) ──────────────────────────────────────

def test_create_validation_fails_closed():
    _F, db, c, *_ = _setup()
    base = {"name": "X", "threshold": 3,
            "advancement_criteria": {"kind": "deterministic"},
            "provenance": {"source": "test"}}
    bad = [
        {**base, "advancement_criteria": {"kind": "vibes"}},
        {**base, "advancement_criteria": "yes"},
        {**base, "threshold": 0},
        {**base, "progress": 4},
        {**base, "progress": 3},  # at threshold but not completed
        {**base, "status": "completed", "progress": 1},
        {**base, "status": "ticking-fast"},
        {**base, "visibility": "everyone"},
        {**base, "provenance": {}},
        {**base, "provenance": {"source": ""}},
        {**base, "stages": [{"at": 9, "label": "beyond"}]},
        {**base, "stages": [{"at": 1, "label": "a"}, {"at": 1, "label": "b"}]},
        {**base, "completion_criteria": {"kind": "deterministic", "description": "x"}},
        {**base, "completion_criteria": {"kind": "semantic"}},
        {**base, "source_event_id": uuid.uuid4()},  # dangling ref
    ]
    for params in bad:
        with pytest.raises(ValueError):
            C.create_clock_authoritative(db, c.id, _rev(db, c), **params)
    db.rollback()
    assert db.execute(select(CampaignClock)).scalars().all() == []
    db.close()


def test_create_idempotent_on_key():
    _F, db, c, *_ = _setup()
    first, event = C.create_clock_authoritative(
        db, c.id, _rev(db, c), name="Siege", threshold=3,
        advancement_criteria={"kind": "deterministic"},
        status="active", provenance={"source": "test"},
        idempotency_key="seed-pressure-1")
    assert event is not None and event.event_type == "clock.created"
    rev_after_first = _rev(db, c)
    second, event2 = C.create_clock_authoritative(
        db, c.id, _rev(db, c), name="Siege", threshold=3,
        advancement_criteria={"kind": "deterministic"},
        status="active", provenance={"source": "test"},
        idempotency_key="seed-pressure-1")
    assert second.id == first.id and event2 is None
    assert _rev(db, c) == rev_after_first  # no revision bump, no second event
    with pytest.raises(ValueError, match="different clock"):
        C.create_clock_authoritative(
            db, c.id, _rev(db, c), name="Other", threshold=3,
            advancement_criteria={"kind": "deterministic"},
            provenance={"source": "test"}, idempotency_key="seed-pressure-1")
    db.close()


# ── Deterministic criteria (no model call) ─────────────────────────────────

def test_deterministic_advance_without_model_call():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=4,
                     advancement_criteria={"kind": "deterministic", "event_types": ["game.play"],
                                           "required_count": 2, "max_advance": 3})
    lo, hi = _play(db, c, 3)
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["clocks_evaluated"] == 1 and out["advanced"] == 1
    res = out["results"][0]
    assert res["outcome"] == "advanced" and res["progress"] == 1 and res["path"] == "deterministic"
    fresh = db.get(CampaignClock, clock.id)
    assert (int(fresh.progress), int(fresh.progress_carry), int(fresh.evaluated_through_sequence)) == (1, 1, hi)
    assert int(fresh.revision) == 2
    ev = _clock_events(db, "clock.advanced")
    assert len(ev) == 1 and ev[0].payload["progress"] == 1
    db.close()


def test_deterministic_carryover_across_ranges():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=5,
                     advancement_criteria={"kind": "deterministic", "event_types": ["game.play"],
                                           "required_count": 3})
    lo, hi = _play(db, c, 2)
    out = C.consolidate_clocks_for_range(db, c.id, 1, hi, _range(db, c, 1, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["results"][0]["outcome"] == "no_change"
    assert int(db.get(CampaignClock, clock.id).progress_carry) == 2
    _play(db, c, 1)
    hi2 = _rev(db, c)
    out = C.consolidate_clocks_for_range(db, c.id, 1, hi2, _range(db, c, 1, hi2),
                                         decision_service=DecisionService(_NeverCall()))
    advanced = [r for r in out["results"] if r.get("outcome") == "advanced"]
    assert len(advanced) == 1 and advanced[0]["progress"] == 1
    assert int(db.get(CampaignClock, clock.id).progress_carry) == 0
    db.close()


def test_deterministic_no_match_is_no_change():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, advancement_criteria={"kind": "deterministic",
                                                 "event_types": ["game.combat"]})
    lo, hi = _play(db, c, 2, etype="game.play")
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["results"][0]["outcome"] == "no_change"
    assert int(db.get(CampaignClock, clock.id).evaluated_through_sequence) == hi
    assert _clock_events(db, "clock.advanced") == []
    assert _clock_events(db, "clock.completed") == []
    db.close()


def test_deterministic_payload_match():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, advancement_criteria={"kind": "deterministic",
                                         "event_types": ["game.play"],
                                         "match": {"alarm": "raised"}})
    lo, hi = _play(db, c, 1, payload={"alarm": "quiet"})
    lo2, hi2 = _play(db, c, 1, payload={"alarm": "raised"})
    assert lo == lo2 - 1
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi2, _range(db, c, lo, hi2),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["results"][0]["outcome"] == "advanced"
    assert out["results"][0]["evidence_count"] == 1
    db.close()


# ── Semantic criteria (bounded decision) ───────────────────────────────────

def test_semantic_advance_and_telemetry_shape():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=5,
                     advancement_criteria={"kind": "semantic", "event_types": ["game.play"],
                                           "max_advance": 2})
    lo, hi = _play(db, c, 1)
    adapter = FakeDecisionAdapter(answers={QUESTION: "ADVANCE_2"})
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(adapter))
    assert len(adapter.calls) == 1
    res = out["results"][0]
    assert res["outcome"] == "advanced" and res["progress"] == 2 and res["path"] == "decision"
    assert res["directive"] == "direct_execute"
    assert res["evidence_count"] == 1
    trace = res["telemetry"]
    assert trace["decision_class"] == "campaign_clock"
    assert trace["candidate_schema_version"] == 1 and trace["frame_schema_version"] == 1
    assert trace["policy_schema_version"] == 1 and trace["telemetry_schema_version"] == 1
    assert trace["model"] == "fake-decision-model-v1"
    # COMPLETE is offered only when ticks could finish the clock or judged
    # completion criteria exist — neither holds here, so it stays illegal.
    assert set(trace["candidate_ids"]) == {"NO_CHANGE", "ADVANCE_1", "ADVANCE_2", "DEFER"}
    assert trace["selected_id"] == "ADVANCE_2" and trace["margin"] == 1.0
    fresh = db.get(CampaignClock, clock.id)
    assert int(fresh.progress) == 2 and int(fresh.evaluated_through_sequence) == hi
    db.close()


def test_semantic_no_change_when_criteria_unmet():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, advancement_criteria={"kind": "semantic"})
    lo, hi = _play(db, c, 1)
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=_scripted("NO_CHANGE"))
    assert out["results"][0]["outcome"] == "no_change"
    assert int(db.get(CampaignClock, clock.id).evaluated_through_sequence) == hi
    assert _clock_events(db, "clock.advanced") == []
    assert _clock_events(db, "clock.completed") == []
    db.close()


def test_model_cannot_invent_transition_amount():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=9,
                     advancement_criteria={"kind": "semantic", "max_advance": 2})
    lo, hi = _play(db, c, 1)
    frame = C.build_clock_frame(clock, evidence=[], evidence_total=0,
                                from_sequence=lo, to_sequence=hi)
    assert "ADVANCE_9" not in {cd.id for cd in frame.candidates}
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=_scripted("ADVANCE_9"))
    res = out["results"][0]
    assert res["outcome"] == "deferred" and res["failure"] is not None
    assert int(db.get(CampaignClock, clock.id).progress) == 0
    db.close()


def test_semantic_advance_suppressed_without_matching_evidence():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=4,
                     advancement_criteria={"kind": "semantic",
                                           "event_types": ["game.combat"]})
    # The range holds only game.play: no event satisfies the prefilter, so
    # ADVANCE_* must not be offered and a confident ADVANCE_1 fails closed.
    assert C.legal_outcome_ids(clock, has_advancement_evidence=False) == ["NO_CHANGE", "DEFER"]
    lo, hi = _play(db, c, 2, etype="game.play")
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=_scripted("ADVANCE_1"))
    res = out["results"][0]
    assert res["outcome"] == "deferred" and res["failure"] is not None
    fresh = db.get(CampaignClock, clock.id)
    assert int(fresh.progress) == 0 and int(fresh.evaluated_through_sequence) == hi
    assert _clock_events(db, "clock.advanced") == []
    assert _clock_events(db, "clock.completed") == []
    db.close()


def test_apply_rejects_advance_without_evidence():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, advancement_criteria={"kind": "semantic"})
    lo, hi = _play(db, c, 1)
    frame = C.build_clock_frame(clock, evidence=[], evidence_total=0,
                                from_sequence=lo, to_sequence=hi)
    assert "ADVANCE_1" not in {cd.id for cd in frame.candidates}
    with pytest.raises(ValueError, match="advancement requires evidence"):
        C.apply_clock_outcome(db, c.id, clock.id, frame=frame, selected_id="ADVANCE_1",
                              evidence_refs=[], from_sequence=lo, to_sequence=hi)
    db.close()


def test_telemetry_persisted_fail_soft():
    F, db, c, *_ = _setup()
    _mkclock(db, c, advancement_criteria={"kind": "semantic"})
    lo, hi = _play(db, c, 1)
    C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                   decision_service=_scripted("NO_CHANGE"),
                                   session_factory=F)
    rows = db.execute(select(DecisionTelemetry).where(
        DecisionTelemetry.decision_class == "campaign_clock")).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.model == "fake-decision-model-v1" and row.selected_id == "NO_CHANGE"
    assert row.candidate_schema_version == 1 and row.frame_schema_version == 1
    assert row.policy_schema_version == 1 and row.telemetry_schema_version == 1
    assert row.probabilities["NO_CHANGE"] == 1.0 and row.margin == 1.0
    db.close()


# ── Non-evaluable statuses ─────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["dormant", "pending"])
def test_inactive_clock_skipped_without_consuming(status):
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, status=status)
    lo, hi = _play(db, c, 1)
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["results"][0]["evaluated"] is False
    assert out["results"][0]["reason"] == f"status_{status}_not_evaluable"
    fresh = db.get(CampaignClock, clock.id)
    assert int(fresh.progress) == 0 and int(fresh.evaluated_through_sequence) == 0
    db.close()


def test_terminal_clocks_never_loaded():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, status="completed", progress=4, threshold=4)
    retired = _mkclock(db, c, status="active", name="Doomed")
    C.retire_clock_authoritative(db, c.id, retired.id, _rev(db, c),
                                 disposition="retired", reason="arc ended",
                                 provenance={"source": "test"})
    hi = _rev(db, c)
    out = C.consolidate_clocks_for_range(db, c.id, 1, hi, _range(db, c, 1, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["clocks_evaluated"] == 0
    db.close()


# ── Hidden-clock secrecy ───────────────────────────────────────────────────

def test_hidden_clock_projection_and_feed():
    _F, db, c, owner, member, outsider = _setup()
    hidden = _mkclock(db, c, name="Secret Doom", visibility="dm_only")
    _mkclock(db, c, name="Open Siege", visibility="campaign", status="active")
    owner_view = C.project_clocks_for_viewer(db, c, owner)
    assert {r["name"] for r in owner_view["clocks"]} == {"Secret Doom", "Open Siege"}
    assert owner_view["hidden_count"] == 0 or "hidden_count" in owner_view
    member_view = C.project_clocks_for_viewer(db, c, member)
    assert [r["name"] for r in member_view["clocks"]] == ["Open Siege"]
    assert "advancement_criteria" in member_view["clocks"][0]  # visible-clock pressure is player-facing
    assert "provenance" not in member_view["clocks"][0]
    assert "completion_effect" not in member_view["clocks"][0]
    stub = C.get_clock_for_viewer(db, c, hidden.id, member)
    assert stub == {"clock_id": str(hidden.id), "visible": False}
    assert C.project_clocks_for_viewer(db, c, outsider) == {"clocks": [], "count": 0}
    assert C.get_clock_for_viewer(db, c, hidden.id, outsider) == {
        "clock_id": str(hidden.id), "visible": False}
    full = C.get_clock_for_viewer(db, c, hidden.id, owner)
    assert full["visible"] is True and full["name"] == "Secret Doom"
    db.close()


def test_hidden_frame_redacts_name_and_mechanics():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, name="Secret Doom", visibility="dm_only",
                     advancement_criteria={"kind": "semantic", "event_types": ["game.play"]},
                     completion_criteria={"kind": "semantic", "description": "the vault falls"})
    evidence = [{"sequence": 1, "event_id": str(uuid.uuid4()), "event_type": "game.play",
                 "visibility": "public"}]
    frame = C.build_clock_frame(clock, evidence=evidence, evidence_total=1,
                                from_sequence=1, to_sequence=1, is_authority=False)
    blob = str(frame.state) + " ".join(cd.label for cd in frame.candidates)
    # Hidden name + judged completion mechanics stay out of member inputs;
    # committed gameplay evidence itself remains legitimate decision input.
    assert "Secret Doom" not in blob and "the vault falls" not in blob
    assert "advancement_criteria" not in str(frame.state)
    full = C.build_clock_frame(clock, evidence=evidence, evidence_total=1,
                               from_sequence=1, to_sequence=1, is_authority=True)
    assert full.state["clock_name"] == "Secret Doom"
    db.close()


def test_private_gameplay_advances_hidden_clock_without_disclosure():
    _F, db, c, owner, member, _outsider = _setup()
    clock = _mkclock(db, c, name="Secret Doom", visibility="dm_only", threshold=2)
    lo, hi = _play(db, c, 1, etype="game.play", visibility="dm_only",
                   payload={"whisper": True})
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["results"][0]["outcome"] == "advanced"
    assert C.project_clocks_for_viewer(db, c, member)["clocks"] == []
    member_feed = list_campaign_events(db, c.id, viewer_id=member)
    assert all(e.visibility == "public" for e in member_feed)
    assert C.get_clock_for_viewer(db, c, clock.id, owner)["progress"] == 1
    db.close()


# ── Completion ─────────────────────────────────────────────────────────────

def test_threshold_completion_records_event_and_effect():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=2, completion_effect={"on_complete": "siege_begins"},
                     advancement_criteria={"kind": "deterministic", "event_types": ["game.play"],
                                           "max_advance": 2})
    lo, hi = _play(db, c, 2)
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["results"][0]["outcome"] == "completed"
    fresh = db.get(CampaignClock, clock.id)
    assert fresh.status == "completed" and fresh.completed_at is not None
    assert fresh.resolution["reason"] == "threshold_reached"
    assert fresh.resolution["completion_effect"] == {"on_complete": "siege_begins"}
    done = _clock_events(db, "clock.completed")
    assert len(done) == 1
    assert done[0].payload["completion_effect"] == {"on_complete": "siege_begins"}
    # Terminal: later ranges skip it entirely.
    hi2 = _rev(db, c)
    out2 = C.consolidate_clocks_for_range(db, c.id, 1, hi2, _range(db, c, 1, hi2),
                                          decision_service=DecisionService(_NeverCall()))
    assert out2["clocks_evaluated"] == 0
    db.close()


def test_defensive_completion_when_threshold_already_met():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=2)
    lo, hi = _play(db, c, 1)
    # Simulate a clock whose progress reached threshold without flipping
    # status (e.g. a pre-#218 row): evaluation completes it mechanically.
    db.get(CampaignClock, clock.id).progress = 2
    db.flush()
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["results"][0]["outcome"] == "completed"
    assert db.get(CampaignClock, clock.id).status == "completed"
    db.close()


def test_semantic_complete_candidate():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=6,
                     advancement_criteria={"kind": "semantic", "max_advance": 1},
                     completion_criteria={"kind": "semantic",
                                          "description": "the council swears fealty"})
    lo, hi = _play(db, c, 1)
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=_scripted("COMPLETE"))
    res = out["results"][0]
    assert res["outcome"] == "completed"
    # Irreversible completion is never direct-executed: the confident
    # selection returns primer_advisory and applies via the mandatory
    # deterministic revalidation (the policy's confirmation step).
    assert res["directive"] == "primer_advisory"
    fresh = db.get(CampaignClock, clock.id)
    assert fresh.status == "completed" and int(fresh.progress) == 6
    assert fresh.resolution["reason"] == "criteria_met"
    db.close()


def test_semantic_complete_requires_completion_evidence():
    _F, db, c, *_ = _setup()

    def _mkvoteclock(**kw):
        params = {"name": "Coup", "threshold": 6,
                  "advancement_criteria": {"kind": "semantic", "max_advance": 1},
                  "completion_criteria": {"kind": "semantic",
                                          "description": "the council swears fealty",
                                          "event_types": ["council.vote"]},
                  "status": "active", "provenance": {"source": "test"}}
        params.update(kw)
        row, _event = C.create_clock_authoritative(db, c.id, _rev(db, c), **params)
        return row

    # Mismatched range: only game.play events, so the council.vote completion
    # filter admits nothing — COMPLETE is not offered and fails closed even
    # though advancement evidence exists.
    clock = _mkvoteclock()
    lo, hi = _play(db, c, 1, etype="game.play")
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=_scripted("COMPLETE"))
    res = out["results"][0]
    assert res["outcome"] == "deferred" and res["failure"] is not None
    assert int(db.get(CampaignClock, clock.id).progress) == 0
    assert _clock_events(db, "clock.completed") == []
    db.close()


def test_semantic_complete_offered_on_completion_evidence():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=6,
                     advancement_criteria={"kind": "semantic", "max_advance": 1},
                     completion_criteria={"kind": "semantic",
                                          "description": "the council swears fealty",
                                          "event_types": ["council.vote"]})
    # Ticks alone could not finish the clock (max_advance 1 of 6 remaining):
    # COMPLETE is legal only via the matching council.vote evidence.
    lo, hi = _play(db, c, 1, etype="council.vote")
    adapter = FakeDecisionAdapter(answers={QUESTION: "COMPLETE"})
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(adapter))
    res = out["results"][0]
    assert res["outcome"] == "completed"
    assert res["directive"] == "primer_advisory"
    assert res["evidence_count"] == 1
    assert "COMPLETE" in res["telemetry"]["candidate_ids"]
    fresh = db.get(CampaignClock, clock.id)
    assert fresh.status == "completed" and int(fresh.progress) == 6
    assert fresh.resolution["reason"] == "criteria_met"
    done = _clock_events(db, "clock.completed")
    assert len(done) == 1 and done[0].payload["evidence"][0]["event_type"] == "council.vote"
    db.close()


# ── Defer / failure ────────────────────────────────────────────────────────

def test_explicit_defer_consumes_range_without_advancing():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, advancement_criteria={"kind": "semantic"})
    lo, hi = _play(db, c, 1)
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=_scripted("DEFER"))
    res = out["results"][0]
    assert res["outcome"] == "deferred" and res["failure"] is None
    assert int(db.get(CampaignClock, clock.id).evaluated_through_sequence) == hi
    assert _clock_events(db, "clock.advanced") == []
    assert _clock_events(db, "clock.completed") == []
    db.close()


def test_decision_failure_defers_and_checkpoint_still_advances():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, advancement_criteria={"kind": "semantic"})
    _play(db, c, 4)
    hi = _rev(db, c)
    doomed = DecisionService(FakeDecisionAdapter(answers={}))  # unscripted: raises
    out = run_post_turn_range(db, c.id, 1, hi, clock_decision_service=doomed)
    assert out["duplicate"] is False
    assert out["result"]["clocks"]["results"][0]["outcome"] == "deferred"
    assert get_checkpoint(db, c.id).processed_through_sequence == hi
    db.close()


def test_required_processing_failure_blocks_checkpoint_then_converges(monkeypatch):
    _F, db, c, *_ = _setup()
    _mkclock(db, c)
    _play(db, c, 4)
    hi = _rev(db, c)
    monkeypatch.setattr(C, "collect_evidence", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("evidence store down")))
    with pytest.raises(RuntimeError, match="evidence store down"):
        run_post_turn_range(db, c.id, 1, hi,
                            clock_decision_service=DecisionService(_NeverCall()))
    assert get_checkpoint(db, c.id).processed_through_sequence == 0
    monkeypatch.undo()
    out = run_post_turn_range(db, c.id, 1, hi,
                              clock_decision_service=DecisionService(_NeverCall()))
    assert out["duplicate"] is False
    assert get_checkpoint(db, c.id).processed_through_sequence == hi
    db.close()


# ── Stale rejection + idempotency + idle ────────────────────────────────────

def test_stale_decision_rejected_and_skipped_without_consuming():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=4,
                     advancement_criteria={"kind": "deterministic"})
    lo, hi = _play(db, c, 1)
    events = _range(db, c, lo, hi)
    frame = C.build_clock_frame(clock, evidence=[], evidence_total=1,
                                from_sequence=lo, to_sequence=hi)
    db.get(CampaignClock, clock.id).revision = 2  # concurrent applier won
    db.flush()
    with pytest.raises(C.ClockStaleError):
        C.apply_clock_outcome(db, c.id, clock.id, frame=frame, selected_id="ADVANCE_1",
                              evidence_refs=[{"sequence": lo, "event_id": str(events[0].id),
                                              "event_type": "game.play"}],
                              from_sequence=lo, to_sequence=hi)
    db.rollback()
    fresh = db.get(CampaignClock, clock.id)
    assert int(fresh.progress) == 0 and int(fresh.evaluated_through_sequence) == 0
    db.close()


def test_consolidate_skips_stale_clock_and_continues(monkeypatch):
    _F, db, c, *_ = _setup()
    first = _mkclock(db, c, name="First")
    _mkclock(db, c, name="Second")
    lo, hi = _play(db, c, 1)
    real_apply = C.apply_clock_outcome

    def _flaky(*args, **kwargs):
        if str(kwargs.get("selected_id")) == "ADVANCE_1" and not hasattr(_flaky, "fired"):
            _flaky.fired = True
            raise C.ClockStaleError("x", 1, 2)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(C, "apply_clock_outcome", _flaky)
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(_NeverCall()))
    by_id = {r["clock_id"]: r for r in out["results"]}
    assert by_id[str(first.id)]["reason"] == "stale_revision_skipped"
    assert by_id[str(first.id)]["evaluated"] is False
    assert int(db.get(CampaignClock, first.id).evaluated_through_sequence) == 0
    db.close()


def test_stale_skip_preserves_sibling_clock_work(monkeypatch):
    F, db, c, *_ = _setup()
    first = _mkclock(db, c, name="First")  # deterministic: advances on game.play
    # Second clock matches nothing in the range: its watermark flush must
    # survive a later sibling's stale skip.
    second = _mkclock(db, c, name="Second",
                      advancement_criteria={"kind": "deterministic",
                                            "event_types": ["game.combat"]})
    third = _mkclock(db, c, name="Third")
    lo, hi = _play(db, c, 1)
    real_apply = C.apply_clock_outcome

    def _flaky(*args, **kwargs):
        if str(args[2]) == str(third.id):
            raise C.ClockStaleError("x", 1, 2)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(C, "apply_clock_outcome", _flaky)
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=DecisionService(_NeverCall()))
    by_id = {r["clock_id"]: r for r in out["results"]}
    assert by_id[str(first.id)]["outcome"] == "advanced"
    assert by_id[str(third.id)]["reason"] == "stale_revision_skipped"
    assert by_id[str(third.id)]["evaluated"] is False
    db.commit()  # outer transaction commits: sibling work must be durable
    F2 = F()
    try:
        assert int(F2.get(CampaignClock, first.id).progress) == 1
        assert len(F2.execute(select(CampaignDomainEvent).where(
            CampaignDomainEvent.event_type == "clock.advanced")).scalars().all()) == 1
        # The flushed no-change watermark of the sibling survived the stale
        # skip (a full-session rollback would have reset it to 0).
        assert int(F2.get(CampaignClock, second.id).evaluated_through_sequence) == hi
        stale = F2.get(CampaignClock, third.id)
        assert int(stale.evaluated_through_sequence) == 0  # stale: watermark untouched
    finally:
        F2.close()
    db.close()


def test_duplicate_retry_never_advances_twice():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, threshold=6)
    _play(db, c, 4)
    hi = _rev(db, c)
    first = run_post_turn_range(db, c.id, 1, hi,
                                clock_decision_service=DecisionService(_NeverCall()))
    assert first["result"]["clocks"]["advanced"] == 1
    progress = int(db.get(CampaignClock, clock.id).progress)
    clock_events = len(_clock_events(db, "clock.advanced"))
    second = run_post_turn_range(db, c.id, 1, hi,
                                 clock_decision_service=DecisionService(_NeverCall()))
    assert second["duplicate"] is True
    assert int(db.get(CampaignClock, clock.id).progress) == progress
    assert len(_clock_events(db, "clock.advanced")) == clock_events
    # Overlapping re-evaluation of the same evidence is also a no-op.
    out = C.consolidate_clocks_for_range(db, c.id, 1, hi, _range(db, c, 1, hi),
                                         decision_service=DecisionService(_NeverCall()))
    assert out["results"][0]["reason"] == "already_evaluated"
    db.close()


def test_idle_history_never_advances_clocks():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, advancement_criteria={"kind": "semantic"})
    lo, hi = _play(db, c, 1, etype="game.play")
    out = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                         decision_service=_scripted("NO_CHANGE"))
    assert out["results"][0]["outcome"] == "no_change"
    # A model that WOULD advance cannot touch already-consumed evidence:
    # reprocessing the same range is a no-op even with ADVANCE scripted.
    repeat = C.consolidate_clocks_for_range(db, c.id, lo, hi, _range(db, c, lo, hi),
                                            decision_service=_scripted("ADVANCE_1"))
    assert repeat["results"][0]["reason"] == "already_evaluated"
    assert int(db.get(CampaignClock, clock.id).progress) == 0
    # And the post-turn duplicate path advances nothing either.
    cp_before = _rev(db, c)
    dup = run_post_turn_range(db, c.id, 1, hi,
                              clock_decision_service=_scripted("ADVANCE_1"))
    assert dup["duplicate"] is False  # checkpoint gap itself is new work
    assert int(db.get(CampaignClock, clock.id).progress) == 0
    assert _rev(db, c) == cp_before  # no clock mutation happened inside
    db.close()


# ── Post-turn integration ──────────────────────────────────────────────────

def test_post_turn_default_path_evaluates_clocks():
    _F, db, c, *_ = _setup()
    _mkclock(db, c, threshold=3)
    _play(db, c, 4)
    hi = _rev(db, c)
    out = run_post_turn_range(db, c.id, 1, hi,
                              clock_decision_service=DecisionService(_NeverCall()))
    assert out["processed_through"] == hi
    assert out["result"]["clocks"]["clocks_evaluated"] == 1
    assert out["result"]["clocks"]["advanced"] == 1
    assert get_checkpoint(db, c.id).processed_through_sequence == hi
    db.close()


def test_apply_rejects_evidence_outside_range():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, advancement_criteria={"kind": "semantic"})
    lo, hi = _play(db, c, 1)
    event = _range(db, c, lo, hi)[0]
    frame = C.build_clock_frame(clock, evidence=[], evidence_total=0,
                                from_sequence=lo, to_sequence=hi)
    with pytest.raises(ValueError, match="outside the evaluated range"):
        C.apply_clock_outcome(db, c.id, clock.id, frame=frame, selected_id="NO_CHANGE",
                              evidence_refs=[{"sequence": hi + 50, "event_id": str(event.id),
                                              "event_type": "game.play"}],
                              from_sequence=lo, to_sequence=hi)
    with pytest.raises(ValueError, match="not found in campaign"):
        C.apply_clock_outcome(db, c.id, clock.id, frame=frame, selected_id="NO_CHANGE",
                              evidence_refs=[{"sequence": lo, "event_id": str(uuid.uuid4()),
                                              "event_type": "game.play"}],
                              from_sequence=lo, to_sequence=hi)
    db.close()


# ── Adventure-resolution hook + seed composition ───────────────────────────

def test_retire_clock_for_adventure_resolution():
    _F, db, c, *_ = _setup()
    clock = _mkclock(db, c, name="Doomed")
    row, event = C.retire_clock_authoritative(
        db, c.id, clock.id, _rev(db, c), disposition="retired",
        reason="arc ended peacefully", provenance={"source": "test"})
    assert row.status == "retired" and event.event_type == "clock.retired"
    assert row.resolution["reason"] == "arc ended peacefully"
    with pytest.raises(ValueError, match="already terminal"):
        C.retire_clock_authoritative(
            db, c.id, clock.id, _rev(db, c), disposition="retired",
            reason="again", provenance={"source": "test"})
    with pytest.raises(ValueError, match="disposition"):
        C.retire_clock_authoritative(
            db, c.id, clock.id, _rev(db, c), disposition="paused",
            reason="x", provenance={"source": "test"})
    db.close()


def test_seed_flow_composes_inline_creation():
    _F, db, c, *_ = _setup()

    def mutate(campaign):
        row, created = C.create_clock_inline(
            db, campaign, name="Opening Pressure", threshold=6,
            advancement_criteria={"kind": "deterministic"},
            status="active", provenance={"source": "world_seed"})
        assert created is True

    _campaign, event = commit_campaign_mutation(
        db, c.id, _rev(db, c), event_type="campaign.seeded",
        operation_id="seed-1", mutate=mutate)
    rows = db.execute(select(CampaignClock).where(
        CampaignClock.campaign_id == c.id)).scalars().all()
    assert len(rows) == 1 and rows[0].status == "active"
    db.close()


def test_migration_chain_stays_linear_with_clocks_head():
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend = Path(__file__).parent.parent
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["a218clocks01"]
    assert script.get_revision("a218clocks01").down_revision == "a214identity02"
