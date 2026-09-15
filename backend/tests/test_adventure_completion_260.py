"""Issue #260 — DM-declared adventure completion without ending the campaign."""

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
from app.adventures.service import (  # noqa: E402
    ADVENTURE_OUTCOMES,
    AdventureAlreadyActiveError,
    AdventureAlreadyCompletedError,
    AdventureNotFoundError,
    complete_adventure,
    get_current_adventure,
    handle_adventure_closing,
    list_adventures,
    start_adventure,
)
from models.campaigns import Adventure, Campaign, CampaignDomainEvent  # noqa: E402
from models.profiles import Profile  # noqa: E402


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def setup():
    factory = _factory()
    with factory() as db:
        owner = uuid.uuid4()
        db.add(Profile(id=owner, email="owner@example.com"))
        camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Long campaign", status="active", revision=0)
        db.add(camp)
        db.commit()
        db.refresh(camp)
        yield factory, camp.id, owner


def _complete(factory, camp_id, outcome, op, **kw):
    with factory() as db:
        camp = db.get(Campaign, camp_id)
        return complete_adventure(
            db, camp_id, outcome=outcome, reason=f"{outcome} reason",
            operation_id=op, expected_revision=int(camp.revision), **kw,
        )


@pytest.mark.parametrize("outcome", sorted(ADVENTURE_OUTCOMES))
def test_all_outcomes_complete_without_ending_campaign(setup, outcome):
    factory, camp_id, _owner = setup
    with factory() as db:
        adv = start_adventure(db, camp_id, f"The {outcome} arc")
        assert adv.status == "active"
    adv, event = _complete(factory, camp_id, outcome, f"op-{outcome}")
    assert adv.status == "completed"
    assert adv.outcome == outcome
    assert adv.completed_at is not None
    assert event.event_type == "adventure.completed"
    assert event.payload["outcome"] == outcome
    assert event.payload["campaign_status"] == "active"
    with factory() as db:
        camp = db.get(Campaign, camp_id)
        assert camp.status == "active"
        assert get_current_adventure(db, camp_id) is None


def test_tpk_and_villain_victory_are_legitimate_completions(setup):
    factory, camp_id, _owner = setup
    for outcome, op in (("tpk", "op-tpk"), ("villain_victory", "op-vv")):
        with factory() as db:
            start_adventure(db, camp_id, f"Arc {op}")
        adv, _event = _complete(factory, camp_id, outcome, op)
        assert adv.status == "completed" and adv.outcome == outcome
        with factory() as db:
            assert db.get(Campaign, camp_id).status == "active"


def test_completion_provenance_and_closing_trigger(setup):
    factory, camp_id, _owner = setup
    turn_id = uuid.uuid4()
    with factory() as db:
        start_adventure(db, camp_id, "Provenance arc")
    with factory() as db:
        camp = db.get(Campaign, camp_id)
        adv, event = complete_adventure(
            db, camp_id, outcome="victory", reason="Dragon slain",
            public_summary="The town is saved.",
            source_turn_id=turn_id, operation_id="op-prov",
            expected_revision=int(camp.revision),
        )
        db.commit()
        adv_id, event_id = adv.id, event.id
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.source_turn_id == turn_id
        assert adv.source_event_id == event_id
        assert adv.public_summary == "The town is saved."
        event = db.get(CampaignDomainEvent, event_id)
        assert event.provenance["source_turn_id"] == str(turn_id)
        assert event.provenance["declared_by"] == "dm"
        from models.reliability import Outbox

        rows = db.execute(
            select(Outbox).where(Outbox.event_type == "adventure.closing")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].payload["adventure_id"] == str(adv_id)


def test_duplicate_completion_is_idempotent(setup):
    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Retry arc")
    adv1, event1 = _complete(factory, camp_id, "retreat", "op-dup")
    with factory() as db:
        camp = db.get(Campaign, camp_id)
        adv2, event2 = complete_adventure(
            db, camp_id, outcome="victory", reason="changed mind",
            operation_id="op-dup", expected_revision=int(camp.revision),
        )
        db.commit()
    assert str(adv2.id) == str(adv1.id)
    assert adv2.outcome == "retreat"  # original outcome preserved
    assert str(event2.id) == str(event1.id)
    with factory() as db:
        rows = db.execute(
            select(Adventure).where(Adventure.campaign_id == camp_id)
        ).scalars().all()
        assert len(rows) == 1
        events = db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == camp_id,
                CampaignDomainEvent.event_type == "adventure.completed",
            )
        ).scalars().all()
        assert len(events) == 1


def test_repeat_completion_without_operation_id_fails_closed(setup):
    factory, camp_id, _owner = setup
    with factory() as db:
        adv = start_adventure(db, camp_id, "Closed arc")
        adv_id = adv.id
    _complete(factory, camp_id, "capture", "op-first")
    with factory() as db:
        camp = db.get(Campaign, camp_id)
        # No active adventure remains...
        with pytest.raises(AdventureNotFoundError):
            complete_adventure(
                db, camp_id, outcome="victory", reason="again",
                expected_revision=int(camp.revision),
            )
        db.rollback()
        # ...and explicitly re-closing the finished one fails closed.
        with pytest.raises(AdventureAlreadyCompletedError):
            complete_adventure(
                db, camp_id, outcome="victory", reason="again",
                adventure_id=adv_id, expected_revision=int(camp.revision),
            )
        db.rollback()


def test_failed_completion_leaves_adventure_open(setup):
    factory, camp_id, _owner = setup
    with factory() as db:
        adv = start_adventure(db, camp_id, "Fragile arc")
        adv_id = adv.id
        camp = db.get(Campaign, camp_id)
        with pytest.raises(ValueError):
            complete_adventure(
                db, camp_id, outcome="tie", reason="not a real outcome",
                operation_id="op-bad", expected_revision=int(camp.revision),
            )
        db.rollback()
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.status == "active"
        assert adv.outcome is None


def test_later_adventures_continue_in_same_campaign(setup):
    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Arc one")
        # Cannot stack a second active adventure while one is still open.
        with pytest.raises(AdventureAlreadyActiveError):
            start_adventure(db, camp_id, "Arc two overlapping")
        db.rollback()
    _complete(factory, camp_id, "failure", "op-arc1")
    # ...but after completion a new arc opens cleanly.
    with factory() as db:
        second = start_adventure(db, camp_id, "Arc two")
        assert second.status == "active"
        assert get_current_adventure(db, camp_id).id == second.id
        assert len(list_adventures(db, camp_id)) == 2
    _complete(factory, camp_id, "victory", "op-arc2")
    with factory() as db:
        assert db.get(Campaign, camp_id).status == "active"
        assert [a.outcome for a in list_adventures(db, camp_id)] == ["failure", "victory"]


def test_no_active_adventure_to_complete(setup):
    factory, camp_id, _owner = setup
    with factory() as db:
        camp = db.get(Campaign, camp_id)
        with pytest.raises(AdventureNotFoundError):
            complete_adventure(
                db, camp_id, outcome="victory", reason="nothing open",
                operation_id="op-empty", expected_revision=int(camp.revision),
            )
        db.rollback()


def test_closing_worker_success_and_failure_isolation(setup, monkeypatch):
    from types import SimpleNamespace

    from app.worker.executor import RetriableError

    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Closing arc")
    adv, _event = _complete(factory, camp_id, "victory", "op-close")

    # Failure in downstream work retries but never invalidates completion.
    import app.adventures.service as svc

    monkeypatch.setattr(svc, "_run_closing_followups", lambda db, adv: (_ for _ in ()).throw(RuntimeError("recap exploded")))
    with factory() as db:
        env = SimpleNamespace(
            job_id=uuid.uuid4(), payload={"adventure_id": str(adv.id), "campaign_id": str(camp_id)}
        )
        with pytest.raises(RetriableError):
            handle_adventure_closing(env, db)
    with factory() as db:
        still = db.get(Adventure, adv.id)
        assert still.status == "completed"
        assert still.outcome == "victory"
        assert still.closing_status == "failed"

    # Recovery succeeds; duplicate delivery is a no-op.
    monkeypatch.undo()
    with factory() as db:
        env = SimpleNamespace(
            job_id=uuid.uuid4(), payload={"adventure_id": str(adv.id), "campaign_id": str(camp_id)}
        )
        result = handle_adventure_closing(env, db)
        assert result["ok"] is True
        again = handle_adventure_closing(env, db)
        assert again["duplicate"] is True
    with factory() as db:
        done = db.get(Adventure, adv.id)
        assert done.closing_status == "succeeded"
        assert done.status == "completed"


def test_staged_effect_contract_validation():
    from app.dm.contract import CompleteAdventureArgs, StagedEffect

    StagedEffect.model_validate({
        "id": "eff_adv_1",
        "effect_type": "complete_adventure",
        "arguments": {"outcome": "villain_victory", "reason": "The lich ascends."},
    })
    CompleteAdventureArgs.model_validate({"outcome": "tpk", "reason": "Total party kill."})
    with pytest.raises(Exception):
        StagedEffect.model_validate({
            "id": "eff_adv_2",
            "effect_type": "complete_adventure",
            "arguments": {"outcome": "tie", "reason": "Not a real outcome."},
        })
    with pytest.raises(Exception):
        StagedEffect.model_validate({
            "id": "eff_adv_3",
            "effect_type": "complete_adventure",
            "arguments": {"outcome": "victory"},
        })


def _streaming_turn_with_completion_effect(db, camp_id, thread_id, adventure_id=None):
    """Directly stage a streaming turn carrying a complete_adventure effect."""
    from models.dm import DmTurn, DmTurnAttempt

    turn = DmTurn(
        id=uuid.uuid4(), campaign_id=camp_id, thread_id=str(thread_id),
        audience="campaign", status="streaming", source_revision=0,
        input_set_revision=1, submission_ids=[],
    )
    db.add(turn)
    db.flush()
    args: dict = {"outcome": "retreat", "reason": "The party flees the collapsing tomb."}
    if adventure_id is not None:
        args["adventure_id"] = str(adventure_id)
    attempt = DmTurnAttempt(
        id=uuid.uuid4(), turn_id=turn.id, attempt_number=1, status="streaming",
        campaign_id=camp_id, thread_id=str(thread_id), audience="campaign",
        source_revision=0, input_set_revision=1, submission_ids=[],
        staged_effects=[{"id": "eff_close_1", "effect_type": "complete_adventure", "arguments": args}],
    )
    db.add(attempt)
    db.flush()
    turn.current_attempt_id = attempt.id
    turn.streaming_attempt_id = attempt.id
    db.flush()
    return turn, attempt


def test_staged_effect_completes_adventure_in_turn_commit(setup):
    from models.dm import DmTurn
    from models.threads import CampaignThread

    factory, camp_id, owner = setup
    thread_id = uuid.uuid4()
    with factory() as db:
        db.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
        adv = start_adventure(db, camp_id, "Tomb arc")
        adv_id = adv.id
        turn, attempt = _streaming_turn_with_completion_effect(db, camp_id, thread_id)
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        from app.dm.turns import commit_turn

        turn, attempt, event = commit_turn(db, turn_id, attempt_id)
        db.commit()
        assert event.event_type == "adventure.completed"
        assert event.payload["outcome"] == "retreat"
        # Implicit-current effect still records the resolved adventure id.
        assert event.payload["adventure_completion"]["adventure_id"] == str(adv_id)
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.status == "completed"
        assert adv.outcome == "retreat"
        assert adv.source_turn_id == turn_id
        assert adv.source_event_id is not None
        assert db.get(DmTurn, turn_id).status == "succeeded"
        assert db.get(Campaign, camp_id).status == "active"


def test_staged_effect_duplicate_retry_is_idempotent(setup):
    from models.threads import CampaignThread

    factory, camp_id, owner = setup
    thread_id = uuid.uuid4()
    with factory() as db:
        db.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
        adv = start_adventure(db, camp_id, "Idempotent arc")
        adv_id = adv.id
        turn, attempt = _streaming_turn_with_completion_effect(db, camp_id, thread_id)
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        from app.dm.turns import commit_turn

        _t, _a, event1 = commit_turn(db, turn_id, attempt_id, operation_id="turn-op-1")
        db.commit()
        first_event_id = event1.id
    # Same logical turn retried with the same operation id returns the
    # original event without touching the adventure again.
    with factory() as db:
        from app.dm.turns import commit_turn

        _t, _a, event2 = commit_turn(db, turn_id, attempt_id, operation_id="turn-op-1")
        assert str(event2.id) == str(first_event_id)
    with factory() as db:
        rows = db.execute(
            select(Adventure).where(Adventure.campaign_id == camp_id)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "completed"


# ── HTTP API ────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient

    from app.auth.service import TEST_USER_ID
    from database import get_db
    from main import app

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Profile(id=TEST_USER_ID, email="owner@example.com"))
        db.commit()
    actor = {"id": TEST_USER_ID}

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setattr(
        "app.campaigns.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor
    finally:
        app.dependency_overrides.clear()


def test_adventure_api_lifecycle(api):
    client, _factory, _actor = api
    camp = client.post("/api/campaigns", json={"name": "API campaign"}).json()["campaign"]
    cid = camp["id"]

    started = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "The Sunken Chapel"},
        headers={"Idempotency-Key": "adv-start-1"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["adventure"]["status"] == "active"

    # Overlapping start is rejected while one is active.
    overlap = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Second arc"},
        headers={"Idempotency-Key": "adv-start-2"},
    )
    assert overlap.status_code == 409

    revision = client.get(f"/api/campaigns/{cid}").json()["campaign"]["revision"]
    done = client.post(
        f"/api/campaigns/{cid}/adventures/current/complete",
        json={
            "expected_revision": revision,
            "outcome": "villain_victory",
            "reason": "The lich completes the ritual.",
            "public_summary": "Darkness falls over the vale.",
        },
        headers={"Idempotency-Key": "adv-done-1"},
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["adventure"]["status"] == "completed"
    assert body["adventure"]["outcome"] == "villain_victory"
    assert body["event"]["event_type"] == "adventure.completed"
    assert body["campaign_status"] == camp["status"]  # completion never ends/archives the campaign

    # Idempotent replay of the same completion key returns the same record.
    revision2 = client.get(f"/api/campaigns/{cid}").json()["campaign"]["revision"]
    replay = client.post(
        f"/api/campaigns/{cid}/adventures/current/complete",
        json={
            "expected_revision": revision,
            "outcome": "villain_victory",
            "reason": "The lich completes the ritual.",
            "public_summary": "Darkness falls over the vale.",
        },
        headers={"Idempotency-Key": "adv-done-1"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["adventure"]["outcome"] == "villain_victory"

    listed = client.get(f"/api/campaigns/{cid}/adventures").json()
    assert listed["campaign_status"] == camp["status"]
    assert len(listed["adventures"]) == 1
    assert listed["current_adventure_id"] is None

    # A later adventure opens in the same campaign.
    again = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Ashes of the Vale"},
        headers={"Idempotency-Key": "adv-start-3"},
    )
    assert again.status_code == 200, again.text

    # Invalid outcome is rejected without closing the new adventure.
    revision3 = client.get(f"/api/campaigns/{cid}").json()["campaign"]["revision"]
    bad = client.post(
        f"/api/campaigns/{cid}/adventures/current/complete",
        json={"expected_revision": revision3, "outcome": "tie", "reason": "nope"},
        headers={"Idempotency-Key": "adv-bad-1"},
    )
    assert bad.status_code == 400
    listed2 = client.get(f"/api/campaigns/{cid}/adventures").json()
    assert listed2["current_adventure_id"] == again.json()["adventure"]["id"]


def test_member_sees_only_public_adventure_fields(api):
    from models.campaigns import CampaignMember

    client, factory, actor = api
    camp = client.post("/api/campaigns", json={"name": "Spoiler campaign"}).json()["campaign"]
    cid = camp["id"]
    member_id = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=member_id, email="member@example.com"))
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        db.commit()

    client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Secret arc", "metadata": {"dm_notes": "the butler did it"}},
        headers={"Idempotency-Key": "adv-spoiler-start"},
    )
    revision = client.get(f"/api/campaigns/{cid}").json()["campaign"]["revision"]
    client.post(
        f"/api/campaigns/{cid}/adventures/current/complete",
        json={
            "expected_revision": revision,
            "outcome": "capture",
            "reason": "DM-only: the traitor is the castellan.",
            "public_summary": "The party wakes in chains.",
        },
        headers={"Idempotency-Key": "adv-spoiler-done"},
    )

    # Owner sees the full record.
    owner_view = client.get(f"/api/campaigns/{cid}/adventures").json()["adventures"][0]
    assert owner_view["reason"] == "DM-only: the traitor is the castellan."
    assert owner_view["metadata"] == {"dm_notes": "the butler did it"}
    assert owner_view["source_turn_id"] is None
    assert "source_event_id" in owner_view

    # Members see identity + outcome + public summary only.
    actor["id"] = member_id
    member_view = client.get(f"/api/campaigns/{cid}/adventures").json()["adventures"][0]
    assert member_view["title"] == "Secret arc"
    assert member_view["outcome"] == "capture"
    assert member_view["public_summary"] == "The party wakes in chains."
    for hidden in ("reason", "metadata", "source_turn_id", "source_event_id",
                   "operation_id", "closing_status", "closing_attempts", "closing_error"):
        assert hidden not in member_view, hidden


def test_concurrent_start_backstop_enforced_by_database(setup):
    from sqlalchemy.exc import IntegrityError

    from models.campaigns import Adventure as AdventureModel

    factory, camp_id, _owner = setup
    with factory() as db:
        first = start_adventure(db, camp_id, "First arc")
        assert first.status == "active"
        # Simulate a loser that passed the application check before the
        # winner inserted: the partial unique index rejects the row.
        rogue = AdventureModel(
            id=uuid.uuid4(), campaign_id=camp_id, title="Rogue arc", status="active",
        )
        db.add(rogue)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    with factory() as db:
        assert len(list_adventures(db, camp_id)) == 1


def test_member_event_feed_hides_dm_reason(api):
    import json

    from models.campaigns import CampaignMember

    client, factory, actor = api
    camp = client.post("/api/campaigns", json={"name": "Leak campaign"}).json()["campaign"]
    cid = camp["id"]
    member_id = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=member_id, email="member@example.com"))
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        db.commit()

    secret = "DM-only: the castellan poisoned the well."
    started = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Well arc"},
        headers={"Idempotency-Key": "adv-leak-start"},
    )
    assert started.status_code == 200, started.text
    revision = client.get(f"/api/campaigns/{cid}").json()["campaign"]["revision"]
    done = client.post(
        f"/api/campaigns/{cid}/adventures/current/complete",
        json={
            "expected_revision": revision,
            "outcome": "failure",
            "reason": secret,
            "public_summary": "The town falls ill.",
        },
        headers={"Idempotency-Key": "adv-leak-done"},
    )
    assert done.status_code == 200, done.text

    actor["id"] = member_id
    feed = client.get(f"/api/campaigns/{cid}/events").json()
    assert feed["events"], "completion event must stay member-visible"
    blob = json.dumps(feed)
    assert secret not in blob
    assert "castellan" not in blob
    completed = [e for e in feed["events"] if e["event_type"] == "adventure.completed"]
    assert len(completed) == 1
    assert completed[0]["payload"]["outcome"] == "failure"
    assert completed[0]["payload"]["public_summary"] == "The town falls ill."
    assert "reason" not in completed[0]["payload"]


def test_closing_consumed_through_production_queue_path(setup, monkeypatch):
    import database
    from app.outbox.service import envelope_for_outbox
    from app.queue.consumer import consume_queue_delivery
    from models.reliability import Outbox

    factory, camp_id, _owner = setup
    monkeypatch.setattr(database, "SessionLocal", factory)
    with factory() as db:
        start_adventure(db, camp_id, "Queue arc")
    adv, _event = _complete(factory, camp_id, "victory", "op-queue")
    with factory() as db:
        assert db.get(Adventure, adv.id).closing_status == "pending"
        # Translate the actual committed outbox row — the exact translation
        # the relay uses — so queue and sweep share one worker identity.
        row = db.execute(
            select(Outbox).where(Outbox.event_type == "adventure.closing")
        ).scalars().one()
        result, dup = consume_queue_delivery(db, envelope_for_outbox(row).to_dict())
        assert dup is False and result["ok"] is True
    with factory() as db:
        assert db.get(Adventure, adv.id).closing_status == "succeeded"


def test_closing_sweep_converges_pending_work(setup):
    from app.adventures.service import run_adventure_closing_sweep
    from models.reliability import Outbox

    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Sweep arc")
    adv, _event = _complete(factory, camp_id, "retreat", "op-sweep")
    with factory() as db:
        row = db.execute(
            select(Outbox).where(Outbox.event_type == "adventure.closing")
        ).scalars().one()
        sweep = run_adventure_closing_sweep(db, limit=10)
        assert sweep["executed"] == [str(row.id)]
        assert sweep["failed"] == []
    with factory() as db:
        assert db.get(Adventure, adv.id).closing_status == "succeeded"
        assert db.get(Outbox, row.id).status == "published"
        # Second sweep finds nothing to do.
        assert run_adventure_closing_sweep(db)["executed"] == []
