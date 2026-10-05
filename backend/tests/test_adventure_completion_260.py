"""Issue #260 — DM-declared adventure completion without ending the campaign."""

from __future__ import annotations

import json
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


def _ensure_thread_id(db, camp_id):
    """The single shared game thread for a campaign (created if missing)."""
    from models.threads import CampaignThread

    thread = db.execute(
        select(CampaignThread).where(
            CampaignThread.campaign_id == camp_id,
            CampaignThread.thread_type == "campaign",
        )
    ).scalars().first()
    if thread is None:
        camp = db.get(Campaign, camp_id)
        thread = CampaignThread(
            id=uuid.uuid4(), campaign_id=camp_id,
            thread_type="campaign", created_by=camp.owner_id,
        )
        db.add(thread)
        db.flush()
    return thread.id


def _discard_turn(factory, turn_id, attempt_id):
    """Remove a failed turn + attempt so the thread accepts later turns."""
    from models.dm import DmTurn, DmTurnAttempt

    with factory() as db:
        attempt = db.get(DmTurnAttempt, attempt_id)
        if attempt is not None:
            db.delete(attempt)
        turn = db.get(DmTurn, turn_id)
        if turn is not None:
            db.delete(turn)
        db.commit()


def _dm_complete(factory, camp_id, outcome, op_id, *, reason=None,
                 public_summary=None, adventure_id=None):
    """Complete the campaign's adventure through the AI DM's staged effect.

    Stages ``complete_adventure`` on a fresh DM turn and commits it — the
    only production completion path. Returns     (adventure_id, event_id, turn_id).
    Raises on failure (failed completions leave the adventure open); call
    _discard_turn afterwards if the caller stages more turns on the thread.
    """
    with factory() as db:
        thread_id = _ensure_thread_id(db, camp_id)
        turn, attempt = _streaming_turn_with_completion_effect(
            db, camp_id, thread_id, adventure_id=adventure_id,
            outcome=outcome, reason=reason or f"{outcome} reason",
            public_summary=public_summary, effect_id=f"eff-{op_id}",
        )
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        from app.dm.turns import commit_turn

        _t, _a, event = commit_turn(db, turn_id, attempt_id, operation_id=op_id)
        db.commit()
        adv_id = event.payload["adventure_completion"]["adventure_id"]
        return uuid.UUID(str(adv_id)), event.id, turn_id


@pytest.mark.parametrize("outcome", sorted(ADVENTURE_OUTCOMES))
def test_all_outcomes_complete_without_ending_campaign(setup, outcome):
    factory, camp_id, _owner = setup
    with factory() as db:
        adv = start_adventure(db, camp_id, f"The {outcome} arc")
        assert adv.status == "active"
    adv_id, event_id, _turn_id = _dm_complete(factory, camp_id, outcome, f"op-{outcome}")
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.status == "completed"
        assert adv.outcome == outcome
        assert adv.completed_at is not None
        event = db.get(CampaignDomainEvent, event_id)
        assert event.event_type == "adventure.completed"
        assert event.payload["outcome"] == outcome
        camp = db.get(Campaign, camp_id)
        assert camp.status == "active"
        assert get_current_adventure(db, camp_id) is None


def test_tpk_and_villain_victory_are_legitimate_completions(setup):
    factory, camp_id, _owner = setup
    for outcome, op in (("tpk", "op-tpk"), ("villain_victory", "op-vv")):
        with factory() as db:
            start_adventure(db, camp_id, f"Arc {op}")
        adv_id, _event_id, _turn_id = _dm_complete(factory, camp_id, outcome, op)
        with factory() as db:
            adv = db.get(Adventure, adv_id)
            assert adv.status == "completed" and adv.outcome == outcome
            assert db.get(Campaign, camp_id).status == "active"


def test_completion_provenance_and_closing_trigger(setup):
    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Provenance arc")
    adv_id, event_id, turn_id = _dm_complete(
        factory, camp_id, "victory", "op-prov",
        reason="Dragon slain", public_summary="The town is saved.",
    )
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.source_turn_id == turn_id
        assert adv.source_event_id == event_id
        assert adv.public_summary == "The town is saved."
        event = db.get(CampaignDomainEvent, event_id)
        # The DM turn is the authoritative provenance for the decision.
        assert event.provenance["source"] == "dm_turn"
        assert event.payload["source_turn_id"] == str(turn_id)
        from models.reliability import Outbox

        rows = db.execute(
            select(Outbox).where(Outbox.event_type == "adventure.closing")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].payload["adventure_id"] == str(adv_id)


def test_duplicate_completion_is_idempotent(setup):
    """Retrying the same DM turn commit returns the original completion.

    The turn commit's operation id makes duplicate delivery a no-op: the
    original outcome is preserved and no second adventure/event is written.
    """
    from app.dm.turns import commit_turn

    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Retry arc")
        thread_id = _ensure_thread_id(db, camp_id)
        turn, attempt = _streaming_turn_with_completion_effect(
            db, camp_id, thread_id, outcome="retreat",
            reason="retreat reason", effect_id="eff-op-dup",
        )
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        _t, _a, event1 = commit_turn(db, turn_id, attempt_id, operation_id="op-dup")
        db.commit()
        first_event_id = event1.id
    with factory() as db:
        _t, _a, event2 = commit_turn(db, turn_id, attempt_id, operation_id="op-dup")
        assert str(event2.id) == str(first_event_id)
    with factory() as db:
        rows = db.execute(
            select(Adventure).where(Adventure.campaign_id == camp_id)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].outcome == "retreat"  # original outcome preserved
        events = db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == camp_id,
                CampaignDomainEvent.event_type == "adventure.completed",
            )
        ).scalars().all()
        assert len(events) == 1


def test_repeat_completion_without_operation_id_fails_closed(setup):
    """A new DM completion with no open adventure fails closed.

    Both the implicit-current form and an explicit re-close of the finished
    adventure raise; the completed adventure is untouched.
    """
    from app.dm.turns import commit_turn

    factory, camp_id, _owner = setup
    with factory() as db:
        adv = start_adventure(db, camp_id, "Closed arc")
        adv_id = adv.id
    _dm_complete(factory, camp_id, "capture", "op-first")
    with factory() as db:
        thread_id = _ensure_thread_id(db, camp_id)
        # No active adventure remains...
        turn, attempt = _streaming_turn_with_completion_effect(
            db, camp_id, thread_id, outcome="victory",
            reason="again", effect_id="eff-again",
        )
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        with pytest.raises(ValueError, match="no active adventure"):
            commit_turn(db, turn_id, attempt_id, operation_id="op-again")
        db.rollback()
    _discard_turn(factory, turn_id, attempt_id)
    with factory() as db:
        # ...and explicitly re-closing the finished one fails closed.
        thread_id = _ensure_thread_id(db, camp_id)
        turn2, attempt2 = _streaming_turn_with_completion_effect(
            db, camp_id, thread_id, adventure_id=adv_id,
            outcome="victory", reason="again", effect_id="eff-again-2",
        )
        turn2_id, attempt2_id = turn2.id, attempt2.id
        db.commit()
    with factory() as db:
        with pytest.raises(ValueError, match="already completed"):
            commit_turn(db, turn2_id, attempt2_id, operation_id="op-again-2")
        db.rollback()
    _discard_turn(factory, turn2_id, attempt2_id)
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.status == "completed" and adv.outcome == "capture"


def test_failed_completion_leaves_adventure_open(setup):
    from app.dm.turns import commit_turn

    factory, camp_id, _owner = setup
    with factory() as db:
        adv = start_adventure(db, camp_id, "Fragile arc")
        adv_id = adv.id
        thread_id = _ensure_thread_id(db, camp_id)
        turn, attempt = _streaming_turn_with_completion_effect(
            db, camp_id, thread_id, outcome="tie",
            reason="not a real outcome", effect_id="eff-op-bad",
        )
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        with pytest.raises(ValueError, match="Invalid adventure outcome"):
            commit_turn(db, turn_id, attempt_id, operation_id="op-bad")
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
    _dm_complete(factory, camp_id, "failure", "op-arc1")
    # ...but after completion a new arc opens cleanly.
    with factory() as db:
        second = start_adventure(db, camp_id, "Arc two")
        assert second.status == "active"
        assert get_current_adventure(db, camp_id).id == second.id
        assert len(list_adventures(db, camp_id)) == 2
    _dm_complete(factory, camp_id, "victory", "op-arc2")
    with factory() as db:
        assert db.get(Campaign, camp_id).status == "active"
        assert [a.outcome for a in list_adventures(db, camp_id)] == ["failure", "victory"]


def test_no_active_adventure_to_complete(setup):
    from app.dm.turns import commit_turn

    factory, camp_id, _owner = setup
    with factory() as db:
        thread_id = _ensure_thread_id(db, camp_id)
        turn, attempt = _streaming_turn_with_completion_effect(
            db, camp_id, thread_id, outcome="victory",
            reason="nothing open", effect_id="eff-op-empty",
        )
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        with pytest.raises(ValueError, match="no active adventure"):
            commit_turn(db, turn_id, attempt_id, operation_id="op-empty")
        db.rollback()


def test_closing_worker_success_and_failure_isolation(setup, monkeypatch):
    from types import SimpleNamespace

    from app.worker.executor import RetriableError

    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Closing arc")
    adv_id, _event_id, _turn_id = _dm_complete(factory, camp_id, "victory", "op-close")

    # Failure in downstream work retries but never invalidates completion.
    import app.adventures.service as svc

    monkeypatch.setattr(svc, "_run_closing_followups", lambda db, adv: (_ for _ in ()).throw(RuntimeError("recap exploded")))
    with factory() as db:
        env = SimpleNamespace(
            job_id=uuid.uuid4(), payload={"adventure_id": str(adv_id), "campaign_id": str(camp_id)}
        )
        with pytest.raises(RetriableError):
            handle_adventure_closing(env, db)
    with factory() as db:
        still = db.get(Adventure, adv_id)
        assert still.status == "completed"
        assert still.outcome == "victory"
        assert still.closing_status == "failed"

    # Recovery succeeds; duplicate delivery is a no-op.
    monkeypatch.undo()
    with factory() as db:
        env = SimpleNamespace(
            job_id=uuid.uuid4(), payload={"adventure_id": str(adv_id), "campaign_id": str(camp_id)}
        )
        result = handle_adventure_closing(env, db)
        assert result["ok"] is True
        again = handle_adventure_closing(env, db)
        assert again["duplicate"] is True
    with factory() as db:
        done = db.get(Adventure, adv_id)
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


def _streaming_turn_with_completion_effect(db, camp_id, thread_id, adventure_id=None, *,
                                             outcome="retreat", reason=None,
                                             public_summary=None, effect_id="eff_close_1"):
    """Directly stage a streaming turn carrying a complete_adventure effect."""
    from models.dm import DmTurn, DmTurnAttempt

    camp = db.get(Campaign, camp_id)
    rev = int(camp.revision or 0)
    turn = DmTurn(
        id=uuid.uuid4(), campaign_id=camp_id, thread_id=str(thread_id),
        audience="campaign", status="streaming", source_revision=rev,
        input_set_revision=rev + 1, submission_ids=[],
    )
    db.add(turn)
    db.flush()
    args: dict = {"outcome": outcome,
                  "reason": reason or "The party flees the collapsing tomb."}
    if public_summary is not None:
        args["public_summary"] = public_summary
    if adventure_id is not None:
        args["adventure_id"] = str(adventure_id)
    attempt = DmTurnAttempt(
        id=uuid.uuid4(), turn_id=turn.id, attempt_number=1, status="streaming",
        campaign_id=camp_id, thread_id=str(thread_id), audience="campaign",
        source_revision=rev, input_set_revision=rev + 1, submission_ids=[],
        staged_effects=[{"id": effect_id, "effect_type": "complete_adventure", "arguments": args}],
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


def test_staged_effect_completion_binds_exact_source_range(setup):
    """Staged-DM completions finalize AFTER the authoritative event exists.

    The derived summary's end cursor must equal the promoted
    adventure.completed event sequence / campaign revision — not the
    pre-commit values visible inside the effect handler.
    """
    from models.campaigns import AdventureSummary
    from models.threads import CampaignThread

    factory, camp_id, owner = setup
    thread_id = uuid.uuid4()
    with factory() as db:
        db.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
        adv = start_adventure(db, camp_id, "Range arc")
        adv_id = adv.id
        turn, attempt = _streaming_turn_with_completion_effect(db, camp_id, thread_id)
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        from app.dm.turns import commit_turn

        turn, attempt, event = commit_turn(db, turn_id, attempt_id)
        db.commit()
        assert event.event_type == "adventure.completed"
        event_seq = int(event.sequence)
        camp_rev = int(db.get(Campaign, camp_id).revision)
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.status == "completed"
        assert adv.source_event_id is not None
        assert adv.end_sequence == event_seq
        assert adv.end_revision == camp_rev
        row = db.execute(
            select(AdventureSummary).where(AdventureSummary.adventure_id == adv_id)
        ).scalars().first()
        assert row is not None and row.status == "current"
        assert row.source_event_to == event_seq
        assert "Range arc" in (row.historical_text or "")


def test_staged_effect_duplicate_retry_is_idempotent(setup):
    from models.threads import CampaignThread

    factory, camp_id, owner = setup
    thread_id = uuid.uuid4()
    with factory() as db:
        db.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
        start_adventure(db, camp_id, "Idempotent arc")
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
        "app.deps.auth.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor
    finally:
        app.dependency_overrides.clear()


def test_adventure_api_lifecycle(api):
    # Starting/listing adventures stay owner admin actions over HTTP; the
    # completion itself goes through the AI DM's staged effect (there is no
    # POST .../complete route anymore).
    client, factory, _actor = api
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

    # The AI DM completes the arc via its staged effect.
    _dm_complete(
        factory, uuid.UUID(cid), "villain_victory", "adv-done-1",
        reason="The lich completes the ritual.",
        public_summary="Darkness falls over the vale.",
    )

    listed = client.get(f"/api/campaigns/{cid}/adventures").json()
    assert listed["campaign_status"] == camp["status"]
    assert len(listed["adventures"]) == 1
    assert listed["current_adventure_id"] is None
    assert listed["adventures"][0]["outcome"] == "villain_victory"

    # A later adventure opens in the same campaign.
    again = client.post(
        f"/api/campaigns/{cid}/adventures",
        json={"title": "Ashes of the Vale"},
        headers={"Idempotency-Key": "adv-start-3"},
    )
    assert again.status_code == 200, again.text

    # An invalid DM outcome is rejected without closing the new adventure.
    from app.dm.turns import commit_turn

    with factory() as db:
        thread_id = _ensure_thread_id(db, uuid.UUID(cid))
        turn, attempt = _streaming_turn_with_completion_effect(
            db, uuid.UUID(cid), thread_id, outcome="tie",
            reason="nope", effect_id="eff-adv-bad-1",
        )
        turn_id, attempt_id = turn.id, attempt.id
        db.commit()
    with factory() as db:
        with pytest.raises(ValueError, match="Invalid adventure outcome"):
            commit_turn(db, turn_id, attempt_id, operation_id="adv-bad-1")
        db.rollback()
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
    _dm_complete(
        factory, uuid.UUID(cid), "capture", "adv-spoiler-done",
        reason="DM-only: the traitor is the castellan.",
        public_summary="The party wakes in chains.",
    )

    # Every player — the campaign owner included (#470) — sees identity +
    # outcome + public summary only.
    owner_view = client.get(f"/api/campaigns/{cid}/adventures").json()["adventures"][0]
    actor["id"] = member_id
    member_view = client.get(f"/api/campaigns/{cid}/adventures").json()["adventures"][0]
    assert owner_view == member_view
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
    _dm_complete(
        factory, uuid.UUID(cid), "failure", "adv-leak-done",
        reason=secret, public_summary="The town falls ill.",
    )

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


def test_closing_sweep_converges_pending_work(setup):
    from app.adventures.service import run_adventure_closing_sweep
    from models.reliability import Outbox

    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Sweep arc")
    adv_id, _event_id, _turn_id = _dm_complete(factory, camp_id, "retreat", "op-sweep")
    with factory() as db:
        row = db.execute(
            select(Outbox).where(Outbox.event_type == "adventure.closing")
        ).scalars().one()
        sweep = run_adventure_closing_sweep(db, limit=10)
        assert sweep["executed"] == [str(row.id)]
        assert sweep["failed"] == []
    with factory() as db:
        assert db.get(Adventure, adv_id).closing_status == "succeeded"
        assert db.get(Outbox, row.id).status == "published"
        # Second sweep finds nothing to do.
        assert run_adventure_closing_sweep(db)["executed"] == []


def test_terminal_closing_job_retires_without_starving_newer_work(setup, monkeypatch):
    import app.adventures.service as svc
    from app.adventures.service import run_adventure_closing_sweep
    from models.reliability import Outbox

    factory, camp_id, _owner = setup
    with factory() as db:
        start_adventure(db, camp_id, "Poison arc")
    adv_a_id, _, _ = _dm_complete(factory, camp_id, "death", "op-poison")
    with factory() as db:
        start_adventure(db, camp_id, "Fresh arc")
    adv_b_id, _, _ = _dm_complete(factory, camp_id, "victory", "op-fresh")

    real_followups = svc._run_closing_followups

    def _flaky(db, adventure):
        if str(adventure.id) == str(adv_a_id):
            raise RuntimeError("recap store offline")
        return real_followups(db, adventure)

    monkeypatch.setattr(svc, "_run_closing_followups", _flaky)

    def _rows(db):
        return {
            str(r.id): r.status
            for r in db.execute(select(Outbox).where(Outbox.event_type == "adventure.closing"))
            .scalars().all()
        }

    with factory() as db:
        # Oldest-first with limit=1 hits the poisoned row: exhaustion is
        # terminal, so the transport row retires instead of requeueing.
        first = run_adventure_closing_sweep(db, limit=1, max_attempts=1)
        assert first["executed"] == []
        assert len(first["failed"]) == 1 and first["failed"][0]["terminal"] is True
        statuses = _rows(db)
        assert len(statuses) == 2
        assert all(s == "published" for s in statuses.values()) is False
        retired = [k for k, v in statuses.items() if v == "published"]
        assert len(retired) == 1
    with factory() as db:
        # The terminal row is never reselected; the newer job still runs.
        second = run_adventure_closing_sweep(db, limit=10)
        assert len(second["executed"]) == 1
        assert second["failed"] == []
        assert db.get(Adventure, adv_b_id).closing_status == "succeeded"
    with factory() as db:
        third = run_adventure_closing_sweep(db, limit=10)
        assert third["executed"] == [] and third["failed"] == []
        # Narrative completion stands; only best-effort closing failed.
        poisoned = db.get(Adventure, adv_a_id)
        assert poisoned.status == "completed" and poisoned.outcome == "death"
        assert poisoned.closing_status == "failed"


def test_effect_argument_redaction_unit():
    from app.adventures.service import redact_private_effect_arguments

    staged = [
        {"id": "e1", "effect_type": "complete_adventure",
         "arguments": {"outcome": "victory", "reason": "secret", "public_summary": "yay"}},
        {"id": "e2", "effect_type": "update_scene",
         "arguments": {"scene_patch": {}, "reason": "kept"}},
    ]
    out = redact_private_effect_arguments(staged)
    assert "reason" not in out[0]["arguments"]
    assert out[0]["arguments"]["outcome"] == "victory"
    assert out[1]["arguments"]["reason"] == "kept"
    # Input untouched (copy, not mutation).
    assert staged[0]["arguments"]["reason"] == "secret"


def _real_completion_contract(secret: str, nested_secret: str) -> dict:
    """A real normalized contract dict carrying sentinels in both lanes."""
    from app.dm.contract import CONTRACT_VERSION, normalize_contract

    return normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "respond",
            "reason": secret,
            "beats": [
                {
                    "id": "beat_1",
                    "type": "narration",
                    "claims": [
                        {
                            "text": "The tomb door grinds shut.",
                            "claim_kind": "observation",
                            "origin": "dm_adjudication",
                            "visibility": "public",
                        }
                    ],
                }
            ],
            "open_player_choice": "What do you do?",
            "staged_effects": [
                {"id": "eff_x", "effect_type": "complete_adventure",
                 "arguments": {"outcome": "capture", "reason": nested_secret,
                               "public_summary": "Chained in the dark."}},
            ],
        }
    ).model_dump(mode="json")


def test_contract_snapshot_projection_hides_internal_reason():
    from app.adventures.service import redact_private_contract_snapshot

    snap = redact_private_contract_snapshot(_real_completion_contract("TOP-SECRET", "NESTED-SECRET"))
    blob = json.dumps(snap)
    assert "TOP-SECRET" not in blob
    assert "NESTED-SECRET" not in blob
    assert redact_private_contract_snapshot(None) is None
    assert redact_private_contract_snapshot({"not": "a contract"}) is None


def test_turn_inspection_redacts_completion_reason_for_members(api):
    import json

    from app.auth.service import TEST_USER_ID
    from models.campaigns import CampaignMember
    from models.dm import DmTurn, DmTurnAttempt
    from models.threads import CampaignThread

    client, factory, actor = api
    camp = client.post("/api/campaigns", json={"name": "Turn leak"}).json()["campaign"]
    cid = camp["id"]
    member_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    top_secret = "TOP-SECRET turn rationale: the castellan did it."
    nested_secret = "NESTED-SECRET effect rationale."
    snapshot = _real_completion_contract(top_secret, nested_secret)
    staged = snapshot["staged_effects"]
    with factory() as db:
        db.add(Profile(id=member_id, email="member@example.com"))
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        thread_id = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == uuid.UUID(cid),
                CampaignThread.thread_type == "campaign",  # shared game thread, not lobby (#243)
            )
        ).scalars().one().id
        db.add(DmTurn(id=turn_id, campaign_id=uuid.UUID(cid), thread_id=str(thread_id),
                      audience="campaign", status="streaming", source_revision=0,
                      input_set_revision=1, submission_ids=[]))
        db.add(DmTurnAttempt(id=uuid.uuid4(), turn_id=turn_id, attempt_number=1,
                             status="streaming", campaign_id=uuid.UUID(cid),
                             thread_id=str(thread_id), audience="campaign",
                             source_revision=0, input_set_revision=1, submission_ids=[],
                             staged_effects=staged,
                             contract_snapshot=snapshot))
        db.commit()

    actor["id"] = member_id
    body = client.get(f"/api/campaigns/{cid}/dm-turns/{turn_id}").json()
    assert len(body["attempts"]) == 1
    mem_args = body["attempts"][0]["staged_effects"][0]["arguments"]
    assert "reason" not in mem_args
    assert mem_args["outcome"] == "capture"
    blob = json.dumps(body)
    assert top_secret not in blob
    assert nested_secret not in blob
    assert "castellan" not in blob

    # The owner is a player too: same redaction (#470).
    actor["id"] = TEST_USER_ID
    owner_body = client.get(f"/api/campaigns/{cid}/dm-turns/{turn_id}").json()
    assert owner_body["attempts"] == body["attempts"]
