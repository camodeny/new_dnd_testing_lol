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
    run2 = _run(db, cost_usd=1.00)
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
