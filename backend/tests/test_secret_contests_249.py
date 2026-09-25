"""Issue #249 — contested secret actions and hidden-cause rolls vs other PCs.

A private initiating player (Alice) can run a secret pickpocket-style
contest against another human PC (Bob): Bob receives a mechanically valid
roll request carrying only a safe public reason, both sides' dice stay
player-supplied, Alice can never author Bob's roll or voluntary behavior,
outcomes commit restricted knowledge without auto-revealing, and shared
channels/events never leak the private action.
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
from app.campaigns.events import list_campaign_events  # noqa: E402
from app.decisions import DecisionService  # noqa: E402
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.dm import decision_routing as routing  # noqa: E402
from app.dm import secret_contests as contests  # noqa: E402
from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.dm.turns import coordinate_turn  # noqa: E402
from app.realtime.channels import live_table_channel  # noqa: E402
from app.realtime.service import InMemoryRealtimePublisher, set_realtime_publisher  # noqa: E402
from app.rolls.service import RollAuthorizationError, fulfill_roll  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import can_read_thread, create_private_thread  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.dm import DmTurn, PlayerRollRequest  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

SECRET = "moonmoth vault passphrase MOTH-SIGIL-249"
SAFE_REASON = "A crowded moment jostles you; stay sharp."


@pytest.fixture
def ctx():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    owner = uuid.uuid4()
    alice = uuid.uuid4()
    bob = uuid.uuid4()
    camp_id = uuid.uuid4()
    alice_char = uuid.uuid4()
    bob_char = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=alice, email="alice@example.com"),
        Profile(id=bob, email="bob@example.com"),
        Campaign(id=camp_id, owner_id=owner, name="Secret Contests", revision=0),
        CampaignMember(campaign_id=camp_id, user_id=owner, role="owner"),
        CampaignMember(campaign_id=camp_id, user_id=alice, role="player"),
        CampaignMember(campaign_id=camp_id, user_id=bob, role="player"),
        CampaignThread(id=uuid.uuid4(), campaign_id=camp_id, thread_type="campaign",
                       title="Campaign", created_by=owner),
        Character(id=alice_char, owner_id=alice, name="Alice PC", system="dnd5e"),
        Dnd5eCharacterSheet(character_id=alice_char, owner_id=alice, character_name="Alice PC"),
        Character(id=bob_char, owner_id=bob, name="Bob PC", system="dnd5e"),
        Dnd5eCharacterSheet(character_id=bob_char, owner_id=bob, character_name="Bob PC"),
    ])
    db.commit()
    shared = db.execute(
        select(CampaignThread).where(
            CampaignThread.campaign_id == camp_id, CampaignThread.thread_type == "campaign")
    ).scalars().one()
    private = create_private_thread(
        db, campaign_id=camp_id, created_by=alice, member_ids=[], title="Whispers")
    db.commit()
    yield {"factory": factory, "campaign_id": camp_id, "owner": owner, "alice": alice,
           "bob": bob, "alice_char": alice_char, "bob_char": bob_char,
           "shared_id": shared.id, "private_id": private.id}
    db.close()


def _db(ctx):
    return ctx["factory"]()


def _failing_decision_service():
    return DecisionService(FakeDecisionAdapter({}))


def _private_turn(ctx, db, text):
    sub = accept_submission(
        db, campaign_id=ctx["campaign_id"], user_id=ctx["alice"],
        character_id=ctx["alice_char"], raw_content=text,
        segments=[{"type": "ic", "text": text}],
        thread_id=str(ctx["private_id"]), audience="private",
    )
    db.commit()
    coord = coordinate_turn(db, ctx["campaign_id"], str(ctx["private_id"]),
                            audience="private", commit=False)
    db.commit()
    assert coord is not None
    return sub, coord[0], coord[1]


def _await_roll_contract(ctx, character_id, request_id="lift1"):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION,
        "mode": "await_roll",
        "reason": "secret lift",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [{
                "text": "The crowd presses close.", "claim_kind": "observation",
                "origin": "dm_adjudication", "visibility": "public",
            }],
        }],
        "roll_request": {
            "request_id": request_id, "character_id": str(character_id),
            "roll_kind": "check", "ability_or_skill": "Sleight of Hand",
            "label": "Secret lift", "reason_public": "Lift the pouch",
            "dc_private": 15,
        },
    })


def _fulfill(db, request_id, actor_id, raw, modifier=0):
    total = raw + modifier
    return fulfill_roll(db, request_id=request_id, actor_id=actor_id, payload={
        "source": "app", "raw_rolls": [raw], "modifier": modifier,
        "total": total, "visibility": "private"})


def _secret_lift_with_initiator_roll(ctx, db, *, alice_raw, tag="a"):
    """Private pickpocket turn through Alice's fulfilled secret roll."""
    _, turn, attempt = _private_turn(ctx, db, f"I lift the pouch. {SECRET}")

    def _generative(packet, feedback=None):
        return _await_roll_contract(ctx, ctx["alice_char"], request_id=f"lift-{tag}")

    result = execute_dm_attempt(
        db, attempt.id, adjudicate=_generative, narrator="deterministic",
        decision_service=_failing_decision_service())
    assert result.mode == "await_roll"
    row = db.execute(
        select(PlayerRollRequest).where(
            PlayerRollRequest.turn_id == turn.id,
            PlayerRollRequest.request_key == f"lift-{tag}")
    ).scalars().one()
    _fulfill(db, row.id, ctx["alice"], alice_raw)
    db.commit()
    db.refresh(turn)
    return turn, row


def _start_pickpocket(ctx, db, turn, **overrides):
    params = {
        "campaign_id": ctx["campaign_id"], "initiating_turn_id": turn.id,
        "initiator_user_id": ctx["alice"], "initiator_character_id": ctx["alice_char"],
        "targets": [{
            "target_user_id": ctx["bob"], "target_character_id": ctx["bob_char"],
            "roll_kind": "check", "ability_or_skill": "Perception",
            "label": "Notice the brush", "reason_public": SAFE_REASON,
        }],
        "contest_key": f"pickpocket-{uuid.uuid4().hex[:8]}", "mode": "opposed",
        "hidden_cause": f"Alice attempts to lift Bob's pouch. {SECRET}",
        "reveal_on_success": False, "reveal_on_failure": False,
        "success_facts": [{"content": "Alice lifted Bob's pouch unseen.", "visibility": "private"}],
        "failure_facts": [{"content": "Alice failed to lift Bob's pouch.", "visibility": "private"}],
    }
    params.update(overrides)
    return contests.start_secret_contest(db, **params)


# ── Secret contest flow + hidden-cause target roll ────────────────────────

def test_secret_pickpocket_opposed_flow(ctx):
    db = _db(ctx)
    try:
        turn, alice_row = _secret_lift_with_initiator_roll(ctx, db, alice_raw=17)
        contest = _start_pickpocket(
            ctx, db, turn, initiator_roll_request_id=alice_row.id)

        # Bob's pending roll is mechanically valid but redacted.
        bob_pending = contests.pending_hidden_cause_rolls_for_user(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["bob"])
        assert len(bob_pending) == 1
        redacted = bob_pending[0]
        assert redacted["roll_kind"] == "check"
        assert redacted["ability_or_skill"] == "Perception"
        assert redacted["reason_public"] == SAFE_REASON
        assert "dc_private" not in redacted
        blob = str(bob_pending)
        assert "MOTH-SIGIL-249" not in blob
        assert str(ctx["alice"]) not in blob

        # Alice has no hidden-cause rolls of her own pending.
        assert contests.pending_hidden_cause_rolls_for_user(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["alice"]) == []

        # Bob answers; the contest resolves initiator-succeeds (18 vs 5).
        _fulfill(db, uuid.UUID(redacted["id"]), ctx["bob"], 5)
        db.commit()
        result = contests.resolve_secret_contest(db, contest_id=contest.id)
        assert result.outcome == contests.OUTCOME_INITIATOR_SUCCEEDS
        assert result.revealed is False
        assert result.replayed is False
        assert result.event.visibility == "private"

        # Branch knowledge committed without revealing the secret.
        from app.world.knowledge import list_facts

        facts = [f for f in list_facts(db, ctx["campaign_id"]) if "lifted" in (f.content or "")]
        assert len(facts) == 1
        assert facts[0].visibility == "private"

        # The initiating turn resumed once target input arrived (normal #204
        # lifecycle continues; the contest outcome stays linked by provenance).
        db.refresh(turn)
        assert turn.status == "pending"
        prov = (result.event.provenance or {})
        assert prov.get("contest_id") == str(contest.id)
        assert prov.get("initiating_turn_id") == str(turn.id)
    finally:
        db.close()


def test_target_vs_dc_hidden_cause_roll(ctx):
    db = _db(ctx)
    try:
        _, turn, _ = _private_turn(ctx, db, "I shadow Bob through the market.")
        contest = contests.start_secret_contest(
            db, campaign_id=ctx["campaign_id"], initiating_turn_id=turn.id,
            initiator_user_id=ctx["alice"], initiator_character_id=ctx["alice_char"],
            targets=[{
                "target_user_id": ctx["bob"], "target_character_id": ctx["bob_char"],
                "roll_kind": "save", "ability_or_skill": "Wisdom",
                "label": "Sense being followed", "reason_public": SAFE_REASON,
            }],
            contest_key="shadow1", mode="target_vs_dc", dc_private=14,
            hidden_cause=f"Alice shadows Bob. {SECRET}",
            success_facts=[{"content": "Alice shadows Bob unseen.", "visibility": "dm_only"}],
            failure_facts=[{"content": "Alice's shadowing risks notice.", "visibility": "dm_only"}],
        )
        (row_id,) = contest.target_roll_request_ids
        row = db.get(PlayerRollRequest, uuid.UUID(row_id))
        # Hidden DC is stored server-side but never serialized publicly.
        assert row.dc_private == 14
        assert "dc_private" not in row.to_dict()
        # Bob beats the hidden DC (16 >= 14): the target holds.
        _fulfill(db, row.id, ctx["bob"], 16)
        db.commit()
        result = contests.resolve_secret_contest(db, contest_id=contest.id)
        assert result.outcome == contests.OUTCOME_TARGET_HOLDS
    finally:
        db.close()


# ── Initiator agency: cannot author target roll or behavior ───────────────

def test_initiator_agency_violation_rejected(ctx):
    db = _db(ctx)
    try:
        turn, alice_row = _secret_lift_with_initiator_roll(ctx, db, alice_raw=10)

        def _offending_contract():
            return normalize_contract({
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "offending adjudication",
                "beats": [{
                    "id": "beat_1", "type": "narration",
                    "claims": [{
                        "text": "Bob hands over his pouch.",
                        "claim_kind": "player_declaration",
                        "actor_ref": {"type": "character", "id": str(ctx["bob_char"])},
                        "origin": "player_transcript",
                    }],
                }],
            })

        with pytest.raises(contests.ContestedAgencyViolation):
            _start_pickpocket(
                ctx, db, turn, contest_key="pp-bad",
                initiator_roll_request_id=alice_row.id,
                initiating_contract=_offending_contract())
        db.rollback()

        def _offending_roll_contract():
            base = _await_roll_contract(ctx, ctx["bob_char"])
            return base

        with pytest.raises(contests.ContestedAgencyViolation):
            _start_pickpocket(
                ctx, db, turn, contest_key="pp-bad2",
                initiator_roll_request_id=alice_row.id,
                initiating_contract=_offending_roll_contract())
        db.rollback()

        # A clean start works; then Alice herself cannot supply Bob's roll.
        contest = _start_pickpocket(
            ctx, db, turn, contest_key="pp-ok",
            initiator_roll_request_id=alice_row.id)
        (row_id,) = contest.target_roll_request_ids
        with pytest.raises(RollAuthorizationError):
            fulfill_roll(db, request_id=uuid.UUID(row_id), actor_id=ctx["alice"], payload={
                "source": "app", "raw_rolls": [20], "modifier": 0,
                "total": 20, "visibility": "private"})
        db.rollback()
    finally:
        db.close()


# ── Target disconnect / reconnect ─────────────────────────────────────────

def test_target_disconnect_reconnect(ctx):
    db = _db(ctx)
    try:
        turn, alice_row = _secret_lift_with_initiator_roll(ctx, db, alice_raw=17)
        contest = _start_pickpocket(
            ctx, db, turn, initiator_roll_request_id=alice_row.id)
        contest_id = contest.id
        (row_id,) = contest.target_roll_request_ids
        # Disconnect before answering: durable pending state survives.
        db.close()
        db2 = _db(ctx)
        try:
            pending = contests.pending_hidden_cause_rolls_for_user(
                db2, campaign_id=ctx["campaign_id"], user_id=ctx["bob"])
            assert [r["id"] for r in pending] == [row_id]
            # Still blocked while the target is away; the AI never answers.
            with pytest.raises(contests.ContestNotReady):
                contests.resolve_secret_contest(db2, contest_id=contest_id)
            db2.rollback()
            # Reconnect and answer from the durable state.
            _fulfill(db2, uuid.UUID(row_id), ctx["bob"], 4)
            db2.commit()
        finally:
            db2.close()
        db3 = _db(ctx)
        try:
            result = contests.resolve_secret_contest(db3, contest_id=contest_id)
            assert result.outcome == contests.OUTCOME_INITIATOR_SUCCEEDS
        finally:
            db3.close()
    except Exception:
        try:
            db.close()
        except Exception:
            pass
        raise


# ── Success / failure visibility differences ──────────────────────────────

def test_success_stays_hidden_failure_reveals(ctx):
    from app.snapshot.surfaces import build_surfaces_for_viewer

    # Success without reveal: only Alice learns the outcome fact.
    db = _db(ctx)
    try:
        turn, alice_row = _secret_lift_with_initiator_roll(ctx, db, alice_raw=17)
        contest = _start_pickpocket(ctx, db, turn, initiator_roll_request_id=alice_row.id)
        (row_id,) = contest.target_roll_request_ids
        _fulfill(db, uuid.UUID(row_id), ctx["bob"], 5)
        db.commit()
        result = contests.resolve_secret_contest(db, contest_id=contest.id)
        assert result.outcome == contests.OUTCOME_INITIATOR_SUCCEEDS
        assert result.revealed is False
        camp = db.get(Campaign, ctx["campaign_id"])
        alice_view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        bob_view = build_surfaces_for_viewer(db, camp, ctx["bob"])
        assert "lifted Bob's pouch unseen" in str(alice_view)
        assert "lifted Bob's pouch unseen" not in str(bob_view)
        assert "MOTH-SIGIL-249" not in str(alice_view) + str(bob_view)
        # Bob's target projection shows no observable outcome when unrevealed.
        bob_proj = contests.project_contest_for_viewer(db, contest.id, ctx["bob"])
        assert bob_proj["role"] == "target"
        assert bob_proj["observable_outcome"] is None
    finally:
        db.close()

    # Failure with reveal: Bob explicitly learns he was targeted.
    db = _db(ctx)
    try:
        turn, alice_row = _secret_lift_with_initiator_roll(ctx, db, alice_raw=3, tag="b")
        contest = _start_pickpocket(
            ctx, db, turn, initiator_roll_request_id=alice_row.id,
            reveal_on_failure=True,
            failure_facts=[{"content": "Bob catches Alice reaching.", "visibility": "private"}])
        (row_id,) = contest.target_roll_request_ids
        _fulfill(db, uuid.UUID(row_id), ctx["bob"], 15)
        db.commit()
        result = contests.resolve_secret_contest(db, contest_id=contest.id)
        assert result.outcome == contests.OUTCOME_TARGET_HOLDS
        assert result.revealed is True
        camp = db.get(Campaign, ctx["campaign_id"])
        bob_view = build_surfaces_for_viewer(db, camp, ctx["bob"])
        assert "catches Alice reaching" in str(bob_view)
        bob_proj = contests.project_contest_for_viewer(db, contest.id, ctx["bob"])
        assert bob_proj["observable_outcome"] == contests.OUTCOME_TARGET_HOLDS
    finally:
        db.close()


# ── Duplicate retry + no shared-event leak ────────────────────────────────

def test_duplicate_retry_applies_once(ctx):
    from app.world.knowledge import list_facts

    db = _db(ctx)
    try:
        turn, alice_row = _secret_lift_with_initiator_roll(ctx, db, alice_raw=17)
        contest = _start_pickpocket(ctx, db, turn, initiator_roll_request_id=alice_row.id)
        (row_id,) = contest.target_roll_request_ids
        _fulfill(db, uuid.UUID(row_id), ctx["bob"], 5)
        db.commit()
        first = contests.resolve_secret_contest(db, contest_id=contest.id)
        before = len([f for f in list_facts(db, ctx["campaign_id"]) if "lifted" in (f.content or "")])
        second = contests.resolve_secret_contest(db, contest_id=contest.id)
        assert second.replayed is True
        assert second.event.id == first.event.id
        after = len([f for f in list_facts(db, ctx["campaign_id"]) if "lifted" in (f.content or "")])
        assert after == before == 1
        # Duplicate fulfillment cannot apply twice either.
        with pytest.raises(Exception):
            _fulfill(db, uuid.UUID(row_id), ctx["bob"], 5)
        db.rollback()
        # Duplicate start with the same key returns the same contest.
        again = _start_pickpocket(
            ctx, db, turn, contest_key=contest.contest_key,
            initiator_roll_request_id=alice_row.id)
        assert again.id == contest.id
        rows = db.execute(
            select(PlayerRollRequest).where(PlayerRollRequest.secret_contest_id == contest.id)
        ).scalars().all()
        assert len(rows) == 1
    finally:
        db.close()


def test_no_shared_event_leak(ctx):
    db = _db(ctx)
    mem = InMemoryRealtimePublisher()
    set_realtime_publisher(mem)
    try:
        turn, alice_row = _secret_lift_with_initiator_roll(ctx, db, alice_raw=17)
        contest = _start_pickpocket(ctx, db, turn, initiator_roll_request_id=alice_row.id)
        (row_id,) = contest.target_roll_request_ids
        _fulfill(db, uuid.UUID(row_id), ctx["bob"], 5)
        db.commit()
        contests.resolve_secret_contest(db, contest_id=contest.id)

        shared_channel = live_table_channel(ctx["campaign_id"], ctx["shared_id"])
        for rec in mem.published:
            assert rec["channel"] != shared_channel
            blob = str(rec["payload"])
            assert "MOTH-SIGIL-249" not in blob
            assert "pouch" not in blob.lower()

        # Member feeds: the private resolve event reaches nobody unauthorized.
        # (Alice sees it as the actor; Bob and the owner do not.)
        event_ids = [e.id for e in list_campaign_events(
            db, ctx["campaign_id"], viewer_id=ctx["bob"], limit=200)]
        resolve_events = [e for e in list_campaign_events(
            db, ctx["campaign_id"], limit=200)
            if e.event_type == "dm.secret_contest_resolved"]
        assert len(resolve_events) == 1
        assert resolve_events[0].id not in event_ids
        owner_ids = [e.id for e in list_campaign_events(
            db, ctx["campaign_id"], viewer_id=ctx["owner"], limit=200)]
        assert resolve_events[0].id not in owner_ids

        # Unauthorized projection fails closed.
        assert contests.project_contest_for_viewer(db, contest.id, ctx["owner"]) is None
        alice_proj = contests.project_contest_for_viewer(db, contest.id, ctx["alice"])
        assert alice_proj["role"] == "initiator"
        assert "MOTH-SIGIL-249" not in str(alice_proj)
        bob_proj = contests.project_contest_for_viewer(db, contest.id, ctx["bob"])
        assert "MOTH-SIGIL-249" not in str(bob_proj)
        assert str(ctx["alice"]) not in str(bob_proj)
    finally:
        set_realtime_publisher(None)
        db.close()
