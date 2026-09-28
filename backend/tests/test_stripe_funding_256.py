"""Issue #256 — Stripe add-funds, confirmation, idempotent re-credit/refund accounting."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.funding import CampaignFundingOperation, StripeWebhookEvent  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import AIRun, OperationTrace  # noqa: E402
from models.usage import CampaignUsageEntry  # noqa: E402

from app.billing import ledger  # noqa: E402
from app.billing import stripe_funding as funding  # noqa: E402
from app.billing.ledger import (  # noqa: E402
    LedgerConflictError,
    get_capacity_summary,
    record_ai_spend_for_run,
    record_entry,
)
from app.billing.resolution_guarantee import evaluate_new_work  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeStripeClient:
    """Scriptable Stripe stand-in: no network, no card data, ever."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.intents: dict[str, dict] = {}
        self.created = 0

    def create_checkout_session(self, *, amount_cents, currency, idempotency_key,
                                metadata, success_url, cancel_url):
        if idempotency_key in self.sessions:  # stable key → no duplicate charge
            return self.sessions[idempotency_key]
        self.created += 1
        sid = f"cs_test_{self.created}"
        pi = f"pi_test_{self.created}"
        session = {
            "id": sid, "object": "checkout.session",
            "url": f"https://checkout.stripe.test/pay/{sid}",
            "payment_status": "unpaid", "status": "open",
            "amount_total": amount_cents, "currency": currency,
            "payment_intent": pi, "metadata": dict(metadata),
        }
        self.sessions[idempotency_key] = session
        self.sessions[sid] = session
        self.intents[pi] = {
            "id": pi, "object": "payment_intent", "status": "requires_payment_method",
            "amount_received": 0, "currency": currency, "metadata": dict(metadata),
        }
        return session

    def mark_paid(self, session_id: str):
        session = self.sessions[session_id]
        session["payment_status"] = "paid"
        session["status"] = "complete"
        intent = self.intents[session["payment_intent"]]
        intent["status"] = "succeeded"
        intent["amount_received"] = session["amount_total"]

    def mark_failed(self, session_id: str):
        session = self.sessions[session_id]
        session["payment_status"] = "unpaid"
        session["status"] = "expired"

    def retrieve_session(self, session_id: str) -> dict:
        return self.sessions[session_id]

    def retrieve_payment_intent(self, payment_intent_id: str) -> dict:
        return self.intents[payment_intent_id]


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _setup(monkeypatch=None):
    eng = _engine()
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = fac()
    owner = uuid.uuid4()
    member = uuid.uuid4()
    db.add_all([Profile(id=owner, email="owner@example.com"),
                Profile(id=member, email="member@example.com")])
    camp = uuid.uuid4()
    db.add(Campaign(id=camp, owner_id=owner, name="Funding table"))
    db.add(CampaignMember(campaign_id=camp, user_id=owner, role="owner"))
    db.add(CampaignMember(campaign_id=camp, user_id=member, role="player"))
    db.commit()
    db.close()
    fake = FakeStripeClient()
    funding.configure_stripe_client(fake)
    if monkeypatch is not None:
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_123")
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    else:
        import os
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_123"
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_123"
    try:
        yield fac, camp, owner, member, fake
    finally:
        funding.configure_stripe_client(None)


@pytest.fixture
def ctx(monkeypatch):
    yield from _setup(monkeypatch)


def _db(fac):
    return fac()


def _checkout_urls(camp):
    base = f"https://app.test/campaigns/{camp}"
    return f"{base}?funding=success", f"{base}?funding=cancel"


def _start(fac, camp, owner, fake, amount=500, key="op-1"):
    db = _db(fac)
    success_url, cancel_url = _checkout_urls(camp)
    op, url = funding.create_funding_operation(
        db, campaign_id=camp, amount_cents=amount, contributor_user_id=owner,
        idempotency_key=key, success_url=success_url, cancel_url=cancel_url,
        stripe_client=fake,
    )
    db.commit()
    op_id = op.id
    db.close()
    assert url and url.startswith("https://checkout.stripe.test/")
    return op_id


def _run(db, camp, *, cost_usd=1.00, status="succeeded", classification="primary",
         billable=True, tag=None):
    tag = tag or uuid.uuid4().hex[:8]
    trace_id = f"trace-256-{tag}"
    db.add(OperationTrace(trace_id=trace_id, operation_id=f"op-{tag}",
                          campaign_id=camp, submitted_at=datetime.now(timezone.utc)))
    db.flush()
    run = AIRun(trace_id=trace_id, operation_id=f"op-{tag}", logical_operation="narrate",
                role="ai_dm", provider="test", model="m", attempt=1,
                classification=classification, billable=billable, status=status,
                started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
                cost_usd=cost_usd)
    db.add(run)
    db.flush()
    return run


def _webhook_event(event_id, event_type, obj):
    return {"id": event_id, "object": "event", "type": event_type,
            "data": {"object": obj}}


def _session_completed_obj(db, op_id, paid=True):
    db_op = db.get(CampaignFundingOperation, op_id)
    return {
        "id": db_op.stripe_checkout_session_id, "object": "checkout.session",
        "payment_status": "paid" if paid else "unpaid",
        "status": "complete" if paid else "open",
        "amount_total": db_op.amount_cents, "currency": "usd",
        "payment_intent": db_op.stripe_payment_intent_id,
        "metadata": {"campaign_id": str(db_op.campaign_id),
                     "funding_operation_id": str(db_op.id)},
    }


# ── 1. success: exactly one auditable credit ─────────────────────────────────


def test_confirmed_payment_creates_exactly_one_credit(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    outcome = funding.handle_stripe_event(
        db, _webhook_event("evt-1", "checkout.session.completed",
                           _session_completed_obj(db, op_id, paid=True)))
    db.commit()
    assert outcome["status"] == "confirmed"
    entries = db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").all()
    assert len(entries) == 1
    assert entries[0].amount_cents == 500
    assert entries[0].idempotency_key == f"stripe_funds:{op_id}"
    assert entries[0].entry_metadata["funding_operation_id"] == str(op_id)
    # No Stripe identifiers leak into the ledger (#253 privacy).
    assert "stripe" not in json.dumps(entries[0].entry_metadata).lower()
    assert "cs_test" not in str(entries[0].note) and "pi_test" not in str(entries[0].note)
    summary = get_capacity_summary(db, camp)
    assert summary["funded_cents"] == 500 and summary["remaining_cents"] == 500
    db.close()


# ── 2. cancel/failure leaves capacity + gameplay untouched ───────────────────


def test_payment_failure_leaves_capacity_and_gameplay_untouched(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    name_before = db.get(Campaign, camp).name
    outcome = funding.handle_stripe_event(
        db, _webhook_event("evt-fail-1", "payment_intent.payment_failed",
                           {"id": db.get(CampaignFundingOperation, op_id).stripe_payment_intent_id,
                            "object": "payment_intent",
                            "metadata": {"campaign_id": str(camp),
                                         "funding_operation_id": str(op_id)}}))
    db.commit()
    assert outcome["status"] == "failed"
    assert db.get(CampaignFundingOperation, op_id).status == "failed"
    assert db.query(CampaignUsageEntry).count() == 0
    assert get_capacity_summary(db, camp)["funded_cents"] == 0
    assert db.get(Campaign, camp).name == name_before
    db.close()


# ── 3. duplicate webhook cannot double-credit ────────────────────────────────


def test_duplicate_webhook_delivery_credits_once(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    event = _webhook_event("evt-dup-1", "checkout.session.completed",
                           _session_completed_obj(db, op_id, paid=True))
    first = funding.handle_stripe_event(db, event)
    db.commit()
    second = funding.handle_stripe_event(db, event)
    db.commit()
    assert first["status"] == "confirmed" and second["status"] == "duplicate"
    assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1
    assert get_capacity_summary(db, camp)["funded_cents"] == 500
    db.close()


# ── 4. reordered webhook delivery cannot double-credit ───────────────────────


def test_reordered_webhooks_credit_once(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    op = db.get(CampaignFundingOperation, op_id)
    pi_obj = {"id": op.stripe_payment_intent_id, "object": "payment_intent",
              "status": "succeeded", "amount_received": 500, "currency": "usd",
              "metadata": {"campaign_id": str(camp), "funding_operation_id": str(op_id)}}
    # payment_intent.succeeded arrives BEFORE checkout.session.completed.
    assert funding.handle_stripe_event(
        db, _webhook_event("evt-re-1", "payment_intent.succeeded", pi_obj))["status"] == "confirmed"
    db.commit()
    assert funding.handle_stripe_event(
        db, _webhook_event("evt-re-2", "checkout.session.completed",
                           _session_completed_obj(db, op_id, paid=True)))["status"] == "confirmed"
    db.commit()
    assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1
    assert get_capacity_summary(db, camp)["funded_cents"] == 500
    db.close()


# ── 5. retried funding-operation creation creates no duplicate charge ────────


def test_retry_same_funding_operation_creates_no_duplicate_charge(ctx):
    fac, camp, owner, _member, fake = ctx
    first_id = _start(fac, camp, owner, fake, amount=500, key="op-retry")
    assert fake.created == 1
    db = _db(fac)
    success_url, cancel_url = _checkout_urls(camp)
    again, url = funding.create_funding_operation(
        db, campaign_id=camp, amount_cents=500, contributor_user_id=owner,
        idempotency_key="op-retry", success_url=success_url, cancel_url=cancel_url,
        stripe_client=fake)
    db.commit()
    assert again.id == first_id
    assert fake.created == 1  # no second Stripe session
    assert url == again.checkout_url
    db.close()
    # Same key, conflicting payload → surfaced, never a second charge.
    db = _db(fac)
    with pytest.raises(funding.FundingValidationError):
        funding.create_funding_operation(
            db, campaign_id=camp, amount_cents=999, contributor_user_id=owner,
            idempotency_key="op-retry", success_url=success_url, cancel_url=cancel_url,
            stripe_client=fake)
    db.rollback()
    db.close()
    assert fake.created == 1


# ── 6. browser close before confirmation: reconcile recovers, redirect can't ─


def test_browser_close_recovers_via_reconciliation_not_redirect(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    op = db.get(CampaignFundingOperation, op_id)
    # Browser closed: no webhook yet. Stripe was actually paid.
    fake.mark_paid(op.stripe_checkout_session_id)
    # Redirect/success-page state alone (unpaid Stripe state) credits nothing.
    db2 = _db(fac)
    op2 = db2.get(CampaignFundingOperation, op_id)
    assert op2.status == "pending"
    assert db2.query(CampaignUsageEntry).count() == 0
    # Reconciliation against authoritative Stripe state confirms exactly once.
    funding.reconcile_funding_operation(db, op, stripe_client=fake)
    db.commit()
    assert op.status == "confirmed"
    assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1
    # A second reconcile (or late webhook) replays without double-credit.
    funding.reconcile_funding_operation(db, op, stripe_client=fake)
    db.commit()
    assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1
    db.close()
    db2.close()


def test_unpaid_reconcile_credits_nothing(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    op = db.get(CampaignFundingOperation, op_id)
    funding.reconcile_funding_operation(db, op, stripe_client=fake)
    db.commit()
    assert op.status == "pending"
    assert db.query(CampaignUsageEntry).count() == 0
    db.close()


# ── 7. funding resumes paused play without touching campaign state ───────────


def test_successful_funding_resumes_paused_play(ctx):
    fac, camp, owner, _member, fake = ctx
    db = _db(fac)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=100,
                 idempotency_key="alloc-1")
    run = _run(db, camp, cost_usd=1.00, tag="pause256")
    record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    assert evaluate_new_work(db, camp)["allowed"] is False
    revision_before = db.get(Campaign, camp).revision
    db.close()
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    funding.handle_stripe_event(
        db, _webhook_event("evt-resume-1", "checkout.session.completed",
                           _session_completed_obj(db, op_id, paid=True)))
    db.commit()
    decision = evaluate_new_work(db, camp)
    assert decision["allowed"] is True and decision["ai_paused"] is False
    assert db.get(Campaign, camp).revision == revision_before  # same state, no rebuild
    db.close()


# ── 8. one-time re-credit linked to the original run ─────────────────────────


def test_failed_counted_run_recredited_once_with_link(ctx):
    fac, camp, owner, _member, _fake = ctx
    db = _db(fac)
    record_entry(db, campaign_id=camp, entry_type="allocation", amount_cents=1000,
                 idempotency_key="alloc-1")
    run = _run(db, camp, cost_usd=4.00, tag="doom256")
    spend = record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    # Downstream the counted work failed — now eligible for one re-credit.
    run.status = "failed"
    db.commit()
    entry = funding.recredit_failed_run(db, campaign_id=camp, ai_run_id=run.id,
                                        actor_user_id=owner, failure_reason="failed")
    db.commit()
    assert entry.entry_type == "recredit" and entry.amount_cents == 400
    assert entry.entry_metadata["recredit_for_ai_run_id"] == str(run.id)
    assert entry.entry_metadata["recredit_for_entry_id"] == str(spend.id)
    assert get_capacity_summary(db, camp)["funded_cents"] == 1400
    # Second re-credit for the same run is rejected.
    with pytest.raises(LedgerConflictError):
        funding.recredit_failed_run(db, campaign_id=camp, ai_run_id=run.id,
                                    actor_user_id=owner, failure_reason="failed")
    db.rollback()
    assert db.query(CampaignUsageEntry).filter_by(entry_type="recredit").count() == 1
    db.close()


def test_recovery_run_needs_no_recredit(ctx):
    fac, camp, owner, _member, _fake = ctx
    db = _db(fac)
    recovery = _run(db, camp, cost_usd=5.00, status="succeeded",
                    classification="recovery", billable=False, tag="rec256")
    db.commit()
    with pytest.raises(funding.FundingValidationError):
        funding.recredit_failed_run(db, campaign_id=camp, ai_run_id=recovery.id,
                                    actor_user_id=owner, failure_reason="failed")
    db.rollback()
    db.close()


def test_succeeded_run_without_attestation_rejected(ctx):
    fac, camp, owner, _member, _fake = ctx
    db = _db(fac)
    run = _run(db, camp, cost_usd=1.00, tag="ok256")
    record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    with pytest.raises(funding.FundingValidationError):
        funding.recredit_failed_run(db, campaign_id=camp, ai_run_id=run.id,
                                    actor_user_id=owner)
    db.rollback()
    # Explicit owner attestation with audit trail is accepted exactly once.
    entry = funding.recredit_failed_run(db, campaign_id=camp, ai_run_id=run.id,
                                        actor_user_id=owner, failure_reason="abandoned",
                                        note="turn abandoned after charge")
    db.commit()
    assert entry.entry_metadata["failure_reason"] == "abandoned"
    db.close()


# ── 9. Stripe refund/reversal reconciled idempotently ────────────────────────


def test_stripe_refund_mirrored_idempotently(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    funding.handle_stripe_event(
        db, _webhook_event("evt-pay-9", "checkout.session.completed",
                           _session_completed_obj(db, op_id, paid=True)))
    db.commit()
    assert get_capacity_summary(db, camp)["funded_cents"] == 500
    op = db.get(CampaignFundingOperation, op_id)
    refund_obj = {"id": "re_test_1", "object": "refund", "amount": 500, "currency": "usd",
                  "payment_intent": op.stripe_payment_intent_id,
                  "metadata": {"campaign_id": str(camp), "funding_operation_id": str(op_id)}}
    first = funding.handle_stripe_event(db, _webhook_event("evt-re-9a", "refund.created", refund_obj))
    db.commit()
    assert first["status"] == "refunded"
    # Money returned → funded pool decreases via signed correction, history intact.
    assert get_capacity_summary(db, camp)["funded_cents"] == 0
    assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1
    # Redelivery replays without a second correction.
    dup = funding.handle_stripe_event(db, _webhook_event("evt-re-9a", "refund.created", refund_obj))
    db.commit()
    assert dup["status"] == "duplicate"
    assert get_capacity_summary(db, camp)["funded_cents"] == 0
    db.close()


def test_reordered_refund_before_confirm_resolves_once(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    op = db.get(CampaignFundingOperation, op_id)
    fake.mark_paid(op.stripe_checkout_session_id)
    refund_obj = {"id": "re_test_2", "object": "refund", "amount": 500, "currency": "usd",
                  "payment_intent": op.stripe_payment_intent_id,
                  "metadata": {"campaign_id": str(camp), "funding_operation_id": str(op_id)}}
    # Refund arrives before any confirm: reconcile confirms first, then mirrors.
    outcome = funding.handle_stripe_event(
        db, _webhook_event("evt-re-10", "refund.created", refund_obj), stripe_client=fake)
    db.commit()
    assert outcome["status"] == "refunded"
    assert db.get(CampaignFundingOperation, op_id).status == "confirmed"
    assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1
    assert get_capacity_summary(db, camp)["funded_cents"] == 0
    db.close()


def _refund_item(refund_id, amount_cents, status="succeeded"):
    item = {"id": refund_id, "object": "refund", "amount": amount_cents, "currency": "usd"}
    if status is not None:
        item["status"] = status
    return item


def _charge_refunded_obj(db, camp, op_id, refund_items):
    op = db.get(CampaignFundingOperation, op_id)
    return {
        "id": "ch_test_partial", "object": "charge",
        "amount": op.amount_cents, "currency": "usd",
        "payment_intent": op.stripe_payment_intent_id,
        "refunds": {"object": "list", "data": list(refund_items)},
        "metadata": {"campaign_id": str(camp), "funding_operation_id": str(op_id)},
    }


def _refund_mirror_count(db, camp):
    return db.query(CampaignUsageEntry).filter(
        CampaignUsageEntry.campaign_id == camp,
        CampaignUsageEntry.entry_type == "admin_adjustment",
        CampaignUsageEntry.amount_cents < 0,
    ).count()


def test_two_partial_refunds_mirror_each_idempotently(ctx):
    """Review regression (PR #451): charge.refunded re-emits the whole refund
    list, so every refund id must mirror — not just the first."""
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    funding.handle_stripe_event(
        db, _webhook_event("evt-pay-part", "checkout.session.completed",
                           _session_completed_obj(db, op_id, paid=True)))
    db.commit()
    assert get_capacity_summary(db, camp)["funded_cents"] == 500
    op = db.get(CampaignFundingOperation, op_id)

    def _per_refund_event(event_id, refund_id, amount_cents, status="succeeded"):
        return _webhook_event(
            event_id, "refund.created",
            {**_refund_item(refund_id, amount_cents, status),
             "payment_intent": op.stripe_payment_intent_id,
             "metadata": {"campaign_id": str(camp), "funding_operation_id": str(op_id)}})

    # 1. First partial refund mirrors on its own per-refund event.
    assert funding.handle_stripe_event(
        db, _per_refund_event("evt-part-a", "re_part_a", 200))["status"] == "refunded"
    db.commit()
    assert get_capacity_summary(db, camp)["funded_cents"] == 300

    # 2. charge.refunded re-emits the FULL list [A, B]: A replays, B mirrors.
    #    The old first-item-only code returned duplicate here and lost B.
    both = _charge_refunded_obj(db, camp, op_id,
                                [_refund_item("re_part_a", 200), _refund_item("re_part_b", 150)])
    assert funding.handle_stripe_event(
        db, _webhook_event("evt-part-charge", "charge.refunded", both))["status"] == "refunded"
    db.commit()
    assert get_capacity_summary(db, camp)["funded_cents"] == 150
    assert _refund_mirror_count(db, camp) == 2

    # 3. Same event id redelivered → duplicate, no movement.
    assert funding.handle_stripe_event(
        db, _webhook_event("evt-part-charge", "charge.refunded", both))["status"] == "duplicate"
    db.commit()
    assert get_capacity_summary(db, camp)["funded_cents"] == 150

    # 4. New event id, same charge payload (both ids already mirrored) → duplicate.
    assert funding.handle_stripe_event(
        db, _webhook_event("evt-part-charge-2", "charge.refunded", both))["status"] == "duplicate"
    db.commit()
    assert get_capacity_summary(db, camp)["funded_cents"] == 150

    # 5. Reordered per-refund event for B alone → duplicate replay.
    assert funding.handle_stripe_event(
        db, _per_refund_event("evt-part-b-late", "re_part_b", 150))["status"] == "duplicate"
    db.commit()
    assert get_capacity_summary(db, camp)["funded_cents"] == 150
    assert _refund_mirror_count(db, camp) == 2

    # 6. Cumulative cap: 350 already mirrored; a further 400 would exceed the
    #    500 funding → refused, capacity untouched.
    with pytest.raises(funding.FundingValidationError):
        funding.handle_stripe_event(db, _per_refund_event("evt-part-over", "re_part_c", 400))
    db.rollback()
    assert get_capacity_summary(db, camp)["funded_cents"] == 150
    assert _refund_mirror_count(db, camp) == 2

    # 7. Non-final (pending) refunds move no money: ignored until final.
    pending_only = _charge_refunded_obj(db, camp, op_id, [_refund_item("re_part_d", 50, "pending")])
    ignored = funding.handle_stripe_event(
        db, _webhook_event("evt-part-pending", "charge.refunded", pending_only))
    db.commit()
    assert ignored["status"] == "ignored"
    assert get_capacity_summary(db, camp)["funded_cents"] == 150
    # ... and its later succeeded delivery mirrors exactly once.
    assert funding.handle_stripe_event(
        db, _per_refund_event("evt-part-d-final", "re_part_d", 50))["status"] == "refunded"
    db.commit()
    assert get_capacity_summary(db, camp)["funded_cents"] == 100
    assert _refund_mirror_count(db, camp) == 3
    db.close()


# ── 10. webhook verification + transport behavior ────────────────────────────


def _sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    t = timestamp if timestamp is not None else int(time.time())
    digest = hmac.new(secret.encode(), f"{t}.".encode() + body,
                      hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


@pytest.fixture
def api(monkeypatch, ctx):
    from fastapi.testclient import TestClient

    from app.auth.service import TEST_USER_ID
    from database import get_db
    from main import app

    fac, _camp, _owner, _member, _fake = ctx
    with fac() as db:
        if db.get(Profile, TEST_USER_ID) is None:
            db.add(Profile(id=TEST_USER_ID, email="api@example.com"))
            db.commit()
        test_camp = Campaign(id=uuid.uuid4(), owner_id=TEST_USER_ID, name="API funding table")
        db.add(test_camp)
        db.add(CampaignMember(campaign_id=test_camp.id, user_id=TEST_USER_ID, role="owner"))
        db.commit()
        api_camp = test_camp.id

    def override_db():
        with fac() as db:
            yield db

    monkeypatch.setenv("NODE_ENV", "test")
    # The API tests use https://app.test return URLs: allowlist that origin
    # so the hostname check exercises the allowlist path (evil hosts stay 400).
    monkeypatch.setenv("FRONTEND_URL", "https://app.test")
    monkeypatch.setattr(
        "app.billing.router.resolve_profile",
        lambda request, db: db.get(Profile, TEST_USER_ID),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), fac, TEST_USER_ID, api_camp
    finally:
        app.dependency_overrides.clear()


def test_webhook_rejects_invalid_signature(api):
    client, fac, _user, camp_id = api
    body = json.dumps({"id": "evt-bad", "type": "checkout.session.completed",
                       "data": {"object": {}}}).encode()
    response = client.post("/api/billing/stripe/webhook", content=body,
                           headers={"Stripe-Signature": "t=123,v1=deadbeef"})
    assert response.status_code == 400
    with fac() as db:
        assert db.query(StripeWebhookEvent).count() == 0
        assert db.query(CampaignUsageEntry).count() == 0


def test_webhook_success_end_to_end(api):
    client, fac, _user, camp_id = api
    with fac() as db:
        op = CampaignFundingOperation(
            campaign_id=camp_id, amount_cents=700, currency="usd", status="pending",
            idempotency_key="api-op-1", stripe_checkout_session_id="cs_api_1",
            stripe_payment_intent_id="pi_api_1")
        db.add(op)
        db.commit()
        op_id = op.id
    obj = {"id": "cs_api_1", "object": "checkout.session", "payment_status": "paid",
           "status": "complete", "amount_total": 700, "currency": "usd",
           "payment_intent": "pi_api_1",
           "metadata": {"campaign_id": str(camp_id), "funding_operation_id": str(op_id)}}
    body = json.dumps({"id": "evt-api-1", "type": "checkout.session.completed",
                       "data": {"object": obj}}).encode()
    response = client.post("/api/billing/stripe/webhook", content=body,
                           headers={"Stripe-Signature": _sign(body, "whsec_test_123")})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    with fac() as db:
        assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1
        # Duplicate delivery over HTTP is a replay, not a credit.
        dup = client.post("/api/billing/stripe/webhook", content=body,
                          headers={"Stripe-Signature": _sign(body, "whsec_test_123")})
        assert dup.json()["status"] == "duplicate"
        assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1


def test_webhook_retry_after_transient_failure_is_safe(api, monkeypatch):
    client, fac, _user, camp_id = api
    with fac() as db:
        op = CampaignFundingOperation(
            campaign_id=camp_id, amount_cents=300, currency="usd", status="pending",
            idempotency_key="api-op-retry", stripe_checkout_session_id="cs_api_r1",
            stripe_payment_intent_id="pi_api_r1")
        db.add(op)
        db.commit()
        op_id = op.id
    obj = {"id": "cs_api_r1", "object": "checkout.session", "payment_status": "paid",
           "status": "complete", "amount_total": 300, "currency": "usd",
           "payment_intent": "pi_api_r1",
           "metadata": {"campaign_id": str(camp_id), "funding_operation_id": str(op_id)}}
    body = json.dumps({"id": "evt-api-retry", "type": "checkout.session.completed",
                       "data": {"object": obj}}).encode()
    headers = {"Stripe-Signature": _sign(body, "whsec_test_123")}
    real_confirm = funding.confirm_funding_operation
    calls = {"n": 0}

    def _flaky(db, operation, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db failure")
        return real_confirm(db, operation, **kwargs)

    monkeypatch.setattr(funding, "confirm_funding_operation", _flaky)
    failed = client.post("/api/billing/stripe/webhook", content=body, headers=headers)
    assert failed.status_code == 500
    with fac() as db:
        assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 0
        # Production rollback removes the dangling event row along with the
        # failed confirmation; this suite's sqlite setup auto-commits, so
        # simulate the rolled-back state explicitly before redelivery.
        db.query(StripeWebhookEvent).filter_by(stripe_event_id="evt-api-retry").delete()
        db.commit()
    retried = client.post("/api/billing/stripe/webhook", content=body, headers=headers)
    assert retried.status_code == 200
    assert retried.json()["status"] == "confirmed"
    with fac() as db:
        assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1
    # A further redelivery is a replay, not a second credit.
    replayed = client.post("/api/billing/stripe/webhook", content=body, headers=headers)
    assert replayed.json()["status"] == "duplicate"
    with fac() as db:
        assert db.query(CampaignUsageEntry).filter_by(entry_type="added_funds").count() == 1


def test_checkout_rejects_untrusted_client_values(api):
    client, fac, _user, camp_id = api
    base = {"success_url": f"https://app.test/campaigns/{camp_id}?funding=success",
            "cancel_url": f"https://app.test/campaigns/{camp_id}?funding=cancel"}
    # Negative / out-of-range amounts are refused server-side.
    for bad in (-100, 0, 1, 99999999, "many"):
        response = client.post(f"/api/campaigns/{camp_id}/funding/checkout",
                               json={**base, "amount_cents": bad},
                               headers={"Idempotency-Key": f"bad-{bad}"})
        assert response.status_code == 400, (bad, response.text)
    # Open-redirect return URLs are refused.
    evil = dict(base, amount_cents=500, success_url="https://evil.test/steal")
    response = client.post(f"/api/campaigns/{camp_id}/funding/checkout", json=evil,
                           headers={"Idempotency-Key": "evil-1"})
    assert response.status_code == 400
    # Another campaign's return path is refused.
    other = dict(base, amount_cents=500,
                 success_url=f"https://app.test/campaigns/{uuid.uuid4()}?funding=success")
    response = client.post(f"/api/campaigns/{camp_id}/funding/checkout", json=other,
                           headers={"Idempotency-Key": "evil-2"})
    assert response.status_code == 400
    with fac() as db:
        assert db.query(CampaignFundingOperation).filter_by(campaign_id=camp_id).count() == 0


def test_checkout_and_status_roundtrip(api):
    client, fac, _user, camp_id = api
    payload = {"amount_cents": 500,
               "success_url": f"https://app.test/campaigns/{camp_id}?funding=success",
               "cancel_url": f"https://app.test/campaigns/{camp_id}?funding=cancel"}
    first = client.post(f"/api/campaigns/{camp_id}/funding/checkout", json=payload,
                        headers={"Idempotency-Key": "round-1"})
    assert first.status_code == 200, first.text
    op_id = first.json()["funding_operation"]["id"]
    # The member-safe projection exposes no Stripe identifiers as fields and
    # no payment details or secrets (the one-time Stripe-hosted checkout URL
    # is the redirect target, not a stored credential).
    operation_blob = json.dumps(first.json()["funding_operation"]).lower()
    assert "stripe_checkout_session_id" not in operation_blob
    assert "stripe_payment_intent_id" not in operation_blob
    assert "contributor" not in operation_blob
    assert "secret" not in operation_blob and "sk_test" not in operation_blob
    assert set(first.json()["funding_operation"]) == {
        "id", "campaign_id", "amount_cents", "currency", "status",
        "checkout_url", "created_at", "confirmed_at",
    }
    # Retry returns the same operation (no duplicate Stripe session).
    again = client.post(f"/api/campaigns/{camp_id}/funding/checkout", json=payload,
                        headers={"Idempotency-Key": "round-1"})
    assert again.json()["funding_operation"]["id"] == op_id
    # Status while unpaid: pending, no credit (redirect state credits nothing).
    status = client.get(f"/api/campaigns/{camp_id}/funding/operations/{op_id}")
    assert status.status_code == 200, status.text
    assert status.json()["funding_operation"]["status"] == "pending"
    with fac() as db:
        assert db.query(CampaignUsageEntry).count() == 0


def test_recredit_endpoint_owner_only_and_idempotent(api):
    client, fac, _user, camp_id = api
    with fac() as db:
        run = _run(db, camp_id, cost_usd=2.00, tag="ep256")
        record_ai_spend_for_run(db, campaign_id=camp_id, ai_run=run)
        db.commit()
        run.status = "failed"
        db.commit()
        run_id = run.id
    payload = {"ai_run_id": str(run_id), "failure_reason": "failed"}
    first = client.post(f"/api/campaigns/{camp_id}/recredits", json=payload,
                        headers={"Idempotency-Key": "recredit-ep-1"})
    assert first.status_code == 200, first.text
    assert first.json()["entry"]["amount_cents"] == 200
    # Same key replays the same entry; conflicting reuse is rejected.
    replay = client.post(f"/api/campaigns/{camp_id}/recredits", json=payload,
                         headers={"Idempotency-Key": "recredit-ep-1"})
    assert replay.status_code == 200
    assert replay.json()["entry"]["id"] == first.json()["entry"]["id"]
    clash = client.post(f"/api/campaigns/{camp_id}/recredits",
                        json={"ai_run_id": str(uuid.uuid4()), "failure_reason": "failed"},
                        headers={"Idempotency-Key": "recredit-ep-1"})
    assert clash.status_code in (400, 409)
    # A second logical re-credit for the same run is rejected outright.
    duplicate = client.post(f"/api/campaigns/{camp_id}/recredits", json=payload,
                            headers={"Idempotency-Key": "recredit-ep-2"})
    assert duplicate.status_code == 409
    with fac() as db:
        assert db.query(CampaignUsageEntry).filter_by(entry_type="recredit").count() == 1


def test_non_member_cannot_start_checkout(api):
    client, fac, _user, _camp_id = api
    outsider = uuid.uuid4()
    with fac() as db:
        db.add(Profile(id=outsider, email="outsider@example.com"))
        db.add(Campaign(id=uuid.uuid4(), owner_id=outsider, name="Other"))
        db.commit()
        other = db.query(Campaign).filter_by(name="Other").one().id
    payload = {"amount_cents": 500,
               "success_url": f"https://app.test/campaigns/{other}?funding=success",
               "cancel_url": f"https://app.test/campaigns/{other}?funding=cancel"}
    response = client.post(f"/api/campaigns/{other}/funding/checkout", json=payload,
                           headers={"Idempotency-Key": "outsider-1"})
    assert response.status_code == 403


# ── 11. funding never selects model quality ──────────────────────────────────


def test_funding_has_no_model_quality_surface():
    import pathlib
    import re
    backend = pathlib.Path(__file__).resolve().parents[1]
    service_src = (backend / "app" / "billing" / "stripe_funding.py").read_text()
    # No model-selection parameters or AI/narrative/rules levers: funding
    # changes capacity only. (Isolation prose may name the boundary; the
    # enforcement is the import allowlist below.)
    for banned in ("temperature", "input_tokens", "output_tokens", "model_name",
                   "model_id", "loot", "dice_roll", "advantage_state"):
        assert banned not in service_src, f"funding service must not contain {banned!r}"
    allowed_prefixes = ("app.billing", "app.observability", "models.",
                        "database", "sqlalchemy", "httpx")
    stdlib = ("hashlib", "hmac", "logging", "os", "time", "uuid",
              "datetime", "typing", "__future__")
    for line in service_src.splitlines():
        match = re.match(r"\s*(?:from|import)\s+([a-zA-Z0-9_.]+)", line)
        if not match:
            continue
        module = match.group(1)
        assert module.split(".")[0] in {p.split(".")[0] for p in allowed_prefixes} | set(stdlib) \
            or module.startswith(allowed_prefixes), \
            f"funding service must not import {module!r}"
    model_src = (backend / "models" / "funding.py").read_text()
    for banned in ("temperature", "loot", "dice", "model_name"):
        assert banned not in model_src, f"funding models must not contain {banned!r}"


def test_ledger_stays_append_only():
    assert not hasattr(ledger, "update_entry")
    assert not hasattr(ledger, "delete_entry")
    assert not hasattr(funding, "credit_from_redirect")
    assert not hasattr(funding, "credit_from_client")


# ── 12. signature edge cases ─────────────────────────────────────────────────


def test_webhook_signature_edge_cases():
    body = b'{"id":"evt-x"}'
    secret = "whsec_edge"
    header = _sign(body, secret)
    funding.verify_webhook_signature(body, header, secret)  # valid passes
    with pytest.raises(funding.WebhookVerificationError):
        funding.verify_webhook_signature(body, None, secret)
    with pytest.raises(funding.WebhookVerificationError):
        funding.verify_webhook_signature(b'{"id":"evt-tampered"}', header, secret)
    with pytest.raises(funding.WebhookVerificationError):
        funding.verify_webhook_signature(body, _sign(body, "whsec_other"), secret)
    stale = _sign(body, secret, timestamp=int(time.time()) - 3600)
    with pytest.raises(funding.WebhookVerificationError):
        funding.verify_webhook_signature(body, stale, secret)


def test_amount_mismatch_never_credits(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    tampered = _session_completed_obj(db, op_id, paid=True)
    tampered["amount_total"] = 5  # attacker/shifted amount: must not credit
    with pytest.raises(funding.FundingValidationError):
        funding.handle_stripe_event(db, _webhook_event("evt-tamper", "checkout.session.completed",
                                                       tampered))
    db.rollback()
    assert db.query(CampaignUsageEntry).count() == 0
    assert db.get(CampaignFundingOperation, op_id).status == "pending"
    db.close()


# ── 13. review hardening regressions ─────────────────────────────────────────


def test_reconcile_amount_mismatch_stays_pending(ctx):
    """Reconcile must apply the same amount/currency gate as the webhook."""
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    op = db.get(CampaignFundingOperation, op_id)
    session = fake.sessions[funding.stripe_idempotency_key(op.id)]
    session["payment_status"] = "paid"
    session["status"] = "complete"
    session["amount_total"] = 5  # shifted Stripe-side amount
    with pytest.raises(funding.FundingValidationError):
        funding.reconcile_funding_operation(db, op, stripe_client=fake)
    db.rollback()
    assert db.get(CampaignFundingOperation, op_id).status == "pending"
    assert db.query(CampaignUsageEntry).count() == 0
    db.close()


def test_webhook_campaign_mismatch_never_credits(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    tampered = _session_completed_obj(db, op_id, paid=True)
    tampered["metadata"] = {"campaign_id": str(uuid.uuid4()),
                            "funding_operation_id": str(op_id)}
    with pytest.raises(funding.FundingValidationError):
        funding.handle_stripe_event(db, _webhook_event("evt-camp-x", "checkout.session.completed",
                                                       tampered))
    db.rollback()
    assert db.query(CampaignUsageEntry).count() == 0
    assert db.get(CampaignFundingOperation, op_id).status == "pending"
    db.close()


def test_strict_amount_comparison_rejects_non_integer(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    op = db.get(CampaignFundingOperation, op_id)
    assert funding._check_amount_matches(op, {"amount_total": 500, "currency": "usd"}) is True
    assert funding._check_amount_matches(op, {"amount_total": 500.0, "currency": "usd"}) is False
    assert funding._check_amount_matches(op, {"amount_total": "500", "currency": "usd"}) is False
    assert funding._check_amount_matches(op, {"amount_total": 5, "currency": "usd"}) is False
    assert funding._check_amount_matches(op, {"amount_total": 500, "currency": "eur"}) is False
    db.close()


def test_concurrent_creation_replays_winner(ctx, monkeypatch):
    """Simulated concurrent insert collision returns the winner, no 2nd charge."""
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500, key="op-race")
    assert fake.created == 1
    real_get = funding._get_by_idempotency
    calls = {"n": 0}

    def _flaky(db, campaign_id, key):
        # First call (pre-insert check) misses the concurrently committed
        # winner; the second call (conflict replay) finds it.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_get(db, campaign_id, key)

    monkeypatch.setattr(funding, "_get_by_idempotency", _flaky)
    db = _db(fac)
    success_url, cancel_url = _checkout_urls(camp)
    op, url = funding.create_funding_operation(
        db, campaign_id=camp, amount_cents=500, contributor_user_id=owner,
        idempotency_key="op-race", success_url=success_url, cancel_url=cancel_url,
        stripe_client=fake)
    db.rollback()
    assert op.id == op_id
    assert fake.created == 1  # no second Stripe session
    db.close()


def test_refund_currency_mismatch_rejected(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    funding.handle_stripe_event(
        db, _webhook_event("evt-pay-fx", "checkout.session.completed",
                           _session_completed_obj(db, op_id, paid=True)))
    db.commit()
    op = db.get(CampaignFundingOperation, op_id)
    refund_obj = {"id": "re_fx_1", "object": "refund", "amount": 500, "currency": "eur",
                  "payment_intent": op.stripe_payment_intent_id,
                  "metadata": {"campaign_id": str(camp), "funding_operation_id": str(op_id)}}
    with pytest.raises(funding.FundingValidationError):
        funding.handle_stripe_event(db, _webhook_event("evt-fx-1", "refund.created", refund_obj))
    db.rollback()
    assert get_capacity_summary(db, camp)["funded_cents"] == 500
    db.close()


def test_refund_exceeding_funding_rejected(ctx):
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    funding.handle_stripe_event(
        db, _webhook_event("evt-pay-big", "checkout.session.completed",
                           _session_completed_obj(db, op_id, paid=True)))
    db.commit()
    op = db.get(CampaignFundingOperation, op_id)
    refund_obj = {"id": "re_big_1", "object": "refund", "amount": 600, "currency": "usd",
                  "payment_intent": op.stripe_payment_intent_id,
                  "metadata": {"campaign_id": str(camp), "funding_operation_id": str(op_id)}}
    with pytest.raises(funding.FundingValidationError):
        funding.handle_stripe_event(db, _webhook_event("evt-big-1", "refund.created", refund_obj))
    db.rollback()
    assert get_capacity_summary(db, camp)["funded_cents"] == 500
    db.close()


def test_refund_for_failed_operation_ignored(ctx):
    """A refund with no confirmed credit behind it posts no correction."""
    fac, camp, owner, _member, fake = ctx
    op_id = _start(fac, camp, owner, fake, amount=500)
    db = _db(fac)
    op = db.get(CampaignFundingOperation, op_id)
    funding.mark_funding_terminal(db, op, status="failed",
                                 stripe_event_id="evt-fail-x", reason="payment_failed")
    db.commit()
    refund_obj = {"id": "re_nocredit_1", "object": "refund", "amount": 500, "currency": "usd",
                  "payment_intent": op.stripe_payment_intent_id,
                  "metadata": {"campaign_id": str(camp), "funding_operation_id": str(op_id)}}
    outcome = funding.handle_stripe_event(
        db, _webhook_event("evt-nocredit-1", "refund.created", refund_obj), stripe_client=fake)
    db.commit()
    assert outcome["status"] == "ignored"
    assert db.query(CampaignUsageEntry).count() == 0
    assert get_capacity_summary(db, camp)["funded_cents"] == 0
    db.close()


def test_return_url_rejects_evil_host_with_valid_path(api):
    client, fac, _user, camp_id = api
    payload = {"amount_cents": 500,
               "success_url": f"https://evil.test/campaigns/{camp_id}?funding=success",
               "cancel_url": f"https://app.test/campaigns/{camp_id}?funding=cancel"}
    response = client.post(f"/api/campaigns/{camp_id}/funding/checkout", json=payload,
                           headers={"Idempotency-Key": "evil-host-1"})
    assert response.status_code == 400
    with fac() as db:
        assert db.query(CampaignFundingOperation).filter_by(campaign_id=camp_id).count() == 0
