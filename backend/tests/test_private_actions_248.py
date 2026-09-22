"""Issue #248 — private DM-thread actions in the forward-DM turn lifecycle.

A player can take canonical game actions in a private DM thread — including
player-controlled secret rolls — and those actions mutate shared/hidden world
state through the same staged/validated commit model as public turns, without
ever emitting public/shared narration or broadening visibility.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

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
from app.decisions.contracts import ChoiceResult, DecisionResponse  # noqa: E402
from app.dm import decision_routing as routing  # noqa: E402
from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.dm.turns import commit_turn_with_effects, coordinate_turn  # noqa: E402
from app.realtime.channels import live_table_channel  # noqa: E402
from app.realtime.service import InMemoryRealtimePublisher, set_realtime_publisher  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import (  # noqa: E402
    can_read_thread,
    create_private_thread,
    get_or_create_private_gameplay_thread,
)
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt, PlayerRollRequest  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import DecisionTelemetry  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

SECRET = "moonmoth vault passphrase MOTH-SIGIL-248"


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
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=alice, email="alice@example.com"),
        Profile(id=bob, email="bob@example.com"),
        Campaign(id=camp_id, owner_id=owner, name="Private Actions", revision=0),
        CampaignMember(campaign_id=camp_id, user_id=owner, role="owner"),
        CampaignMember(campaign_id=camp_id, user_id=alice, role="player"),
        CampaignMember(campaign_id=camp_id, user_id=bob, role="player"),
        CampaignThread(id=uuid.uuid4(), campaign_id=camp_id, thread_type="campaign",
                       title="Campaign", created_by=owner),
        Character(id=alice_char, owner_id=alice, name="Alice PC", system="dnd5e"),
        Dnd5eCharacterSheet(character_id=alice_char, owner_id=alice, character_name="Alice PC"),
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
           "bob": bob, "alice_char": alice_char, "shared_id": shared.id, "private_id": private.id}
    db.close()


def _db(ctx):
    return ctx["factory"]()


def _submit_private(db, ctx, text, *, character_id=None):
    sub = accept_submission(
        db, campaign_id=ctx["campaign_id"], user_id=ctx["alice"],
        character_id=character_id, raw_content=text,
        segments=[{"type": "ic", "text": text}],
        thread_id=str(ctx["private_id"]), audience="private",
    )
    db.commit()
    coord = coordinate_turn(db, ctx["campaign_id"], str(ctx["private_id"]),
                            audience="private", commit=False)
    db.commit()
    assert coord is not None
    return sub, coord[0], coord[1]


def _respond_contract(text, staged=()):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION,
        "mode": "respond",
        "reason": "private adjudication",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [{
                "text": text, "claim_kind": "observation",
                "origin": "dm_adjudication", "visibility": "public",
            }],
        }],
        "staged_effects": list(staged),
    })


def _silent_service():
    return DecisionService(
        FakeDecisionAdapter({routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID}))


def _failing_decision_service():
    # No scripted answer: the adapter raises malformed -> generative escape.
    return DecisionService(FakeDecisionAdapter({}))


def _must_not_run(packet, feedback=None):
    raise AssertionError("generative adjudication must not run on the direct path")


# ── Coordination ──────────────────────────────────────────────────────────

def test_private_submission_coordinates_private_turn(ctx):
    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(db, ctx, f"I pocket the sigil. {SECRET}")
        assert turn.audience == "private"
        assert attempt.audience == "private"
        assert turn.thread_id == str(ctx["private_id"])
        # Shared thread is untouched: no turn assembled there.
        assert coordinate_turn(db, ctx["campaign_id"], str(ctx["shared_id"])) is None
    finally:
        db.close()


# ── Bounded direct path (no generative call) ──────────────────────────────

def test_private_supported_intent_uses_bounded_direct_path(ctx):
    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(db, ctx, "ooc rest note, nothing happens")
        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_must_not_run, narrator="deterministic",
            decision_service=_silent_service(),
        )
        assert result is not None
        assert db.get(DmTurn, turn.id).status == "succeeded"
        event = result.event
        assert event.visibility == "private"
        assert (event.payload or {}).get("thread_id") == str(ctx["private_id"])
        assert (event.payload or {}).get("audience") == "private"
        # Decision telemetry carries IDs only — never the secret.
        rows = db.query(DecisionTelemetry).all()
        assert rows, "expected routing telemetry"
        blob = str([(r.candidate_ids, r.selected_id, r.revalidation_error) for r in rows])
        assert SECRET.split()[0] not in blob and "MOTH-SIGIL-248" not in blob
    finally:
        db.close()


# ── Creative escalation preserves input, restricts scope ──────────────────

def test_private_creative_action_escalates_with_input_intact(ctx):
    db = _db(ctx)
    try:
        secret_text = f"I whisper the forbidden rite. {SECRET}"
        _, turn, attempt = _submit_private(db, ctx, secret_text)
        seen = {}

        def _generative(packet, feedback=None):
            seen["packet"] = packet
            return _respond_contract("The shadows lean closer to listen.")

        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_generative, narrator="deterministic",
            decision_service=_failing_decision_service(),
        )
        assert result is not None
        assert db.get(DmTurn, turn.id).status == "succeeded"
        # Original private input reached the generative DM intact...
        packet = seen["packet"]
        assert packet.audience.audience == "private"
        assert secret_text in packet.serialize_for_adjudication()
        # ...while shared telemetry/projections carry no secret content.
        rows = db.query(DecisionTelemetry).all()
        blob = str([(r.candidate_ids, r.selected_id, r.revalidation_error) for r in rows])
        assert "MOTH-SIGIL-248" not in blob
        event = result.event
        assert event.visibility == "private"
    finally:
        db.close()


def test_private_frame_contains_only_authorized_state(ctx):
    db = _db(ctx)
    try:
        from app.rolls.service import request_rolls

        _, turn, attempt = _submit_private(db, ctx, "I search the vault for traps.")
        request_rolls(
            db, campaign_id=ctx["campaign_id"], turn_id=turn.id, attempt_id=attempt.id,
            requests=[{
                "request_key": "trap-check", "requested_user_id": str(ctx["alice"]),
                "character_id": str(ctx["alice_char"]), "roll_kind": "check",
                "ability_or_skill": "Perception", "label": "Trap search",
                "advantage_state": "normal", "reason_public": "Search the vault",
                "dc_private": 18,
            }],
        )
        db.commit()
        signals = routing.collect_signals(db, attempt, turn)
        assert signals.audience == "private"
        frame = routing.build_route_frame(signals)
        assert frame.state["audience"] == "private"
        blob = str(frame.state)
        # Pending-roll labels only: the hidden DC never enters the frame.
        assert "Trap search" in blob
        assert "18" not in blob and "dc_private" not in blob
    finally:
        db.close()


def test_private_primer_stays_thread_scoped():
    from app.dm.context import ContextAudience, LaneName

    for audience_kind, expect_visibility in (("private", "private"), ("campaign", "campaign")):
        audience = ContextAudience(
            campaign_id=str(uuid.uuid4()), thread_id=str(uuid.uuid4()),
            audience=audience_kind, user_ids=[str(uuid.uuid4())],
        )
        lane = SimpleNamespace(name=LaneName.PLAYER_INPUTS, records=[])
        packet = SimpleNamespace(
            audience=audience, lanes=[lane],
            model_copy=lambda deep=False: SimpleNamespace(
                audience=audience,
                lanes=[SimpleNamespace(name=LaneName.PLAYER_INPUTS, records=list(lane.records))],
            ),
        )
        primed = routing.attach_primer(
            packet, {"selected_id": routing.ROUTE_SILENT_ID, "frame_id": "f1",
                     "probability": 0.6, "confidence": 0.6, "margin": 0.1})
        (record,) = primed.lanes[0].records
        assert record.visibility == expect_visibility
        if audience_kind == "private":
            assert record.authorization.thread_ids == [audience.thread_id]


def test_unauthorized_attempt_never_builds_frame(ctx):
    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(db, ctx, "I act in shadow.")
        # Corrupt the audience binding: frame assembly must refuse.
        attempt.audience = "campaign"
        db.add(attempt)
        db.commit()
        outcome = routing.route_attempt(
            db, attempt=attempt, turn=turn, decision_service=_silent_service())
        assert outcome.directive == "escalate"
        assert outcome.contract is None
        assert outcome.trace.get("decision_skipped") is True
    finally:
        db.close()


# ── Secret rolls: player-supplied, thread-scoped ───────────────────────────

def _await_roll_contract(ctx):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION,
        "mode": "await_roll",
        "reason": "hidden trap check",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [{
                "text": "Something glints beneath the dust.", "claim_kind": "observation",
                "origin": "dm_adjudication", "visibility": "public",
            }],
        }],
        "roll_request": {
            "request_id": "trapcheck1", "character_id": str(ctx["alice_char"]),
            "roll_kind": "check", "ability_or_skill": "Perception",
            "label": "Secret trap search", "reason_public": "Search the vault",
            "dc_private": 18,
        },
    })


def test_private_secret_roll_lifecycle(ctx):
    from app.rolls.service import RollAuthorizationError, fulfill_roll

    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(
            db, ctx, "I search the vault.", character_id=ctx["alice_char"])

        def _generative(packet, feedback=None):
            return _await_roll_contract(ctx)

        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_generative, narrator="deterministic",
            decision_service=_failing_decision_service())
        assert result.mode == "await_roll"
        assert db.get(DmTurn, turn.id).status == "awaiting_roll"

        rows = db.execute(
            select(PlayerRollRequest).where(PlayerRollRequest.turn_id == turn.id)
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.thread_id == str(ctx["private_id"])
        assert row.dc_private == 18
        # Server-side DC never serializes to unauthorized readers.
        assert "dc_private" not in row.to_dict()
        # Thread membership gates reads: bob is excluded, alice included.
        assert can_read_thread(db, ctx["campaign_id"], ctx["private_id"], ctx["bob"]) is False
        assert can_read_thread(db, ctx["campaign_id"], ctx["private_id"], ctx["alice"]) is True

        # Another player can never supply the roll.
        with pytest.raises(RollAuthorizationError):
            fulfill_roll(db, request_id=row.id, actor_id=ctx["bob"], payload={
                "source": "app", "raw_rolls": [20], "modifier": 0,
                "total": 20, "visibility": "private"})
        db.rollback()

        req, fulfillment, resumed, _ = fulfill_roll(
            db, request_id=row.id, actor_id=ctx["alice"], payload={
                "source": "app", "raw_rolls": [14], "modifier": 3,
                "total": 17, "visibility": "private"})
        db.commit()
        assert fulfillment.total == 17
        # Private fulfillment redacts dice from anyone but the roller/owner path.
        assert "total" not in fulfillment.to_dict()
        assert fulfillment.to_dict(include_private=True)["total"] == 17
        # The same logical turn resumes with private scope intact.
        assert resumed is not None
        assert resumed.audience == "private"
        assert str(resumed.thread_id) == str(ctx["private_id"])
        # Duplicate fulfillment cannot apply twice.
        with pytest.raises(Exception):
            fulfill_roll(db, request_id=row.id, actor_id=ctx["alice"], payload={
                "source": "app", "raw_rolls": [14], "modifier": 3,
                "total": 17, "visibility": "private"})
        db.rollback()
    finally:
        db.close()


# ── Hidden effects + restricted events, no shared narration ────────────────

def _private_effect_contract():
    return _respond_contract(
        "You memorize the hidden sigil.",
        staged=[{
            "id": "fx1", "effect_type": "assert_fact",
            "arguments": {
                "content": f"The vault sigil is a moth. {SECRET}",
                "epistemic_state": "confirmed", "visibility": "dm_only",
            },
        }, {
            "id": "rec1", "effect_type": "record_world_event",
            "arguments": {
                "event_type": "whispered_lore", "summary": f"Sigil memorized. {SECRET}",
                "visibility": "dm_private",
            },
        }],
    )


def test_private_canonical_effects_without_shared_narration(ctx):
    from app.snapshot.surfaces import build_surfaces_for_viewer

    db = _db(ctx)
    mem = InMemoryRealtimePublisher()
    set_realtime_publisher(mem)
    try:
        _, turn, attempt = _submit_private(db, ctx, f"I study the sigil. {SECRET}")

        def _generative(packet, feedback=None):
            return _private_effect_contract()

        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_generative, narrator="deterministic",
            decision_service=_failing_decision_service())
        assert result is not None
        event = result.event
        assert event.visibility == "private"
        assert (event.payload or {}).get("thread_id") == str(ctx["private_id"])

        # Hidden state mutated: the fact is durable but restricted.
        from app.world.knowledge import list_facts

        facts = [f for f in list_facts(db, ctx["campaign_id"]) if "moth" in (f.content or "")]
        assert len(facts) == 1
        assert facts[0].visibility in ("dm_only", "private")

        # Bob's projections and event feed stay clean.
        camp = db.get(Campaign, ctx["campaign_id"])
        bob_view = build_surfaces_for_viewer(db, camp, ctx["bob"])
        assert "MOTH-SIGIL-248" not in str(bob_view)
        feed_ids = [e.id for e in list_campaign_events(
            db, ctx["campaign_id"], viewer_id=ctx["bob"], limit=200)]
        assert event.id not in feed_ids

        # Narration published only to the private thread channel — the
        # shared channel never saw the private action.
        channels = {rec["channel"] for rec in mem.published}
        assert channels, "expected realtime delivery"
        assert channels == {live_table_channel(ctx["campaign_id"], ctx["private_id"])}
        for rec in mem.published:
            assert "MOTH-SIGIL-248" not in str(rec["payload"]) or \
                rec["channel"] == live_table_channel(ctx["campaign_id"], ctx["private_id"])

        # Shared thread has no DM streams from this turn.
        from app.dm_streams.service import list_streams_for_thread

        assert list_streams_for_thread(db, ctx["campaign_id"], ctx["shared_id"]) == []
    finally:
        set_realtime_publisher(None)
        db.close()


def test_private_event_visible_to_later_private_reasoning_not_shared(ctx):
    from app.dm.context import LaneName, assemble_attempt_context

    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(db, ctx, "First secret.")

        def _generative(packet, feedback=None):
            return _private_effect_contract()

        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_generative, narrator="deterministic",
            decision_service=_failing_decision_service())
        assert result is not None
        event_id = result.event.id

        # A later private turn on the same thread keeps the causal link.
        _, _, attempt2 = _submit_private(db, ctx, "Second secret.")
        packet = assemble_attempt_context(
            db, attempt2.id,
            supplemental_status={LaneName.CURRENT_SCENE: "not_applicable"})
        history = next(
            lane for lane in packet.lanes if lane.name == LaneName.RECENT_HISTORY)
        assert any(r.record_id == f"domain-event:{event_id}" for r in history.records)

        # A shared turn never sees it.
        from models.threads import PlayerSubmission

        shared_sub = accept_submission(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["bob"],
            raw_content="Loud shared action",
            segments=[{"type": "ic", "text": "Loud shared action"}],
            thread_id=str(ctx["shared_id"]), audience="campaign")
        db.commit()
        coord = coordinate_turn(db, ctx["campaign_id"], str(ctx["shared_id"]),
                                audience="campaign", commit=False)
        db.commit()
        assert coord is not None
        shared_packet = assemble_attempt_context(
            db, coord[1].id,
            supplemental_status={LaneName.CURRENT_SCENE: "not_applicable"})
        shared_history = next(
            lane for lane in shared_packet.lanes if lane.name == LaneName.RECENT_HISTORY)
        assert all(r.record_id != f"domain-event:{event_id}" for r in shared_history.records)
        _ = (turn, shared_sub)
    finally:
        db.close()


# ── Failure, idempotency, post-turn, access ────────────────────────────────

def test_stale_private_candidate_escalates(ctx):
    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(db, ctx, "I slip into shadow.")

        def _decide(request):
            # A newer private submission supersedes this attempt mid-decision.
            accept_submission(
                db, campaign_id=ctx["campaign_id"], user_id=ctx["alice"],
                raw_content="I also hold my breath.",
                segments=[{"type": "ic", "text": "I also hold my breath."}],
                thread_id=str(ctx["private_id"]), audience="private")
            coordinate_turn(db, ctx["campaign_id"], str(ctx["private_id"]),
                            audience="private", commit=False)
            db.commit()
            ids = [c.id for c in request.questions[0].candidates]
            probs = {i: (1.0 if i == routing.ROUTE_SILENT_ID else 0.0) for i in ids}
            return DecisionResponse(results={
                request.questions[0].question_id: ChoiceResult(
                    question_id=request.questions[0].question_id,
                    selected_id=routing.ROUTE_SILENT_ID,
                    probabilities=probs, confidence=1.0)},
                provider="stub", model="stub", latency_ms=0, trace_id="t",
                operation_id="o", usage={})

        service = SimpleNamespace(decide=_decide)
        outcome = routing.route_attempt(
            db, attempt=attempt, turn=turn, decision_service=service)
        assert outcome.directive == "escalate"
        assert outcome.contract is None
        assert "revalidation_error" in outcome.trace
        # The stale decision mutated nothing prematurely.
        assert db.get(DmTurnAttempt, attempt.id).staged_effects in (None, [])

        # The superseding attempt still executes normally with private scope.
        fresh_turn = db.get(DmTurn, turn.id)
        assert str(fresh_turn.current_attempt_id) != str(attempt.id)

        def _generative(packet, feedback=None):
            return _respond_contract("Shadow passes over.")

        result = execute_dm_attempt(
            db, fresh_turn.current_attempt_id, adjudicate=_generative,
            narrator="deterministic", decision_service=_failing_decision_service())
        assert result is not None
        assert result.event.visibility == "private"
        assert db.get(DmTurn, turn.id).status == "succeeded"
    finally:
        db.close()


def test_decision_failure_leaves_no_premature_mutation(ctx):
    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(db, ctx, "I attempt something odd.")

        def _generative(packet, feedback=None):
            assert db.get(DmTurnAttempt, attempt.id).staged_effects in (None, [])
            return _respond_contract("The dark answers.")

        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_generative, narrator="deterministic",
            decision_service=_failing_decision_service())
        assert result is not None
        assert db.get(DmTurn, turn.id).status == "succeeded"
    finally:
        db.close()


def test_duplicate_private_commit_applies_once(ctx):
    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(db, ctx, "I seal the pact.")

        def _generative(packet, feedback=None):
            return _private_effect_contract()

        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_generative, narrator="deterministic",
            decision_service=_failing_decision_service())
        assert result is not None
        before = db.query(CampaignDomainEvent).filter_by(
            campaign_id=ctx["campaign_id"]).count()
        fresh_attempt = db.get(DmTurnAttempt, attempt.id)
        op = fresh_attempt.commit_operation_id or str(attempt.id)
        t2, a2, event2 = commit_turn_with_effects(
            db, turn.id, attempt.id, operation_id=op)
        after = db.query(CampaignDomainEvent).filter_by(
            campaign_id=ctx["campaign_id"]).count()
        assert after == before
        assert event2.id == result.event.id
        _ = (t2, a2)
    finally:
        db.close()


def test_post_turn_preserves_private_visibility(ctx):
    from app.post_turn.service import run_post_turn_range
    from app.world.knowledge import list_facts

    db = _db(ctx)
    try:
        _, turn, attempt = _submit_private(db, ctx, "I record the secret.")

        def _generative(packet, feedback=None):
            return _private_effect_contract()

        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_generative, narrator="deterministic",
            decision_service=_failing_decision_service())
        assert result is not None
        out = run_post_turn_range(
            db, ctx["campaign_id"], result.event.sequence, result.event.sequence)
        assert out.get("duplicate") is False
        mat = out["result"]["materialization"]
        # record_world_event compiled without widening its dm_private scope.
        assert mat["applied"]["facts"] == 1
        facts = [f for f in list_facts(db, ctx["campaign_id"]) if "Sigil memorized" in (f.content or "")]
        assert len(facts) == 1
        assert facts[0].visibility in ("dm_only", "private")
        _ = turn
    finally:
        db.close()


def test_owner_denied_reconnect_stable_private_history(ctx):
    from app.dm_streams.service import list_streams_for_thread, reconstruct_text

    db = _db(ctx)
    try:
        # Owner has no thread membership: private existence stays hidden.
        assert can_read_thread(db, ctx["campaign_id"], ctx["private_id"], ctx["owner"]) is False
        # AI-DM thread bootstrap is idempotent per player.
        t1, c1 = get_or_create_private_gameplay_thread(
            db, campaign_id=ctx["campaign_id"], created_by=ctx["alice"],
            private_kind="dm", participant_ids=[], title="Private with AI DM")
        db.commit()
        t2, c2 = get_or_create_private_gameplay_thread(
            db, campaign_id=ctx["campaign_id"], created_by=ctx["alice"],
            private_kind="dm", participant_ids=[], title="Private with AI DM")
        assert str(t1.id) == str(t2.id) and c1 is True and c2 is False

        _, turn, attempt = _submit_private(db, ctx, "A quiet confession.")

        def _generative(packet, feedback=None):
            return _respond_contract("The dark keeps your counsel.")

        result = execute_dm_attempt(
            db, attempt.id, adjudicate=_generative, narrator="deterministic",
            decision_service=_failing_decision_service())
        assert result is not None
        stream_id = db.get(DmTurnAttempt, attempt.id).stream_id
        assert stream_id is not None
        # Reconnect in a brand-new session: same authorized view.
        db.close()
        db2 = _db(ctx)
        try:
            assert reconstruct_text(db2, stream_id) != ""
            assert [str(s.id) for s in
                    list_streams_for_thread(db2, ctx["campaign_id"], ctx["private_id"])]
            assert list_streams_for_thread(db2, ctx["campaign_id"], ctx["shared_id"]) == []
            assert can_read_thread(db2, ctx["campaign_id"], ctx["private_id"], ctx["owner"]) is False
            assert can_read_thread(db2, ctx["campaign_id"], ctx["private_id"], ctx["bob"]) is False
        finally:
            db2.close()
        _ = turn
    finally:
        try:
            db.close()
        except Exception:
            pass
