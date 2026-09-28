"""Issue #221 — explicit repair records, directives, deterministic fixes, retcon propagation.

Covers the issue verification list: stale summary repair, wrong scene
field, duplicate entity merge/keep-distinct, player-visible correction,
ambiguous conflict adjudication, explicit retcon, dependent
summary/embedding refresh, secret-safe correction, and idempotent retry.
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
from app.decisions import DecisionService  # noqa: E402
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.repair.service import (  # noqa: E402
    APPLY_REPAIR,
    DEFER,
    KEEP_DISTINCT,
    MERGE,
    REPAIR_QUESTION_ID,
    RETCON,
    RepairCandidate,
    adjudicate_repair,
    apply_repair,
    apply_retcon,
    build_repair_directive_context_records,
    close_directive,
    consume_directive,
    create_directive_for_repair,
    create_repair,
    get_repair_stats,
    list_open_directives,
)
from models.campaigns import Campaign  # noqa: E402
from models.repair import CampaignRepair, RepairDirective  # noqa: E402


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
    c = Campaign(owner_id=owner, name="repairs-221")
    db.add(c)
    db.flush()
    db.commit()
    db.refresh(c)
    return F, db, c


def _entity(db, c, name="Mara Venn", **kw):
    from app.world.service import create_entity_inline
    ent, _ = create_entity_inline(db, db.get(Campaign, c.id), entity_type="npc", name=name, **kw)
    db.flush()
    return ent


def _fact(db, c, content="The bridge stands.", **kw):
    from app.world.knowledge import create_fact_inline
    row, _ = create_fact_inline(db, db.get(Campaign, c.id), content=content, **kw)
    db.flush()
    return row


# ── Audit completeness ────────────────────────────────────────────────────────

def test_repair_has_before_after_evidence_reason_source_audit():
    _F, db, c = _setup()
    ent = _entity(db, c, summary="old summary")
    repair, created = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"summary": "new summary"}}],
        evidence={"source": "incident-1", "records": [str(ent.id)]},
        conflicting_records=[{"kind": "world_entity", "id": str(ent.id)}],
        reason="stale derived summary",
        source="system", fingerprint="audit-1", operation_id="op-audit-1",
        commit=True,
    )
    assert created
    out = apply_repair(db, c.id, repair.id, operation_id="op-audit-1", commit=True)
    assert out["status"] == "applied"
    db.refresh(repair)
    assert repair.before_state and repair.applied_changes
    applied = repair.applied_changes[0]
    assert applied["before"]["summary"] == "old summary"
    assert applied["after"]["summary"] == "new summary"
    assert repair.evidence and repair.reason and repair.source
    assert repair.operation_id == "op-audit-1"


# ── Deterministic stale derived repair without DM adjudication ────────────────

def test_stale_summary_repairs_without_dm_call():
    _F, db, c = _setup()
    from app.campaigns.events import commit_campaign_mutation
    from app.world import summaries as _summaries

    rev = int(db.get(Campaign, c.id).revision or 0)
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type="game.play", payload={"n": 1},
        visibility="campaign", operation_id="op-sum-1",
    )
    db.refresh(event)
    row = _summaries.CampaignSummary if hasattr(_summaries, "CampaignSummary") else None
    from models.world import CampaignSummary
    s = CampaignSummary(campaign_id=c.id, scope="running", from_sequence=event.sequence,
                        to_sequence=event.sequence, source_revision=event.sequence,
                        status="current", visibility="campaign", prose="stale prose",
                        claims=[{"id": "c1", "text": "stale claim"}])
    db.add(s)
    db.flush()
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "summary", "from_sequence": event.sequence,
                           "to_sequence": event.sequence, "reason": "stale derived"}],
        reason="stale summary", fingerprint="sum-1", operation_id="op-sum-rep",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-sum-rep", commit=True)
    assert out["status"] == "applied"
    db.refresh(s)
    assert s.status == "stale"
    # No decision service was ever consulted — deterministic path only.
    assert repair.detection_path == "automatic"


# ── Wrong scene field ─────────────────────────────────────────────────────────

def test_wrong_scene_field_repair():
    _F, db, c = _setup()
    from app.world.service import apply_scene_update_inline, get_current_scene
    campaign = db.get(Campaign, c.id)
    apply_scene_update_inline(db, campaign, new_revision=int(campaign.revision or 0) + 1,
                              location_name="Wrong Tavern", operation_id="op-scene-0")
    db.flush()
    repair, _ = create_repair(
        db, c.id, repair_type="scene_field",
        proposed_changes=[{"domain": "scene", "patch": {"location_name": "Right Tavern"}}],
        reason="wrong scene field", fingerprint="scene-1", operation_id="op-scene-1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-scene-1", commit=True)
    assert out["status"] == "applied"
    scene = get_current_scene(db, c.id)
    assert scene.location_name == "Right Tavern"
    assert out["applied_changes"][0]["before"]["location_name"] == "Wrong Tavern"


# ── Duplicate entity merge / keep-distinct ────────────────────────────────────

def test_duplicate_entity_merge_preserves_provenance():
    _F, db, c = _setup()
    canonical = _entity(db, c, name="Mara Venn", idempotency_key="mara-canon")
    dup = _entity(db, c, name="Mara", idempotency_key="mara-dup")
    repair, _ = create_repair(
        db, c.id, repair_type="entity_merge",
        proposed_changes=[{"domain": "entity_merge", "duplicate_id": str(dup.id),
                           "canonical_id": str(canonical.id), "reason": "duplicate JIT NPC"}],
        reason="duplicate merge", fingerprint="merge-1", operation_id="op-merge-1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-merge-1", commit=True)
    assert out["status"] == "applied"
    db.refresh(dup)
    assert dup.superseded_by_id == canonical.id
    assert dup.status == "archived"
    # Canonical row survives; duplicate history preserved, not deleted.
    assert db.get(type(canonical), canonical.id) is not None


def test_keep_distinct_resolution_leaves_both_entities_live():
    _F, db, c = _setup()
    from app.repair.service import REGISTERED_DOMAINS
    assert "entity_merge" in REGISTERED_DOMAINS
    first = _entity(db, c, name="Mara Venn", idempotency_key="k1")
    second = _entity(db, c, name="Mara Venn", idempotency_key="k2")
    # KEEP_DISTINCT is an adjudication outcome: no merge handler runs, both stay live.
    repair, _ = create_repair(
        db, c.id, repair_type="entity_keep_distinct",
        proposed_changes=[],
        reason="same name, separate people per evidence",
        source="dm_adjudicated", fingerprint="distinct-1", operation_id="op-distinct-1",
        commit=True,
    )
    repair.decision_policy = {"decision_class": "campaign_repair", "selected": KEEP_DISTINCT}
    db.add(repair)
    db.flush()
    out = apply_repair(db, c.id, repair.id, operation_id="op-distinct-1", commit=True)
    assert out["status"] == "applied"
    assert first.superseded_by_id is None and second.superseded_by_id is None


# ── Player-visible correction + directive lifecycle ───────────────────────────

def test_player_visible_correction_creates_consumable_directive():
    _F, db, c = _setup()
    ent = _entity(db, c, name="Tavern", visibility="campaign")
    repair, _ = create_repair(
        db, c.id, repair_type="fact_correction",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"summary": "It was the Rusty Flagon, not the Prancing Pony."},
                           "player_visible": True}],
        conflicting_records=[{"kind": "world_entity", "id": str(ent.id), "visibility": "campaign"}],
        evidence={"player_visible_exposure": True, "correction_text": "It was the Rusty Flagon, not the Prancing Pony."},
        reason="players saw the wrong name",
        requires_player_visible_correction=True,
        player_visible_correction={"correction_text": "It was the Rusty Flagon, not the Prancing Pony.",
                                   "scope": "campaign"},
        fingerprint="pvc-1", operation_id="op-pvc-1", commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-pvc-1", commit=True)
    assert out["status"] == "applied"
    assert out["directive_created"] is True
    directives = list_open_directives(db, c.id)
    assert len(directives) == 1
    # Directive enters the forward-DM context lane.
    records = build_repair_directive_context_records(db, c.id)
    assert len(records) == 1
    assert records[0]["visibility"] == "dm_only"
    assert records[0]["use"] == "adjudication_only"
    # One-time consumption/closure semantics.
    first = consume_directive(db, c.id, directives[0].id, commit=True)
    assert first["status"] == "consumed"
    again = consume_directive(db, c.id, directives[0].id, commit=True)
    assert again["duplicate"] is True
    closed = close_directive(db, c.id, directives[0].id, commit=True)
    assert closed["status"] == "closed"
    assert list_open_directives(db, c.id) == []
    # Player projection reveals only allowed text.
    db.refresh(repair)
    proj = repair.player_projection()
    assert proj and proj["correction_text"].startswith("It was the Rusty Flagon")
    assert "evidence" not in proj


# ── Ambiguous conflict routes through DM decision; defer stays explicit ──────

def test_ambiguous_conflict_routes_through_dm_decision():
    _F, db, c = _setup()
    repair, _ = create_repair(
        db, c.id, repair_type="ambiguous",
        proposed_changes=[{"domain": "fact", "target_id": "00000000-0000-0000-0000-000000000000",
                           "patch": {"content": "x"}}],
        evidence={"note": "ambiguous paraphrase"}, reason="ambiguous",
        fingerprint="amb-1", operation_id="op-amb-1", commit=True,
    )
    service = DecisionService(FakeDecisionAdapter(answers={REPAIR_QUESTION_ID: APPLY_REPAIR}))
    verdict = adjudicate_repair(
        db, repair, decision_service=service,
        candidates=[RepairCandidate(APPLY_REPAIR, "apply"), RepairCandidate(DEFER, "defer")],
        commit=True,
    )
    assert verdict.selected_id == APPLY_REPAIR
    db.refresh(repair)
    assert repair.detection_path == "dm_adjudicated"
    assert repair.status == "pending"


def test_defer_leaves_conflict_explicit():
    _F, db, c = _setup()
    repair, _ = create_repair(
        db, c.id, repair_type="ambiguous",
        proposed_changes=[], evidence={"note": "insufficient"},
        reason="no defensible repair", fingerprint="amb-2",
        operation_id="op-amb-2", commit=True,
    )
    service = DecisionService(FakeDecisionAdapter(answers={REPAIR_QUESTION_ID: DEFER}))
    verdict = adjudicate_repair(db, repair, decision_service=service, commit=True)
    assert verdict.selected_id == DEFER
    db.refresh(repair)
    assert repair.status in ("deferred", "adjudication_required")
    assert repair.status not in ("applied", "retconned")


# ── Explicit retcon preserves history + propagates to dependents ─────────────

def test_explicit_retcon_preserves_history_and_refreshes_dependents():
    _F, db, c = _setup()
    from models.world import CampaignSummary, WorldFact
    fact = _fact(db, c, content="The bridge stands.", epistemic_state="confirmed",
                 visibility="campaign", idempotency_key="bridge-1")
    old_id = fact.id
    s = CampaignSummary(campaign_id=c.id, scope="running", from_sequence=1, to_sequence=1,
                        source_revision=1, status="current", visibility="campaign",
                        prose="bridge stands", claims=[{"id": "c1", "text": "bridge stands"}])
    db.add(s)
    db.flush()
    out = apply_retcon(
        db, c.id,
        proposed_changes=[{"domain": "fact", "target_id": str(old_id),
                           "patch": {"content": "The bridge had already collapsed."},
                           "reason": "explicit retcon"}],
        conflicting_records=[{"kind": "world_fact", "id": str(old_id), "visibility": "campaign"}],
        reason="canon correction with history preserved",
        correction_text="Correction: the bridge had already collapsed.",
        operation_id="op-retcon-1", commit=True,
    )
    assert out["status"] == "retconned"
    old = db.get(WorldFact, old_id)
    assert old.status == "superseded"  # preserved, not deleted
    assert old.superseded_by_id is not None
    new = db.get(WorldFact, old.superseded_by_id)
    assert new.content == "The bridge had already collapsed."
    db.refresh(s)
    assert s.status == "stale"  # dependent derived state invalidated
    repair = db.get(CampaignRepair, uuid.UUID(out["repair_id"]))
    assert bool(repair.is_retcon) and bool(repair.requires_player_visible_correction)
    assert list_open_directives(db, c.id)


# ── Dependent embedding refresh ───────────────────────────────────────────────

def test_repair_marks_dependent_embeddings_stale():
    _F, db, c = _setup()
    from app.world import semantic as _semantic
    ent = _entity(db, c, name="Ember Inn")
    row = _semantic.index_source_record(db, c.id, "world_entity", ent.id, commit=True)
    assert row is not None
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"summary": "repaired summary"}}],
        reason="touch entity", fingerprint="emb-1", operation_id="op-emb-1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-emb-1", commit=True)
    assert out["status"] == "applied"
    from models.world import WorldEmbedding
    rows = list(db.execute(select(WorldEmbedding).where(
        WorldEmbedding.campaign_id == c.id,
        WorldEmbedding.source_type == "world_entity",
    )).scalars().all())
    assert rows and all(r.status == "stale" for r in rows)


# ── Secret safety ─────────────────────────────────────────────────────────────

def test_secret_repair_does_not_broaden_visibility():
    _F, db, c = _setup()
    ent = _entity(db, c, name="Hidden Vault", visibility="dm_only")
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                           "patch": {"visibility": "campaign"}}],
        reason="attempted widening", fingerprint="sec-1", operation_id="op-sec-1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-sec-1", commit=True)
    assert out["status"] == "failed"
    db.refresh(ent)
    assert ent.visibility == "dm_only"


def test_repair_does_not_grant_knowledge_after_leak():
    _F, db, c = _setup()
    ent = _entity(db, c, name="Bob")
    fact = _fact(db, c, content="Bob is the spy.", visibility="dm_only", idempotency_key="spy-1")
    repair, _ = create_repair(
        db, c.id, repair_type="knowledge_fix",
        proposed_changes=[{"domain": "visibility_grant", "action": "grant",
                           "target_kind": "fact", "target_id": str(fact.id),
                           "grantee_user_id": str(uuid.uuid4())}],
        reason="auto-grant after leak", fingerprint="sec-2", operation_id="op-sec-2",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-sec-2", commit=True)
    assert out["status"] == "failed"


# ── Idempotent retry ──────────────────────────────────────────────────────────

def test_duplicate_repair_execution_is_idempotent():
    _F, db, c = _setup()
    ent = _entity(db, c, summary="before")
    params = dict(repair_type="deterministic_derived",
                  proposed_changes=[{"domain": "entity", "target_id": str(ent.id),
                                     "patch": {"summary": "after"}}],
                  reason="idempotent", fingerprint="idem-1", operation_id="op-idem-1")
    first, created = create_repair(db, c.id, **params, commit=True)
    assert created
    second, created2 = create_repair(db, c.id, **params, commit=True)
    assert not created2 and second.id == first.id
    out1 = apply_repair(db, c.id, first.id, operation_id="op-idem-1", commit=True)
    assert out1["status"] == "applied"
    rev_after_first = db.get(type(ent), ent.id).revision
    out2 = apply_repair(db, c.id, first.id, operation_id="op-idem-1", commit=True)
    assert out2["duplicate"] is True
    assert db.get(type(ent), ent.id).revision == rev_after_first


def test_failed_repair_remains_retryable():
    _F, db, c = _setup()
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[{"domain": "nope", "target_id": "x"}],
        reason="bad domain", fingerprint="fail-1", operation_id="op-fail-1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-fail-1", commit=True)
    assert out["status"] == "failed"
    db.refresh(repair)
    assert repair.status == "failed" and repair.retry_count >= 1
    assert repair.error


# ── Multi-domain + observability ──────────────────────────────────────────────

def test_repair_touches_registered_domains_and_reports_stats():
    _F, db, c = _setup()
    ent = _entity(db, c, name="Clocktower")
    fact = _fact(db, c, content="The bell rings at dawn.", idempotency_key="bell-1")
    repair, _ = create_repair(
        db, c.id, repair_type="deterministic_derived",
        proposed_changes=[
            {"domain": "entity", "target_id": str(ent.id), "patch": {"summary": "tall tower"}},
            {"domain": "fact", "target_id": str(fact.id), "patch": {"content": "The bell rings at dusk."}},
            {"domain": "summary", "from_sequence": 1, "to_sequence": 5, "reason": "derived refresh"},
            {"domain": "projection", "reason": "projection refresh"},
        ],
        reason="multi-domain", fingerprint="multi-1", operation_id="op-multi-1",
        commit=True,
    )
    out = apply_repair(db, c.id, repair.id, operation_id="op-multi-1", commit=True)
    assert out["status"] == "applied"
    assert len(out["applied_changes"]) == 4
    stats = get_repair_stats(db, c.id)
    assert stats["repairs"] >= 1
    assert stats["by_type"]["deterministic_derived"] >= 1
    assert stats["affected_domains"]["entity"] >= 1
    assert stats["affected_domains"]["fact"] >= 1


def test_retcon_and_merge_outcomes_recorded_in_stats():
    _F, db, c = _setup()
    service = DecisionService(FakeDecisionAdapter(answers={REPAIR_QUESTION_ID: RETCON}))
    repair, _ = create_repair(
        db, c.id, repair_type="ambiguous", proposed_changes=[],
        evidence={"a": 1}, reason="x", fingerprint="st-1",
        operation_id="op-st-1", commit=True,
    )
    verdict = adjudicate_repair(
        db, repair, decision_service=service,
        candidates=[RepairCandidate(RETCON, "retcon"), RepairCandidate(MERGE, "merge"),
                    RepairCandidate(DEFER, "defer")],
        commit=True,
    )
    assert verdict.selected_id == RETCON
    stats = get_repair_stats(db, c.id)
    assert stats["dm_adjudicated"] >= 1
