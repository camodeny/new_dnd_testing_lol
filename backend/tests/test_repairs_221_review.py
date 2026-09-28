"""Issue #221 review hardening — focused regression tests.

Covers the handoff review findings: character-state campaign association,
clock domain validation, secret-scoped correction projection (no
reason/evidence leak), ambiguous-apply gating with candidate-change
binding, and strict linked-incident/derived-invalidation failure handling.
"""
from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.decisions import DecisionService  # noqa: E402
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.repair.service import (  # noqa: E402
    APPLY_REPAIR,
    KEEP_DISTINCT,
    MERGE,
    REPAIR_QUESTION_ID,
    RETCON,
    RepairCandidate,
    adjudicate_repair,
    apply_repair,
    apply_retcon,
    create_repair,
    list_open_directives,
)
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.post_turn import PostTurnConsistencyIncident  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.repair import CampaignRepair  # noqa: E402


def _factory():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _setup():
    F = _factory()
    db = F()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@x.com"))
    db.flush()
    c = Campaign(owner_id=owner, name="repairs-221-review")
    db.add(c)
    db.flush()
    db.commit()
    db.refresh(c)
    return F, db, c, owner


def _member(db, campaign_id, email="member@x.com"):
    uid = uuid.uuid4()
    db.add(Profile(id=uid, email=email))
    db.flush()
    db.add(CampaignMember(campaign_id=campaign_id, user_id=uid))
    db.flush()
    return uid


def _character_with_sheet(db, owner_id, name="Ari"):
    char = Character(owner_id=owner_id, system="dnd5e", name=name, status="complete")
    db.add(char)
    db.flush()
    sheet = Dnd5eCharacterSheet(character_id=char.id, owner_id=owner_id, character_name=name)
    db.add(sheet)
    db.flush()
    return char, sheet


def _clock(db, c, **kw):
    from app.world.clocks import create_clock_inline
    base = dict(name="Doom", threshold=4,
                advancement_criteria={"kind": "deterministic", "event_types": ["game.play"]},
                status="active", visibility="campaign",
                provenance={"source": "test"})
    base.update(kw)
    row, _ = create_clock_inline(db, db.get(Campaign, c.id), **base)
    db.flush()
    return row


def _entity(db, c, name="Node", **kw):
    from app.world.service import create_entity_inline
    ent, _ = create_entity_inline(db, db.get(Campaign, c.id), entity_type="npc", name=name, **kw)
    db.flush()
    return ent


def _fact(db, c, content="The vault is sealed.", **kw):
    from app.world.knowledge import create_fact_inline
    row, _ = create_fact_inline(db, db.get(Campaign, c.id), content=content, **kw)
    db.flush()
    return row


# ── 1. character_state campaign association ───────────────────────────────────

def test_character_state_cross_campaign_rejected():
    _F, db, c, owner = _setup()
    char, sheet = _character_with_sheet(db, owner)
    db.commit()
    # Second campaign with no membership link to the character owner.
    other_owner = uuid.uuid4()
    db.add(Profile(id=other_owner, email="other@x.com"))
    db.flush()
    other = Campaign(owner_id=other_owner, name="other-campaign")
    db.add(other)
    db.flush()
    db.commit()
    repair, _ = create_repair(
        db, other.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "character_state", "character_id": str(char.id),
                           "patch": {"hit_points_current": 1}}],
        reason="cross-campaign write", fingerprint="chr-x-1", operation_id="op-chr-x-1",
        commit=True,
    )
    out = apply_repair(db, other.id, repair.id, operation_id="op-chr-x-1", commit=True)
    assert out["status"] == "failed"
    assert "member" in (out["error"] or "").lower()
    db.refresh(sheet)
    assert sheet.hit_points_current == 10  # untouched


def test_character_state_owner_mismatch_rejected():
    _F, db, c, owner = _setup()
    char, sheet = _character_with_sheet(db, owner)
    intruder = uuid.uuid4()
    db.add(Profile(id=intruder, email="intruder@x.com"))
    db.flush()
    sheet.owner_id = intruder  # sheet no longer associated with its character's owner
    db.flush()
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "character_state", "character_id": str(char.id),
                           "patch": {"hit_points_current": 1}}],
        reason="owner mismatch", fingerprint="chr-x-2", operation_id="op-chr-x-2",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-chr-x-2", commit=True)
    assert out["status"] == "failed"


def test_character_state_member_happy_path():
    _F, db, c, owner = _setup()
    member = _member(db, c.id)
    char, sheet = _character_with_sheet(db, member)
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "character_state", "character_id": str(char.id),
                           "patch": {"hit_points_current": 3}}],
        reason="member sheet fix", fingerprint="chr-ok-1", operation_id="op-chr-ok-1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-chr-ok-1", commit=True)
    assert out["status"] == "applied"
    db.refresh(sheet)
    assert sheet.hit_points_current == 3


# ── 2. clock domain validation ────────────────────────────────────────────────

def test_clock_negative_progress_rejected():
    _F, db, c, owner = _setup()
    clock = _clock(db, c)
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="clock_fix",
        proposed_changes=[{"domain": "clock", "target_id": str(clock.id),
                           "patch": {"progress": -2}}],
        reason="negative", fingerprint="clk-1", operation_id="op-clk-1", commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-clk-1", commit=True)
    assert out["status"] == "failed"


def test_clock_over_threshold_while_evaluable_rejected():
    _F, db, c, owner = _setup()
    clock = _clock(db, c, threshold=4, status="active")
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="clock_fix",
        proposed_changes=[{"domain": "clock", "target_id": str(clock.id),
                           "patch": {"progress": 9}}],
        reason="over threshold", fingerprint="clk-2", operation_id="op-clk-2", commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-clk-2", commit=True)
    assert out["status"] == "failed"
    assert "threshold" in (out["error"] or "").lower()


def test_clock_terminal_reopen_rejected_and_valid_completion_applies():
    _F, db, c, owner = _setup()
    done = _clock(db, c, threshold=2, status="completed", progress=2,
                  idempotency_key="clk-done")
    live = _clock(db, c, threshold=2, status="active", progress=1,
                  idempotency_key="clk-live")
    db.commit()
    bad, _ = create_repair(
        db, c.id, repair_type="clock_fix",
        proposed_changes=[{"domain": "clock", "target_id": str(done.id),
                           "patch": {"status": "active"}}],
        reason="reopen terminal", fingerprint="clk-3", operation_id="op-clk-3", commit=True,
    )
    out = apply_repair(db, c.id, bad.id, operation_id="op-clk-3", commit=True)
    assert out["status"] == "failed"
    assert "terminal" in (out["error"] or "").lower()
    # Valid fix: complete the over-advanced live clock instead of trimming silently.
    live.progress = 9  # simulate the deterministic contradiction canon holds
    db.flush()
    db.commit()
    good, _ = create_repair(
        db, c.id, repair_type="clock_fix",
        proposed_changes=[{"domain": "clock", "target_id": str(live.id),
                           "patch": {"status": "completed"}}],
        reason="complete over-threshold", fingerprint="clk-4", operation_id="op-clk-4",
        commit=True,
    )
    out = apply_repair(db, c.id, good.id, operation_id="op-clk-4", commit=True)
    assert out["status"] == "applied"


def test_clock_threshold_and_watermark_not_repairable():
    _F, db, c, owner = _setup()
    clock = _clock(db, c)
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="clock_fix",
        proposed_changes=[{"domain": "clock", "target_id": str(clock.id),
                           "patch": {"threshold": 99}}],
        reason="threshold rewrite", fingerprint="clk-5", operation_id="op-clk-5", commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-clk-5", commit=True)
    assert out["status"] == "failed"


# ── 3. secret-scoped correction (no leak, no default broadcast) ───────────────

def test_secret_incident_repairs_silently_with_no_directive():
    _F, db, c, owner = _setup()
    ent = _entity(db, c, name="Hidden Vault", visibility="dm_only")
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"summary": "quiet correction"}}],
        conflicting_records=[{"kind": "world_entity", "id": str(ent.id), "visibility": "dm_only"}],
        evidence={"note": "dm-only residue"},
        reason="secret derived fix", fingerprint="sec-1", operation_id="op-sec-1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-sec-1", commit=True)
    assert out["status"] == "applied"
    assert out["directive_created"] is False
    assert list_open_directives(db, c.id) == []
    db.refresh(repair)
    assert not repair.requires_player_visible_correction
    assert repair.player_projection() is None


def test_player_correction_requires_explicit_text_and_never_leaks_reason():
    _F, db, c, owner = _setup()
    ent = _entity(db, c, name="Tavern", visibility="campaign")
    db.commit()
    secret_reason = "DM-ONLY reasoning that must never reach players"
    repair, _ = create_repair(
        db, c.id, repair_type="fact_correction",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"summary": "new name"}}],
        conflicting_records=[{"kind": "world_entity", "id": str(ent.id), "visibility": "campaign"}],
        evidence={"note": "players saw it"},
        reason=secret_reason, fingerprint="sec-2", operation_id="op-sec-2",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-sec-2", commit=True)
    assert out["status"] == "failed"
    assert "correction_text" in (out["error"] or "")
    assert list_open_directives(db, c.id) == []
    db.refresh(ent)
    assert ent.summary != "new name"  # preflight failed before touching state
    db.refresh(repair)
    assert repair.player_projection() is None
    # Recoverable: retry with explicit player-safe text applies cleanly.
    out = apply_repair(db, c.id, repair.id, operation_id="op-sec-2b",
                       correction_text="The tavern's name is corrected.",
                       commit=True)
    assert out["status"] == "applied"
    db.refresh(repair)
    proj = repair.player_projection()
    assert proj and proj["correction_text"] == "The tavern's name is corrected."
    assert secret_reason not in proj["correction_text"]


def test_secret_retcon_stays_silent():
    _F, db, c, owner = _setup()
    fact = _fact(db, c, content="The spy is Bob.", visibility="dm_only",
                 epistemic_state="confirmed", idempotency_key="spy-s1")
    db.commit()
    out = apply_retcon(
        db, c.id,
        proposed_changes=[{"domain": "fact", "target_id": str(fact.id),
                           "patch": {"content": "The spy is Carol."}, "reason": "retcon"}],
        conflicting_records=[{"kind": "world_fact", "id": str(fact.id), "visibility": "dm_only"}],
        reason="secret retcon", correction_text="The spy is Carol.",
        operation_id="op-secr-1", commit=True,
    )
    assert out["status"] == "retconned"
    assert out.get("directive_created") is False
    assert list_open_directives(db, c.id) == []
    repair = db.get(CampaignRepair, uuid.UUID(out["repair_id"]))
    assert not repair.requires_player_visible_correction
    assert repair.player_projection() is None


# ── 4. ambiguous gating + candidate binding ───────────────────────────────────

def test_ambiguous_apply_blocked_until_approved():
    _F, db, c, owner = _setup()
    fact = _fact(db, c, content="The bridge stands.", epistemic_state="confirmed",
                 visibility="campaign", idempotency_key="br-x1")
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="ambiguous",
        proposed_changes=[{"domain": "fact", "target_id": str(fact.id),
                           "patch": {"content": "The bridge fell."}}],
        evidence={"note": "ambiguous paraphrase"}, reason="ambiguous",
        fingerprint="amb-x1", operation_id="op-amb-x1", commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-amb-x1", commit=True)
    assert out["status"] == "failed"
    assert "adjudication" in (out["error"] or "").lower()
    db.refresh(fact)
    assert fact.status == "active"  # untouched


def test_adjudication_binds_candidate_changes():
    _F, db, c, owner = _setup()
    fact = _fact(db, c, content="The bridge stands.", epistemic_state="confirmed",
                 visibility="campaign", idempotency_key="br-x2")
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="ambiguous",
        proposed_changes=[{"domain": "fact", "target_id": str(fact.id),
                           "patch": {"content": "STALE PROPOSAL"}}],
        evidence={"correction_text": "The bridge had already collapsed.",
                  "player_visible_exposure": True},
        reason="ambiguous", fingerprint="amb-x2", operation_id="op-amb-x2",
        commit=True,
    )
    bound = [{"domain": "fact", "target_id": str(fact.id),
              "patch": {"content": "The bridge had already collapsed."},
              "reason": "dm-selected fix"}]
    service = DecisionService(FakeDecisionAdapter(answers={REPAIR_QUESTION_ID: APPLY_REPAIR}))
    verdict = adjudicate_repair(
        db, repair, decision_service=service,
        candidates=[RepairCandidate(APPLY_REPAIR, "apply", changes=bound),
                    RepairCandidate(RETCON, "retcon", changes=[]),
                    RepairCandidate("DEFER", "defer")],
        commit=True,
    )
    assert verdict.selected_id == APPLY_REPAIR
    db.refresh(repair)
    assert list(repair.proposed_changes or []) == bound
    out = apply_repair(db, c.id, repair.id, operation_id="op-amb-x2b", commit=True)
    assert out["status"] == "applied"
    from models.world import WorldFact
    new = db.get(WorldFact, uuid.UUID(out["applied_changes"][0]["after"]["fact_id"]))
    assert new.content == "The bridge had already collapsed."


def test_keep_distinct_approved_noop_applies_without_state_touch():
    _F, db, c, owner = _setup()
    from app.world.service import create_entity_inline
    a, _ = create_entity_inline(db, db.get(Campaign, c.id), entity_type="npc",
                                name="Mara Venn", idempotency_key="kd-a")
    b, _ = create_entity_inline(db, db.get(Campaign, c.id), entity_type="npc",
                                name="Mara Venn", idempotency_key="kd-b")
    db.flush()
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="ambiguous", proposed_changes=[],
        evidence={"note": "same name, maybe distinct"}, reason="ambiguous identity",
        fingerprint="amb-x3", operation_id="op-amb-x3", commit=True,
    )
    service = DecisionService(FakeDecisionAdapter(answers={REPAIR_QUESTION_ID: KEEP_DISTINCT}))
    verdict = adjudicate_repair(
        db, repair, decision_service=service,
        candidates=[RepairCandidate(MERGE, "merge", changes=[
            {"domain": "entity_merge", "duplicate_id": str(b.id),
             "canonical_id": str(a.id)}]),
            RepairCandidate(KEEP_DISTINCT, "keep distinct", changes=[]),
                    RepairCandidate("DEFER", "defer")],
        commit=True,
    )
    assert verdict.selected_id == KEEP_DISTINCT
    out = apply_repair(db, c.id, repair.id, operation_id="op-amb-x3b", commit=True)
    assert out["status"] == "applied"
    assert out["applied_changes"] == []
    db.refresh(a)
    db.refresh(b)
    assert a.superseded_by_id is None and b.superseded_by_id is None


def test_deferred_repair_cannot_apply_until_readjudicated():
    _F, db, c, owner = _setup()
    repair, _ = create_repair(
        db, c.id, repair_type="ambiguous", proposed_changes=[],
        evidence={"note": "thin"}, reason="thin",
        fingerprint="amb-x4", operation_id="op-amb-x4", commit=True,
    )
    service = DecisionService(FakeDecisionAdapter(answers={REPAIR_QUESTION_ID: "DEFER"}))
    verdict = adjudicate_repair(db, repair, decision_service=service, commit=True)
    assert verdict.selected_id == "DEFER"
    out = apply_repair(db, c.id, repair.id, operation_id="op-amb-x4b", commit=True)
    assert out["status"] in ("deferred", "adjudication_required")
    assert "applied_changes" not in out


# ── 5. strict incident + derived failure handling ─────────────────────────────

def _incident(db, campaign_id, key="inc-x1"):
    row = PostTurnConsistencyIncident(
        campaign_id=campaign_id, from_sequence=1, to_sequence=1,
        incident_key=key, incident_type="fact_conflict", category="canon_conflict",
        severity="standard", status="open", detection_path="deterministic",
        evidence={}, affected_records=[],
    )
    db.add(row)
    db.flush()
    return row


def test_cross_campaign_incident_link_fails_repair():
    _F, db, c, owner = _setup()
    other_owner = uuid.uuid4()
    db.add(Profile(id=other_owner, email="o2@x.com"))
    db.flush()
    other = Campaign(owner_id=other_owner, name="other")
    db.add(other)
    db.flush()
    incident = _incident(db, other.id, key="inc-foreign")
    db.commit()
    ent = _entity(db, c, name="Local", idempotency_key="loc-1")
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"summary": "x"}}],
        incident_id=incident.id,
        reason="foreign link", fingerprint="inc-x1", operation_id="op-inc-x1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-inc-x1", commit=True)
    assert out["status"] == "failed"
    assert "another campaign" in (out["error"] or "").lower()
    db.refresh(ent)
    assert ent.summary != "x"  # rolled back with the failed incident resolve
    db.refresh(incident)
    assert incident.status == "open"


def test_missing_incident_link_fails_instead_of_silent_applied():
    _F, db, c, owner = _setup()
    ent = _entity(db, c, name="Local2", idempotency_key="loc-2")
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"summary": "y"}}],
        incident_id=uuid.uuid4(),  # dangling link
        reason="dangling link", fingerprint="inc-x2", operation_id="op-inc-x2",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-inc-x2", commit=True)
    assert out["status"] == "failed"
    assert "incident" in (out["error"] or "").lower()


def test_linked_incident_resolves_on_success():
    _F, db, c, owner = _setup()
    ent = _entity(db, c, name="Local3", idempotency_key="loc-3")
    incident = _incident(db, c.id, key="inc-ok")
    db.commit()
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"summary": "z"}}],
        incident_id=incident.id,
        reason="linked ok", fingerprint="inc-x3", operation_id="op-inc-x3",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-inc-x3", commit=True)
    assert out["status"] == "applied"
    db.refresh(incident)
    assert incident.status == "resolved"


def test_invalid_embedding_invalidation_fails_repair():
    _F, db, c, owner = _setup()
    repair, _ = create_repair(
        db, c.id, repair_type="embedding_refresh",
        proposed_changes=[{"domain": "embedding", "action": "mark_stale",
                           "source_type": "not_a_source", "source_id": str(uuid.uuid4())}],
        reason="bad embedding ref", fingerprint="emb-x1", operation_id="op-emb-x1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-emb-x1", commit=True)
    assert out["status"] == "failed"


def test_unknown_repair_type_rejected_at_creation():
    _F, db, c, owner = _setup()
    import pytest as _pytest
    with _pytest.raises(ValueError):
        create_repair(db, c.id, repair_type="vibes", proposed_changes=[],
                      reason="x", commit=True)


# ── 6. failed multi-domain repair must persist no partial state ───────────────

def test_failed_multidomain_repair_persists_no_partial_state():
    """First handler succeeds, later handler fails: committed DB state must
    show the first domain unchanged (verified with a fresh session, not the
    potentially dirty identity map)."""
    from models.world import WorldEntity

    _F, db, c, owner = _setup()
    ent = _entity(db, c, name="Keep", summary="before", idempotency_key="rb-ent-1")
    db.commit()
    ent_id = ent.id
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[
            {"domain": "entity", "target_id": str(ent.id),
             "patch": {"summary": "MUTATED"}},
            {"domain": "clock", "target_id": str(uuid.uuid4()),
             "patch": {"progress": 1}},
        ],
        reason="first ok, second fails",
        fingerprint="rb-1", operation_id="op-rb-1", commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-rb-1", commit=True)
    assert out["status"] == "failed"
    assert "not found" in (out["error"] or "").lower()
    repair_id = repair.id
    db.close()
    fresh = _F()
    try:
        row = fresh.get(WorldEntity, ent_id)
        assert row.summary == "before"
        assert row.revision == 1
        rep = fresh.get(CampaignRepair, repair_id)
        assert rep.status == "failed"
        assert rep.applied_changes == []
    finally:
        fresh.close()
