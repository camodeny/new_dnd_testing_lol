"""Issue #219 — rebuildable campaign summaries with bounded claim verification."""
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
from app.post_turn.service import get_checkpoint, run_post_turn_range  # noqa: E402
from app.world.knowledge import (  # noqa: E402
    create_fact_inline,
    list_facts,
    supersede_fact_inline,
)
from app.world.summaries import (  # noqa: E402
    DEFER,
    SUMMARY_QUESTION_ID,
    SUPPORTED,
    UNSUPPORTED,
    SummaryDraft,
    SummaryError,
    consolidate_summary_for_range,
    get_summary_stats,
    get_valid_summaries_for_context,
    mark_summaries_stale,
    rebuild_stale_summaries,
    split_claims,
)
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import CampaignSummary  # noqa: E402


@pytest.fixture(autouse=True)
def _no_auto_trigger(monkeypatch):
    monkeypatch.setenv("POST_TURN_AUTO_TRIGGER", "0")


class _BoomAdapter(FakeDecisionAdapter):
    def execute(self, request, *, model, timeout):
        raise RuntimeError("verifier down")


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
    c = Campaign(owner_id=owner, name="summaries-219")
    db.add(c)
    db.flush()
    db.commit()
    db.refresh(c)
    return F, db, c


def _rev(db, c):
    return int(db.get(Campaign, c.id).revision or 0)


def _commit(db, c, payload=None, etype="game.play", visibility="public", op=None):
    rev = _rev(db, c)
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type=etype, payload=payload or {"n": rev + 1},
        visibility=visibility, operation_id=op or f"op-{rev + 1}-{uuid.uuid4().hex[:6]}",
    )
    db.refresh(event)
    return event


def _scripted(answer):
    return DecisionService(FakeDecisionAdapter(answers={SUMMARY_QUESTION_ID: answer}))


def _good_provider(evidence, *, from_sequence, to_sequence, visibility, attempt, feedback):
    sentences = " ".join(
        f"Sequence {item['sequence']} records {item['event_type']}" for item in evidence
        if item.get("kind", "domain_event") == "domain_event"
    )
    return SummaryDraft(
        prose=f"Campaign events {from_sequence}-{to_sequence}: {sentences}",
        provider="test", model="test-model-v1", latency_ms=3.0,
    )


def _row(db, c, lo, hi):
    return db.execute(select(CampaignSummary).where(
        CampaignSummary.campaign_id == c.id,
        CampaignSummary.from_sequence == lo, CampaignSummary.to_sequence == hi,
    )).scalars().first()


# ── Normal summary ───────────────────────────────────────────────────────────

def test_normal_summary_is_current_with_supported_claims():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1, "note": "party enters the crypt"})
    e2 = _commit(db, c, payload={"n": 2, "note": "lantern lit"})
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e2.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert out["status"] == "current"
    assert out["claim_count"] >= 1
    assert out["distribution"][SUPPORTED] == out["claim_count"]
    row = _row(db, c, e1.sequence, e2.sequence)
    assert row.status == "current"
    assert row.version == 1
    assert row.source_revision == e2.sequence
    assert row.generation_model == "test-model-v1"
    assert row.generation_provider == "test"
    assert row.verification_policy["decision_class"] == "campaign_summary_verify"
    assert all(cl["verdict"] == SUPPORTED for cl in row.claims)
    assert all(cl["source_from"] == e1.sequence and cl["source_to"] == e2.sequence
               for cl in row.claims)


def test_every_summary_identifies_source_range_revision():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    row = _row(db, c, e1.sequence, e1.sequence)
    d = row.to_dict()
    assert (d["from_sequence"], d["to_sequence"]) == (e1.sequence, e1.sequence)
    assert d["source_revision"] == e1.sequence
    assert d["source_hash"]
    assert d["is_derived"] is True and d["authority"] == "derived_summary"


def test_generation_and_verification_roles_are_separately_traceable():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    stats = get_summary_stats(db, c.id)
    assert stats["generation_role"] == "summary_generation"
    assert stats["verification_role"] == "summary_verification"
    assert stats["verification_decision_class"] == "campaign_summary_verify"
    assert stats["latest_generation_model"] == "test-model-v1"
    assert stats["latest_generation_latency_ms"] == 3
    assert stats["latest_source_span"] == [e1.sequence, e1.sequence]


# ── Hidden source / visibility ───────────────────────────────────────────────

def test_hidden_source_blocks_party_visible_summary():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1, "note": "party feasts"}, visibility="public")
    e2 = _commit(db, c, payload={"n": 2, "note": "secret betrayal"}, visibility="dm_only")

    def leaky(evidence, **kw):
        return SummaryDraft(prose="The party feasts. The secret betrayal is revealed.")

    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e2.sequence,
        visibility="campaign", provider=leaky,
        decision_service=_scripted(SUPPORTED),
    )
    assert out["status"] == "failed"
    assert out["reason"] == "deterministic_rejection"
    assert any("visibility_widening" in f for f in out["failures"])
    row = _row(db, c, e1.sequence, e2.sequence)
    assert row.status == "failed"
    assert row.deterministic_failures >= 1


def test_deterministic_rejection_wins_despite_positive_semantic_score():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1}, visibility="public")
    e2 = _commit(db, c, payload={"n": 2}, visibility="dm_only")
    adapter = FakeDecisionAdapter(answers={SUMMARY_QUESTION_ID: SUPPORTED})
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e2.sequence,
        visibility="campaign", provider=_good_provider,
        decision_service=DecisionService(adapter),
    )
    assert out["status"] == "failed"
    assert out["reason"] == "deterministic_rejection"
    # The judge is never even consulted on a deterministically rejected draft.
    assert adapter.calls == []


def test_dm_internal_summary_may_compress_hidden_sources():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1}, visibility="public")
    e2 = _commit(db, c, payload={"n": 2}, visibility="dm_only")
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e2.sequence,
        visibility="dm_only", provider=_good_provider,
        decision_service=_scripted(SUPPORTED),
    )
    assert out["status"] == "current"
    lane = get_valid_summaries_for_context(db, c.id, dm_internal=True)
    assert len(lane) == 1 and lane[0]["visibility"] == "dm_only"
    # Player-facing callers never see the restricted summary.
    assert get_valid_summaries_for_context(db, c.id, dm_internal=False) == []


# ── Unsupported / uncertain claims ───────────────────────────────────────────

def test_unsupported_claims_rejected_and_regenerated_not_canonized():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})

    def invented(evidence, **kw):
        return SummaryDraft(
            prose="The party finds a dragon hoard of ten thousand gold.")

    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=invented, decision_service=_scripted(UNSUPPORTED),
        max_regenerations=1,
    )
    assert out["status"] == "failed"
    assert out["reason"] == "unsupported_claims_rejected"
    row = _row(db, c, e1.sequence, e1.sequence)
    assert row.status == "failed"
    assert row.unsupported_count >= 1
    # Nothing became quasi-canon: no current summary exists for the range.
    assert get_valid_summaries_for_context(db, c.id, dm_internal=True) == []


def test_uncertain_claim_defers_instead_of_supported():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(DEFER),
    )
    assert out["status"] == "deferred"
    row = _row(db, c, e1.sequence, e1.sequence)
    assert row.status == "deferred"
    assert row.uncertain_count >= 1
    assert get_valid_summaries_for_context(db, c.id, dm_internal=True) == []


def test_verifier_failure_defers_and_is_retryable():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider,
        decision_service=DecisionService(_BoomAdapter(answers={})),
    )
    assert out["status"] == "deferred"
    row = _row(db, c, e1.sequence, e1.sequence)
    assert row.status == "deferred"
    # Retry with a healthy verifier converges to current.
    out2 = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert out2["status"] == "current"


def test_generation_failure_is_retryable_and_never_touches_sources():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    fact, _ = create_fact_inline(
        db, db.get(Campaign, c.id), content="The crypt door is sealed.",
        visibility="campaign", operation_id="seed-fact",
        idempotency_key="seed-fact-1", source_event_id=e1.id,
    )
    db.commit()
    before = sorted(f.content for f in list_facts(db, c.id))

    def boom(evidence, **kw):
        raise RuntimeError("generator down")

    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=boom, decision_service=_scripted(SUPPORTED),
    )
    assert out["status"] == "failed"
    assert out["reason"] == "generation_failed"
    # Authoritative sources are untouched.
    assert sorted(f.content for f in list_facts(db, c.id)) == before
    assert db.get(CampaignDomainEvent, e1.id) is not None
    # Retry with a working generator succeeds.
    out2 = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert out2["status"] == "current"


# ── Convergence ──────────────────────────────────────────────────────────────

def test_resummarization_converges_rather_than_drifts():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    e2 = _commit(db, c, payload={"n": 2})
    camp = db.get(Campaign, c.id)
    first = consolidate_summary_for_range(
        db, camp, e1.sequence, e2.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert first["status"] == "current"
    before = _row(db, c, e1.sequence, e2.sequence)
    version, rebuilds, prose = before.version, before.rebuild_count, before.prose
    second = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e2.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert second["status"] == "current" and second.get("converged") is True
    after = _row(db, c, e1.sequence, e2.sequence)
    assert after.version == version
    assert after.prose == prose
    assert after.rebuild_count == rebuilds + 1


# ── Repair invalidation + rebuild ────────────────────────────────────────────

def test_repair_invalidates_and_rebuilds_affected_summaries():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    e2 = _commit(db, c, payload={"n": 2})
    fact, _ = create_fact_inline(
        db, db.get(Campaign, c.id), content="The vault is empty.",
        epistemic_state="claimed", visibility="campaign",
        operation_id="seed-fact", idempotency_key="repair-fact-1",
        source_event_id=e1.id,
    )
    db.commit()
    consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e2.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    row = _row(db, c, e1.sequence, e2.sequence)
    assert row.status == "current"
    stale_before = row.stale_count

    # Repair (supersede) the source fact: the affected summary must go stale.
    supersede_fact_inline(
        db, db.get(Campaign, c.id), fact.id,
        content="The vault is full.", epistemic_state="confirmed",
        operation_id="repair-1", idempotency_key="repair-fact-2",
    )
    db.commit()
    db.refresh(row)
    assert row.status == "stale"
    assert row.stale_count == stale_before + 1

    # Rebuild revalidates and converges (same prose inputs → same version).
    outcomes = rebuild_stale_summaries(
        db, c.id, provider=_good_provider,
        decision_service=_scripted(SUPPORTED),
    )
    assert len(outcomes) == 1 and outcomes[0]["status"] == "current"
    db.refresh(row)
    assert row.status == "current"
    assert row.version == 1
    assert row.rebuild_count >= 1


def test_mark_summaries_stale_without_bounds_invalidates_all():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert mark_summaries_stale(db, c.id, reason="test-repair") == 1
    assert _row(db, c, e1.sequence, e1.sequence).status == "stale"
    # Non-overlapping bounds leave the row alone.
    consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert mark_summaries_stale(
        db, c.id, from_sequence=99, to_sequence=100, reason="far-away") == 0
    assert _row(db, c, e1.sequence, e1.sequence).status == "current"


# ── Embedding refresh ────────────────────────────────────────────────────────

def test_embedding_refresh_triggered_from_source_version_changes(monkeypatch):
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    fact, _ = create_fact_inline(
        db, db.get(Campaign, c.id), content="The bridge collapsed.",
        visibility="campaign", operation_id="seed-fact",
        idempotency_key="embed-fact-1", source_event_id=e1.id,
    )
    db.commit()
    seen: list[tuple[str, str]] = []

    import app.world.semantic as semantic

    orig_write = semantic.note_authoritative_write
    orig_index = semantic.request_semantic_index

    def spy_write(db_arg, campaign_id, entries, **kw):
        seen.extend(entries)
        return orig_write(db_arg, campaign_id, entries, **kw)

    def spy_index(db_arg, campaign_id, source_type, source_id, **kw):
        seen.append((source_type, str(source_id)))
        return orig_index(db_arg, campaign_id, source_type, source_id, **kw)

    monkeypatch.setattr(semantic, "note_authoritative_write", spy_write)
    monkeypatch.setattr(semantic, "request_semantic_index", spy_index)
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert out["status"] == "current"
    fact_hits = [sid for kind, sid in seen if kind == "world_fact"]
    assert any(str(sid) == str(fact.id) for sid in fact_hits)
    assert ("domain_event", str(e1.id)) in seen
    assert out["embedding_refresh"]["facts"] == 1
    assert out["embedding_refresh"]["domain_events"] == 1


# ── Context lane + post-turn hook ────────────────────────────────────────────

def test_forward_dm_distinguishes_summary_from_direct_sources():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    lane = get_valid_summaries_for_context(db, c.id, dm_internal=True)
    assert len(lane) == 1
    item = lane[0]
    assert item["authority"] == "derived_summary"
    assert item["lane"] == "summary"
    assert item["is_derived"] is True
    assert set(item["outranked_by"]) >= {
        "domain_event", "world_fact", "world_relation", "world_entity"}
    assert item["source_range"] == [e1.sequence, e1.sequence]


def test_post_turn_run_verifies_running_summary_to_current():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    e2 = _commit(db, c, payload={"n": 2})
    out = run_post_turn_range(
        db, c.id, e1.sequence, e2.sequence,
        clock_decision_service=_scripted(SUPPORTED),
    )
    assert out["duplicate"] is False
    # The worker verifies (runtime default or injected service): the running
    # summary is context-eligible, not stuck pending forever.
    row = _row(db, c, 1, e2.sequence)
    assert row is not None and row.status == "current"
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == e2.sequence


def test_post_turn_run_down_verifier_defers_without_threatening_state(monkeypatch):
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    e2 = _commit(db, c, payload={"n": 2})
    # The worker defaults to the runtime decision service when none is
    # injected; a down verifier fails closed to deferred (retryable), never
    # silently canon and never threatening the checkpoint.
    import app.decisions

    monkeypatch.setattr(
        app.decisions, "DecisionService",
        lambda *a, **k: DecisionService(_BoomAdapter(answers={})),
    )
    out = run_post_turn_range(db, c.id, e1.sequence, e2.sequence)
    assert out["duplicate"] is False
    row = _row(db, c, 1, e2.sequence)
    assert row is not None and row.status == "deferred"
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == e2.sequence


def test_explicit_none_service_stays_pending_and_retryable():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=None,
    )
    assert out["status"] == "pending"
    assert out["reason"] == "awaiting_verification"
    # A later pass with a verifier converges to current on the same row.
    out2 = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    assert out2["status"] == "current"
    assert _row(db, c, e1.sequence, e1.sequence).status == "current"


def test_summary_failure_never_invalidates_committed_gameplay():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1}, visibility="public")
    e2 = _commit(db, c, payload={"n": 2}, visibility="dm_only")
    out = run_post_turn_range(db, c.id, e1.sequence, e2.sequence)
    # Materialization + clocks (authoritative) still succeed; only the
    # derived running summary records its deterministic rejection.
    assert out["duplicate"] is False
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == e2.sequence


# ── Caller errors + claim splitting ──────────────────────────────────────────

def test_empty_or_gappy_range_raises_caller_error():
    _F, db, c = _setup()
    with pytest.raises(SummaryError):
        consolidate_summary_for_range(
            db, db.get(Campaign, c.id), 1, 1, provider=_good_provider,
            decision_service=_scripted(SUPPORTED),
        )
    with pytest.raises(SummaryError):
        consolidate_summary_for_range(
            db, db.get(Campaign, c.id), 1, 1, visibility="nope",
            provider=_good_provider, decision_service=_scripted(SUPPORTED),
        )


def test_split_claims_is_deterministic():
    prose = "The party enters the crypt. The lantern is lit! Is anyone there? Yes."
    first, second = split_claims(prose), split_claims(prose)
    assert first == second and len(first) == 4
    assert split_claims("") == []


# ── Verification coverage (no silent omission) ───────────────────────────────

def test_over_limit_draft_rejected_never_becomes_current():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})

    def wordy(evidence, **kw):
        return SummaryDraft(
            prose=" ".join(f"Event detail number {i} holds." for i in range(60)))

    adapter = FakeDecisionAdapter(answers={SUMMARY_QUESTION_ID: SUPPORTED})
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=wordy, decision_service=DecisionService(adapter),
    )
    assert out["status"] == "failed"
    assert out["reason"] == "deterministic_rejection"
    assert any("too_many_claims" in f for f in out["failures"])
    # The over-budget draft never became quasi-canon: the judge was never
    # even consulted, and no current summary exists for the range.
    assert adapter.calls == []
    assert _row(db, c, e1.sequence, e1.sequence).status == "failed"
    assert get_valid_summaries_for_context(db, c.id, dm_internal=True) == []


def test_long_sentence_fully_covered_by_verification():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    long_sentence = " ".join(f"word{i}" for i in range(240)) + "."
    assert len(long_sentence) > 1000

    def lengthy(evidence, **kw):
        return SummaryDraft(prose=f"Sequence {e1.sequence} records play. {long_sentence}")

    adapter = FakeDecisionAdapter(answers={SUMMARY_QUESTION_ID: SUPPORTED})
    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=lengthy, decision_service=DecisionService(adapter),
    )
    assert out["status"] == "current"
    # Every span was individually judged: one decision call per chunk, each
    # within the span bound — nothing past character 500 escaped review.
    row = _row(db, c, e1.sequence, e1.sequence)
    assert len(adapter.calls) == len(row.claims) >= 3
    assert all(len(cl["text"]) <= 502 for cl in row.claims)


class _MarkerAdapter(FakeDecisionAdapter):
    """Supports every claim except ones carrying the planted marker."""

    def execute(self, request, *, model, timeout):
        data = super().execute(request, model=model, timeout=timeout)
        claim_text = (request.state.get("claim") or {}).get("text", "")
        data["answers"][SUMMARY_QUESTION_ID] = (
            UNSUPPORTED if "UNVERIFIABLE-MARKER-XYZ" in claim_text else SUPPORTED
        )
        return data


def test_unsupported_tail_past_span_bound_rejects_draft():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    filler = " ".join(f"word{i}" for i in range(120))
    # The marker sits well past character 500 of its sentence: under the old
    # truncating splitter it would have escaped verification entirely.
    tail = f"{filler} UNVERIFIABLE-MARKER-XYZ " + " ".join(
        f"tail{i}" for i in range(60)) + "."

    def sneaky(evidence, **kw):
        return SummaryDraft(
            prose=f"Sequence {e1.sequence} records play. {tail}")

    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=sneaky,
        decision_service=DecisionService(_MarkerAdapter(answers={})),
        max_regenerations=0,
    )
    assert out["status"] == "failed"
    assert out["reason"] == "unsupported_claims_rejected"
    assert get_valid_summaries_for_context(db, c.id, dm_internal=True) == []


# ── Repair changes verifier evidence ─────────────────────────────────────────

class _EvidenceAwareAdapter(FakeDecisionAdapter):
    """Supports a claim only when live evidence excerpts state it.

    A claim restated by a retracted record is contradicted even when a
    stale event payload still echoes it — retractions are explicit
    non-support, so such claims are UNSUPPORTED.
    """

    def execute(self, request, *, model, timeout):
        data = super().execute(request, model=model, timeout=timeout)
        claim = ((request.state.get("claim") or {}).get("text") or "").strip().lower()
        live_text, dead_text = [], []
        for item in request.state.get("evidence") or []:
            text = str(
                item.get("payload_excerpt") or item.get("content_excerpt") or "")
            (dead_text if item.get("status") == "retracted" else live_text).append(text)
        live, dead = " ".join(live_text).lower(), " ".join(dead_text).lower()
        variants = {claim, claim.rstrip(".!?")} - {""}
        supported = any(v in live for v in variants)
        retracted_hit = any(v in dead for v in variants)
        data["answers"][SUMMARY_QUESTION_ID] = (
            SUPPORTED if (supported and not retracted_hit) else UNSUPPORTED
        )
        return data


def test_repair_to_contradictory_fact_cannot_restore_old_claim():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    fact, _ = create_fact_inline(
        db, db.get(Campaign, c.id), content="The vault is empty.",
        visibility="campaign", operation_id="vault-1",
        idempotency_key="vault-fact-1", source_event_id=e1.id,
    )
    db.commit()

    def vault_empty(evidence, **kw):
        return SummaryDraft(prose="The vault is empty.")

    def aware_service():
        return DecisionService(_EvidenceAwareAdapter(answers={}))

    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        visibility="campaign", provider=vault_empty,
        decision_service=aware_service(),
    )
    assert out["status"] == "current"
    row = _row(db, c, e1.sequence, e1.sequence)
    hash_before = row.source_hash

    # Repair the source fact to contradictory content: the summary goes stale.
    supersede_fact_inline(
        db, db.get(Campaign, c.id), fact.id,
        content="The vault is full.", operation_id="vault-2",
        idempotency_key="vault-fact-2",
    )
    db.commit()
    db.refresh(row)
    assert row.status == "stale"

    # Rebuild re-verifies against the REPAIRED record versions, not the
    # pre-repair event evidence alone: the old claim cannot come back.
    rebuild_adapter = _EvidenceAwareAdapter(answers={})
    outcomes = rebuild_stale_summaries(
        db, c.id, provider=vault_empty,
        decision_service=DecisionService(rebuild_adapter))
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "failed"
    assert outcomes[0]["reason"] == "unsupported_claims_rejected"
    # The judge actually saw the repaired fact version in its evidence.
    seen = [
        item for call in rebuild_adapter.calls
        for item in call["state"].get("evidence") or []
    ]
    assert any(
        item.get("kind") == "world_fact"
        and "full" in str(item.get("content_excerpt") or "")
        for item in seen
    )
    db.refresh(row)
    assert row.status == "failed"
    assert row.source_hash != hash_before
    assert get_valid_summaries_for_context(db, c.id, dm_internal=True) == []


def test_retracted_fact_cannot_restore_old_claim():
    _F, db, c = _setup()
    # The event payload itself still asserts the old content: without the
    # retraction marker in verifier evidence, the old claim would verify.
    e1 = _commit(db, c, payload={"n": 1, "note": "the vault is empty"})
    fact, _ = create_fact_inline(
        db, db.get(Campaign, c.id), content="The vault is empty.",
        visibility="campaign", operation_id="vault-1",
        idempotency_key="retract-fact-1", source_event_id=e1.id,
    )
    db.commit()

    def vault_empty(evidence, **kw):
        return SummaryDraft(prose="The vault is empty.")

    def aware_service():
        return DecisionService(_EvidenceAwareAdapter(answers={}))

    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        visibility="campaign", provider=vault_empty,
        decision_service=aware_service(),
    )
    assert out["status"] == "current"
    row = _row(db, c, e1.sequence, e1.sequence)
    hash_before = row.source_hash

    # Terminal retraction: no active version remains, but the retracted row
    # must still reach the verifier as a retraction marker.
    supersede_fact_inline(
        db, db.get(Campaign, c.id), fact.id,
        new_status="retracted", operation_id="vault-2",
        idempotency_key="retract-fact-2",
    )
    db.commit()
    db.refresh(row)
    assert row.status == "stale"

    rebuild_adapter = _EvidenceAwareAdapter(answers={})
    outcomes = rebuild_stale_summaries(
        db, c.id, provider=vault_empty,
        decision_service=DecisionService(rebuild_adapter))
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "failed"
    assert outcomes[0]["reason"] == "unsupported_claims_rejected"
    seen = [
        item for call in rebuild_adapter.calls
        for item in call["state"].get("evidence") or []
    ]
    assert any(
        item.get("kind") == "world_fact"
        and item.get("status") == "retracted"
        for item in seen
    )
    db.refresh(row)
    assert row.status == "failed"
    assert row.source_hash != hash_before
    assert get_valid_summaries_for_context(db, c.id, dm_internal=True) == []


def test_rebuild_generates_corrected_claim_from_repaired_fact():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    fact, _ = create_fact_inline(
        db, db.get(Campaign, c.id), content="The vault is empty.",
        visibility="campaign", operation_id="vault-1",
        idempotency_key="regen-fact-1", source_event_id=e1.id,
    )
    db.commit()

    def record_reader(evidence, **kw):
        vault = [
            str(item.get("content_excerpt") or "")
            for item in evidence
            if item.get("kind") in ("world_fact", "world_relation")
            and item.get("status") == "active"
            and "vault" in str(item.get("content_excerpt") or "").lower()
        ]
        assert vault, "generator must see the repaired record"
        return SummaryDraft(prose=vault[0])

    def aware_service():
        return DecisionService(_EvidenceAwareAdapter(answers={}))

    out = consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        visibility="campaign", provider=record_reader,
        decision_service=aware_service(),
    )
    assert out["status"] == "current"
    row = _row(db, c, e1.sequence, e1.sequence)
    assert "empty" in (row.prose or "").lower()

    supersede_fact_inline(
        db, db.get(Campaign, c.id), fact.id,
        content="The vault is full.", operation_id="vault-2",
        idempotency_key="regen-fact-2",
    )
    db.commit()
    db.refresh(row)
    assert row.status == "stale"

    # The rebuild regenerates from the repaired record — corrected prose
    # verifies to current instead of merely failing the old claim.
    outcomes = rebuild_stale_summaries(
        db, c.id, provider=record_reader, decision_service=aware_service())
    assert len(outcomes) == 1 and outcomes[0]["status"] == "current"
    db.refresh(row)
    assert row.status == "current"
    assert "full" in (row.prose or "").lower()
    assert "empty" not in (row.prose or "").lower()
    lane = get_valid_summaries_for_context(db, c.id, dm_internal=True)
    assert len(lane) == 1 and "full" in (lane[0]["prose"] or "").lower()

# ── Player-facing authorization ──────────────────────────────────────────────

def test_player_facing_context_requires_campaign_membership():
    _F, db, c = _setup()
    e1 = _commit(db, c, payload={"n": 1})
    consolidate_summary_for_range(
        db, db.get(Campaign, c.id), e1.sequence, e1.sequence,
        provider=_good_provider, decision_service=_scripted(SUPPORTED),
    )
    member = uuid.uuid4()
    outsider = uuid.uuid4()
    db.add(Profile(id=member, email="member@x.com"))
    db.add(Profile(id=outsider, email="outsider@x.com"))
    db.add(CampaignMember(campaign_id=c.id, user_id=member, role="player"))
    db.commit()

    owner = db.get(Campaign, c.id).owner_id
    assert len(get_valid_summaries_for_context(
        db, c.id, viewer_user_id=owner, dm_internal=False)) == 1
    assert len(get_valid_summaries_for_context(
        db, c.id, viewer_user_id=member, dm_internal=False)) == 1
    # No viewer, garbage viewer, and non-member viewers get nothing — a
    # known campaign ID alone must not disclose summaries.
    assert get_valid_summaries_for_context(db, c.id, dm_internal=False) == []
    assert get_valid_summaries_for_context(
        db, c.id, viewer_user_id="not-a-uuid", dm_internal=False) == []
    assert get_valid_summaries_for_context(
        db, c.id, viewer_user_id=outsider, dm_internal=False) == []
    # DM-internal authority lane is unaffected by viewer gating.
    assert len(get_valid_summaries_for_context(db, c.id, dm_internal=True)) == 1
