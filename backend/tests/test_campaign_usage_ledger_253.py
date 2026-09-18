"""Issue #253 — campaign monetary capacity ledger + shared contributions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import AIRun, OperationTrace  # noqa: E402
from models.usage import CampaignUsageEntry  # noqa: E402

from app.billing import ledger  # noqa: E402
from app.billing.ledger import (  # noqa: E402
    AccountingError,
    AmbiguousCostError,
    LedgerConflictError,
    NonBillableRunError,
    get_capacity_summary,
    public_capacity,
    reconcile,
    record_ai_spend_for_run,
    record_entry,
)


def _factory():
    engine = create_engine("sqlite://", poolclass=None)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(db, *, members: int = 1):
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@example.com"))
    users = [owner]
    for i in range(members - 1):
        u = uuid.uuid4()
        db.add(Profile(id=u, email=f"p{i}@example.com"))
        users.append(u)
    camp = uuid.uuid4()
    db.add(Campaign(id=camp, owner_id=owner, name="Ledger table"))
    db.commit()
    return camp, users


def _run(db, *, classification="primary", billable=True, cost_usd=1.25,
         status="succeeded", campaign_id=None, trace_id=None):
    trace_id = trace_id or f"trace-{uuid.uuid4().hex[:12]}"
    if campaign_id is not None:
        db.add(OperationTrace(trace_id=trace_id, operation_id=f"op-{trace_id}",
                              campaign_id=campaign_id, submitted_at=datetime.now(timezone.utc)))
        db.flush()
    run = AIRun(trace_id=trace_id, operation_id=f"op-{trace_id}", logical_operation="narrate",
                role="ai_dm", provider="test", model="m", attempt=1,
                classification=classification, billable=billable, status=status,
                started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
                cost_usd=cost_usd)
    db.add(run)
    db.flush()
    return run


# ── 1. single-player allocation + spend ───────────────────────────────────

def test_single_player_allocation_and_spend():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=500,
                 idempotency_key="alloc-1")
    db.commit()
    run = _run(db, cost_usd=1.25, campaign_id=camp)
    record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    summary = get_capacity_summary(db, camp)
    assert summary["funded_cents"] == 500
    assert summary["consumed_cents"] == 125  # actual run cost, not token counts
    assert summary["remaining_cents"] == 375
    assert summary["percent_used"] == 25.0
    assert reconcile(db, camp) == []
    db.close()


# ── 2. multiplayer shared pool ────────────────────────────────────────────

def test_multiplayer_contributions_share_one_pool():
    factory = _factory()
    db = factory()
    camp, users = _seed(db, members=3)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=200,
                 idempotency_key="alloc-1", contributor_user_id=users[0])
    record_entry(db, campaign_id=camp, entry_type="contribution", amount_cents=300,
                 idempotency_key="contrib-2", contributor_user_id=users[1])
    record_entry(db, campaign_id=camp, entry_type="contribution", amount_cents=500,
                 idempotency_key="contrib-3", contributor_user_id=users[2])
    db.commit()
    run = _run(db, cost_usd=2.00, campaign_id=camp)
    record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    summary = get_capacity_summary(db, camp)
    assert summary["funded_cents"] == 1000
    assert summary["consumed_cents"] == 200
    assert summary["remaining_cents"] == 800
    assert summary["percent_used"] == 20.0
    assert len(summary["contributors"]) == 3
    # Gameplay authority untouched: owner still owns the campaign.
    assert str(db.get(Campaign, camp).owner_id) == str(users[0])
    db.close()


# ── 3. recovery / non-billable exclusion ──────────────────────────────────

def test_recovery_runs_cannot_reduce_capacity():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=1000,
                 idempotency_key="alloc-1")
    db.commit()
    recovery = _run(db, classification="recovery", billable=False, cost_usd=5.00,
                    campaign_id=camp)
    db.commit()  # durable before the excluded charge attempt
    with pytest.raises(NonBillableRunError):
        record_ai_spend_for_run(db, campaign_id=camp, ai_run=recovery)
    db.rollback()
    sneaky = _run(db, classification="primary", billable=False, cost_usd=5.00)
    db.commit()
    with pytest.raises(NonBillableRunError):
        record_ai_spend_for_run(db, campaign_id=camp, ai_run=sneaky)
    db.rollback()
    summary = get_capacity_summary(db, camp)
    assert summary["consumed_cents"] == 0
    assert summary["remaining_cents"] == 1000
    # Recovery cost tracked separately, still free.
    assert summary["recovery_cost_usd"] == pytest.approx(5.00)
    assert db.query(CampaignUsageEntry).count() == 1
    db.close()


# ── 4. duplicate accounting / idempotent reprocessing ─────────────────────

def test_reprocessing_same_run_never_double_charges():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=1000,
                 idempotency_key="alloc-1")
    db.commit()
    run = _run(db, cost_usd=1.00, campaign_id=camp)
    first = record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    # Reprocess with default key and with explicit same key: same row back.
    again = record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    explicit = record_ai_spend_for_run(db, campaign_id=camp, ai_run=run,
                                       idempotency_key=f"ai_spend:{run.id}")
    db.commit()
    assert again.id == first.id and explicit.id == first.id
    assert get_capacity_summary(db, camp)["consumed_cents"] == 100
    assert db.query(CampaignUsageEntry).filter_by(entry_type="ai_spend").count() == 1
    # Same idempotency key, same payload → same row (failed-write retry path).
    same = record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=500,
                        idempotency_key="alloc-retry")
    db.commit()
    same_again = record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=500,
                              idempotency_key="alloc-retry")
    assert same_again.id == same.id
    # Same key, conflicting payload → surfaced, not double-posted.
    with pytest.raises(LedgerConflictError):
        record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=999,
                     idempotency_key="alloc-retry")
    db.rollback()
    db.close()


# ── 5. re-credit / refund via compensating entries ─────────────────────────

def test_recredit_and_refund_are_compensating_entries():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=1000,
                 idempotency_key="alloc-1")
    run = _run(db, cost_usd=4.00, campaign_id=camp)
    record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    before = db.query(CampaignUsageEntry).count()
    record_entry(db, campaign_id=camp, entry_type="refund", amount_cents=400,
                 idempotency_key="refund-1", note="double-charge goodwill")
    record_entry(db, campaign_id=camp, entry_type="recredit", amount_cents=100,
                 idempotency_key="recredit-1")
    db.commit()
    # History grew; nothing was edited or deleted.
    assert db.query(CampaignUsageEntry).count() == before + 2
    summary = get_capacity_summary(db, camp)
    assert summary["funded_cents"] == 1500
    assert summary["consumed_cents"] == 400
    assert summary["remaining_cents"] == 1100
    db.close()


# ── 6. admin adjustment ───────────────────────────────────────────────────

def test_admin_adjustment_signed_correction():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=1000,
                 idempotency_key="alloc-1")
    db.commit()
    record_entry(db, campaign_id=camp, entry_type="admin_adjustment", amount_cents=-150,
                 idempotency_key="adj-1", note="over-grant correction")
    record_entry(db, campaign_id=camp, entry_type="admin_adjustment", amount_cents=50,
                 idempotency_key="adj-2", note="goodwill")
    db.commit()
    summary = get_capacity_summary(db, camp)
    assert summary["funded_cents"] == 900
    assert summary["remaining_cents"] == 900
    with pytest.raises(AccountingError):
        record_entry(db, campaign_id=camp, entry_type="admin_adjustment", amount_cents=0,
                     idempotency_key="adj-zero")
    db.rollback()
    db.close()


# ── 7. percentage calc: grace, added funds, BYOK marker ────────────────────

def test_percentage_from_ledger_with_grace_and_byok():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=400,
                 idempotency_key="alloc-1")
    record_entry(db, campaign_id=camp, entry_type="added_funds", amount_cents=400,
                 idempotency_key="funds-1")
    record_entry(db, campaign_id=camp, entry_type="grace", amount_cents=200,
                 idempotency_key="grace-1")
    record_entry(db, campaign_id=camp, entry_type="byok_marker", amount_cents=0,
                 idempotency_key="byok-1", note="user key run, non-platform")
    db.commit()
    run = _run(db, cost_usd=2.50, campaign_id=camp)
    record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    summary = get_capacity_summary(db, camp)
    assert summary["funded_cents"] == 1000
    assert summary["consumed_cents"] == 250
    assert summary["byok_run_markers"] == 1  # marker has no capacity effect
    assert summary["percent_used"] == 25.0
    # Empty ledger edge: nothing funded, nothing spent → 0%.
    camp2, _ = _seed(db)
    assert get_capacity_summary(db, camp2)["percent_used"] == 0.0
    # Spent with no funding → surfaces as 100%, never div-by-zero.
    run2 = _run(db, cost_usd=1.00, campaign_id=camp2)
    record_ai_spend_for_run(db, campaign_id=camp2, ai_run=run2)
    assert get_capacity_summary(db, camp2)["percent_used"] == 100.0
    # Public projection is aggregates only.
    public = public_capacity(db, camp)
    assert set(public) == {"campaign_id", "funded_cents", "consumed_cents",
                           "remaining_cents", "percent_used", "contributor_count"}
    db.close()


# ── 8. failure semantics: ambiguity surfaced, gameplay untouched ──────────

def test_ambiguous_cost_surfaced_and_gameplay_untouched():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=1000,
                 idempotency_key="alloc-1")
    db.commit()
    name_before = db.get(Campaign, camp).name
    bad = _run(db, cost_usd=None, campaign_id=camp)  # succeeded primary, no cost
    db.commit()  # durable before the excluded charge attempt
    with pytest.raises(AmbiguousCostError):
        record_ai_spend_for_run(db, campaign_id=camp, ai_run=bad)
    db.rollback()
    # Accounting failure wrote nothing and rewrote no gameplay.
    assert db.query(CampaignUsageEntry).filter_by(entry_type="ai_spend").count() == 0
    assert db.get(Campaign, camp).name == name_before
    # Failed write is recoverable: once cost is known, the same run charges once.
    bad.cost_usd = 0.75
    db.commit()
    entry = record_ai_spend_for_run(db, campaign_id=camp, ai_run=bad)
    db.commit()
    assert entry.amount_cents == -75
    assert record_ai_spend_for_run(db, campaign_id=camp, ai_run=bad).id == entry.id
    # Reconcile flags the earlier gap style: uncharged billable run.
    run2 = _run(db, cost_usd=1.00, campaign_id=camp)
    db.commit()
    errors = reconcile(db, camp)
    assert any(str(run2.id) in e for e in errors)
    db.close()


# ── 9. billing isolation from DM/rules inputs ─────────────────────────────

def test_billing_state_not_in_dm_or_rules_inputs():
    import pathlib
    backend = pathlib.Path(__file__).resolve().parents[1]
    context_src = (backend / "app" / "dm" / "context.py").read_text()
    assert "billing" not in context_src
    assert "usage_entries" not in context_src and "campaign_usage" not in context_src
    from app.dm.context import LaneName
    lane_values = {lane.value for lane in LaneName}
    assert not (lane_values & {"billing", "usage", "capacity", "entitlement", "pricing"})
    # Amount validation is deterministic code authority: bad signs rejected.
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    with pytest.raises(AccountingError):
        record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=-5,
                     idempotency_key="bad-sign")
    with pytest.raises(AccountingError):
        record_entry(db, campaign_id=camp, entry_type="byok_marker", amount_cents=10,
                     idempotency_key="bad-byok")
    db.rollback()
    db.close()


def test_ledger_is_append_only_surface():
    assert not hasattr(ledger, "update_entry")
    assert not hasattr(ledger, "delete_entry")
    assert not hasattr(ledger, "adjust_entry_in_place")


# ── 10. spend write boundary: no raw ai_spend, trace-attributed campaign ────

def test_raw_ai_spend_rejected_and_cross_campaign_charge_refused():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    other, _ = _seed(db)
    # Raw ai_spend insertion bypasses run validation → rejected outright.
    run = _run(db, cost_usd=1.00, campaign_id=camp)
    db.commit()  # durable run + trace before the refused charge attempts
    with pytest.raises(AccountingError):
        record_entry(db, campaign_id=camp, entry_type="ai_spend", amount_cents=-100,
                     idempotency_key="raw-spend", ai_run_id=run.id)
    db.rollback()
    # A run cannot be charged to a different campaign than its trace.
    with pytest.raises(LedgerConflictError):
        record_ai_spend_for_run(db, campaign_id=other, ai_run=run)
    db.rollback()
    # A run with no operation trace at all is equally unchargeable.
    orphan = _run(db, cost_usd=1.00)
    with pytest.raises(LedgerConflictError):
        record_ai_spend_for_run(db, campaign_id=camp, ai_run=orphan)
    db.rollback()
    assert db.query(CampaignUsageEntry).filter_by(entry_type="ai_spend").count() == 0
    # The correctly attributed charge still works.
    entry = record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    assert entry.amount_cents == -100
    db.close()


# ── 11. uniqueness-conflict recovery keeps the session usable ───────────────

def test_uniqueness_conflict_returns_matching_winner_without_poisoning_session():
    from app.billing.ledger import _insert_entry
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    first = record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=500,
                         idempotency_key="race-1")
    db.commit()
    # Bypass the precheck to simulate a lost update race: the insert hits the
    # unique constraint, the savepoint rolls back, and the matching winner is
    # returned — the session must stay usable afterwards.
    winner = _insert_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=500,
                           idempotency_key="race-1")
    assert winner.id == first.id
    assert db.query(CampaignUsageEntry).filter_by(idempotency_key="race-1").count() == 1
    # Session usable: a fresh key still writes.
    second = record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=100,
                          idempotency_key="race-2")
    assert second.id != first.id
    # Same key, conflicting payload → surfaced, never silent replay.
    with pytest.raises(LedgerConflictError):
        _insert_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=999,
                      idempotency_key="race-1")
    db.rollback()
    db.close()


# ── 12. contributor + zero-cost idempotency strictness ──────────────────────

def test_contributor_mismatch_and_zero_cost_collision_are_conflicts():
    factory = _factory()
    db = factory()
    camp, users = _seed(db, members=2)
    record_entry(db, campaign_id=camp, entry_type="contribution", amount_cents=300,
                 idempotency_key="contrib-1", contributor_user_id=users[0])
    db.commit()
    # Same key, same amount, different contributor → conflict, not replay.
    with pytest.raises(LedgerConflictError):
        record_entry(db, campaign_id=camp, entry_type="contribution", amount_cents=300,
                     idempotency_key="contrib-1", contributor_user_id=users[1])
    db.rollback()
    # Zero-cost spend colliding with an existing non-spend key → conflict.
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=1000,
                 idempotency_key="ai_spend:zero-run")
    db.commit()
    free = _run(db, cost_usd=0.0, campaign_id=camp)
    db.commit()  # durable run + trace before the refused charge attempt
    with pytest.raises(LedgerConflictError):
        record_ai_spend_for_run(db, campaign_id=camp, ai_run=free,
                                idempotency_key="ai_spend:zero-run")
    db.rollback()
    # Genuine zero-cost marker under its own key still records exactly once.
    marker = record_ai_spend_for_run(db, campaign_id=camp, ai_run=free)
    assert marker.amount_cents == 0
    assert record_ai_spend_for_run(db, campaign_id=camp, ai_run=free).id == marker.id
    db.close()


# ── 13. production wiring: finish_ai_run charges exactly once ───────────────

def test_finish_ai_run_charges_primary_run_exactly_once():
    from app.observability.service import finish_ai_run, start_ai_run
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    db.close()
    tid = f"trace-{uuid.uuid4().hex[:12]}"
    run = start_ai_run(factory, logical_operation="forward_dm_adjudicate", role="ai_dm",
                       provider="test", model="m", classification="primary", billable=True,
                       trace_id=tid)
    finish_ai_run(factory, run.id, status="succeeded", cost_usd=1.25, campaign_id=camp)
    db = factory()
    entries = db.query(CampaignUsageEntry).filter_by(entry_type="ai_spend").all()
    assert len(entries) == 1
    assert entries[0].amount_cents == -125
    assert str(entries[0].ai_run_id) == str(run.id)
    assert reconcile(db, camp) == []
    db.close()
    # Re-finalization (retry) replays idempotently: still exactly one entry.
    finish_ai_run(factory, run.id, status="succeeded", cost_usd=1.25, campaign_id=camp)
    db = factory()
    assert db.query(CampaignUsageEntry).filter_by(entry_type="ai_spend").count() == 1
    assert reconcile(db, camp) == []
    db.close()
    # Recovery runs finalize observability with no spend entry.
    recovery = start_ai_run(factory, logical_operation="forward_dm_adjudicate", role="ai_dm",
                            provider="test", model="m", classification="recovery", billable=False,
                            trace_id=f"trace-{uuid.uuid4().hex[:12]}")
    finish_ai_run(factory, recovery.id, status="succeeded", cost_usd=0.50, campaign_id=camp)
    db = factory()
    assert db.query(CampaignUsageEntry).filter_by(entry_type="ai_spend").count() == 1
    db.close()


# ── 14. reconcile: None cost is always ambiguous ────────────────────────────

def test_reconcile_flags_none_cost_spend_even_at_zero_amount():
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    run = _run(db, cost_usd=None, campaign_id=camp)  # succeeded primary, no cost
    db.add(CampaignUsageEntry(campaign_id=camp, entry_type="ai_spend", amount_cents=0,
                             ai_run_id=run.id, idempotency_key=f"ai_spend:{run.id}"))
    db.commit()
    errors = reconcile(db, camp)
    assert any("ambiguous" in e for e in errors)
    db.close()


# ── 15. canonical pricing: usage + env config → USD ─────────────────────────

def test_cost_usd_for_uses_configured_pricing(monkeypatch):
    from app.billing import config
    monkeypatch.setattr(config, "DEFAULT_INPUT_PER_MTOK_USD", 2.0)
    monkeypatch.setattr(config, "DEFAULT_OUTPUT_PER_MTOK_USD", 8.0)
    assert config.cost_usd_for("p", "m", {"prompt_tokens": 1_000_000,
                                         "completion_tokens": 500_000}) == 6.0
    assert config.tokens_from_usage({"prompt_tokens": 10, "completion_tokens": 5}) == (10, 5)
    # Unknown usage or unpriced model → None (ambiguous, never zero-guessed).
    assert config.cost_usd_for("p", "m", {}) is None
    assert config.cost_usd_for("p", "m", None) is None
    monkeypatch.setattr(config, "DEFAULT_INPUT_PER_MTOK_USD", None)
    assert config.cost_usd_for("p", "m", {"prompt_tokens": 100}) is None


# ── 16. production narration: streamed usage is charged exactly once ────────

def test_primary_narration_stream_charges_exactly_once(monkeypatch):
    import app.providers as providers_pkg
    from app.billing import config as billing_config
    from app.dm.adjudication import build_provider_narrator
    from app.dm.narration import NarratorRequest
    from app.providers import policy as role_policy
    from app.providers import registry as reg
    from app.providers.contracts import NormalizedStreamEvent

    monkeypatch.setattr(billing_config, "DEFAULT_INPUT_PER_MTOK_USD", 2.0)
    monkeypatch.setattr(billing_config, "DEFAULT_OUTPUT_PER_MTOK_USD", 8.0)
    factory = _factory()
    db = factory()
    camp, _ = _seed(db)
    db.commit()

    class _FakeAdapter:
        name = "p1"

    def _fake_stream(adapter, request):
        yield NormalizedStreamEvent(kind="token", text="hello ")
        yield NormalizedStreamEvent(kind="token", text="world")
        yield NormalizedStreamEvent(kind="done", usage={"prompt_tokens": 1_000_000,
                                                       "completion_tokens": 500_000})

    monkeypatch.setattr(providers_pkg, "stream_chat", _fake_stream)
    monkeypatch.setattr(role_policy, "execution_path", lambda role: [("p1", "m")])
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    monkeypatch.setattr(reg.provider_registry, "get", lambda name: _FakeAdapter())
    monkeypatch.setattr("app.providers.areas.resolve_area",
                        lambda area: (_FakeAdapter(), "m", "p1"))

    narrate = build_provider_narrator(db=db, campaign_id=camp)
    assert "".join(narrate(NarratorRequest(prompt="p", projection={}))) == "hello world"
    entries = db.query(CampaignUsageEntry).filter_by(entry_type="ai_spend").all()
    assert len(entries) == 1
    # Actual streamed cost: 2.0 * 1M input + 8.0 * 0.5M output = $6.00.
    assert entries[0].amount_cents == -600
    assert reconcile(db, camp) == []

    # Recovery narration finalizes observability with no spend entry.
    narrate_retry = build_provider_narrator(db=db, campaign_id=camp, is_retry=True)
    assert "".join(narrate_retry(NarratorRequest(prompt="p", projection={}))) == "hello world"
    assert db.query(CampaignUsageEntry).filter_by(entry_type="ai_spend").count() == 1
    db.close()
