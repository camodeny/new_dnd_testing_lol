"""Issue #220 — post-turn deterministic + semantic consistency incidents.

Verifies contradictions between newly materialized state, completed visible
turns, clocks, current scene, facts, and summaries become explicit incidents
instead of silent normalization: deterministic conflicts need no AI call,
ambiguous semantic residue uses bounded judgments, and required unresolved
incidents keep the range incomplete.
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
from app.decisions import DecisionService  # noqa: E402
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.post_turn.incidents import (  # noqa: E402
    CONSISTENT,
    CONTRADICTION,
    INCIDENT_QUESTION_ID,
    UNCERTAIN,
    ProposedWrite,
    SemanticPair,
    get_consistency_stats,
    is_range_complete,
    list_unresolved_incidents,
    resolve_incident,
    verify_post_turn_consistency,
)
from app.post_turn.service import get_checkpoint, run_post_turn_range  # noqa: E402
from app.world.knowledge import create_fact_inline  # noqa: E402
from app.world.service import apply_scene_update_inline, create_entity_inline  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.post_turn import PostTurnConsistencyIncident  # noqa: E402
from models.world import CampaignClock, CampaignSummary  # noqa: E402


@pytest.fixture(autouse=True)
def _no_auto_trigger(monkeypatch):
    monkeypatch.setenv("POST_TURN_AUTO_TRIGGER", "0")


def _factory():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _setup():
    from models.profiles import Profile

    F = _factory()
    db = F()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@x.com"))
    db.flush()
    c = Campaign(owner_id=owner, name="incidents-220")
    db.add(c)
    db.flush()
    db.commit()
    db.refresh(c)
    return F, db, c


def _rev(db, c):
    return int(db.get(Campaign, c.id).revision or 0)


def _commit(db, c, payload=None, etype="game.play", visibility="campaign", op=None):
    rev = _rev(db, c)
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type=etype, payload=payload or {"n": rev + 1},
        visibility=visibility, operation_id=op or f"op-{rev + 1}-{uuid.uuid4().hex[:6]}",
    )
    db.refresh(event)
    return event


def _scripted(answer):
    return DecisionService(FakeDecisionAdapter(answers={INCIDENT_QUESTION_ID: answer}))


def _aria_dead(db, c):
    """Canon: Aria visibly dead, backed by a member-visible committed event."""
    e1 = _commit(db, c, payload={"outcome": "Aria falls in the crypt"},
                 etype="dm.turn_committed", visibility="campaign")
    ent, _ = create_entity_inline(
        db, db.get(Campaign, c.id), entity_type="npc", name="Aria",
        status="dead", visibility="campaign", idempotency_key="aria-220",
    )
    db.flush()
    ent.source_event_id = e1.id
    db.flush()
    return e1, ent


# ── 1. visible death vs surviving state ────────────────────────────────────

def test_visible_death_vs_surviving_state_is_deterministic():
    _F, db, c = _setup()
    e1, ent = _aria_dead(db, c)
    adapter = FakeDecisionAdapter(answers={})
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e1.sequence,
        proposed=[ProposedWrite(kind="entity_status", target="Aria",
                                value={"status": "active"},
                                source_sequence=e1.sequence)],
        decision_service=DecisionService(adapter),
    )
    assert out["complete"] is False
    assert adapter.calls == [], "deterministic conflicts must not call any model"
    assert len(out["incidents"]) == 1
    inc = out["incidents"][0]
    assert inc["incident_type"] == "visible_turn_conflict"
    assert inc["severity"] == "high"
    assert inc["status"] == "open"
    assert inc["detection_path"] == "deterministic"
    assert inc["evidence"]["canon"]["status"] == "dead"
    assert inc["evidence"]["newer_committed_is_authority"] is True
    assert is_range_complete(db, c.id, e1.sequence, e1.sequence) is False


# ── 2. exact current-location conflict ─────────────────────────────────────

def test_exact_current_location_conflict():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    tavern, _ = create_entity_inline(
        db, db.get(Campaign, c.id), entity_type="location", name="Tavern",
        visibility="campaign", idempotency_key="tavern-220")
    crypt, _ = create_entity_inline(
        db, db.get(Campaign, c.id), entity_type="location", name="Crypt",
        visibility="campaign", idempotency_key="crypt-220")
    db.flush()
    apply_scene_update_inline(db, db.get(Campaign, c.id), new_revision=e1.sequence,
                              location_entity_id=tavern.id, location_name="Tavern")
    db.commit()
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e1.sequence,
        proposed=[ProposedWrite(kind="scene_location", target="scene",
                                value={"location_entity_id": str(crypt.id),
                                       "location_name": "Crypt"},
                                source_sequence=e1.sequence)],
    )
    assert out["complete"] is False
    inc = out["incidents"][0]
    assert inc["incident_type"] == "scene_location_conflict"
    assert inc["evidence"]["canon"]["location_entity_id"] == str(tavern.id)


# ── 3. stale delayed hidden consequence ────────────────────────────────────

def test_stale_delayed_hidden_consequence_newer_canon_wins():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    e2 = _commit(db, c)
    e5 = _commit(db, c)
    camp = db.get(Campaign, c.id)
    fact, _ = create_fact_inline(
        db, camp, content="The vault is sealed.", epistemic_state="confirmed",
        visibility="campaign", source_event_id=e5.id,
        operation_id="fact-sealed-220", idempotency_key="vault-220")
    db.commit()
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e5.sequence,
        proposed=[ProposedWrite(kind="fact", target="vault",
                                value={"fact_id": str(fact.id),
                                       "content": "The vault is open."},
                                source_sequence=e2.sequence,
                                visibility="dm_only")],
    )
    assert out["complete"] is False
    inc = out["incidents"][0]
    assert inc["incident_type"] == "stale_hidden_consequence"
    assert inc["evidence"]["newer_committed_is_authority"] is True
    # The verifier records; it never rewrites canon itself.
    db.refresh(fact)
    assert fact.content == "The vault is sealed."


# ── 4. deterministic clock contradiction ───────────────────────────────────

def test_deterministic_clock_contradiction():
    from app.world import clocks as C

    _F, db, c = _setup()
    e1 = _commit(db, c)
    clock, _event = C.create_clock_authoritative(
        db, c.id, _rev(db, c), name="Ritual", threshold=5,
        advancement_criteria={"kind": "deterministic", "event_types": ["game.play"]},
        status="active", progress=3, provenance={"source": "test"})
    db.commit()
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e1.sequence,
        proposed=[ProposedWrite(kind="clock", target="Ritual",
                                value={"progress": 1},
                                source_sequence=e1.sequence)],
    )
    assert out["complete"] is False
    inc = out["incidents"][0]
    assert inc["incident_type"] == "clock_contradiction"
    assert inc["detection_path"] == "deterministic"
    assert inc["evidence"]["canon"]["progress"] == 3


# ── 5/6/7. semantic residue: contradiction / non-conflict / uncertain ─────

def _pair():
    return SemanticPair(
        pair_id="aria-fate",
        canon_claim={"text": "Aria is alive and well in the tavern.",
                     "visibility": "campaign", "record_ref": "fact:aria-alive"},
        new_claim={"text": "Aria has passed away from this world.",
                   "visibility": "campaign", "record_ref": "summary:claim-0"},
        target_ref="aria-fate",
        context="post-turn summary vs canon fact",
    )


def test_paraphrased_semantic_contradiction():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e1.sequence,
        semantic_pairs=[_pair()], decision_service=_scripted(CONTRADICTION),
    )
    assert out["complete"] is False
    assert out["decision_distribution"][CONTRADICTION] == 1
    inc = out["incidents"][0]
    assert inc["incident_type"] == "semantic_contradiction"
    assert inc["detection_path"] == "semantic"
    assert inc["status"] == "open"
    assert inc["decision_policy"]["decision_class"] == "post_turn_consistency"
    assert inc["decision_model"]
    assert is_range_complete(db, c.id, e1.sequence, e1.sequence) is False


def test_semantic_non_conflict_records_no_incident():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e1.sequence,
        semantic_pairs=[_pair()], decision_service=_scripted(CONSISTENT),
    )
    assert out["complete"] is True
    assert out["incidents"] == []
    assert out["decision_distribution"][CONSISTENT] == 1


def test_uncertain_deferred_case_stays_unresolved():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e1.sequence,
        semantic_pairs=[_pair()], decision_service=_scripted(UNCERTAIN),
    )
    assert out["complete"] is False
    inc = out["incidents"][0]
    assert inc["incident_type"] == "semantic_deferred"
    assert inc["status"] == "deferred"
    assert is_range_complete(db, c.id, e1.sequence, e1.sequence) is False


def test_decision_provider_failure_preserves_unresolved_state():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    # Unscripted question: the fake adapter raises instead of inventing output.
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e1.sequence,
        semantic_pairs=[_pair()],
        decision_service=DecisionService(FakeDecisionAdapter(answers={})),
    )
    assert out["complete"] is False
    inc = out["incidents"][0]
    assert inc["incident_type"] == "semantic_deferred"
    assert inc["status"] == "deferred"


# ── 8. positive semantic cannot override deterministic ─────────────────────

def test_positive_semantic_cannot_override_deterministic():
    _F, db, c = _setup()
    e1, ent = _aria_dead(db, c)
    pair = SemanticPair(
        pair_id="aria-alive-claim",
        canon_claim={"text": "Aria is dead.", "visibility": "campaign",
                     "record_ref": f"entity:{ent.id}"},
        new_claim={"text": "Aria lives.", "visibility": "campaign",
                   "record_ref": "proposal:aria"},
        target_ref=str(ent.id),
    )
    out = verify_post_turn_consistency(
        db, c.id, e1.sequence, e1.sequence,
        proposed=[ProposedWrite(kind="entity_status", target="Aria",
                                value={"status": "active"},
                                source_sequence=e1.sequence)],
        semantic_pairs=[pair], decision_service=_scripted(CONSISTENT),
    )
    assert out["decision_distribution"][CONSISTENT] == 1
    assert out["complete"] is False
    types = {i["incident_type"] for i in out["incidents"]}
    assert "visible_turn_conflict" in types
    assert "semantic_contradiction" not in types


# ── 9. duplicate detection (idempotent) ────────────────────────────────────

def test_duplicate_detection_is_idempotent():
    _F, db, c = _setup()
    e1, _ent = _aria_dead(db, c)
    kw = dict(
        proposed=[ProposedWrite(kind="entity_status", target="Aria",
                                value={"status": "active"},
                                source_sequence=e1.sequence)],
    )
    first = verify_post_turn_consistency(db, c.id, e1.sequence, e1.sequence, **kw)
    second = verify_post_turn_consistency(db, c.id, e1.sequence, e1.sequence, **kw)
    assert len(first["incidents"]) == 1 and len(second["incidents"]) == 1
    assert first["incidents"][0]["id"] == second["incidents"][0]["id"]
    rows = db.execute(select(PostTurnConsistencyIncident).where(
        PostTurnConsistencyIncident.campaign_id == c.id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].repeat_count == 2


# ── 10. verifier failure ───────────────────────────────────────────────────

def test_verifier_failure_recorded_operational_not_complete(monkeypatch):
    from app.post_turn import incidents as I

    _F, db, c = _setup()
    e1 = _commit(db, c)

    def _boom(*args, **kwargs):
        raise RuntimeError("canon read exploded")

    monkeypatch.setattr(I, "detect_canon_self_conflicts", _boom)
    with pytest.raises(RuntimeError, match="canon read exploded"):
        verify_post_turn_consistency(db, c.id, e1.sequence, e1.sequence)
    rows = db.execute(select(PostTurnConsistencyIncident).where(
        PostTurnConsistencyIncident.campaign_id == c.id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].incident_type == "verifier_failure"
    assert rows[0].status == "verifier_failed"
    assert is_range_complete(db, c.id, e1.sequence, e1.sequence) is False


# ── derived summaries are checkable + gate the checkpoint ──────────────────

def test_summary_contradiction_detectable_even_though_derived():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    e2 = _commit(db, c)
    camp = db.get(Campaign, c.id)
    create_entity_inline(db, camp, entity_type="npc", name="Aria",
                         visibility="campaign", idempotency_key="aria-sum-220")
    create_fact_inline(db, camp, content="Aria is alive and well.",
                       epistemic_state="confirmed", visibility="campaign",
                       operation_id="sum-fact-220", idempotency_key="aria-alive-220")
    db.add(CampaignSummary(
        id=uuid.uuid4(), campaign_id=c.id, scope="running",
        from_sequence=e1.sequence, to_sequence=e2.sequence,
        source_revision=e2.sequence, status="current", visibility="campaign",
        prose="Aria is dead in the crypt.",
        claims=[{"id": "claim-0", "text": "Aria is dead in the crypt.",
                 "source_from": e1.sequence, "source_to": e2.sequence,
                 "verdict": "SUPPORTED"}],
        claim_count=1,
    ))
    db.commit()
    out = verify_post_turn_consistency(db, c.id, e1.sequence, e2.sequence)
    assert out["complete"] is False
    assert any(i["incident_type"] == "summary_contradiction"
               for i in out["incidents"])


def test_dangling_source_ref_is_a_source_conflict():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    camp = db.get(Campaign, c.id)
    fact, _ = create_fact_inline(
        db, camp, content="The vault is sealed.", epistemic_state="confirmed",
        visibility="campaign", source_event_id=e1.id,
        operation_id="dangle-220", idempotency_key="dangle-220")
    db.flush()
    # Simulate a truth row whose source no longer exists (bypasses the
    # writer, which validates provenance at write time).
    fact.source_event_id = uuid.uuid4()
    db.commit()
    out = verify_post_turn_consistency(db, c.id, e1.sequence, e1.sequence)
    assert out["complete"] is False
    assert any(i["incident_type"] == "source_conflict" for i in out["incidents"])


def test_required_unresolved_incidents_block_post_turn_complete():
    from app.post_turn.incidents import ConsistencyBlocked

    _F, db, c = _setup()
    e1 = _commit(db, c)
    e2 = _commit(db, c)
    camp = db.get(Campaign, c.id)
    fact, _ = create_fact_inline(
        db, camp, content="The vault is sealed.", epistemic_state="confirmed",
        visibility="campaign", source_event_id=e2.id,
        operation_id="dangle-pipe-220", idempotency_key="dangle-pipe-220")
    db.flush()
    # Simulate a truth row whose source no longer exists (bypasses the
    # writer, which validates provenance at write time). Earlier pipeline
    # phases (materialize/clocks) leave facts untouched, so the
    # contradiction survives to verification.
    fact.source_event_id = uuid.uuid4()
    db.commit()
    with pytest.raises(ConsistencyBlocked):
        run_post_turn_range(db, c.id, e1.sequence, e2.sequence)
    assert int(get_checkpoint(db, c.id, commit=False).processed_through_sequence or 0) == 0
    pending = list_unresolved_incidents(db, c.id)
    assert len(pending) == 1
    assert pending[0]["incident_type"] == "source_conflict"
    # Player-facing listing redacts private evidence.
    redacted = list_unresolved_incidents(db, c.id, dm_internal=False)
    assert "evidence" not in redacted[0]
    # Repair resolves; the range can complete afterwards.
    resolve_incident(db, uuid.UUID(pending[0]["id"]))
    assert is_range_complete(db, c.id, e1.sequence, e2.sequence) is True
    stats = get_consistency_stats(db, c.id)
    assert stats["by_type"]["source_conflict"] == 1
    assert stats["by_detection_path"]["deterministic"] == 1
    assert stats["avg_time_to_resolution_seconds"] is not None


def test_clean_range_completes_with_no_incidents():
    _F, db, c = _setup()
    e1 = _commit(db, c)
    e2 = _commit(db, c)
    out = verify_post_turn_consistency(db, c.id, e1.sequence, e2.sequence)
    assert out == {
        "complete": True,
        "from_sequence": e1.sequence,
        "to_sequence": e2.sequence,
        "incidents": [],
        "unresolved": 0,
        "decision_distribution": {CONSISTENT: 0, CONTRADICTION: 0, UNCERTAIN: 0},
        "decision_model": None,
        "detection_latency_ms": out["detection_latency_ms"],
    }
    assert is_range_complete(db, c.id, e1.sequence, e2.sequence) is True
